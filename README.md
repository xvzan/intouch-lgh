# intouch-lgh

A read-only Python library for inspecting and querying the community-observed
InTouch LGH/IDX layouts represented by the bundled public fixtures and the
additional archive samples used during development.

This is **not an official AVEVA file-format specification**. Compatibility
with other InTouch revisions, active/incomplete archives, compressed layouts,
and unobserved record types remains unverified.

## Design principles

- Preserve physical LGH record order; do not sort, deduplicate, interpolate,
  resample, or silently discard observations.
- Preserve raw FILETIME ticks as unsigned 64-bit integers.
- Preserve IEEE-754 payload bits as unsigned 64-bit integers. `values` is an
  interpretation view; `value_bits` is authoritative for exact fidelity.
- Keep the core independent of NumPy, PyArrow, and database drivers.
- Keep database writes, Parquet file management, and ETL policy in the
  calling application.

## Install

Core only:

```bash
pip install .
```

Optional NumPy adapter:

```bash
pip install '.[numpy]'
```

Development and benchmarks:

```bash
pip install '.[test,benchmark]'
```

## Main API

```python
from intouch_lgh import Archive

with Archive.open(
    "tests/fixtures/24072300.lgh",
    "tests/fixtures/24072300.idx",
    validate="full",
) as archive:
    # NumPy-independent batches with separate raw numeric columns.
    for raw_batch in archive.iter_batches(
        tags="MY_REAL",
        batch_size=65_536,
    ):
        for block, record_index, filetime_ticks, value_bits in raw_batch.iter_raw():
            process_raw(block.tag, filetime_ticks, value_bits)
```

`RawBatch.filetime_ticks` and `RawBatch.value_bits` are separate immutable
little-endian uint64 buffers. `value_bits` is the authoritative original
IEEE-754 payload; `values` in the NumPy/Arrow adapters is only a float64 view.
Each `RawBlock` maps a contiguous row range to its source Tag, value type, LGH
block number, and physical offset. `record_indices` retains each row's original
position within its source block. A batch owns its buffers and remains valid
after the `Archive` is closed. Creating a batch de-interleaves the original
16-byte on-disk entries once into the two final column buffers.

For the convenience object interface:

```python
with Archive.open("tests/fixtures/24072300.lgh", "tests/fixtures/24072300.idx") as archive:
    for record in archive.iter_records(tags="MY_REAL"):
        process(record)
```

## Validated Tag catalogue

After opening with `validate="full"`, the archive exposes
`archive.tag_counts`, a read-only mapping from Tag name to the number of
numeric observations in that Tag:

```python
with Archive.open(lgh_path, idx_path, validate="full") as archive:
    for tag, count in archive.tag_counts.items():
        print(tag, count)
```

The counts are accumulated directly from validated LGH numeric block headers
during the IDX/LGH full validation pass, without creating one `BlockRef`
object per indexed block. This does not construct or
decode a `Record` for every observation. Accessing `tag_counts` after
`validate="none"` raises `RuntimeError` until `archive.validate_full()` has
completed successfully. The mapping is published only after the entire
LGH/IDX validation succeeds.

The full IDX/LGH coverage check also tracks referenced physical blocks in a
compact bytearray. It does not materialize a Python `IndexEntry` list, a second
block-number list, and a large set solely to test coverage. `Archive.validate_full()`
uses a fused IDX/LGH block-header validation and Tag count collection path;
no separate full LGH block scan is needed. The standalone
`IDXReader.verify_lgh()` API is retained for independent callers.

## Optional NumPy adapter

NumPy is not imported by `import intouch_lgh`. Import the adapter only when
needed:

```python
from intouch_lgh import Archive
from intouch_lgh.numpy_adapter import iter_numpy_batches

with Archive.open("tests/fixtures/24072300.lgh", "tests/fixtures/24072300.idx") as archive:
    for batch in iter_numpy_batches(
        archive,
        tags="MY_REAL",
        batch_size=65_536,
        copy=False,
    ):
        process(batch.filetime_ticks, batch.values, batch.value_bits)
```

With `copy=False`, NumPy arrays are contiguous, read-only views over the
separate `RawBatch.filetime_ticks` and `RawBatch.value_bits` buffers. There is
**no additional copy from RawBatch to NumPy**. The RawBatch itself is detached
from the mmap and owns the two selected column buffers. Use `copy=True` when
writable arrays are needed; that mode copies the FILETIME and value-bit columns.

