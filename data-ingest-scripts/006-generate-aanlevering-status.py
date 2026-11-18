#!/usr/bin/env python3
"""
Reads 'Status rapport' and 'Meest recente' of every aanlevering from
aanlevering_MJP_2026.csv and writes them as Turtle (.ttl) migration files,
batched per ROWS_PER_FILE triples:

    <aanlevering/{id}> vmm:statusRapport "Herziening-Overdracht-2026" .
    <aanlevering/{older_id}> dct:isReplacedBy <aanlevering/{latest_id}> .
    <aanlevering/{latest_id}> dct:replaces <aanlevering/{older_id}> .

Next to every .ttl file a .graph file with the same name is written,
containing the target graph URI, so mu-migrations-service loads the data into
that graph.

'Meest recente' is not stored as a flag but as replacement links: per
Bestuur ID exactly one aanlevering has "Ja"; every other aanlevering of that
bestuur is replaced by it. The most recent aanlevering of a bestuur is thus
the one without dct:isReplacedBy. A bestuur with zero or several "Ja"
aanleveringen gets no links and is reported on stdout.

The subject is the Aanlevering URI the insert notebooks create
(vmm-dev/data/insert/insert_vmm.ipynb, uri("aanlevering", id)), with the id
normalized the same way as format_vmm.ipynb (identifier()): "12.0" -> "12".

Before the .ttl files, a .sparql migration is written that deletes every
existing vmm:statusRapport triple and every dct:isReplacedBy / dct:replaces
link between two aanleveringen from the target graph, so rerunning the
script does not leave stale values behind. It sorts before the .ttl files
("delete" < "insert"), so mu-migrations-service runs it first.

Rows without an Aanlevering ID are skipped and reported on stdout. If an
Aanlevering ID occurs several times, the first row is kept and a warning is
printed.

Output: {timestamp}-delete-aanlevering-status.sparql,
        {timestamp}-insert-aanlevering-status-001.ttl / .graph, ...

Usage:
    python 006-generate-aanlevering-status.py
"""

import csv
import math
from collections import Counter, defaultdict
from datetime import datetime
from itertools import islice
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent

AANLEVERING_CSV = SCRIPT_DIR / "../../MJP_2026/aanlevering_MJP_2026.csv"
OUTPUT_DIR = SCRIPT_DIR / "../config/migrations/add-aanlevering-status"
OUTPUT_PREFIX = "insert-aanlevering-status"
DELETE_PREFIX = "delete-aanlevering-status"

TARGET_GRAPH = "http://mu.semte.ch/graphs/public"

ROWS_PER_FILE = 50000   # triples per generated .ttl file

# Must match vmm-dev/data/insert/insert_vmm.ipynb
VMM_BASE_URI = "http://lblod.data.gift/vocabularies/vmm/"

ID_COLUMN = "Aanlevering ID"
BESTUUR_COLUMN = "Bestuur ID"
MEEST_RECENT_COLUMN = "Meest recente"
STATUS_COLUMN = "Status rapport"

TURTLE_PREFIXES = """@prefix vmm: <http://lblod.data.gift/vocabularies/vmm/> .
@prefix dct: <http://purl.org/dc/terms/> .

"""

