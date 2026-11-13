# Downstream interoperability validation

This validation suite checks that the Arrow output is consumable by downstream
Python data tools without making those tools core dependencies.

## Install

Core remains dependency-free. To install all optional consumers in the current
environment:

```powershell
python -m pip install -e ".[interop]"
```

Or install only what you want to test:

```powershell
python -m pip install pyarrow pandas polars duckdb
```

## Run

```powershell
python .\scripts\verify_interop.py
```

The test suite always runs the PyArrow/Parquet round trip when PyArrow is
installed. Pandas, Polars, and DuckDB tests run when their respective packages
are installed and otherwise are skipped.

## What is checked

- Multi-batch Arrow tables with per-batch dictionary contents.
- Parquet write/read round trip.
- Exact preservation of `filetime_ticks` and authoritative `value_bits`.
- Preservation of source record offsets and row order in a single Parquet file.
- Pandas, Polars, and DuckDB consumption of the Arrow table.
- Exact uint64 raw-column round trips through each consumer.

## Important data-fidelity rule

`value_bits` is the authoritative IEEE-754 representation. The `value` column
is a convenient floating-point interpretation. Consumers may normalize or
reinterpret exceptional floating-point values during arithmetic or conversion;
applications requiring exact NaN payload preservation must retain and use
`value_bits`.

This suite does not claim that arbitrary SQL transformations or partitioned
Parquet datasets preserve global physical row order. For downstream queries,
order explicitly by `record_offset` when physical order is required.
