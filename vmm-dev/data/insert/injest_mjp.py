from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from typing import Any, Iterator

import requests

# ==============================================================================
# 1. Configuration
# ==============================================================================

# Direct Virtuoso SPARQL endpoint.
SPARQL_URL = "http://localhost:8890/sparql"

# All MJP records are stored in the shared public graph.
PUBLIC_GRAPH = "http://mu.semte.ch/graphs/public"

# Jobs, Tasks, NodeShapes, DataContainers live here.
HARVESTING_GRAPH = "http://mu.semte.ch/graphs/harvesting"

# The code list for the VAP Klimaatadaptatie model is published as a concept scheme.
CODELIST_URI = "http://data.lblod.gift/id/conceptscheme/vap-klimaatadaptatie"

# One JSON object per line.
MJP_SOURCE_FILE = Path("data/vap_mjp_structured.jsonl")

# Set to True to print and validate generated updates without sending them.
DRY_RUN = False

# Optional safety limit. Use None to process the complete JSONL file.
MAX_DELIVERIES: int | None = None

# A plain INSERT is idempotent for unchanged RDF triples.
# Keep this False unless an existing delivery must be fully replaced.
REPLACE_EXISTING_DELIVERIES = False

# URI conventions grounded in the proposed model's `vmm:` namespace and
# instance patterns such as `vmm:actie/{id}`.
VMM_BASE = "http://lblod.data.gift/vocabularies/vmm/"

BODY_URI = "http://data.lblod.info/id/bestuurseenheden/353234a365664e581db5c2f7cc07add2534b47b8e1ab87c821fc6e6365e6bef5"

PREFIXES = """
PREFIX dct:    <http://purl.org/dc/terms/>
PREFIX eli:    <http://data.europa.eu/eli/ontology#>
PREFIX epvoc:  <https://data.europarl.europa.eu/def/epvoc#>
PREFIX mu:     <http://mu.semte.ch/vocabularies/core/>
PREFIX schema: <https://schema.org/>
PREFIX vmm:    <http://lblod.data.gift/vocabularies/vmm/>
PREFIX xsd:    <http://www.w3.org/2001/XMLSchema#>
""".strip()

# ==============================================================================
# 2. SPARQL Helpers
# ==============================================================================

def sparql_update(query: str) -> requests.Response:
    """Execute a SPARQL Update against Virtuoso."""
    response = requests.post(
        SPARQL_URL,
        data={"query": query},
        timeout=120,
    )
    response.raise_for_status()
    return response


def sparql_query(query: str) -> list[dict[str, Any]]:
    """Execute a SPARQL SELECT query and return its bindings."""
    response = requests.get(
        SPARQL_URL,
        params={"query": query, "format": "application/sparql-results+json"},
        timeout=120,
    )
    response.raise_for_status()
    return response.json().get("results", {}).get("bindings", [])


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip() == ""


def text_value(value: Any) -> str | None:
    """Return a trimmed string, or None for empty/whitespace-only values."""
    if is_blank(value):
        return None
    return str(value).strip()


def first_value(data: dict[str, Any], *keys: str) -> Any:
    """Read the first available key, allowing snake_case and camelCase input."""
    for key in keys:
        if key in data:
            return data[key]
    return None


def sparql_string(value: Any, *, lang: str | None = None) -> str:
    """Encode a Python value as a SPARQL-safe quoted string literal."""
    text = text_value(value)
    if text is None:
        raise ValueError("Cannot serialize a blank value as a required literal.")
    encoded = json.dumps(text, ensure_ascii=False)
    return f"{encoded}@{lang}" if lang else encoded


def sparql_boolean(value: Any) -> str | None:
    """
    Convert source boolean representations to an xsd:boolean lexical form.
    Returns None when the value is absent.
    """
    if is_blank(value):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and value in (0, 1):
        return "true" if int(value) == 1 else "false"

    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "ja"}:
        return "true"
    if normalized in {"false", "0", "no", "nee"}:
        return "false"
    raise ValueError(f"Unsupported boolean value: {value!r}")


