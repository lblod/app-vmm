#!/usr/bin/env python3
"""
Reads the 'prioritair' field of the strategische doelstellingen,
beleidsdoelstellingen, actieplannen and acties from the MJP CSVs and writes it
as Turtle (.ttl) migration files, batched per ROWS_PER_FILE triples:
    <{kind}/{id}/expression> vmm:prioritair "true"^^xsd:boolean .
Next to every .ttl file a .graph file with the same name is written,
containing the target graph URI, so mu-migrations-service loads the data into
that graph.

Value mapping:
    "Ja"     -> "true"^^xsd:boolean
    "Nee"    -> "false"^^xsd:boolean
    anything else (empty, "0", ...) -> no triple

The subject is the Expression URI the insert notebooks create
(vmm-dev/data/insert/insert_vmm.ipynb, expression_uri()), with the id
normalized the same way as format_vmm.ipynb (identifier()): "12.0" -> "12".

Before the .ttl files, a .sparql migration is written that deletes every
existing vmm:prioritair triple from the target graph. Earlier imports stored
"false" for empty fields, and stored nothing for actieplannen and acties; the
delete makes sure only the values from this script remain. It sorts before the
.ttl files ("delete" < "insert"), so mu-migrations-service runs it first.

Rows without an id are skipped and reported on stdout; the placeholder
Strategische doelstelling id 0 is left out and only counted. If an id occurs
several times in one CSV with different values, the first is kept and a
warning is printed.

Output: {timestamp}-delete-prioritair.sparql,
        {timestamp}-insert-prioritair-001.ttl / .graph, ...

Usage:
    python 005-generate-prioritair.py
"""

import csv
import math
from collections import Counter
from datetime import datetime
from itertools import islice
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent

MJP_DIR = SCRIPT_DIR / "../../Beleid_MJP_2026"
OUTPUT_DIR = SCRIPT_DIR / "../config/migrations/add-prioritair"
OUTPUT_PREFIX = "insert-prioritair"
DELETE_PREFIX = "delete-prioritair"

TARGET_GRAPH = "http://mu.semte.ch/graphs/public"

ROWS_PER_FILE = 50000   # triples per generated .ttl file

# Must match vmm-dev/data/insert/insert_vmm.ipynb
VMM_BASE_URI = "http://lblod.data.gift/vocabularies/vmm/"

# (URI kind, CSV file, id column, prioritair column)
SOURCES = [
    ("strategische_doelstelling", "strategische_doesltelling_MJP_2026.csv",
     "Strategische doelstelling ID", "Strategische doelstelling prioritair"),
    ("beleidsdoelstelling", "beleidsdoelstelling_MJP_2026.csv",
     "Beleidsdoelstelling ID", "Beleidsdoelstelling prioritair"),
    ("actieplan", "actieplan_MJP_2026.csv",
     "Actieplan ID", "Actieplan prioritair"),
    ("actie", "actie_MJP_2026.csv",
     "Actie ID", "Actie prioritair"),
]

VALUE_MAPPING = {"Ja": "true", "Nee": "false"}

# Ids that are not real resources ("geen strategische doelstelling")
IGNORED_IDS = {"strategische_doelstelling": {"0"}}

TURTLE_PREFIXES = """@prefix vmm: <http://lblod.data.gift/vocabularies/vmm/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

"""

DELETE_QUERY = f"""PREFIX vmm: <http://lblod.data.gift/vocabularies/vmm/>

DELETE {{
  GRAPH <{TARGET_GRAPH}> {{
    ?expression vmm:prioritair ?prioritair .
  }}
}} WHERE {{
  GRAPH <{TARGET_GRAPH}> {{
    ?expression vmm:prioritair ?prioritair .
  }}
}}
"""


def batched(iterable, size):
    """Yield lists of at most `size` items from any iterable (streaming)."""
    it = iter(iterable)
    while batch := list(islice(it, size)):
        yield batch


