"""
strip_validation_status_column.py
──────────────────────────────────
One-off cleanup script: removes the "__validation_status__" column from
every existing *_duplicates.parquet file, across every data_type/IP,
in place.

Usage (from the same directory/venv as the running server, so
output_writer.py and config.py are importable):

    python3 strip_validation_status_column.py            # dry run, lists what it would change
    python3 strip_validation_status_column.py --apply     # actually rewrites the files

Safe to re-run: files that don't have the column are skipped untouched.
"""
from __future__ import annotations

import sys

import fastparquet as fp
import pandas as pd

from output_writer import iter_all_outputs

COLUMN_TO_DROP = "__validation_status__"


def main(apply: bool) -> None:
    changed = 0
    skipped = 0
    failed = 0

    for data_type, ip_name, file_id, path in iter_all_outputs(kind="duplicates"):
        try:
            df = pd.read_parquet(path, engine="fastparquet")
        except Exception as e:
            print(f"[FAIL]    {path}  ({e})")
            failed += 1
            continue

        if COLUMN_TO_DROP not in df.columns:
            skipped += 1
            continue

        label = f"{data_type}/{ip_name or file_id}"
        if apply:
            df = df.drop(columns=[COLUMN_TO_DROP])
            fp.write(str(path), df, compression="ZSTD")
            print(f"[CHANGED] {label}: {path}  (dropped '{COLUMN_TO_DROP}', {len(df)} rows rewritten)")
        else:
            print(f"[WOULD CHANGE] {label}: {path}  (has '{COLUMN_TO_DROP}', {len(df.columns)} cols)")
        changed += 1

    print()
    print(f"Done. changed={changed} skipped(no column)={skipped} failed={failed}")
    if not apply and changed:
        print("Dry run only — re-run with --apply to actually rewrite these files.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
