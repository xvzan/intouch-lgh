"""Conservative validation of the two observed LGH/IDX sample layouts.

A successful audit means the current *experimental* structures are internally
consistent. It does NOT independently prove the log contains all observations.
"""
from __future__ import annotations

from pathlib import Path
from .index import IDXReader
from .reader import LGHReader, FormatError


def audit_archive_pair(lgh_path: str | Path, idx_path: str | Path) -> dict:
    """Strictly scan numeric records and verify IDX block coverage/tag names.

    Raises FormatError on unsupported/corrupt block or index structure.
    Intended for closed immutable file pairs.
    """
    lgh_path = Path(lgh_path)
    idx = IDXReader(idx_path)
    index_report = idx.verify_lgh(lgh_path)
    reader = LGHReader(lgh_path)
    # Audit typed block identity, not only matching names. An IDX reference
    # matching the name of a metadata block must never be treated as data.
    numeric_blocks = {block.offset: block.tag for block in reader.blocks()}
    metadata_offsets = {offset for offset, _ in reader.metadata_blocks}
    indexed_numeric = {}
    for entry in idx.entries():
        if entry.lgh_offset in metadata_offsets:
            continue
        indexed_numeric[entry.lgh_offset] = entry.tag
    if numeric_blocks != indexed_numeric:
        missing = sorted(set(numeric_blocks) - set(indexed_numeric))
        extra = sorted(set(indexed_numeric) - set(numeric_blocks))
        differing = sorted(offset for offset in numeric_blocks.keys() & indexed_numeric.keys()
                           if numeric_blocks[offset] != indexed_numeric[offset])
        raise FormatError(f"IDX numeric-block mapping mismatch: missing={missing[:5]} "
                          f"extra={extra[:5]} differing={differing[:5]}")
    tag_counts: dict[str, int] = {}
    records = 0
    for rec in reader.records():
        records += 1
        tag_counts[rec.tag] = tag_counts.get(rec.tag, 0) + 1
    return {
        'lgh_file': str(lgh_path), 'idx_file': str(idx_path),
        'numeric_records': records, 'tags': tag_counts,
        'metadata_blocks': len(reader.metadata_blocks),
        'unknown_blocks': len(reader.unknown_blocks),
        **index_report,
    }