`batch.values` is a float64 view of `batch.value_bits`. For exact IEEE-754
preservation—including NaN payloads and signaling NaNs—retain and use
`value_bits`, not the result of floating-point operations.


## Optional Apache Arrow adapter

PyArrow is optional and is not imported by the core package:

```bash
pip install '.[arrow]'
```

```python
from intouch_lgh import Archive
from intouch_lgh.arrow_adapter import iter_arrow_batches

with Archive.open("tests/fixtures/24072300.lgh", "tests/fixtures/24072300.idx") as archive:
    for batch in iter_arrow_batches(
        archive,
        tags="MY_REAL",
        batch_size=65_536,
    ):
        process(batch)
```

Arrow `uint64` FILETIME and `uint64` value-bit arrays are constructed directly
from RawBatch buffers. The Arrow `float64` value array shares the same buffer as
`value_bits`; it does not reinterpret values through Python floats. Tag and
type columns are dictionary encoded. Optional per-row provenance columns are
materialized only when requested. The adapter does not write Parquet files.

The Arrow adapter remains optional. Its included interoperability suite was
run successfully on Windows with Python 3.14.3, PyArrow 26.0.1, Pandas 2.3.3,
Polars 2.1.0, and DuckDB 2.0.0. This is validation evidence for that environment,
not a guarantee for every dependency/platform combination.

## Query and validation behavior

- `validate="full"` performs a full LGH/IDX pair audit when opening the archive.
- `validate="none"` skips whole-file coverage audit but still checks accessed
  structures and blocks.
- Tag queries use the observed IDX tree layout. The currently supported
  structure has been cross-checked against multiple samples; other versions
  may differ.
- Time filters are half-open: `start <= timestamp < end`.
- Results retain LGH physical block order and original within-block order.
- The open archive checks file identity/metadata before reads. Do not modify
  either file while an `Archive` is open.

## Scope and limitations

The parser is based on reverse engineering and observed samples, not an
official specification. Two small public fixture pairs and additional larger
archives have been used in development. This is meaningful
cross-sample evidence, not proof of compatibility with every InTouch version.
The package is read-only and does not support files actively being written.

See `docs/design_v1.md` for architecture and data-layout decisions. Historical
implementation and corpus-audit reports are maintained separately and are not
included in the public source distribution.


## Downstream interoperability validation

Optional interoperability dependencies can be installed with:

```bash
pip install -e ".[interop]"
```

This adds PyArrow, Pandas, Polars, and DuckDB as optional tools. They are not
core dependencies. Run the validation suite with:

```bash
python scripts/verify_interop.py
```

See `docs/interop_validation.md` for scope and data-fidelity notes.


## Streaming Parquet export

Install the optional Parquet adapter with `pip install intouch-lgh[parquet]`.
It streams `Archive` batches directly through PyArrow to a temporary Parquet
file, verifies the completed footer, and atomically publishes the result. It
retains raw `filetime_ticks`, authoritative `value_bits`, and LGH physical
provenance. See `docs/parquet_export.md`.


## Parquet source metadata privacy

Parquet and Arrow schema metadata now store only the LGH basename by default,
rather than the full local path. Use `source_metadata="full"` to opt in to the
full path, or `"none"` to omit the source path.

## Parquet performance matrix

Install benchmark dependencies with `pip install -e ".[interop,benchmark]"`.
Run `python scripts/benchmark_parquet_matrix.py PATH_TO_LGH` to compare batch
sizes, Row Group sizes, compression codecs, peak RSS, and DuckDB/Polars reads.


## Parquet validation benchmark (v1.0.0a8)

Run `python scripts/benchmark_parquet_validation.py PATH_TO_LGH` for an
independent Batch Size × Row Group Size benchmark, isolated export memory and
performance measurements, and DuckDB/Polars query comparisons. Benchmark
reports are generated locally and are not distributed with the package.


## Focused small-Batch Size validation (v1.0.0a9)

Run `scripts/benchmark_small_batches.ps1` to compare 4K, 8K, and 16K source
batches with a fixed 64K Parquet Row Group and five independent export runs.



