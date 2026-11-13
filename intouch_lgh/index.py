"""Experimental IDX block-reference decoder for the PKUbuntu public fixtures.

Observed index nodes occupy 1024 bytes each. Earlier small samples placed
all content within the first 512 bytes, but larger samples use both halves.
The first 1024 bytes contain the file header. Node kind 1 stores one-based
LGH block references and names; kind 0 is not fully decoded.

The implementation validates reference-to-LGH name relationships but makes
no claims about B-tree topology, time seeking, or other InTouch versions.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from bisect import bisect_left
import struct
import time

from .reader import BLOCK_SIZE, HEADER_SIZE, ENTRY_SIZE, MAX_ENTRIES, OPCODES, FormatError, Record, FILETIME_MAX_TICKS, _ENTRY_VALUE, _VALUE_BITS


@dataclass(frozen=True, slots=True)
class IndexEntry:
    index_page: int
    position: int
    block_number: int  # one-based LGH block number in the observed layout
    tag: str

    @property
    def lgh_offset(self) -> int:
        if self.block_number < 1:
            raise FormatError('Zero block reference is reserved')
        return (self.block_number - 1) * BLOCK_SIZE


@dataclass(frozen=True, slots=True)
class IndexPage:
    page_number: int
    kind: int
    node_id: int
    aux: int
    entries: tuple[IndexEntry, ...]


class IDXReader:
    def __init__(self, filename: str | Path, *, encoding: str = 'utf-8'):
        self.filename = Path(filename)
        self.encoding = encoding
        self.last_verify_diagnostics: dict[str, int | float] = {}

    def pages(self) -> Iterator[IndexPage]:
        # IDX nodes occupy 1024-byte logical pages. Small public samples
        # happened to fit their names in the first 512-byte half; larger
        # archives place names across both halves.
        page_size = 1024
        size = self.filename.stat().st_size
        if size < 2 * page_size or size % page_size:
            raise FormatError(f'Unsupported IDX size: {size}')
        with self.filename.open('rb') as f:
            header = f.read(BLOCK_SIZE)
            if header[:4] != b'\0'*4 or header[8:12] != bytes.fromhex('000400fc'):
                raise FormatError('Unsupported IDX header signature')
            # The remainder of the first logical page is zero in samples.
            if f.read(page_size - BLOCK_SIZE).strip(b'\0'):
                raise FormatError('Expected zero-filled IDX header padding')
            for logical_no in range(1, size // page_size):
                raw = f.read(page_size)
                if len(raw) != page_size:
                    raise FormatError(f'Truncated IDX page {logical_no}')
                kind, count, node_id, aux = struct.unpack_from('<HHII', raw, 0)
                if kind not in (0, 1):
                    raise FormatError(f'Unknown IDX page type {kind} at logical page {logical_no}')
                if count > (page_size - 12) // 5:
                    raise FormatError(f'Unreasonable IDX item count {count} at page {logical_no}')
                nums = struct.unpack_from(f'<{count}I', raw, 12) if count else ()
                cursor = 12 + 4 * count
                names = []
                for _ in range(count):
                    end = raw.find(b'\0', cursor)
                    if end < 0:
                        raise FormatError(f'Unterminated IDX name at page {logical_no}')
                    try:
                        name = raw[cursor:end].decode(self.encoding)
                    except UnicodeDecodeError as exc:
                        raise FormatError(f'Invalid IDX text at page {logical_no}') from exc
                    names.append(name)
                    cursor = end + 1
                if raw[cursor:].strip(b'\0'):
                    raise FormatError(f'Unexpected IDX payload at page {logical_no}')
                entries = tuple(IndexEntry(2*logical_no, i, number, name)
                                for i, (number, name) in enumerate(zip(nums, names, strict=True)))
                yield IndexPage(2*logical_no, kind, node_id, aux, entries)

    def entries(self) -> Iterator[IndexEntry]:
        """Return leaf refs. The observed zero/sentinel marker isn't an LGH block."""
        for page in self.pages():
            if page.kind == 1:
                for entry in page.entries:
                    if entry.block_number == 0:
                        # Both public examples include one sentinel 0 / 0x7f.
                        if entry.tag != '\x7f':
                            raise FormatError(f'Unexpected zero reference: {entry}')
                        continue
                    yield entry

    def verify_lgh(self, lgh_file: str | Path) -> dict[str, int | float]:
        """Cross-check leaf references and names against raw LGH block headers.

        Diagnostics split IDX page decoding from the per-entry LGH header
        probes. Timings are observational only and do not change validation.
        """
        started = time.perf_counter()
        self.last_verify_diagnostics = {}
        lgh_file = Path(lgh_file)
        size = lgh_file.stat().st_size
        if size < BLOCK_SIZE or size % BLOCK_SIZE:
            raise FormatError('LGH size is not a multiple of 512')
        count = size // BLOCK_SIZE
        idx_size = self.filename.stat().st_size
        seen = bytearray(count + 1)
        indexed_count = 0
        pages_parsed = 0
        idx_parse_seconds = 0.0
        lgh_header_check_seconds = 0.0
        lgh_header_reads = 0

        page_iter = iter(self.pages())
        with lgh_file.open('rb') as f:
            while True:
                parse_started = time.perf_counter()
                try:
                    page = next(page_iter)
                except StopIteration:
                    idx_parse_seconds += time.perf_counter() - parse_started
                    break
                idx_parse_seconds += time.perf_counter() - parse_started
                pages_parsed += 1
                if page.kind != 1:
                    continue

                check_started = time.perf_counter()
                for entry in page.entries:
                    number = entry.block_number
                    if number == 0:
                        # Preserve the established zero/sentinel rule from
                        # entries(): exactly one observed sentinel 0 / 0x7f.
                        if entry.tag != '\x7f':
                            raise FormatError(f'Unexpected zero reference: {entry}')
                        continue
                    if number < 2 or number > count:
                        raise FormatError(
                            f'IDX/LGH coverage mismatch; missing=[], extra={[number]}'
                        )
                    if seen[number]:
                        raise FormatError('Duplicate IDX block references')
                    seen[number] = 1
                    indexed_count += 1

                    f.seek(entry.lgh_offset + 16)
                    tag_bytes = f.read(40)
                    lgh_header_reads += 1
                    if len(tag_bytes) != 40:
                        raise FormatError(
                            f'Truncated LGH tag header at block {entry.block_number}'
                        )
                    tag_bytes = tag_bytes.split(b'\0', 1)[0]
                    try:
                        tag = tag_bytes.decode(self.encoding)
                    except UnicodeDecodeError as exc:
                        raise FormatError(
                            f'Cannot decode LGH tag at block {entry.block_number}'
                        ) from exc
                    if tag != entry.tag:
                        raise FormatError(
                            f'IDX/LGH tag mismatch at block {entry.block_number}: '
                            f'{entry.tag!r} vs {tag!r}'
                        )
                lgh_header_check_seconds += time.perf_counter() - check_started

        coverage_started = time.perf_counter()
        if indexed_count != count - 1:
            missing = []
            for number in range(2, count + 1):
                if not seen[number]:
                    missing.append(number)
                    if len(missing) == 10:
                        break
            raise FormatError(
                f'IDX/LGH coverage mismatch; missing={missing}, extra=[]'
            )
        coverage_seconds = time.perf_counter() - coverage_started

        self.last_verify_diagnostics = {
            'idx_pages_parsed': pages_parsed,
            'idx_entries_checked': indexed_count,
            'idx_parse_seconds': idx_parse_seconds,
            'idx_lgh_header_check_seconds': lgh_header_check_seconds,
            'idx_coverage_check_seconds': coverage_seconds,
            'idx_verify_seconds': time.perf_counter() - started,
            'lgh_header_reads': lgh_header_reads,
            'lgh_header_bytes_requested': lgh_header_reads * 40,
        }
        # Preserve the original public return shape for existing callers.
        return {
            'lgh_blocks': count,
            'indexed_blocks': indexed_count,
            'index_pages': idx_size // 1024 - 1,
        }

    def _tree_page(self, file, page_id: int, total_pages: int):
        """Decode one IDX page on demand (observed 1024-byte layout)."""
        if not 2 <= page_id <= total_pages:
            raise FormatError(f'IDX tree page out of bounds: {page_id}')
        file.seek((page_id - 1) * 1024)
        raw = file.read(1024)
        if len(raw) != 1024:
            raise FormatError(f'Truncated IDX tree page {page_id}')
        kind, count, next_id, prev_id = struct.unpack_from('<HHII', raw)
        if kind not in (0, 1) or count > (1024 - 12) // 5:
            raise FormatError(f'Unsupported IDX tree page at {page_id}')
        numbers = struct.unpack_from(f'<{count}I', raw, 12) if count else ()
        cursor = 12 + count * 4
        keys = []
        for _ in range(count):
            end = raw.find(b'\0', cursor)
            if end == -1:
                raise FormatError(f'Unterminated IDX tree key at {page_id}')
            try:
                keys.append(raw[cursor:end].decode(self.encoding))
            except UnicodeDecodeError as exc:
                raise FormatError(f'Invalid IDX tree key at {page_id}') from exc
            cursor = end + 1
        if raw[cursor:].strip(b'\0'):
            raise FormatError(f'Unexpected IDX tree suffix at {page_id}')
        if keys != sorted(keys):
            raise FormatError(f'Unsorted IDX tree keys at {page_id}')
        return kind, next_id, prev_id, tuple(zip(keys, numbers, strict=True))

    def _tree_blocks_for_tag(self, tag: str) -> list[int]:
        """Experimental tree traversal; scans only path and matching leaf span.

        Validation of all IDX/LGH blocks remains the responsibility of
        verify_lgh(). This query validates only the nodes it touches.
        """
        size = self.filename.stat().st_size
        if size < 2048 or size % 1024:
            raise FormatError(f'Unsupported IDX size: {size}')
        total_pages = size // 1024
        with self.filename.open('rb') as file:
            header = file.read(1024)
            if header[:4] != b'\0' * 4 or header[8:12] != bytes.fromhex('000400fc'):
                raise FormatError('Unsupported IDX tree header signature')
            if header[512:].strip(b'\0'):
                raise FormatError('Unsupported IDX tree header padding')
            declared_pages = struct.unpack_from('<I', header, 4)[0]
            root = struct.unpack_from('<I', header, 0x20)[0]
            if declared_pages != total_pages + 1:
                raise FormatError('IDX header page count mismatch')
            if not 2 <= root <= total_pages:
                raise FormatError('IDX root out of bounds')
            current = root
            visited: set[int] = set()
            while True:
                if current in visited:
                    raise FormatError('IDX tree cycle detected')
                visited.add(current)
                kind, next_id, _prev_id, entries = self._tree_page(file, current, total_pages)
                keys = [key for key, _ in entries]
                pos = bisect_left(keys, tag)
                if current == root and kind != 0:
                    raise FormatError('IDX root does not point to internal page')
                if kind == 1:
                    break
                if pos == len(entries):
                    return []
                current = entries[pos][1]
            result: list[int] = []
            while True:
                for key, block in entries:
                    if key == tag:
                        if block == 0:
                            raise FormatError('Zero LGH block reference for requested tag')
                        result.append(block)
                    elif key > tag:
                        return sorted(result)
                if next_id == 0:
                    return sorted(result)
                if next_id in visited:
                    raise FormatError('IDX leaf chain cycle detected')
                visited.add(next_id)
                kind, next_id, _prev_id, entries = self._tree_page(file, next_id, total_pages)
                if kind != 1:
                    raise FormatError('IDX leaf link points to non-leaf page')

    def blocks_for_tag(self, tag: str, *, method: str = 'scan') -> list[int]:
        """Return LGH block numbers using 'scan' (default) or experimental 'tree'."""
        if method == 'scan':
            return sorted(e.block_number for e in self.entries() if e.tag == tag)
        if method == 'tree':
            return self._tree_blocks_for_tag(tag)
        raise ValueError("method must be 'scan' or 'tree'")

    def records_for_tag(self, lgh_file: str | Path, tag: str, *, verify: bool = True, validate_types: bool = False, method: str = 'scan') -> Iterator[Record]:
        """Read a Tag using IDX block references rather than scanning the whole LGH.

        Verification defaults to checking complete IDX/LGH coverage first;
        disable only when the pair was already verified and is unchanged.
        Results follow LGH physical block order, not sorted timestamp order.
        Pass validate_types=True to apply the same strict BOOL/INT checks as
        LGHReader(validate_types=True).
        """
        if method not in ('scan', 'tree'):
            raise ValueError("method must be 'scan' or 'tree'")
        lgh_file = Path(lgh_file)
        if verify:
            self.verify_lgh(lgh_file)
        source_file = str(lgh_file)
        with lgh_file.open('rb') as f:
            for number in self.blocks_for_tag(tag, method=method):
                block_offset = (number - 1) * BLOCK_SIZE
                f.seek(block_offset)
                chunk = f.read(BLOCK_SIZE)
                if len(chunk) != BLOCK_SIZE:
                    raise FormatError(f'Truncated LGH block at {block_offset}')
                opcode = chunk[:4]
                if opcode not in OPCODES:
                    raise FormatError(f'Indexed block {number} for {tag!r} is not a numeric data block')
                count = int.from_bytes(chunk[4:8], 'little')
                if not 1 <= count <= MAX_ENTRIES:
                    raise FormatError(f'Invalid LGH count {count} at block {number}')
                try:
                    block_tag = chunk[16:56].split(b'\0', 1)[0].decode(self.encoding)
                except UnicodeDecodeError as exc:
                    raise FormatError(f'Invalid LGH tag at block {number}') from exc
                if block_tag != tag:
                    raise FormatError(f'IDX lookup mismatch at LGH block {number}')
                for index in range(count):
                    start = HEADER_SIZE + index * ENTRY_SIZE
                    ticks, value = _ENTRY_VALUE.unpack_from(chunk, start)
                    bits = _VALUE_BITS.unpack_from(chunk, start + 8)[0]
                    if ticks > FILETIME_MAX_TICKS:  # unpacked as unsigned uint64
                        raise FormatError(f'FILETIME outside Python datetime range: {ticks}')
                    rec = Record(tag, OPCODES[opcode], value, ticks, source_file,
                                 block_offset, block_offset+start, index, bits)
                    if validate_types:
                        _ = rec.logical_value
                    yield rec


