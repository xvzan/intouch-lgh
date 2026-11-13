# Changelog

## 1.0.2

- Add phase-level full-validation diagnostics and elapsed-time counters.
- Validate IDX-to-LGH block headers in a single pass and derive per-tag counts.
- Preserve `IDXReader.verify_lgh()` behavior and existing API semantics.

## 1.0.1

- Expose validated per-tag record counts as `Archive.tag_counts`.
- Reduce peak memory use and redundant IDX traversals during full validation.

## 1.0.0

- Publish the read-only LGH/IDX parser and raw-record query API.
- Preserve original FILETIME ticks, IEEE-754 bits, physical record order and provenance.
- Offer optional NumPy, Arrow, Parquet and database integration helpers.
