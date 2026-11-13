from intouch_lgh.reader import LGHReader
"""Semantic values are an optional validated *view*; raw f64 is unchanged."""
from pathlib import Path
import pytest
from intouch_lgh import FormatError
from test_reader import make_block

FIXTURES = Path(__file__).parent / "fixtures"


def test_real_fixtures_semantic_types():
    counts = {}
    for name in ("24072200", "24072300"):
        records = list(LGHReader(FIXTURES / f"{name}.lgh", validate_types=True).records())
        for r in records:
            counts[r.value_type] = counts.get(r.value_type, 0) + 1
            logical = r.logical_value
            if r.value_type == "BOOL":
                assert type(logical) is bool and float(logical) == r.value
            elif r.value_type == "INT":
                assert type(logical) is int and logical == r.value
            else:
                assert type(logical) is float and logical == r.value
    assert counts == {"REAL": 1867, "INT": 1600, "BOOL": 1468}


@pytest.mark.parametrize("opcode,value", [
    (b"\xec\x13\x01\x00", 2.0),
    (b"\xec\x13\x01\x00", 0.5),
    (b"\xec\x13\x02\x00", 3.5),
    (b"\xec\x13\x02\x00", float("nan")),
])
def test_invalid_semantic_payload_only_rejected_in_strict_mode(tmp_path, opcode, value):
    p = tmp_path / "synthetic.lgh"
    p.write_bytes(make_block(opcode=opcode, observations=[(116444736000000000, value)]))
    raw = list(LGHReader(p).records())
    assert len(raw) == 1  # Preserve original payload; avoid silent data loss
    with pytest.raises(FormatError, match="Invalid"):
        _ = raw[0].logical_value
    with pytest.raises(FormatError, match="Invalid"):
        list(LGHReader(p, validate_types=True).records())


def test_python_cli_module_help(capsys):
    from intouch_lgh.cli import main
    # main takes argv from sys.argv; in the test install interface is covered by pyproject.
    assert callable(main)
