#!/usr/bin/env python3
"""
Converts the records in transactie_MJP_2026.csv into schema:MoneyTransfer
resources and writes them as Turtle (.ttl) migration files, batched per
ROWS_PER_FILE transactions. Next to every .ttl file a .graph file with the
same name is written, containing the target graph URI, so mu-migrations-service
loads the data into that graph.

Column mapping:
    Transactie ID   -> dct:identifier       ("...")
    Transactiesoort -> schema:additionalType ("...")
    Bedrag          -> schema:amount         (bare Turtle decimal, e.g. 2040.00)
    Boekjaar        -> elod:financialYear    ("...")
    Rapport jaar    -> dct:created           ("...")
    Actie ID        -> schema:result         (<http://lblod.data.gift/vocabularies/vmm/actie/{id}>)

Only transactions whose Actie is annotated in the triplestore are kept: the
Actie must be realized by an expression (eli:is_realized_by) that is the target
of an annotation (oa:hasTarget). This is checked with batched SELECT queries
against SPARQL_ENDPOINT, once per unique Actie. Other rows are filtered out and
only counted.

Rows without a Transactie ID, and rows whose Transactie ID was already seen,
are skipped and reported on stdout. Individual fields that are empty or
invalid are left out of the resource and reported as a warning.

Output: {timestamp}-insert-transacties-001.ttl / .graph,
        {timestamp}-insert-transacties-002.ttl / .graph, ...

Usage:
    python generate_transactie_ttl.py
"""

import csv
import math
import uuid
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import islice
from pathlib import Path
from urllib.parse import quote

import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent

TRANSACTIE_CSV = SCRIPT_DIR / "../../MJP_2026/transactie_MJP_2026.csv"
OUTPUT_DIR = SCRIPT_DIR / "../config/migrations"
OUTPUT_PREFIX = "insert-transacties"

TARGET_GRAPH = "http://mu.semte.ch/graphs/public"

SPARQL_ENDPOINT = "http://localhost:8890/sparql"
LOOKUP_BATCH_SIZE = 250   # batch size for the Actie SELECT queries
ROWS_PER_FILE = 10000

# Subject URI of each transaction: TRANSACTIE_BASE_URI + Transactie ID
TRANSACTIE_BASE_URI = "http://lblod.data.gift/vocabularies/vmm/transactie/"
ACTIE_BASE_URI = "http://lblod.data.gift/vocabularies/vmm/actie/"

# Add a deterministic mu:uuid (uuid5 of the subject URI), which mu-cl-resources
# and most other mu-semtech services need to expose a resource.
# Set to False if you want strictly the mapped predicates only.
INCLUDE_MU_UUID = True

TRANSACTIE_ID_COLUMN = "Transactie ID"
TRANSACTIESOORT_COLUMN = "Transactiesoort"
BEDRAG_COLUMN = "Bedrag"
BOEKJAAR_COLUMN = "Boekjaar"
RAPPORT_JAAR_COLUMN = "Rapport jaar"
ACTIE_ID_COLUMN = "Actie ID"

REQUIRED_COLUMNS = [
    TRANSACTIE_ID_COLUMN,
    TRANSACTIESOORT_COLUMN,
    BEDRAG_COLUMN,
    BOEKJAAR_COLUMN,
    RAPPORT_JAAR_COLUMN,
    ACTIE_ID_COLUMN,
]

ANNOTATED_ACTIE_QUERY_TEMPLATE = """
PREFIX oa: <http://www.w3.org/ns/oa#>
PREFIX eli: <http://data.europa.eu/eli/ontology#>
SELECT DISTINCT ?actie WHERE {{
  VALUES ?actie {{
{values}
  }}
  ?actie eli:is_realized_by ?expression .
  ?annotation oa:hasTarget ?expression .
}}
"""

TURTLE_PREFIXES = """@prefix schema: <http://schema.org/> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix elod: <http://linkedeconomy.org/ontology#> .
@prefix mu: <http://mu.semte.ch/vocabularies/core/> .

"""


def batched(iterable, size):
    """Yield lists of at most `size` items from any iterable (streaming)."""
    it = iter(iterable)
    while batch := list(islice(it, size)):
        yield batch


def actie_uri(actie_id):
    return ACTIE_BASE_URI + quote(actie_id, safe="")


def fetch_annotated_acties(actie_uris):
    """
    Query the triplestore in batches and return the set of Actie URIs that are
    realized by an expression which is the target of an annotation.
    """
    annotated = set()
    unique_uris = sorted(set(actie_uris))

    for batch in batched(unique_uris, LOOKUP_BATCH_SIZE):
        values_block = "\n".join(f"    <{uri}>" for uri in batch)
        query = ANNOTATED_ACTIE_QUERY_TEMPLATE.format(values=values_block)

        response = requests.post(
            SPARQL_ENDPOINT,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
        )
        response.raise_for_status()

        for binding in response.json()["results"]["bindings"]:
            annotated.add(binding["actie"]["value"])

    return annotated


