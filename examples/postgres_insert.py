"""Example: psycopg is chosen by the application, not by intouch_lgh."""
from pathlib import Path
from intouch_lgh import Archive


def import_lgh(path: str, dsn: str) -> int:
    import psycopg  # pip install psycopg[binary]

    total = 0
    with Archive.open(path, Path(path).with_suffix(".idx"), validate="full") as archive:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS history (
                    source_file TEXT NOT NULL, record_offset BIGINT NOT NULL,
                    tag TEXT NOT NULL, filetime_ticks NUMERIC(20,0) NOT NULL,
                    value_bits_hex CHAR(16) NOT NULL,
                    PRIMARY KEY (source_file, record_offset))""")
                for row in archive.iter_records():
                    cur.execute(
                        "INSERT INTO history VALUES (%s, %s, %s, %s, %s)",
                        (row.source_file, row.record_offset, row.tag,
                         row.filetime_ticks, f"{row.value_bits:016x}"),
                    )
                    total += 1
    return total
