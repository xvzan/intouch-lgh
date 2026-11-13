from __future__ import annotations

from pathlib import Path
import struct
import gc

import pytest

pa = pytest.importorskip("pyarrow")

from intouch_lgh import Archive, RawBatch, RawBlock
from intouch_lgh.arrow_adapter import iter_arrow_batches, to_arrow_batch

FIXTURES = Path(__file__).parent / "fixtures"


def _raw_special():
    payloads = [
        0x7FF8000000000042,
        0xFFF8000000001234,
        0x7FF0000000000042,
        0x7FF0000000000000,
        0xFFF0000000000000,
        0x8000000000000000,
    ]
    ticks = b"".join(struct.pack("<Q", i + 100) for i in range(len(payloads)))
    bits = b"".join(struct.pack("<Q", value) for value in payloads)
    return RawBatch(
        filetime_ticks=ticks,
        value_bits=bits,
        record_indices=bytes(range(len(payloads))),
        blocks=(RawBlock(2, 512, "TAG", "REAL", 0, len(payloads)),),
        source_file="synthetic.lgh",
    ), payloads


def test_arrow_primitive_columns_share_raw_batch_buffers():
    raw, payloads = _raw_special()
    batch = to_arrow_batch(raw)
    assert batch.num_rows == len(payloads)
    assert batch.schema.field("filetime_ticks").type == pa.uint64()
    assert batch.schema.field("value_bits").type == pa.uint64()
    assert batch.schema.field("value").type == pa.float64()

    ticks_addr = batch.column(batch.schema.get_field_index("filetime_ticks")).buffers()[1].address
    bits_addr = batch.column(batch.schema.get_field_index("value_bits")).buffers()[1].address
    values_addr = batch.column(batch.schema.get_field_index("value")).buffers()[1].address
    assert ticks_addr == pa.py_buffer(raw.filetime_ticks).address
    assert bits_addr == pa.py_buffer(raw.value_bits).address
    assert values_addr == bits_addr

    # Inspect raw buffers rather than converting signaling NaNs to Python floats.
    assert batch.column(batch.schema.get_field_index("value_bits")).buffers()[1].to_pybytes() == raw.value_bits
    assert batch.column(batch.schema.get_field_index("value")).buffers()[1].to_pybytes() == raw.value_bits


def test_arrow_dictionary_metadata_and_provenance_columns():
    raw = RawBatch(
        filetime_ticks=struct.pack("<QQQ", 1, 2, 3),
        value_bits=struct.pack("<QQQ", 11, 12, 13),
        record_indices=bytes([0, 1, 0]),
        blocks=(
            RawBlock(2, 512, "A", "REAL", 0, 2),
            RawBlock(4, 1536, "B", "INT", 2, 3),
        ),
        source_file="fixture.lgh",
    )
    batch = to_arrow_batch(
        raw,
        columns=("tag", "value_type", "block_offset", "record_offset", "record_index"),
    )
    assert batch.column(0).to_pylist() == ["A", "A", "B"]
    assert batch.column(1).to_pylist() == ["REAL", "REAL", "INT"]
    assert batch.column(2).to_pylist() == [512, 512, 1536]
    assert batch.column(3).to_pylist() == [568, 584, 1592]
    assert batch.column(4).to_pylist() == [0, 1, 0]
    assert batch.schema.metadata[b"intouch_lgh.record_order"].startswith(b"LGH physical")


def test_arrow_adapter_preserves_data_after_archive_close():
    lgh = FIXTURES / "24072300.lgh"
    archive = Archive.open(lgh, lgh.with_suffix(".idx"), validate="none")
    batches = iter_arrow_batches(archive, tags="MY_REAL", batch_size=100)
    arrow_batch = next(batches)
    expected_ticks = arrow_batch.column(
        arrow_batch.schema.get_field_index("filetime_ticks")
    ).to_pylist()
    expected_bits = arrow_batch.column(
        arrow_batch.schema.get_field_index("value_bits")
    ).buffers()[1].to_pybytes()
    batches.close()
    gc.collect()
    archive.close()
    gc.collect()
    assert arrow_batch.column(
        arrow_batch.schema.get_field_index("filetime_ticks")
    ).to_pylist() == expected_ticks
    assert arrow_batch.column(
        arrow_batch.schema.get_field_index("value_bits")
    ).buffers()[1].to_pybytes() == expected_bits


