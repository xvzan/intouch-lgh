"""Stream an LGH/IDX archive directly to Parquet without CSV or row objects.

Install the optional adapter with ``pip install intouch-lgh[parquet]``.
"""
from pathlib import Path
import argparse

from intouch_lgh import Archive
from intouch_lgh.parquet_adapter import export_parquet


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lgh", type=Path)
    parser.add_argument("parquet", type=Path)
    parser.add_argument("--idx", type=Path)
    parser.add_argument("--batch-size", type=int, default=65_536)
    parser.add_argument("--row-group-size", type=int)
    parser.add_argument("--compression", default="zstd")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    idx = args.idx or args.lgh.with_suffix(".idx")
    with Archive.open(args.lgh, idx, validate="full") as archive:
        result = export_parquet(
            archive,
            args.parquet,
            batch_size=args.batch_size,
            row_group_size=args.row_group_size,
            compression=args.compression,
            overwrite=args.overwrite,
        )

    print(f"Rows: {result.rows_written:,}")
    print(f"Batches: {result.batches_written:,}")
    print(f"Row groups: {result.row_groups_written:,}")
    print(f"File size: {result.file_size_bytes:,} bytes")
    print(f"Output: {result.path}")


if __name__ == "__main__":
    main()
