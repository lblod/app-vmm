#!/usr/bin/env python3
"""
Converts beleidsveld.csv into a hierarchical SKOS codelist and writes it as a
Turtle (.ttl) migration file, with a .graph file of the same name next to it
containing the target graph URI, so mu-migrations-service loads the data into
that graph.

Hierarchy (3 levels, one skos:ConceptScheme):
    Beleidsdomein     skos:topConceptOf the scheme     (e.g. 01 Algemeen bestuur)
    Beleidssubdomein  skos:broader a Beleidsdomein     (e.g. 011 Algemene diensten)
    Beleidsveld       skos:broader a Beleidssubdomein  (e.g. 0110 Secretariaat)

Every row in the CSV is a separate Beleidsveld concept, identified by its
Beleidsveld ID: the same Beleidsveld code can occur in several taxonomies
(some of them municipality-specific), each time under its own ID.

Beleidsdomeinen and Beleidssubdomeinen are shared between rows and identified
by their code ('Beleidsdomein code', 'Beleidssubdomein code'); the hierarchy
is built from those codes. If one code occurs with several omschrijvingen, the
most frequent one becomes the skos:prefLabel and the others become
skos:altLabel.

Rows whose Beleidsveld ID is in IGNORED_BELEIDSVELD_IDS (the 'Geen
beleidsveld' row) are left out and only counted.

Column mapping:
    Beleidsveld ID                -> dct:identifier  ("...")   (Beleidsveld only)
    ... code                      -> skos:notation   ("...")
    ... omschrijving              -> skos:prefLabel  ("..."@nl)

Every concept also gets skos:inScheme, and skos:broader / skos:narrower links
in both directions. The scheme lists its top concepts via skos:hasTopConcept.

URIs and mu:uuids are deterministic: the concept scheme has a fixed uuid
(CONCEPT_SCHEME_UUID) and each concept gets a uuid5 of its level and key
(Beleidsveld ID, or domain/subdomain code), namespaced by that scheme uuid.
Rerunning the script on the same data yields the same URIs, so a regenerated
migration does not duplicate concepts.

Rows without a Beleidsveld ID, rows whose Beleidsveld ID was already seen, and
rows without a domain or subdomain code are skipped and reported on stdout. If a
subdomain code turns up under a different domain than before, the first
domain is kept and a warning is printed.

Output: {timestamp}-insert-beleidsvelden-codelist.ttl / .graph

Usage:
    python generate_beleidsveld_ttl.py
"""

import csv
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent

BELEIDSVELD_CSV = SCRIPT_DIR / "../../Dimensies/beleidsveld.csv"
OUTPUT_DIR = SCRIPT_DIR / "../config/migrations"
OUTPUT_PREFIX = "insert-beleidsvelden-codelist"

TARGET_GRAPH = "http://mu.semte.ch/graphs/public"

# Fixed uuid of the concept scheme. Do NOT change this once the migration has
# been run: it determines the scheme URI and (as uuid5 namespace) every
# concept URI.
CONCEPT_SCHEME_UUID = uuid.UUID("3f6b1c2e-8d4a-4e7b-9a51-6c0d2f8e4b17")
CONCEPT_SCHEME_LABEL = "Beleidsvelden"

CONCEPT_SCHEME_BASE_URI = "http://lblod.data.gift/concept-schemes/"
CONCEPT_BASE_URI = "http://lblod.data.gift/concepts/"

LABEL_LANGUAGE = "nl"

# Beleidsveld IDs that are not real Beleidsvelden ("Geen beleidsveld")
IGNORED_BELEIDSVELD_IDS = {"0"}

BELEIDSVELD_ID_COLUMN = "Beleidsveld ID"

DOMEIN_CODE_COLUMN = "Beleidsdomein code"
DOMEIN_OMSCHRIJVING_COLUMN = "Beleidsdomein omschrijving"

SUBDOMEIN_CODE_COLUMN = "Beleidssubdomein code"
SUBDOMEIN_OMSCHRIJVING_COLUMN = "Beleidssubdomein omschrijving"

VELD_CODE_COLUMN = "Beleidsveld code"
VELD_OMSCHRIJVING_COLUMN = "Beleidsveld omschrijving"

REQUIRED_COLUMNS = [
    BELEIDSVELD_ID_COLUMN,
    DOMEIN_CODE_COLUMN, DOMEIN_OMSCHRIJVING_COLUMN,
    SUBDOMEIN_CODE_COLUMN, SUBDOMEIN_OMSCHRIJVING_COLUMN,
    VELD_CODE_COLUMN, VELD_OMSCHRIJVING_COLUMN,
]

