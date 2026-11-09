"""Compare cleanup_filter against the live adapter's recorded outputs.

Reads a COPY of transcriptions.db (never the live profile file) and replays
raw_text through clean_text with the deployed options. Rows whose processed
text differs because of app-side snippet expansion are reported separately.

Usage: python -I history_parity.py <copy-of-transcriptions.db> [--since ISO]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup_filter import clean_text  # noqa: E402

# Cleanup was first active for the dictation at 2026-10-03T07:37Z (the Oct 2
# rows predate deployment of the filter).
DEFAULT_SINCE = "2026-10-03 07:37"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--since", default=DEFAULT_SINCE)
    ap.add_argument("--show", action="store_true", help="print mismatching text")
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{Path(a.db).as_posix()}?mode=ro", uri=True)
    rows = con.execute(
        "select id, timestamp, raw_text, text from transcriptions "
        "where raw_text is not null and timestamp >= ? order by id", (a.since,)).fetchall()
    match = changed_match = snippet = mismatch = 0
    for id_, ts, raw, live in rows:
        ours = clean_text(raw, enabled=True, protected_terms=[], remove_repetitions=True)
        if ours == live:
            match += 1
            changed_match += raw != live
            continue
        # Snippet expansion happens in the app after cleanup; if our output
        # equals raw and the live text is longer, attribute it to snippets.
        if ours == raw and len(live) > len(raw):
            snippet += 1
            continue
        mismatch += 1
        print(f"MISMATCH row {id_} {ts}")
        if a.show:
            print("  raw :", raw)
            print("  live:", live)
            print("  ours:", ours)
    print(f"rows={len(rows)} exact={match} (of which cleanup-changed={changed_match}) "
          f"snippet-only={snippet} mismatches={mismatch}")
    return 1 if mismatch else 0


if __name__ == "__main__":
    sys.exit(main())