def turtle_string(value):
    """Return a plain Turtle string literal with proper escaping."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def turtle_decimal(value):
    """
    Return a bare Turtle decimal (e.g. 2040.00), or None if the value is not a
    valid decimal. format(..., "f") avoids exponent notation (e.g. 1E+3), which
    Turtle would read as a double. A number without a decimal point would be
    read as an integer, so ".0" is appended in that case.
    """
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    text = format(number, "f")
    return text if "." in text else f"{text}.0"


def build_resource(row, line_no):
    """
    Build the Turtle block for one transaction row.
    Returns (turtle_block, warnings); turtle_block is None if the row
    cannot be converted at all.
    """
    warnings = []
    transactie_id = (row.get(TRANSACTIE_ID_COLUMN) or "").strip()
    if not transactie_id:
        return None, [f"line {line_no}: no {TRANSACTIE_ID_COLUMN}"]

    subject_uri = TRANSACTIE_BASE_URI + quote(transactie_id, safe="")
    predicates = ["a schema:MoneyTransfer"]

    if INCLUDE_MU_UUID:
        resource_uuid = uuid.uuid5(uuid.NAMESPACE_URL, subject_uri)
        predicates.append(f'mu:uuid "{resource_uuid}"')

    predicates.append(f"dct:identifier {turtle_string(transactie_id)}")

    def add_string(column, predicate):
        value = (row.get(column) or "").strip()
        if value:
            predicates.append(f"{predicate} {turtle_string(value)}")
        else:
            warnings.append(f"Transactie ID {transactie_id}: empty '{column}'")

    add_string(TRANSACTIESOORT_COLUMN, "schema:additionalType")

    bedrag = (row.get(BEDRAG_COLUMN) or "").strip()
    bedrag_literal = turtle_decimal(bedrag) if bedrag else None
    if bedrag_literal is not None:
        predicates.append(f"schema:amount {bedrag_literal}")
    else:
        warnings.append(f"Transactie ID {transactie_id}: "
                        f"invalid or empty '{BEDRAG_COLUMN}' ({bedrag!r})")

    add_string(BOEKJAAR_COLUMN, "elod:financialYear")
    add_string(RAPPORT_JAAR_COLUMN, "dct:created")

    actie_id = (row.get(ACTIE_ID_COLUMN) or "").strip()
    if actie_id:
        predicates.append(f"schema:result <{actie_uri(actie_id)}>")
    else:
        warnings.append(f"Transactie ID {transactie_id}: empty '{ACTIE_ID_COLUMN}'")

    block = f"<{subject_uri}>\n    " + " ;\n    ".join(predicates) + " .\n"
    return block, warnings


def main():
    # --- First pass: validate header, count rows per Actie ---
    # utf-8-sig transparently strips a BOM, which would otherwise corrupt the
    # first column name.
    rows_per_actie = Counter()
    total_rows = 0
    with open(TRANSACTIE_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"Missing column(s) in {TRANSACTIE_CSV}: {missing}")
        for row in reader:
            total_rows += 1
            actie_id = (row.get(ACTIE_ID_COLUMN) or "").strip()
            if actie_id:
                rows_per_actie[actie_uri(actie_id)] += 1

    print(f"Read {total_rows} records from {TRANSACTIE_CSV}")
    print(f"Found {len(rows_per_actie)} unique Acties")

    # --- Ask the triplestore which Acties are annotated ---
    annotated_acties = fetch_annotated_acties(rows_per_actie.keys())
    expected_rows = sum(rows_per_actie[uri] for uri in annotated_acties)
    print(f"{len(annotated_acties)} Acties are annotated in the triplestore, "
          f"covering {expected_rows} transactions")

    if expected_rows == 0:
        print("No transactions to write, no .ttl files generated.")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    max_batches = math.ceil(expected_rows / ROWS_PER_FILE)
    pad_width = max(3, len(str(max_batches)))
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

    stats = {"skipped": 0, "filtered": 0, "warnings": 0}
    seen_ids = set()

    def resources(reader):
        # start=2: line 1 is the header
        for line_no, row in enumerate(reader, start=2):
            actie_id = (row.get(ACTIE_ID_COLUMN) or "").strip()
            if not actie_id or actie_uri(actie_id) not in annotated_acties:
                stats["filtered"] += 1
                continue

            block, warnings = build_resource(row, line_no)
            for warning in warnings:
                print(f"WARNING {warning}")
            stats["warnings"] += len(warnings) if block is not None else 0

            if block is None:
                print(f"SKIPPED line {line_no}: no {TRANSACTIE_ID_COLUMN}")
                stats["skipped"] += 1
                continue

            transactie_id = row[TRANSACTIE_ID_COLUMN].strip()
            if transactie_id in seen_ids:
                print(f"SKIPPED line {line_no}: duplicate Transactie ID {transactie_id}")
                stats["skipped"] += 1
                continue
            seen_ids.add(transactie_id)

            yield block

    # --- Stream the CSV and write batched .ttl + .graph files ---
    written = 0
    num_files = 0
    with open(TRANSACTIE_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        for batch_index, batch in enumerate(batched(resources(reader), ROWS_PER_FILE), start=1):
            basename = f"{timestamp}-{OUTPUT_PREFIX}-{str(batch_index).zfill(pad_width)}"
            ttl_path = OUTPUT_DIR / f"{basename}.ttl"
            graph_path = OUTPUT_DIR / f"{basename}.graph"

            with open(ttl_path, "w", encoding="utf-8") as out:
                out.write(TURTLE_PREFIXES)
                out.write("\n".join(batch))

            with open(graph_path, "w", encoding="utf-8") as out:
                out.write(TARGET_GRAPH)

            written += len(batch)
            num_files += 1
            print(f"Wrote {len(batch)} transactions to {ttl_path.resolve()} (+ .graph)")

    print(f"\nConverted {written} transactions")
    print(f"Filtered out {stats['filtered']} rows (Actie not annotated or empty)")
    print(f"Skipped {stats['skipped']} rows")
    print(f"{stats['warnings']} field warning(s)")
    print(f"Done: {num_files} .ttl file(s) (+ .graph) generated.")


if __name__ == "__main__":
    main()