DOMEIN = "Beleidsdomein"
SUBDOMEIN = "Beleidssubdomein"
VELD = "Beleidsveld"

TURTLE_PREFIXES = """@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix mu: <http://mu.semte.ch/vocabularies/core/> .

"""


def value(row, column):
    return (row.get(column) or "").strip()


def scheme_uri():
    return CONCEPT_SCHEME_BASE_URI + str(CONCEPT_SCHEME_UUID)


def new_concept(level, key, code):
    """A concept dict; its uuid is a uuid5 of its level and key."""
    concept_id = uuid.uuid5(CONCEPT_SCHEME_UUID, f"{level}|{key}")
    return {
        "level": level,
        "key": key,
        "uuid": concept_id,
        "uri": CONCEPT_BASE_URI + str(concept_id),
        "code": code,
        "labels": Counter(),   # omschrijving -> number of rows using it
        "identifier": None,    # Beleidsveld ID (Beleidsveld level only)
        "parent": None,        # broader concept
        "children": [],        # narrower concepts, in CSV order
    }


def sort_key(concept):
    """Sort by code, then by key (numerically where possible)."""
    key = concept["key"]
    return (concept["code"], key.isdigit(), int(key) if key.isdigit() else key)


def turtle_string(text, language=None):
    """Return a Turtle string literal with proper escaping."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"@{language}' if language else f'"{escaped}"'


def build_concept_block(concept):
    """Build the Turtle block for one skos:Concept."""
    predicates = ["a skos:Concept", f'mu:uuid "{concept["uuid"]}"']

    # most_common() sorts by count, ties in first-seen order
    labels = [label for label, _ in concept["labels"].most_common() if label]
    if labels:
        predicates.append(f"skos:prefLabel {turtle_string(labels[0], LABEL_LANGUAGE)}")
    for alt_label in labels[1:]:
        predicates.append(f"skos:altLabel {turtle_string(alt_label, LABEL_LANGUAGE)}")

    if concept["code"]:
        predicates.append(f"skos:notation {turtle_string(concept['code'])}")

    if concept["identifier"] is not None:
        predicates.append(f"dct:identifier {turtle_string(concept['identifier'])}")

    predicates.append(f"skos:inScheme <{scheme_uri()}>")

    if concept["parent"] is None:
        predicates.append(f"skos:topConceptOf <{scheme_uri()}>")
    else:
        predicates.append(f"skos:broader <{concept['parent']['uri']}>")

    for child in sorted(concept["children"], key=sort_key):
        predicates.append(f"skos:narrower <{child['uri']}>")

    return f"<{concept['uri']}>\n    " + " ;\n    ".join(predicates) + " .\n"


def build_scheme_block(top_concepts):
    """Build the Turtle block for the skos:ConceptScheme."""
    predicates = [
        "a skos:ConceptScheme",
        f'mu:uuid "{CONCEPT_SCHEME_UUID}"',
        f"skos:prefLabel {turtle_string(CONCEPT_SCHEME_LABEL, LABEL_LANGUAGE)}",
    ]
    for concept in top_concepts:
        predicates.append(f"skos:hasTopConcept <{concept['uri']}>")
    return f"<{scheme_uri()}>\n    " + " ;\n    ".join(predicates) + " .\n"


def main():
    domeinen = {}      # key -> concept
    subdomeinen = {}   # key -> concept
    velden = {}        # Beleidsveld ID -> concept
    stats = {"rows": 0, "ignored": 0, "skipped": 0, "warnings": 0}

    # --- Read the CSV and build the concepts ---
    # utf-8-sig transparently strips a BOM, which would otherwise corrupt the
    # first column name.
    with open(BELEIDSVELD_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"Missing column(s) in {BELEIDSVELD_CSV}: {missing}")

        # start=2: line 1 is the header
        for line_no, row in enumerate(reader, start=2):
            stats["rows"] += 1

            beleidsveld_id = value(row, BELEIDSVELD_ID_COLUMN)
            if beleidsveld_id in IGNORED_BELEIDSVELD_IDS:
                stats["ignored"] += 1
                continue
            if not beleidsveld_id:
                print(f"SKIPPED line {line_no}: no {BELEIDSVELD_ID_COLUMN}")
                stats["skipped"] += 1
                continue
            if beleidsveld_id in velden:
                print(f"SKIPPED line {line_no}: duplicate "
                      f"{BELEIDSVELD_ID_COLUMN} {beleidsveld_id}")
                stats["skipped"] += 1
                continue

            # Domains and subdomains are keyed on their code
            domein_code = value(row, DOMEIN_CODE_COLUMN)
            subdomein_code = value(row, SUBDOMEIN_CODE_COLUMN)
            if not domein_code or not subdomein_code:
                print(f"SKIPPED line {line_no} ({BELEIDSVELD_ID_COLUMN} "
                      f"{beleidsveld_id}): no "
                      f"{DOMEIN_CODE_COLUMN if not domein_code else SUBDOMEIN_CODE_COLUMN}")
                stats["skipped"] += 1
                continue

            # --- Beleidsdomein ---
            domein = domeinen.get(domein_code)
            if domein is None:
                domein = domeinen[domein_code] = new_concept(DOMEIN, domein_code, domein_code)
            domein["labels"][value(row, DOMEIN_OMSCHRIJVING_COLUMN)] += 1

            # --- Beleidssubdomein ---
            subdomein = subdomeinen.get(subdomein_code)
            if subdomein is None:
                subdomein = subdomeinen[subdomein_code] = new_concept(
                    SUBDOMEIN, subdomein_code, subdomein_code)
                subdomein["parent"] = domein
                domein["children"].append(subdomein)
            elif subdomein["parent"] is not domein:
                print(f"WARNING line {line_no}: {SUBDOMEIN} '{subdomein_code}' appears "
                      f"under {DOMEIN} '{domein_code}', but was first seen under "
                      f"'{subdomein['parent']['key']}'; keeping the first")
                stats["warnings"] += 1
            subdomein["labels"][value(row, SUBDOMEIN_OMSCHRIJVING_COLUMN)] += 1

            # --- Beleidsveld (one per row) ---
            veld = new_concept(VELD, beleidsveld_id, value(row, VELD_CODE_COLUMN))
            veld["identifier"] = beleidsveld_id
            label = value(row, VELD_OMSCHRIJVING_COLUMN)
            if label:
                veld["labels"][label] += 1
            else:
                print(f"WARNING line {line_no}: {BELEIDSVELD_ID_COLUMN} "
                      f"{beleidsveld_id} has no label")
                stats["warnings"] += 1
            veld["parent"] = subdomein
            subdomein["children"].append(veld)
            velden[beleidsveld_id] = veld

    print(f"Read {stats['rows']} records from {BELEIDSVELD_CSV}")
    print(f"Found {len(domeinen)} {DOMEIN}en, {len(subdomeinen)} {SUBDOMEIN}en "
          f"and {len(velden)} {VELD}en")

    for concept in list(domeinen.values()) + list(subdomeinen.values()):
        if len(concept["labels"]) > 1:
            labels = [label for label, _ in concept["labels"].most_common()]
            print(f"NOTE {concept['level']} '{concept['key']}' has several "
                  f"omschrijvingen; prefLabel '{labels[0]}', altLabel {labels[1:]}")

    if not velden:
        print("No concepts to write, no .ttl file generated.")
        return

    # --- Write the .ttl + .graph file ---
    top_concepts = sorted(domeinen.values(), key=sort_key)
    blocks = [build_scheme_block(top_concepts)]
    for concepts in (domeinen, subdomeinen, velden):
        for concept in sorted(concepts.values(), key=sort_key):
            blocks.append(build_concept_block(concept))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    basename = f"{timestamp}-{OUTPUT_PREFIX}"
    ttl_path = OUTPUT_DIR / f"{basename}.ttl"
    graph_path = OUTPUT_DIR / f"{basename}.graph"

    with open(ttl_path, "w", encoding="utf-8") as out:
        out.write(TURTLE_PREFIXES)
        out.write("\n".join(blocks))

    with open(graph_path, "w", encoding="utf-8") as out:
        out.write(TARGET_GRAPH)

    num_concepts = len(domeinen) + len(subdomeinen) + len(velden)
    print(f"\nWrote 1 concept scheme and {num_concepts} concepts "
          f"to {ttl_path.resolve()} (+ .graph)")
    print(f"Ignored {stats['ignored']} rows ({BELEIDSVELD_ID_COLUMN} in "
          f"{sorted(IGNORED_BELEIDSVELD_IDS)})")
    print(f"Skipped {stats['skipped']} rows")
    print(f"{stats['warnings']} warning(s)")


if __name__ == "__main__":
    main()