def test_arrow_arrays_keep_raw_python_buffers_alive():
    raw, payloads = _raw_special()
    expected_ticks = raw.filetime_ticks
    expected_bits = raw.value_bits
    batch = to_arrow_batch(raw, columns=("filetime_ticks", "value_bits", "value"))
    del raw
    gc.collect()

    ticks = batch.column(0).buffers()[1]
    bits = batch.column(1).buffers()[1]
    values = batch.column(2).buffers()[1]
    assert ticks.to_pybytes() == expected_ticks
    assert bits.to_pybytes() == expected_bits
    assert values.to_pybytes() == expected_bits
    assert bits.address == values.address


def test_arrow_projection_and_empty_batch():
    raw, _ = _raw_special()
    batch = to_arrow_batch(raw, columns=("filetime_ticks", "value"))
    assert batch.schema.names == ["filetime_ticks", "value"]
    empty = RawBatch(b"", b"", b"", (), "empty.lgh")
    empty_batch = to_arrow_batch(empty)
    assert empty_batch.num_rows == 0


def test_arrow_rejects_unknown_or_duplicate_columns():
    raw, _ = _raw_special()
    with pytest.raises(ValueError, match="unknown"):
        to_arrow_batch(raw, columns=("not_a_column",))
    with pytest.raises(ValueError, match="duplicates"):
        to_arrow_batch(raw, columns=("value", "value"))


def test_tag_dictionary_index_type_is_stable_across_batches():
    def make_raw(tags):
        n = len(tags)
        blocks = tuple(
            RawBlock(i + 1, (i + 1) * 512, tag, "REAL", i, i + 1)
            for i, tag in enumerate(tags)
        )
        return RawBatch(
            filetime_ticks=b"".join(struct.pack("<Q", i + 1) for i in range(n)),
            value_bits=b"".join(struct.pack("<Q", i + 100) for i in range(n)),
            record_indices=bytes(n),
            blocks=blocks,
            source_file="dictionary-test.lgh",
        )

    small = to_arrow_batch(make_raw(["A", "B"]))
    large = to_arrow_batch(make_raw([f"TAG_{i:03d}" for i in range(300)]))
    assert small.schema.field("tag").type.index_type == pa.uint32()
    assert large.schema.field("tag").type.index_type == pa.uint32()
    table = pa.Table.from_batches([small, large])
    assert table.num_rows == 302
    assert table.column("tag").to_pylist() == ["A", "B"] + [
        f"TAG_{i:03d}" for i in range(300)
    ]


def test_source_file_metadata_defaults_to_basename_and_supports_privacy_modes():
    raw, _ = _raw_special()
    raw = RawBatch(
        filetime_ticks=raw.filetime_ticks,
        value_bits=raw.value_bits,
        record_indices=raw.record_indices,
        blocks=raw.blocks,
        source_file=r"C:\\Users\\alice\\Downloads\\sample.lgh",
    )

    default = to_arrow_batch(raw)
    assert default.schema.metadata[b"intouch_lgh.source_file"] == b"sample.lgh"
    assert b"alice" not in default.schema.metadata[b"intouch_lgh.source_file"]

    full = to_arrow_batch(raw, source_metadata="full")
    assert full.schema.metadata[b"intouch_lgh.source_file"] == (
        r"C:\\Users\\alice\\Downloads\\sample.lgh".encode()
    )

    hidden = to_arrow_batch(raw, source_metadata="none")
    assert b"intouch_lgh.source_file" not in hidden.schema.metadata

    with pytest.raises(ValueError, match="source_metadata"):
        to_arrow_batch(raw, source_metadata="invalid")


def test_arrow_iterator_validates_source_metadata_policy_before_first_batch():
    lgh = FIXTURES / "24072300.lgh"
    archive = Archive.open(lgh, lgh.with_suffix(".idx"), validate="none")
    try:
        iterator = iter_arrow_batches(
            archive, tags="__NO_SUCH_TAG__", source_metadata="invalid"
        )
        with pytest.raises(ValueError, match="source_metadata"):
            next(iterator)
    finally:
        archive.close()
