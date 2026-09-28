#!/usr/bin/env python3
"""
Links the transactions from transactie_MJP_2026.csv to their Beleidsveld
concept in the Beleidsvelden codelist (see generate_beleidsveld_ttl.py), and
writes the links as Turtle (.ttl) migration files, batched per ROWS_PER_FILE
links:
    <transactie> dct:subject <beleidsveld> .
Next to every .ttl file a .graph file with the same name is written,
containing the target graph URI, so mu-migrations-service loads the data into
that graph.

The Beleidsveld concept is resolved in the triplestore via its dct:identifier
(the 'Beleidsveld ID') within the Beleidsvelden concept scheme, with batched
SELECT queries against SPARQL_ENDPOINT, once per unique Beleidsveld ID.

Only transactions that were loaded by generate_transactie_ttl.py are linked:
the same filter is applied (the Actie must be realized by an expression that
is the target of an annotation), so no queries are generated for transactions
that are not in the triplestore.

Rows whose Beleidsveld ID is in IGNORED_BELEIDSVELD_IDS ('Geen beleidsveld')
are left out and only counted. Rows without a Transactie ID or Beleidsveld ID,
and rows whose Transactie ID was already seen, are skipped and reported on
stdout. Beleidsveld IDs that do not resolve to a concept are reported once
each, with the number of transactions using them.

Output: {timestamp}-insert-transactie-beleidsveld-001.ttl / .graph,
        {timestamp}-insert-transactie-beleidsveld-002.ttl / .graph, ...

Run this after the codelist and transactie migrations have been loaded in the
triplestore at SPARQL_ENDPOINT.

Usage:
    python generate_transactie_beleidsveld_ttl.py
"""

import csv
import math
from collections import Counter
from datetime import datetime
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
OUTPUT_PREFIX = "insert-transactie-beleidsveld"

TARGET_GRAPH = "http://mu.semte.ch/graphs/public"

SPARQL_ENDPOINT = "http://localhost:8890/sparql"
LOOKUP_BATCH_SIZE = 250   # batch size for the SELECT queries against the triplestore
ROWS_PER_FILE = 50000     # links (= triples) per generated .ttl file

# Must match generate_transactie_ttl.py
TRANSACTIE_BASE_URI = "http://lblod.data.gift/vocabularies/vmm/transactie/"
ACTIE_BASE_URI = "http://lblod.data.gift/vocabularies/vmm/actie/"

# Must match generate_beleidsveld_ttl.py
CONCEPT_SCHEME_URI = ("http://lblod.data.gift/concept-schemes/"
                      "3f6b1c2e-8d4a-4e7b-9a51-6c0d2f8e4b17")
IGNORED_BELEIDSVELD_IDS = {"0"}

TRANSACTIE_ID_COLUMN = "Transactie ID"
ACTIE_ID_COLUMN = "Actie ID"
BELEIDSVELD_ID_COLUMN = "Beleidsveld ID"