DELETE_QUERY = f"""PREFIX vmm: <http://lblod.data.gift/vocabularies/vmm/>
PREFIX dct: <http://purl.org/dc/terms/>

DELETE {{
  GRAPH <{TARGET_GRAPH}> {{
    ?aanlevering vmm:statusRapport ?status .
  }}
}} WHERE {{
  GRAPH <{TARGET_GRAPH}> {{
    ?aanlevering vmm:statusRapport ?status .
  }}
}}

;

DELETE {{
  GRAPH <{TARGET_GRAPH}> {{
    ?older dct:isReplacedBy ?newer .
    ?newer dct:replaces ?older .
  }}
}} WHERE {{
  GRAPH <{TARGET_GRAPH}> {{
    ?older a vmm:Aanlevering .
    ?newer a vmm:Aanlevering .
    {{ ?older dct:isReplacedBy ?newer . }}
    UNION
    {{ ?newer dct:replaces ?older . }}
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


def aanlevering_uri(aanlevering_id):
    return f"{VMM_BASE_URI}aanlevering/{aanlevering_id}"


def turtle_string(text):
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def read_aanleveringen(csv_path, stats):
    """Return a list of dicts (id, bestuur, meest_recent, status), one per Aanlevering ID."""
    aanleveringen = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        columns = (ID_COLUMN, BESTUUR_COLUMN, MEEST_RECENT_COLUMN, STATUS_COLUMN)
        missing = [c for c in columns if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"Missing column(s) in {csv_path}: {missing}")

        # start=2: line 1 is the header
        for line_no, row in enumerate(reader, start=2):
            stats["rows"] += 1

            aanlevering_id = normalize_id(value(row, ID_COLUMN))
            if not aanlevering_id:
                print(f"SKIPPED line {line_no}: no {ID_COLUMN}")
                stats["skipped"] += 1
                continue
            if aanlevering_id in aanleveringen:
                print(f"WARNING line {line_no}: {ID_COLUMN} {aanlevering_id} occurs "
                      f"more than once; keeping the first")
                stats["duplicates"] += 1
                continue

            meest_recent = value(row, MEEST_RECENT_COLUMN)
            if meest_recent not in ("Ja", "Nee"):
                print(f"WARNING line {line_no}: {ID_COLUMN} {aanlevering_id} has "
                      f"{MEEST_RECENT_COLUMN} {meest_recent!r}; treated as not most recent")
                stats["meest_recent_other"] += 1

            aanleveringen[aanlevering_id] = {
                "id": aanlevering_id,
                "bestuur": normalize_id(value(row, BESTUUR_COLUMN)),
                "meest_recent": meest_recent == "Ja",
                "status": value(row, STATUS_COLUMN),
            }

    return list(aanleveringen.values())


def status_triples(aanleveringen, status_counts):
    triples = []
    for a in aanleveringen:
        if not a["status"]:
            status_counts["(empty)"] += 1
            continue
        status_counts[a["status"]] += 1
        triples.append(
            f'<{aanlevering_uri(a["id"])}> vmm:statusRapport {turtle_string(a["status"])} .')
    return triples


def replacement_triples(aanleveringen, stats):
    by_bestuur = defaultdict(list)
    for a in aanleveringen:
        if not a["bestuur"]:
            print(f"WARNING {ID_COLUMN} {a['id']} has no {BESTUUR_COLUMN}; "
                  f"no replacement links")
            stats["no_bestuur"] += 1
            continue
        by_bestuur[a["bestuur"]].append(a)

    triples = []
    for bestuur, group in by_bestuur.items():
        stats[f"besturen with {len(group)} aanlevering(en)"] += 1
        latest = [a for a in group if a["meest_recent"]]
        if len(latest) != 1:
            print(f"WARNING {BESTUUR_COLUMN} {bestuur} has {len(latest)} aanleveringen "
                  f"marked '{MEEST_RECENT_COLUMN}' = Ja (expected 1); no replacement links")
            stats["besturen_without_single_latest"] += 1
            continue

        newer_uri = aanlevering_uri(latest[0]["id"])
        for a in group:
            if a is latest[0]:
                continue
            older_uri = aanlevering_uri(a["id"])
            triples.append(f"<{older_uri}> dct:isReplacedBy <{newer_uri}> .")
            triples.append(f"<{newer_uri}> dct:replaces <{older_uri}> .")
            stats["links"] += 1
    return triples


def main():
    csv_path = AANLEVERING_CSV.resolve()
    stats = Counter()
    aanleveringen = read_aanleveringen(csv_path, stats)
    print(f"Read {stats['rows']} records from {csv_path}: "
          f"{len(aanleveringen)} aanleveringen, {stats['skipped']} skipped, "
          f"{stats['duplicates']} duplicate(s), "
          f"{stats['meest_recent_other']} with '{MEEST_RECENT_COLUMN}' other than Ja/Nee")

    status_counts = Counter()
    triples = status_triples(aanleveringen, status_counts)
    print(f"\n{STATUS_COLUMN}:")
    for status, count in status_counts.most_common():
        print(f"  {count:5d}  {status}")

    replacement_stats = Counter()
    triples.extend(replacement_triples(aanleveringen, replacement_stats))
    print(f"\n{MEEST_RECENT_COLUMN}:")
    for key in sorted(k for k in replacement_stats if k.startswith("besturen with ")):
        print(f"  {replacement_stats[key]:5d}  {key}")
    print(f"  {replacement_stats['besturen_without_single_latest']:5d}  besturen skipped "
          f"(not exactly one Ja)")
    print(f"  {replacement_stats['no_bestuur']:5d}  aanleveringen skipped (no {BESTUUR_COLUMN})")
    print(f"  {replacement_stats['links']:5d}  replacement links "
          f"(dct:isReplacedBy + dct:replaces)")

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
            for triple in batch:
                out.write(triple + "\n")

        with open(graph_path, "w", encoding="utf-8") as out:
            out.write(TARGET_GRAPH)

        print(f"Wrote {len(batch)} triples to {ttl_path.resolve()} (+ .graph)")

    print(f"\nDone: 1 .sparql file and {num_batches} .ttl file(s) (+ .graph) generated.")


if __name__ == "__main__":
    main()
