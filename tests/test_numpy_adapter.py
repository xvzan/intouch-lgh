from __future__ import annotations

from pathlib import Path
import builtins
import struct
import subprocess
import sys

import numpy as np
import pytest

from intouch_lgh import Archive, RawBatch, RawBlock
from intouch_lgh.numpy_adapter import iter_numpy_batches, to_numpy_batch

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_archive():
    lgh = FIXTURES / "24072300.lgh"
    return lgh, lgh.with_suffix(".idx")


def test_core_raw_batches_match_record_api_and_keep_physical_order():
    lgh, idx = _fixture_archive()
    from intouch_lgh.reader import LGHReader

    expected = list(LGHReader(lgh).records(tags={"MY_REAL", "MY_INT"}))
    with Archive.open(lgh, idx, validate="full") as archive:
        actual = []
        for batch in archive.iter_batches(tags={"MY_REAL", "MY_INT"}, batch_size=73):
            assert len(batch.filetime_ticks) == len(batch) * 8
            assert len(batch.value_bits) == len(batch) * 8
            assert len(batch.record_indices) == len(batch)
            for block, index, ticks, bits in batch.iter_raw():
                actual.append((
                    block.tag, block.value_type, block.block_offset,
                    index, ticks, bits,
                ))
        wanted = [
            (r.tag, r.value_type, r.block_offset, r.record_index,
             r.filetime_ticks, r.value_bits)
            for r in expected
        ]
        assert actual == wanted


def test_raw_batch_is_detached_and_survives_archive_close():
    lgh, idx = _fixture_archive()
    archive = Archive.open(lgh, idx, validate="none")
    batch = next(archive.iter_batches(tags="MY_REAL", batch_size=128))
    before_ticks = batch.filetime_ticks
    before_bits = batch.value_bits
    archive.close()
    assert batch.filetime_ticks == before_ticks
    assert batch.value_bits == before_bits
    assert len(list(batch.iter_raw())) == len(batch)


def test_numpy_views_share_raw_batch_buffer_without_copy():
    lgh, idx = _fixture_archive()
    with Archive.open(lgh, idx, validate="none") as archive:
        raw = next(archive.iter_batches(tags="MY_REAL", batch_size=100))
        batch = to_numpy_batch(raw, copy=False)

        assert batch.filetime_ticks.dtype == np.dtype("<u8")
        assert batch.value_bits.dtype == np.dtype("<u8")
        assert batch.values.dtype == np.dtype("<f8")
        assert batch.filetime_ticks.strides == (8,)
        assert batch.value_bits.strides == (8,)
        assert batch.values.strides == (8,)
        assert np.shares_memory(batch.filetime_ticks, np.frombuffer(raw.filetime_ticks, dtype="u1"))
        assert np.shares_memory(batch.value_bits, np.frombuffer(raw.value_bits, dtype="u1"))
        assert np.shares_memory(batch.values, batch.value_bits)
        assert not batch.value_bits.flags.writeable
        assert not batch.values.flags.writeable
        assert not batch.copied


def test_numpy_copy_mode_produces_contiguous_columns():
    lgh, idx = _fixture_archive()
    with Archive.open(lgh, idx, validate="none") as archive:
        raw = next(archive.iter_batches(tags="MY_REAL", batch_size=100))
        batch = to_numpy_batch(raw, copy=True)
        assert batch.filetime_ticks.flags.c_contiguous
        assert batch.value_bits.flags.c_contiguous
        assert batch.values.flags.c_contiguous
        assert batch.values.strides == (8,)
        assert batch.copied  # explicit copy mode
        assert batch.filetime_ticks.flags.writeable
        assert batch.value_bits.flags.writeable
        assert batch.values.flags.writeable


def test_numpy_adapter_iterates_batches_and_maps_block_metadata():
    lgh, idx = _fixture_archive()
    with Archive.open(lgh, idx, validate="none") as archive:
        batches = list(iter_numpy_batches(
            archive, tags={"MY_REAL", "MY_INT"}, batch_size=91
        ))
        assert sum(map(len, batches)) == sum(
            1 for _ in archive.iter_records(tags={"MY_REAL", "MY_INT"})
        )
        for batch in batches:
            assert len(batch.row_tags) == len(batch)
            assert len(batch.row_value_types) == len(batch)
            assert len(batch.record_offsets) == len(batch)
            assert batch.block_starts[0] == 0
            assert batch.block_starts[-1] == len(batch)
            assert len(batch.block_offsets) == len(batch.blocks)


