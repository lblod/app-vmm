#!/usr/bin/env python3
"""
Matches records from aanlevering_MJP_2026.csv to their Bestuurseenheid URI
in the Virtuoso triplestore (via bestuur.csv as a cross-reference table),
and generates one or more .sparql files containing INSERT queries that
link each Aanlevering to its Bestuurseenheid(en) via prov:wasAttributedTo.

Special case: if a Bestuur ID's 'KBO nummer' has no match in the triplestore
AND its 'Type bestuur' is 'Gemeente en OCMW', we look up the two underlying
records in bestuur.csv that share the same 'RE nummer' (the 'Gemeente' and
'OCMW' rows, identified via 'Type bestuur origineel') and use each of those
that resolves to a URI.

If a row has no match for any other reason, it is skipped and reported on
stdout.

Output: insert-aanlevering-bestuurseenheid-001.sparql, -002.sparql, ...
        each containing up to VALUES_BATCH_SIZE (?aanleveringId ?bestuurseenheid)
        pairs in a single INSERT query.

Usage:
    python generate_aanlevering_sparql.py
"""

import csv
import math
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent

BESTUUR_CSV = SCRIPT_DIR / "../../Dimensies/bestuur.csv"
AANLEVERING_CSV = SCRIPT_DIR / "../../MJP_2026/aanlevering_MJP_2026.csv"
OUTPUT_DIR = SCRIPT_DIR / "../config/migrations"
OUTPUT_PREFIX = "insert-aanlevering-bestuurseenheid"

SPARQL_ENDPOINT = "http://localhost:8890/sparql"
LOOKUP_BATCH_SIZE = 250   # batch size for the SELECT queries against the triplestore
VALUES_BATCH_SIZE = 250   # batch size for VALUES pairs per generated INSERT file

KBO_COLUMN = "KBO nummer"
BESTUUR_ID_COLUMN = "Bestuur ID"
RE_NUMMER_COLUMN = "RE nummer"
TYPE_BESTUUR_COLUMN = "Type bestuur"
TYPE_BESTUUR_ORIGINEEL_COLUMN = "Type bestuur origineel"

GEMEENTE_EN_OCMW = "Gemeente en OCMW"
GEMEENTE = "Gemeente"
OCMW = "OCMW"

LOOKUP_QUERY_TEMPLATE = """
PREFIX dct: <http://purl.org/dc/terms/>
SELECT DISTINCT ?o ?uri WHERE {{
  VALUES ?o {{
{values}
  }}
  ?uri a <http://data.vlaanderen.be/ns/besluit#Bestuurseenheid> ;
       dct:identifier ?o .
}}
"""

INSERT_QUERY_TEMPLATE = """PREFIX vmm: <http://lblod.data.gift/vocabularies/vmm/>
PREFIX dct: <http://purl.org/dc/terms/>
PREFIX prov: <http://www.w3.org/ns/prov#>

INSERT {{
  GRAPH <http://mu.semte.ch/graphs/public> {{
    ?aanlevering prov:wasAttributedTo ?bestuurseenheid .
  }}
}} WHERE {{
   VALUES (?aanleveringId ?bestuurseenheid) {{
{values}
   }}
  ?aanlevering a vmm:Aanlevering ;
       dct:identifier ?aanleveringId .
}}
"""


