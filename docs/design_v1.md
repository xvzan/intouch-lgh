# v1.0 architecture decisions (alpha)

## Responsibilities

### Core
- Parse the currently observed LGH/IDX layouts.
- Own archive file handles and read-only mappings.
- Locate Tags through the observed IDX tree.
- Preserve LGH physical order and raw payloads.
- Expose a small NumPy-independent `RawBatch` API.
- Validate the archive pair and detect file replacement/change.

### Optional adapters
- NumPy: provide contiguous zero-copy views or explicit writable copies over
  `RawBatch`.
- Arrow: optional adapter constructs primitive arrays directly from raw column
  buffers and dictionary-encodes block metadata. It is not a database/Parquet
  writer and remains experimental until PyArrow integration tests run.

### Application responsibilities
- Database schemas, transactions, conflict policies, retries and checkpoints.
- Parquet partitioning, compression, file naming and atomic publication.
- Timezone interpretation, resampling, interpolation, and business semantics.

## Data model

An LGH numeric observation is currently observed as a 16-byte payload:
`<Qd` (unsigned 64-bit FILETIME ticks followed by an IEEE-754 binary64).
The exact raw value bits are the fidelity authority.

`RawBatch` stores two immutable byte buffers: little-endian uint64
`filetime_ticks` and uint64 `value_bits`. These are separate primitive columns,
not interleaved rows. `record_indices` retains original within-block indices.
Each `RawBlock` stores one source block's Tag, value type, block number,
physical offset, and the row interval in the batch.

This avoids per-record Python `Record` objects and avoids repeating Tag/type/
block-offset metadata for every row. The selected 16-byte source payloads are
de-interleaved once into the two final column buffers. The batch is detached
from mmap, so its lifetime does not prevent `Archive.close()`.

## NumPy adapter

`intouch_lgh.numpy_adapter` is optional and imports NumPy only when its adapter
functions are called.

`copy=False` uses `np.frombuffer` separately over the two RawBatch uint64
buffers. The FILETIME and value-bit columns are contiguous, read-only views
(stride 8 bytes). `values` is a float64 view of `value_bits`. No copy occurs
between RawBatch and NumPy.

`copy=True` copies FILETIME and value-bit fields into contiguous arrays. This
may be faster for repeated vectorized calculations, but uses additional memory.

## Ordering and query semantics

- Blocks are returned in ascending LGH physical block order.
- Records inside each block retain encoded order.
- Time filtering is half-open and does not reorder rows.
- Tag query block numbers are sorted into physical LGH order before reading.
- Duplicate observations and equal timestamps are retained.

## Validation

`validate='full'` runs a full LGH/IDX pair audit at open. `validate='none'`
does not claim whole-file integrity; accessed pages and blocks are still
checked. File identity/metadata is checked during iteration. In-place changes
that preserve all observed stat metadata are outside the current guarantee.

## Open questions

- Whether the observed IDX header/tree is stable across other InTouch versions.
- Whether zero-filled LGH blocks always represent unused padding.
- Whether additional record opcodes/layouts exist.
- Whether `RawBatch` should eventually support segmented mmap-backed views;
  this would avoid the one copy but requires an explicit lifetime/lease API.
- Run the optional Arrow adapter integration tests with PyArrow installed on
  supported operating systems and architectures.
- Evaluate whether a future leased mmap-backed batch can avoid the one copy
  from the source archive without making close/lifetime semantics surprising.
