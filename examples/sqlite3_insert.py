"""Usage example: caller owns SQLite schema, transaction, and NaN policy."""
import sqlite3
import struct
from pathlib import Path
from intouch_lgh import Archive


def import_lgh(path: str, database: str) -> int:
    idx_path = Path(path).with_suffix(".idx")
    count = 0
    with Archive.open(path, idx_path, validate="full") as archive:
        with sqlite3.connect(database) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS history (
                source_file TEXT, record_offset INTEGER, tag TEXT,
                filetime_ticks TEXT, value_bits BLOB NOT NULL,
                PRIMARY KEY (source_file, record_offset))""")
            for row in archive.iter_records():
                db.execute(
                    "INSERT INTO history VALUES (?, ?, ?, ?, ?)",
                    (row.source_file, row.record_offset, row.tag,
                     str(row.filetime_ticks), struct.pack("<Q", row.value_bits)),
                )
                count += 1
    return count


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("lgh")
    parser.add_argument("sqlite_db")
    args = parser.parse_args()
    print(import_lgh(args.lgh, args.sqlite_db))