def inspect_idx_topology(idx_file: str | Path) -> dict:
    """Validate the observed multi-level IDX tree and linked-leaf topology.

    The diagnostic follows the root pointer in the IDX header. Kind-0 entries
    are treated as child logical-page references; kind-1 pages are leaves whose
    ``aux`` and ``node_id`` fields form the previous/next leaf chain.

    This is an evidence-based diagnostic for the currently observed 1024-byte
    IDX page layout, not a claim that every InTouch IDX generation uses it.
    """
    reader = IDXReader(idx_file)
    pages = list(reader.pages())
    by_id = {page.page_number // 2 + 1: page for page in pages}
    path = Path(idx_file)
    raw_size = path.stat().st_size
    total_logical_pages = raw_size // 1024
    findings: list[str] = []

    with path.open("rb") as file:
        header = file.read(1024)
    if len(header) != 1024:
        findings.append("truncated IDX header")
        declared_pages = root_id = 0
    else:
        declared_pages = struct.unpack_from("<I", header, 4)[0]
        root_id = struct.unpack_from("<I", header, 0x20)[0]
        if header[:4] != b"\0" * 4 or header[8:12] != bytes.fromhex("000400fc"):
            findings.append("unsupported IDX header signature")
        if header[512:].strip(b"\0"):
            findings.append("non-zero IDX header padding")
        if declared_pages != total_logical_pages + 1:
            findings.append(
                f"header page count {declared_pages}, expected {total_logical_pages + 1}"
            )
        if root_id not in by_id:
            findings.append(f"root page {root_id} does not exist")

    if not by_id or root_id not in by_id:
        return {
            "matches_observed_layout": False,
            "issues": findings,
            "root_logical_id": root_id,
            "root_count": int(root_id in by_id and by_id[root_id].kind == 0),
            "internal_node_count": sum(p.kind == 0 for p in pages),
            "leaf_count": sum(p.kind == 1 for p in pages),
            "page_count": len(pages),
            "reachable_page_count": 0,
            "unreachable_page_ids": sorted(by_id),
        }

    root = by_id[root_id]
    if root.kind != 0:
        findings.append(f"root page {root_id} is not an internal node")

    visited: set[int] = set()
    active: set[int] = set()
    parent_counts: dict[int, int] = {}
    leaf_order: list[int] = []
    depth_counts: dict[int, int] = {}
    edge_count = 0
    boundary_mismatches: list[tuple[int, str, int, str | None]] = []

    def walk(page_id: int, depth: int) -> tuple[str | None, str | None]:
        nonlocal edge_count
        if page_id in active:
            findings.append(f"cycle detected at page {page_id}")
            return None, None
        if page_id in visited:
            findings.append(f"page {page_id} is referenced more than once")
            return None, None
        page = by_id.get(page_id)
        if page is None:
            findings.append(f"child page {page_id} does not exist")
            return None, None

        visited.add(page_id)
        active.add(page_id)
        depth_counts[depth] = depth_counts.get(depth, 0) + 1
        keys = [entry.tag for entry in page.entries]
        if keys != sorted(keys):
            findings.append(f"page {page_id} keys are not ordered")

        if page.kind == 1:
            leaf_order.append(page_id)
            active.remove(page_id)
            return (keys[0], keys[-1]) if keys else (None, None)

        if page.kind != 0:
            findings.append(f"page {page_id} has unknown kind {page.kind}")
            active.remove(page_id)
            return None, None
        if not page.entries:
            findings.append(f"internal page {page_id} has no child entries")
            active.remove(page_id)
            return None, None

        first_key = None
        previous_child_max = None
        for entry in page.entries:
            child_id = entry.block_number
            edge_count += 1
            parent_counts[child_id] = parent_counts.get(child_id, 0) + 1
            child = by_id.get(child_id)
            if child is None:
                findings.append(f"internal page {page_id} references non-leaf or missing child {child_id}")
                continue
            child_min, child_max = walk(child_id, depth + 1)
            if child.kind not in (0, 1):
                continue
            if child_max is not None and child_max != entry.tag:
                boundary_mismatches.append((page_id, entry.tag, child_id, child_max))
            if previous_child_max is not None and child_min is not None and child_min < previous_child_max:
                findings.append(f"overlapping/out-of-order child key ranges at page {page_id}")
            if first_key is None:
                first_key = child_min
            previous_child_max = child_max

        active.remove(page_id)
        return first_key, previous_child_max

    walk(root_id, 0)
    for parent, expected_key, child_id, actual_key in boundary_mismatches:
        findings.append(
            f"page {parent} boundary {expected_key!r} does not match "
            f"child {child_id} maximum key {actual_key!r}"
        )

    duplicate_parents = sorted(page_id for page_id, count in parent_counts.items() if count > 1)
    if duplicate_parents:
        findings.append(f"pages have multiple parents: {duplicate_parents[:20]}")

    unreachable = sorted(set(by_id) - visited)
    if unreachable:
        findings.append(f"unreachable pages: {unreachable[:20]}")

    # The depth-first traversal of a valid ordered tree yields the leaf-chain order.
    leaf_link_issues = []
    for index, page_id in enumerate(leaf_order):
        page = by_id[page_id]
        expected_prev = leaf_order[index - 1] if index else 0
        expected_next = leaf_order[index + 1] if index + 1 < len(leaf_order) else 0
        if page.aux != expected_prev:
            leaf_link_issues.append(
                f"leaf {page_id} prev={page.aux}, expected {expected_prev}"
            )
        if page.node_id != expected_next:
            leaf_link_issues.append(
                f"leaf {page_id} next={page.node_id}, expected {expected_next}"
            )
    findings.extend(leaf_link_issues)

    leaf_boundaries = [
        (by_id[page_id].entries[0].tag, by_id[page_id].entries[-1].tag)
        for page_id in leaf_order if by_id[page_id].entries
    ]
    for left, right in zip(leaf_boundaries, leaf_boundaries[1:]):
        if left[1] > right[0]:
            findings.append("leaf key ranges are not globally ordered")
            break

    return {
        "matches_observed_layout": not findings,
        "issues": findings,
        "root_physical_page": root.page_number,
        "root_logical_id": root_id,
        "root_count": int(root.kind == 0),
        "internal_node_count": sum(p.kind == 0 for p in pages),
        "leaf_count": sum(p.kind == 1 for p in pages),
        "page_count": len(pages),
        "reachable_page_count": len(visited),
        "unreachable_page_ids": unreachable,
        "max_depth": max(depth_counts, default=0),
        "nodes_by_depth": dict(sorted(depth_counts.items())),
        "edge_count": edge_count,
        "multiple_parent_page_ids": duplicate_parents,
        "leaf_link_issue_count": len(leaf_link_issues),
        "leaf_logical_ids_in_key_order": leaf_order,
        "logical_to_physical_formula": "physical_page=2*(logical_id-1)",
    }