def value(row, column):
    return (row.get(column) or "").strip()


def normalize_id(raw):
    """Same normalization as identifier() in format_vmm.ipynb: "12.0" -> "12"."""
    try:
        number = float(raw)
        if number.is_integer():
            return str(int(number))
    except ValueError:
        pass
    return raw


def expression_uri(kind, resource_id):
    return f"{VMM_BASE_URI}{kind}/{resource_id}/expression"


def read_source(kind, csv_path, id_column, prioritair_column, stats):
    """Return a dict mapping Expression URI -> "true"/"false" for one CSV."""
    values = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in (id_column, prioritair_column)
                   if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"Missing column(s) in {csv_path}: {missing}")

        # start=2: line 1 is the header
        for line_no, row in enumerate(reader, start=2):
            stats["rows"] += 1

            resource_id = normalize_id(value(row, id_column))
            if not resource_id:
                print(f"SKIPPED {csv_path.name} line {line_no}: no {id_column}")
                stats["skipped"] += 1
                continue
            if resource_id in IGNORED_IDS.get(kind, set()):
                stats["ignored"] += 1
                continue

            # Anything other than Ja/Nee (e.g. the "0" in the doelstelling
            # CSVs) counts as not filled in
            prioritair = value(row, prioritair_column)
            literal = VALUE_MAPPING.get(prioritair)
            if literal is None:
                stats["empty"] += 1
                continue

            uri = expression_uri(kind, resource_id)
            if uri in values:
                if values[uri] != literal:
                    print(f"WARNING {csv_path.name} line {line_no}: {id_column} "
                          f"{resource_id} occurs with conflicting values; keeping "
                          f"the first ({values[uri]})")
                    stats["conflicts"] += 1
                continue

            values[uri] = literal
            stats[prioritair] += 1

    return values


def main():
    triples = []
    for kind, filename, id_column, prioritair_column in SOURCES:
        csv_path = (MJP_DIR / filename).resolve()
        stats = Counter()
        values = read_source(kind, csv_path, id_column, prioritair_column, stats)
        triples.extend(values.items())
        print(f"{kind}: read {stats['rows']} records from {csv_path}: "
              f"{stats['Ja']} Ja, {stats['Nee']} Nee, {stats['empty']} empty or other, "
              f"{stats['ignored']} ignored, {stats['skipped']} skipped, "
              f"{stats['conflicts']} conflicting duplicate(s)")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

    # --- Write the .sparql that clears the existing values ---
    delete_path = OUTPUT_DIR / f"{timestamp}-{DELETE_PREFIX}.sparql"
    with open(delete_path, "w", encoding="utf-8") as out:
        out.write(DELETE_QUERY)
    print(f"\nWrote {delete_path.resolve()}")

    if not triples:
        print("No values to write, no .ttl files generated.")
        return

    # --- Write batched .ttl + .graph files ---
    num_batches = math.ceil(len(triples) / ROWS_PER_FILE)
    pad_width = max(3, len(str(num_batches)))

    for batch_index, batch in enumerate(batched(triples, ROWS_PER_FILE), start=1):
        basename = f"{timestamp}-{OUTPUT_PREFIX}-{str(batch_index).zfill(pad_width)}"
        ttl_path = OUTPUT_DIR / f"{basename}.ttl"
        graph_path = OUTPUT_DIR / f"{basename}.graph"

        with open(ttl_path, "w", encoding="utf-8") as out:
            out.write(TURTLE_PREFIXES)
            for uri, literal in batch:
                out.write(f'<{uri}> vmm:prioritair "{literal}"^^xsd:boolean .\n')

        with open(graph_path, "w", encoding="utf-8") as out:
            out.write(TARGET_GRAPH)

        print(f"Wrote {len(batch)} triples to {ttl_path.resolve()} (+ .graph)")

    print(f"\nDone: 1 .sparql file and {num_batches} .ttl file(s) (+ .graph) generated.")


if __name__ == "__main__":
    main()
