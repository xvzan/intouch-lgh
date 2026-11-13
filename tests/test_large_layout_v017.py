from intouch_lgh.index import IDXReader
from intouch_lgh.reader import LGHReader
"""Regression coverage for 2025 archives without shipping their raw contents."""
import os
from pathlib import Path
import struct
import pytest


def test_index_node_text_can_span_512_boundary(tmp_path):
    # A 1024-byte logical index node with 19 names 25 bytes each.
    header = bytearray(1024)
    header[8:12] = bytes.fromhex('000400fc')
    node = bytearray(1024)
    struct.pack_into('<HHII', node, 0, 1, 19, 0, 0)
    struct.pack_into('<19I', node, 12, *range(2, 21))
    offset = 12 + 19*4
    for i in range(19):
        name = f'LONG_TAG_NAME_NUMBER_{i:04d}'.encode()
        node[offset:offset+len(name)+1] = name + b'\0'
        offset += len(name)+1
    assert offset > 512
    path = tmp_path / 'example.idx'
    path.write_bytes(header+node)
    pages=list(IDXReader(path).pages())
    assert len(pages)==1 and len(pages[0].entries)==19
    assert pages[0].entries[-1].tag == 'LONG_TAG_NAME_NUMBER_0018'


@pytest.mark.parametrize('opcode,name', [(0x001613EC,'$HIST_ROLLIN'),(0x001713EC,'$HIST_ROLLOVER')])
def test_new_metadata_opcode(tmp_path, opcode, name):
    raw = bytearray(1024)
    raw[8:12] = bytes.fromhex('000400fc')
    struct.pack_into('<IIQ',raw,512,opcode,1,133000000000000000)
    raw[512+16:512+16+len(name)] = name.encode('ascii')
    path=tmp_path/'sample.lgh';path.write_bytes(raw)
    reader=LGHReader(path)
    assert list(reader.records())==[]
    assert reader.metadata_blocks == [(0,'file_header'),(512,name)]


def test_optional_large_archive():
    # Optional large-file regression: supply a directory of your own archive pairs.
    base_value = os.environ.get("INTOUCH_LGH_EXTRA_FIXTURES") or os.environ.get("INTOUCH_LGH_CORPUS_DIR")
    if not base_value:
        pytest.skip("Set INTOUCH_LGH_EXTRA_FIXTURES to enable optional large archive validation")
    base = Path(base_value)
    paths = sorted(p for p in base.glob("*.lgh") if p.with_suffix(".idx").is_file())
    if not paths:
        pytest.skip("No complete LGH/IDX archive pairs found")
    lgh = max(paths, key=lambda p: p.stat().st_size)
    idx = lgh.with_suffix(".idx")
    count = sum(1 for _ in LGHReader(lgh).records())
    assert count > 0
    assert sum(1 for _ in LGHReader(lgh).records()) == count
    report = IDXReader(idx).verify_lgh(lgh)
    assert report["indexed_blocks"] == report["lgh_blocks"] - 1