REQUIRED_COLUMNS = [
    TRANSACTIE_ID_COLUMN,
    ACTIE_ID_COLUMN,
    BELEIDSVELD_ID_COLUMN,
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

BELEIDSVELD_QUERY_TEMPLATE = """
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX dct: <http://purl.org/dc/terms/>
SELECT DISTINCT ?id ?concept WHERE {{
  VALUES ?id {{
{values}
  }}
  ?concept a skos:Concept ;
       skos:inScheme <{scheme}> ;
       dct:identifier ?id .
}}
"""

TURTLE_PREFIXES = """@prefix dct: <http://purl.org/dc/terms/> .

"""


def batched(iterable, size):
    """Yield lists of at most `size` items from any iterable (streaming)."""
    it = iter(iterable)
    while batch := list(islice(it, size)):
        yield batch


def value(row, column):
    return (row.get(column) or "").strip()


def transactie_uri(transactie_id):
    return TRANSACTIE_BASE_URI + quote(transactie_id, safe="")


def actie_uri(actie_id):
    return ACTIE_BASE_URI + quote(actie_id, safe="")


def sparql_string(text):
    """Return a SPARQL string literal with proper escaping."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def run_select(query):
    response = requests.post(
        SPARQL_ENDPOINT,
        data={"query": query},
        headers={"Accept": "application/sparql-results+json"},
    )
    response.raise_for_status()
    return response.json()["results"]["bindings"]


def fetch_annotated_acties(actie_uris):
    """
    Query the triplestore in batches and return the set of Actie URIs that are
    realized by an expression which is the target of an annotation.
    """
    annotated = set()
    for batch in batched(sorted(set(actie_uris)), LOOKUP_BATCH_SIZE):
        values_block = "\n".join(f"    <{uri}>" for uri in batch)
        query = ANNOTATED_ACTIE_QUERY_TEMPLATE.format(values=values_block)
        for binding in run_select(query):
            annotated.add(binding["actie"]["value"])
    return annotated


def fetch_beleidsveld_concepts(beleidsveld_ids):
    """
    Query the triplestore in batches and return a dict mapping
    Beleidsveld ID -> Beleidsveld concept URI for every match found.
    """
    id_to_uri = {}
    for batch in batched(sorted(set(beleidsveld_ids)), LOOKUP_BATCH_SIZE):
        values_block = "\n".join(f"    {sparql_string(i)}" for i in batch)
        query = BELEIDSVELD_QUERY_TEMPLATE.format(
            values=values_block, scheme=CONCEPT_SCHEME_URI)
        for binding in run_select(query):
            beleidsveld_id = binding["id"]["value"]
            concept = binding["concept"]["value"]
            if id_to_uri.get(beleidsveld_id, concept) != concept:
                print(f"WARNING Beleidsveld ID {beleidsveld_id} matches several "
                      f"concepts; using {id_to_uri[beleidsveld_id]}")
                continue
            id_to_uri[beleidsveld_id] = concept
    return id_to_uri


def main():
    # --- Load transactie_MJP_2026.csv ---
    # utf-8-sig transparently strips a BOM, which would otherwise corrupt the
    # first column name.
    with open(TRANSACTIE_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"Missing column(s) in {TRANSACTIE_CSV}: {missing}")
        # Only keep the columns we need, to limit memory use
        rows = [
            (line_no, value(row, TRANSACTIE_ID_COLUMN),
             value(row, ACTIE_ID_COLUMN), value(row, BELEIDSVELD_ID_COLUMN))
            # start=2: line 1 is the header
            for line_no, row in enumerate(reader, start=2)
        ]

    print(f"Read {len(rows)} records from {TRANSACTIE_CSV}")

    # --- Ask the triplestore which Acties are annotated ---
    actie_uris = {actie_uri(actie_id) for _, _, actie_id, _ in rows if actie_id}
    annotated_acties = fetch_annotated_acties(actie_uris)
    print(f"{len(annotated_acties)} of {len(actie_uris)} Acties are annotated "
          f"in the triplestore")

    # --- Resolve the Beleidsveld IDs of those transactions ---
    beleidsveld_ids = {
        beleidsveld_id
        for _, _, actie_id, beleidsveld_id in rows
        if actie_id and actie_uri(actie_id) in annotated_acties
        and beleidsveld_id and beleidsveld_id not in IGNORED_BELEIDSVELD_IDS
    }
    id_to_concept = fetch_beleidsveld_concepts(beleidsveld_ids)
    print(f"Resolved {len(id_to_concept)} of {len(beleidsveld_ids)} Beleidsveld IDs "
          f"to a concept in the triplestore")

    # --- Build the (transactie URI, Beleidsveld concept URI) pairs ---
    pairs = []
    stats = {"filtered": 0, "ignored": 0, "skipped": 0}
    unresolved = Counter()
    seen_ids = set()

    for line_no, transactie_id, actie_id, beleidsveld_id in rows:
        if not actie_id or actie_uri(actie_id) not in annotated_acties:
            stats["filtered"] += 1
            continue

        if not transactie_id:
            print(f"SKIPPED line {line_no}: no {TRANSACTIE_ID_COLUMN}")
            stats["skipped"] += 1
            continue
        if transactie_id in seen_ids:
            print(f"SKIPPED line {line_no}: duplicate "
                  f"{TRANSACTIE_ID_COLUMN} {transactie_id}")
            stats["skipped"] += 1
            continue
        seen_ids.add(transactie_id)

        if beleidsveld_id in IGNORED_BELEIDSVELD_IDS:
            stats["ignored"] += 1
            continue
        if not beleidsveld_id:
            print(f"SKIPPED line {line_no} ({TRANSACTIE_ID_COLUMN} {transactie_id}): "
                  f"no {BELEIDSVELD_ID_COLUMN}")
            stats["skipped"] += 1
            continue

        concept = id_to_concept.get(beleidsveld_id)
        if concept is None:
            unresolved[beleidsveld_id] += 1
            stats["skipped"] += 1
            continue

        pairs.append((transactie_uri(transactie_id), concept))

    for beleidsveld_id, count in sorted(unresolved.items()):
        print(f"SKIPPED {count} transaction(s): {BELEIDSVELD_ID_COLUMN} "
              f"{beleidsveld_id} has no concept in the triplestore")

    print(f"\nBuilt {len(pairs)} (transactie, Beleidsveld) pairs")
    print(f"Filtered out {stats['filtered']} rows (Actie not annotated or empty)")
    print(f"Ignored {stats['ignored']} rows ({BELEIDSVELD_ID_COLUMN} in "
          f"{sorted(IGNORED_BELEIDSVELD_IDS)})")
    print(f"Skipped {stats['skipped']} rows")

    # --- Write batched .ttl + .graph files ---
    if not pairs:
        print("No pairs to write, no .ttl files generated.")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    num_batches = math.ceil(len(pairs) / ROWS_PER_FILE)
    pad_width = max(3, len(str(num_batches)))
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

    for batch_index, batch in enumerate(batched(pairs, ROWS_PER_FILE), start=1):
        basename = f"{timestamp}-{OUTPUT_PREFIX}-{str(batch_index).zfill(pad_width)}"
        ttl_path = OUTPUT_DIR / f"{basename}.ttl"
        graph_path = OUTPUT_DIR / f"{basename}.graph"

        with open(ttl_path, "w", encoding="utf-8") as out:
            out.write(TURTLE_PREFIXES)
            for transactie, concept in batch:
                out.write(f"<{transactie}> dct:subject <{concept}> .\n")

        with open(graph_path, "w", encoding="utf-8") as out:
            out.write(TARGET_GRAPH)

        print(f"Wrote {len(batch)} links to {ttl_path.resolve()} (+ .graph)")

    print(f"\nDone: {num_batches} .ttl file(s) (+ .graph) generated.")


if __name__ == "__main__":
    main()