def uri(kind: str, source_id: Any) -> str:
    source_id_text = text_value(source_id)
    if source_id_text is None:
        raise ValueError(f"Missing identifier for {kind}.")
    return f"{VMM_BASE}{kind}/{source_id_text}"


def expression_uri(kind: str, source_id: Any) -> str:
    return f"{uri(kind, source_id)}/expression"

# ==============================================================================
# 3. JSONL Reader and Validator
# ==============================================================================

def iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield `(line_number, object)` for every non-empty JSONL line."""
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON on line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(
                    f"Line {line_number} must contain a JSON object, "
                    f"not {type(record).__name__}."
                )
            yield line_number, record


def require_id(entity: dict[str, Any], *, kind: str, context: str) -> str:
    identifier = text_value(entity.get("id"))
    if identifier is None:
        raise ValueError(f"{context}: {kind} is missing its `id`.")
    return identifier


def validate_delivery(delivery: dict[str, Any], *, line_number: int) -> dict[str, int]:
    """
    Validate required hierarchy identifiers and return entity counts.
    Empty arrays are allowed, but every present entity must have an ID.
    """
    delivery_id = text_value(delivery.get("aanlevering_id"))
    if delivery_id is None:
        raise ValueError(
            f"Line {line_number}: delivery is missing `aanlevering_id`."
        )

    counts = {
        "aanleveringen": 1,
        "beleidsdoelstellingen": 0,
        "actieplannen": 0,
        "acties": 0,
    }

    seen: dict[str, set[str]] = {
        "beleidsdoelstelling": set(),
        "actieplan": set(),
        "actie": set(),
    }

    beleidsdoelstellingen = delivery.get("beleidsdoelstellingen", []) or []
    if not isinstance(beleidsdoelstellingen, list):
        raise ValueError(
            f"Line {line_number}: `beleidsdoelstellingen` must be an array."
        )

    for bd_index, bd in enumerate(beleidsdoelstellingen):
        if not isinstance(bd, dict):
            raise ValueError(
                f"Line {line_number}, beleidsdoelstelling {bd_index}: "
                "expected an object."
            )
        bd_id = require_id(
            bd, kind="beleidsdoelstelling", context=f"Line {line_number}",
        )
        if bd_id in seen["beleidsdoelstelling"]:
            raise ValueError(
                f"Line {line_number}: duplicate beleidsdoelstelling ID {bd_id}."
            )
        seen["beleidsdoelstelling"].add(bd_id)
        counts["beleidsdoelstellingen"] += 1

        actieplannen = bd.get("actieplannen", []) or []
        if not isinstance(actieplannen, list):
            raise ValueError(
                f"Line {line_number}, beleidsdoelstelling {bd_id}: "
                "`actieplannen` must be an array."
            )

        for ap_index, ap in enumerate(actieplannen):
            if not isinstance(ap, dict):
                raise ValueError(
                    f"Line {line_number}, actieplan {ap_index}: "
                    "expected an object."
                )
            ap_id = require_id(
                ap, kind="actieplan",
                context=f"Line {line_number}, beleidsdoelstelling {bd_id}",
            )
            if ap_id in seen["actieplan"]:
                raise ValueError(
                    f"Line {line_number}: duplicate actieplan ID {ap_id}."
                )
            seen["actieplan"].add(ap_id)
            counts["actieplannen"] += 1

            acties = ap.get("acties", []) or []
            if not isinstance(acties, list):
                raise ValueError(
                    f"Line {line_number}, actieplan {ap_id}: "
                    "`acties` must be an array."
                )

            for action_index, action in enumerate(acties):
                if not isinstance(action, dict):
                    raise ValueError(
                        f"Line {line_number}, action {action_index}: "
                        "expected an object."
                    )
                action_id = require_id(
                    action, kind="actie",
                    context=f"Line {line_number}, actieplan {ap_id}",
                )
                if action_id in seen["actie"]:
                    raise ValueError(
                        f"Line {line_number}: duplicate action ID {action_id}."
                    )
                seen["actie"].add(action_id)
                counts["acties"] += 1

    return counts

# ==============================================================================
# 4. JSON-to-RDF Transformation
# ==============================================================================

def resource_uuid(resource_uri: str) -> str:
    """Generate a deterministic UUID for any resource based on its URI."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, resource_uri))


