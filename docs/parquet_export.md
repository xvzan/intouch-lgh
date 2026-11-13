# Streaming Parquet export

The optional Parquet adapter streams the archive through the existing
`Archive -> RawBatch -> Arrow RecordBatch` path. It does not construct one
Python `Record` per observation and does not use CSV, Pandas, or Polars as an
intermediate format.

## Install

```powershell
python -m pip install -e ".[parquet]"
```

## Python API

```python
from intouch_lgh import Archive
from intouch_lgh.parquet_adapter import export_parquet

with Archive.open("tests/fixtures/24072300.lgh", "tests/fixtures/24072300.idx", validate="full") as archive:
    result = export_parquet(
        archive,
        "24072300.parquet",
        batch_size=65_536,
        row_group_size=65_536,
        compression="zstd",
    )

print(result.rows_written, result.file_size_bytes)
```

The archive must remain open for the duration of the export. Each yielded
`RawBatch` is detached from the mmap and owns its buffers. The exporter holds
at most the current RawBatch and Arrow RecordBatch plus the Parquet writer's
bounded internal buffers; it never materializes the complete archive as a
Python list or Arrow Table.

## Default columns

| Column | Arrow / Parquet type | Meaning |
|---|---|---|
| `tag` | dictionary<string, uint32> in Arrow; string in Parquet | Tag name |
| `value_type` | dictionary<string, uint8> in Arrow; string in Parquet | LGH value type |
| `filetime_ticks` | uint64 | Original FILETIME ticks |
| `value_bits` | uint64 | Authoritative IEEE-754 binary64 bit pattern |
| `value` | float64 | Convenient interpretation of `value_bits` |
| `block_offset` | uint64 | Source LGH block byte offset |
| `record_index` | uint8 | Original position inside the source block |
| `record_offset` | uint64 | Source LGH record byte offset |

`value_bits` is authoritative. Consumers that require exact NaN payload,
signaling-NaN, signed-zero, or other bit-level preservation must retain this
integer column and must not attempt to reconstruct it from `value` after
arithmetic or type conversion.

By default, schema metadata records only the source LGH basename, not the
full local path (`source_metadata="full"` opts into full paths and
`source_metadata="none"` omits it). It also records the physical-order contract, raw
FILETIME semantics, and the authoritative status of `value_bits`.

## Bounded memory and row groups

- `batch_size` is the maximum number of source records in one RawBatch.
- `row_group_size` is the maximum row count passed to each Parquet write call.
- The exporter does not buffer multiple RawBatches to fill a larger row group.
  Consequently a row group can be smaller than `row_group_size` at batch
  boundaries. This is intentional to keep memory bounded and predictable.
- The output preserves LGH physical block order. If downstream operations
  reorder rows, sort by `record_offset` to recover source physical order.

## Atomic output behavior

The output is first written to a unique `.partial` file in the destination
folder. The Parquet footer is closed and re-opened for a row-count check before
publication. On success:

- `overwrite=False` (default) publishes using an atomic no-clobber hard link;
  an existing destination raises `FileExistsError`.
- `overwrite=True` atomically replaces the destination with `os.replace`.

If parsing, writing, or footer validation fails, the temporary file is removed
and an existing destination is left untouched. The destination directory must
already exist. On filesystems that do not support hard links, no-clobber mode
raises an explanatory error; use `overwrite=True` only when replacement is
intended.

## CLI example

```powershell
python .\examples\parquet_export.py tests/fixtures/24072300.lgh .\out\24072300.parquet
```