## Cross-file validation (v1.0.0a10)

Use `scripts/benchmark_cross_files.ps1` to validate multiple LGH/IDX pairs
with the selected 16K Batch / 64K Row Group / ZSTD export settings.



## External corpus regression (v1.0.0a11)

Validate user-provided LGH/IDX pairs with full integrity, strict type,
multi-level IDX topology, and tree-vs-scan checks:

```powershell
python -m pip install -e ".[test]"
.\scripts\validate_corpus.ps1 -CorpusDir "D:\path\to\private\corpus"
```

By default, the validation report is written into the supplied corpus
folder, not the repository. You can override this with `-Output`. Sample
archives and corpus-specific validation reports are not bundled in the
public package.


## Timestamp semantics audit (v1.0.0a12)

To run the audit on a local corpus:

```powershell
python .\scripts\audit_timestamps.py C:\path\to\corpus
```

The audit preserves raw order and duplicate timestamps; it does not apply a
timezone conversion or resample observations. Corpus-specific audit reports
are kept outside the public distribution.


## Cross-archive boundary audit (v1.0.0a13)

The independent `intouch_lgh_diagnostics` package provides
`intouch-lgh-audit-boundaries` (also available as
`scripts/audit_archive_boundaries.py` in the source tree). It compares per-Tag
first/last observations across ordered LGH/IDX pairs, preserves physical order
and duplicate timestamps, and does not impose a universal sampling-gap
threshold. See `docs/archive_boundary_audit_v1a13.md`. Detailed reports from user-provided
industrial archives are not included in the public distribution.

## Reproducing the included tests

From the project source directory, install `.[test]`, then run
`python -m pytest -q -rs`. The optional Arrow, Parquet, and interop checks
require the matching extras; run `scripts/verify_arrow.ps1` and
`scripts/verify_interop.ps1` on Windows to install and verify them.

## Release status

The library supports observed LGH/IDX layouts only. Unobserved formats are
not guaranteed; raw data must not be repaired or resampled silently.

## License and third-party materials

The `intouch-lgh` project implementation is licensed under the Apache License,
Version 2.0. Copyright 2026 Xv Zan. See `LICENSE` and `NOTICE`.

The bundled public test fixtures and example output from
[PKUbuntu/intouch-dumper](https://github.com/PKUbuntu/intouch-dumper) remain
under their original MIT License, Copyright (c) 2024 Boarmon. They are not
relicensed under Apache 2.0. See `THIRD_PARTY_NOTICES.md` and
`tests/fixtures/UPSTREAM_LICENSE.txt`.

The project was developed with AI-assisted code generation, under the
direction, review, and testing of Xv Zan. This acknowledgement does not
replace the original license and attribution of third-party materials.


## Compatibility testing

The project declares Python 3.10 or newer. The released source distribution
contains the full public test suite; CI checks Python 3.10 through 3.14 on
Linux and Windows. A passing CI run is evidence for the corresponding
Python/dependency combination, not a guarantee for other InTouch layouts.

On Windows, use `scripts/verify_python_versions.ps1` to run the test suite
under installed Python 3.10/3.12 interpreters. See
`docs/release_validation.md` for the release validation checklist.


### Full-validation diagnostics (1.0.2)

`Archive.validation_report` exposes optional phase timings and work counters when `validate="full"` is used. Full validation walks IDX leaf entries once, cross-checks each Tag against the corresponding fixed-size LGH block header, validates the block header, and builds the Tag record-count catalogue in the same pass. This avoids the former per-entry file seek/read followed by a second full 512-byte LGH scan; record payloads are not decoded during archive validation. `lgh_block_header_bytes_read` reports the fixed headers (and any blank-block tails) actually inspected. `lgh_header_bytes_requested` retains its legacy meaning of 40-byte Tag-header comparison work, while `lgh_logical_bytes_scanned` describes the complete logical LGH block range covered by validation rather than physical disk I/O. `lgh_block_scan_seconds` is `0.0` because the separate sequential scan no longer exists. The public `IDXReader.verify_lgh()` method and its return mapping remain unchanged. See [`docs/full_validation_optimization.md`](docs/full_validation_optimization.md) for the implementation rationale, error-ordering tests, and benchmark methodology.