def delivery_to_triples(delivery: dict[str, Any]) -> tuple[list[str], dict[str, list[str]]]:
    """
    Convert one complete MJP JSON delivery into a flat list of triple strings.

    Mapped top-level fields: aanlevering_id, rapportjaar.
    Skipped (no model predicates yet): type_rapport, status, bestuur.
    """
    triples: list[str] = []
    uris: dict[str, list[str]] = {}

    delivery_id = text_value(delivery.get("aanlevering_id"))
    assert delivery_id is not None

    delivery_uri = uri("aanlevering", delivery_id)
    triples.append(f"<{delivery_uri}> a vmm:Aanlevering .")
    triples.append(f"<{delivery_uri}> mu:uuid {sparql_string(resource_uuid(delivery_uri))} .")
    triples.append(f"<{delivery_uri}> dct:identifier {sparql_string(delivery_id)} .")

    report_year = text_value(delivery.get("rapportjaar"))
    if report_year is not None:
        triples.append(f"<{delivery_uri}> schema:datePublished {sparql_string(report_year)} .")

    # --- Beleidsdoelstellingen (ComplexWork) ---
    for bd in delivery.get("beleidsdoelstellingen", []) or []:
        bd_id = text_value(bd["id"])
        assert bd_id is not None

        bd_work = uri("beleidsdoelstelling", bd_id)
        bd_expr = expression_uri("beleidsdoelstelling", bd_id)

        if "bd_work" not in uris:
            uris["bd_work"] = []
        if "bd_expr" not in uris:
            uris["bd_expr"] = []
        uris["bd_work"].append(bd_work)
        uris["bd_expr"].append(bd_expr)

        # Expression
        triples.append(f"<{bd_expr}> a eli:Expression, vmm:Beleidsdoelstelling .")
        triples.append(f"<{bd_expr}> mu:uuid {sparql_string(resource_uuid(bd_expr))} .")
        for pred, val in [
            ("eli:title", "beleidsdoelstelling [" + bd.get("code") + "]"),
            ("schema:code", bd.get("code")),
            ("eli:description", first_value(bd, "korte_omschrijving", "korteOmschrijving")),
            ("epvoc:expressionContent", first_value(bd, "lange_omschrijving", "langeOmschrijving")),
            ("schema:comment", bd.get("commentaar")),
            ("schema:review", bd.get("evaluatie")),
        ]:
            text = text_value(val)
            if text is not None:
                triples.append(f"<{bd_expr}> {pred} {sparql_string(text)} .")
        priority = sparql_boolean(bd.get("prioritair"))
        if priority is not None:
            triples.append(f'<{bd_expr}> vmm:prioritair "{priority}"^^xsd:boolean .')

        # Work
        triples.append(f"<{bd_work}> a eli:ComplexWork .")
        triples.append(f"<{bd_work}> mu:uuid {sparql_string(resource_uuid(bd_work))} .")
        triples.append(f"<{bd_work}> dct:identifier {sparql_string(bd_id)} .")
        triples.append(f"<{bd_work}> eli:work_type vmm:Beleidsdoelstelling .")
        triples.append(f"<{bd_work}> eli:is_realized_by <{bd_expr}> .")

        # Link delivery → beleidsdoelstelling
        triples.append(f"<{delivery_uri}> eli:has_member <{bd_work}> .")

        # --- Actieplannen (ComplexWork) ---
        for ap in bd.get("actieplannen", []) or []:
            ap_id = text_value(ap["id"])
            assert ap_id is not None

            ap_work = uri("actieplan", ap_id)
            ap_expr = expression_uri("actieplan", ap_id)

            if "ap_work" not in uris:
                uris["ap_work"] = []
            if "ap_expr" not in uris:
                uris["ap_expr"] = []
            uris["ap_work"].append(ap_work)
            uris["ap_expr"].append(ap_expr)

            # Expression
            triples.append(f"<{ap_expr}> a eli:Expression, vmm:Actieplan .")
            triples.append(f"<{ap_expr}> mu:uuid {sparql_string(resource_uuid(ap_expr))} .")
            for pred, val in [
                ("eli:title", "actieplan [" + ap.get("code") + "]"),
                ("schema:code", ap.get("code")),
                ("eli:description", first_value(ap, "korte_omschrijving", "korteOmschrijving")),
                ("epvoc:expressionContent", first_value(ap, "lange_omschrijving", "langeOmschrijving")),
                ("schema:comment", ap.get("commentaar")),
                ("schema:review", ap.get("evaluatie")),
            ]:
                text = text_value(val)
                if text is not None:
                    triples.append(f"<{ap_expr}> {pred} {sparql_string(text)} .")
            priority = sparql_boolean(ap.get("prioritair"))

            # Work
            triples.append(f"<{ap_work}> a eli:ComplexWork .")
            triples.append(f"<{ap_work}> mu:uuid {sparql_string(resource_uuid(ap_work))} .")
            triples.append(f"<{ap_work}> dct:identifier {sparql_string(ap_id)} .")
            triples.append(f"<{ap_work}> eli:work_type vmm:Actieplan .")
            triples.append(f"<{ap_work}> eli:is_realized_by <{ap_expr}> .")

            # Link beleidsdoelstelling → actieplan
            triples.append(f"<{bd_work}> eli:has_member <{ap_work}> .")

            # --- Acties (Work) ---
            for action in ap.get("acties", []) or []:
                act_id = text_value(action["id"])
                assert act_id is not None

                act_work = uri("actie", act_id)
                act_expr = expression_uri("actie", act_id)

                if "act_work" not in uris:
                    uris["act_work"] = []
                if "act_expr" not in uris:
                    uris["act_expr"] = []
                uris["act_work"].append(act_work)
                uris["act_expr"].append(act_expr)

                # Expression
                triples.append(f"<{act_expr}> a eli:Expression, vmm:Actie .")
                triples.append(f"<{act_expr}> mu:uuid {sparql_string(resource_uuid(act_expr))} .")
                for pred, val in [
                    ("eli:title", "actie [" + action.get("code") + "]"),
                    ("schema:code", action.get("code")),
                    ("eli:description", first_value(action, "korte_omschrijving", "korteOmschrijving")),
                    ("epvoc:expressionContent", first_value(action, "lange_omschrijving", "langeOmschrijving")),
                    ("schema:comment", action.get("commentaar")),
                    ("schema:review", action.get("evaluatie")),
                ]:
                    text = text_value(val)
                    if text is not None:
                        triples.append(f"<{act_expr}> {pred} {sparql_string(text)} .")
                priority = sparql_boolean(action.get("prioritair"))

                # Work
                triples.append(f"<{act_work}> a eli:Work .")
                triples.append(f"<{act_work}> mu:uuid {sparql_string(resource_uuid(act_work))} .")
                triples.append(f"<{act_work}> dct:identifier {sparql_string(act_id)} .")
                triples.append(f"<{act_work}> eli:work_type vmm:Actie .")
                triples.append(f"<{act_work}> eli:is_realized_by <{act_expr}> .")

                # Link actieplan → actie
                triples.append(f"<{ap_work}> eli:has_member <{act_work}> .")

    return triples, uris