def test_numpy_special_float_payloads_are_bit_exact():
    payloads = [
        0x7FF8000000000042,  # quiet NaN with payload
        0xFFF8000000001234,  # negative quiet NaN
        0x7FF0000000000042,  # signaling NaN payload
        0x7FF0000000000000,  # +Inf
        0xFFF0000000000000,  # -Inf
        0x8000000000000000,  # -0.0
    ]
    ticks = b"".join(struct.pack("<Q", i) for i in range(len(payloads)))
    value_bits = b"".join(struct.pack("<Q", bits) for bits in payloads)
    raw = RawBatch(
        filetime_ticks=ticks,
        value_bits=value_bits,
        record_indices=bytes(range(len(payloads))),
        blocks=(RawBlock(2, 512, "TAG", "REAL", 0, len(payloads)),),
        source_file="synthetic.lgh",
    )
    batch = to_numpy_batch(raw, copy=False)
    assert batch.value_bits.tolist() == payloads
    assert batch.values.view("<u8").tolist() == payloads


def test_numpy_core_is_not_imported_by_default():
    code = """
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'numpy' or name.startswith('numpy.'):
        raise AssertionError('numpy imported by core')
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
import intouch_lgh
import intouch_lgh.numpy_adapter
from intouch_lgh import Archive, RawBatch
print('ok')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_raw_batch_rejects_wrong_column_buffer_length():
    with pytest.raises(ValueError, match="8 bytes per record"):
        RawBatch(
            filetime_ticks=b"short",
            value_bits=struct.pack("<Q", 2),
            record_indices=b"\x00",
            blocks=(RawBlock(2, 512, "TAG", "REAL", 0, 1),),
            source_file="synthetic.lgh",
        )


def test_raw_batch_rejects_inconsistent_block_row_ranges():
    with pytest.raises(ValueError, match="contiguous"):
        RawBatch(
            filetime_ticks=struct.pack("<Q", 1),
            value_bits=struct.pack("<Q", 2),
            record_indices=b"\x00",
            blocks=(RawBlock(2, 512, "TAG", "REAL", 1, 2),),
            source_file="synthetic.lgh",
        )


def test_archive_raw_and_numpy_paths_preserve_special_payload_bits(tmp_path):
    import shutil
    from intouch_lgh.reader import LGHReader

    source_lgh, source_idx = _fixture_archive()
    lgh = tmp_path / source_lgh.name
    idx = tmp_path / source_idx.name
    shutil.copyfile(source_lgh, lgh)
    shutil.copyfile(source_idx, idx)

    first = next(block for block in LGHReader(lgh).blocks() if block.tag == "MY_REAL")
    payloads = [
        0x7FF8000000000042,
        0xFFF8000000001234,
        0x7FF0000000000042,
        0x7FF0000000000000,
        0xFFF0000000000000,
        0x8000000000000000,
    ]
    with lgh.open("r+b") as stream:
        for i, bits in enumerate(payloads):
            stream.seek(first.offset + 56 + i * 16 + 8)
            stream.write(struct.pack("<Q", bits))

    with Archive.open(lgh, idx, validate="full") as archive:
        raw = next(archive.iter_batches(tags="MY_REAL", batch_size=64))
        block = next(b for b in raw.blocks if b.block_offset == first.offset)
        row_start = block.row_start
        raw_bits = [
            struct.unpack_from("<Q", raw.value_bits, (row_start + i) * 8)[0]
            for i in range(len(payloads))
        ]
        assert raw_bits == payloads

        numpy_batch = to_numpy_batch(raw, copy=False)
        numpy_bits = numpy_batch.value_bits[row_start:row_start + len(payloads)].tolist()
        assert numpy_bits == payloads
        assert numpy_batch.values[row_start:row_start + len(payloads)].view("<u8").tolist() == payloads


def test_numpy_batch_remains_valid_after_archive_close():
    lgh, idx = _fixture_archive()
    archive = Archive.open(lgh, idx, validate="none")
    batch = next(iter_numpy_batches(archive, tags="MY_REAL", batch_size=100))
    expected = batch.value_bits.tolist()
    archive.close()
    assert batch.value_bits.tolist() == expected
    assert batch.raw.filetime_buffer.readonly
    assert batch.raw.value_bits_buffer.readonly