def chunked(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def fetch_kbo_to_uri(kbo_nummers):
    """
    Query the triplestore in batches and return a dict mapping
    KBO nummer -> Bestuurseenheid URI for every match found.
    """
    kbo_to_uri = {}
    unique_ids = sorted(set(kbo_nummers))

    for batch in chunked(unique_ids, LOOKUP_BATCH_SIZE):
        values_block = "\n".join(f'    "{ident}"' for ident in batch)
        query = LOOKUP_QUERY_TEMPLATE.format(values=values_block)

        response = requests.post(
            SPARQL_ENDPOINT,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
        )
        response.raise_for_status()

        for binding in response.json()["results"]["bindings"]:
            kbo = binding["o"]["value"]
            uri = binding["uri"]["value"]
            kbo_to_uri[kbo] = uri

    return kbo_to_uri


def main():
    # --- Load bestuur.csv, indexed by Bestuur ID and by RE nummer ---
    with open(BESTUUR_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        bestuur_rows = list(reader)

    bestuur_by_id = {row[BESTUUR_ID_COLUMN]: row for row in bestuur_rows}

    bestuur_by_re_nummer = {}
    for row in bestuur_rows:
        bestuur_by_re_nummer.setdefault(row[RE_NUMMER_COLUMN], []).append(row)

    print(f"Read {len(bestuur_rows)} records from {BESTUUR_CSV}")

    # --- Load aanlevering_MJP_2026.csv ---
    with open(AANLEVERING_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        aanlevering_rows = list(reader)

    print(f"Read {len(aanlevering_rows)} records from {AANLEVERING_CSV}")

    # --- Query the triplestore once for every KBO nummer in bestuur.csv ---
    all_kbo_nummers = [row[KBO_COLUMN] for row in bestuur_rows]
    kbo_to_uri = fetch_kbo_to_uri(all_kbo_nummers)
    print(f"Resolved {len(kbo_to_uri)} KBO nummer -> URI matches from the triplestore")

    # --- Build the (Aanlevering ID, Bestuurseenheid URI) pairs ---
    pairs = []
    skipped = 0

    for aanlevering_row in aanlevering_rows:
        aanlevering_id = aanlevering_row["Aanlevering ID"]
        bestuur_id = aanlevering_row[BESTUUR_ID_COLUMN]

        bestuur_row = bestuur_by_id.get(bestuur_id)
        if bestuur_row is None:
            print(f"SKIPPED Aanlevering ID {aanlevering_id}: "
                  f"Bestuur ID {bestuur_id} not found in {BESTUUR_CSV}")
            skipped += 1
            continue

        kbo_nummer = bestuur_row[KBO_COLUMN]
        uri = kbo_to_uri.get(kbo_nummer)

        if uri is not None:
            pairs.append((aanlevering_id, uri))
            continue

        # No direct match. Check for the Gemeente en OCMW special case.
        if bestuur_row[TYPE_BESTUUR_COLUMN] == GEMEENTE_EN_OCMW:
            re_nummer = bestuur_row[RE_NUMMER_COLUMN]
            siblings = bestuur_by_re_nummer.get(re_nummer, [])

            gemeente_row = next(
                (r for r in siblings if r[TYPE_BESTUUR_ORIGINEEL_COLUMN] == GEMEENTE),
                None,
            )
            ocmw_row = next(
                (r for r in siblings if r[TYPE_BESTUUR_ORIGINEEL_COLUMN] == OCMW),
                None,
            )

            found_any = False
            for sub_row in (gemeente_row, ocmw_row):
                if sub_row is None:
                    continue
                sub_uri = kbo_to_uri.get(sub_row[KBO_COLUMN])
                if sub_uri is not None:
                    pairs.append((aanlevering_id, sub_uri))
                    found_any = True

            if not found_any:
                print(f"SKIPPED Aanlevering ID {aanlevering_id}: Bestuur ID {bestuur_id} "
                      f"is '{GEMEENTE_EN_OCMW}' but neither Gemeente nor OCMW "
                      f"sub-record (RE nummer {re_nummer}) matched in the triplestore")
                skipped += 1
        else:
            print(f"SKIPPED Aanlevering ID {aanlevering_id}: Bestuur ID {bestuur_id} "
                  f"(KBO nummer {kbo_nummer}, Type bestuur "
                  f"'{bestuur_row[TYPE_BESTUUR_COLUMN]}') has no match in the triplestore")
            skipped += 1

    print(f"\nBuilt {len(pairs)} (Aanlevering ID, Bestuurseenheid URI) pairs")
    print(f"Skipped {skipped} aanlevering records with no resolvable match")

    # --- Write batched .sparql files ---
    if not pairs:
        print("No pairs to write, no .sparql files generated.")
        return

    num_batches = math.ceil(len(pairs) / VALUES_BATCH_SIZE)
    pad_width = max(3, len(str(num_batches)))

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

    for batch_index, batch in enumerate(chunked(pairs, VALUES_BATCH_SIZE), start=1):
        values_lines = "\n".join(
            f'      ("{aanlevering_id}" <{uri}> )'
            for aanlevering_id, uri in batch
        )
        query = INSERT_QUERY_TEMPLATE.format(values=values_lines)

        filename = f"{OUTPUT_DIR}/{timestamp}-{OUTPUT_PREFIX}-{str(batch_index).zfill(pad_width)}.sparql"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(query)

        print(f"Wrote {len(batch)} pairs to {filename}")

    print(f"\nDone: {num_batches} .sparql file(s) generated.")


if __name__ == "__main__":
    main()