def build_insert_query(triples: list[str]) -> str:
    body = "\n    ".join(triples)
    return f"""
{PREFIXES}

INSERT DATA {{
  GRAPH <{PUBLIC_GRAPH}> {{
    {body}
  }}
}}
""".strip()

# ==============================================================================
# Main Pipeline Execution (Steps 1 to 5)
# ==============================================================================

def main() -> None:
    print("Configuration loaded.")
    print(f"Source: {MJP_SOURCE_FILE}")
    print(f"Target graph: {PUBLIC_GRAPH}")
    print(f"Dry run: {DRY_RUN}\n")

    # --- Step 3: Validate input file ---
    if not MJP_SOURCE_FILE.exists():
        raise FileNotFoundError(
            f"JSONL source file not found: {MJP_SOURCE_FILE.resolve()}"
        )

    deliveries: list[tuple[int, dict[str, Any]]] = []
    total_counts = {
        "aanleveringen": 0,
        "beleidsdoelstellingen": 0,
        "actieplannen": 0,
        "acties": 0,
    }

    for line_number, delivery in iter_jsonl(MJP_SOURCE_FILE):
        counts = validate_delivery(delivery, line_number=line_number)
        deliveries.append((line_number, delivery))
        for key, value in counts.items():
            total_counts[key] += value

        if MAX_DELIVERIES is not None and len(deliveries) >= MAX_DELIVERIES:
            break

    print(f"Validated {len(deliveries)} JSONL deliveries.")
    for key, value in total_counts.items():
        print(f"  {key}: {value}")
    print()

    # --- Step 4: JSON-to-RDF Transformation ---
    generated_updates: list[tuple[int, str, list[str]]] = []

    for line_number, delivery in deliveries:
        delivery_id = text_value(delivery["aanlevering_id"])
        assert delivery_id is not None
        triples, uris = delivery_to_triples(delivery)
        generated_updates.append((line_number, delivery_id, triples))

    print(f"Generated {len(generated_updates)} SPARQL updates.")
    if generated_updates:
        first_line, first_id, first_triples = generated_updates[0]
        print(
            f"First delivery: line {first_line}, ID {first_id}, "
            f"{len(first_triples)} triples"
        )

    # --- Step 5: Execute SPARQL Inserts ---
    MAX_TRIPLES_PER_UPDATE = 4_000
    successful = 0
    failed: list[tuple[int, str, str]] = []

    print("\nStarting SPARQL insertion batch...")
    for index, (line_number, delivery_id, triples) in enumerate(generated_updates, start=1):
        current_batch = 0
        try:
            batches = [
                triples[offset : offset + MAX_TRIPLES_PER_UPDATE]
                for offset in range(0, len(triples), MAX_TRIPLES_PER_UPDATE)
            ]

            if DRY_RUN:
                print(
                    f"[DRY RUN] {index}/{len(generated_updates)} "
                    f"delivery {delivery_id}: {len(triples)} triples "
                    f"in {len(batches)} batch(es)"
                )
                continue

            for current_batch, batch in enumerate(batches, start=1):
                sparql_update(build_insert_query(batch))

            successful += 1
            print(
                f"Inserted {index}/{len(generated_updates)}: "
                f"delivery {delivery_id} ({len(triples)} triples in "
                f"{len(batches)} batch(es))"
            )
        except Exception as exc:
            batch_context = (
                f"batch {current_batch}/{len(batches)}: "
                if current_batch and 'batches' in locals()
                else ""
            )
            error = f"{batch_context}{exc}"
            failed.append((line_number, delivery_id, error))
            print(
                f"FAILED line {line_number}, delivery {delivery_id}: {error}"
            )

    if DRY_RUN:
        print("\nDry run complete; no updates were sent.")
    else:
        print(f"\nSuccessfully inserted: {successful}")
        print(f"Failed: {len(failed)}")
        if failed:
            print("\nFailures:")
            for line_number, delivery_id, error in failed:
                print(f"  line {line_number}, delivery {delivery_id}: {error}")


if __name__ == "__main__":
    main()