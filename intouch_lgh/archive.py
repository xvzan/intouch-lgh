"""Unified, read-only LGH/IDX archive access for the v1 API prototype.

This first alpha uses the v0.20 format decoders as an internal reference engine.
It adds one lifetime-managed Archive, mmap-backed block access, multi-Tag query
planning, and a compact optional NumPy batch representation.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from collections.abc import Mapping
from bisect import bisect_left
from pathlib import Path
from typing import Iterable, Iterator
import mmap
import os
import struct
import time
from array import array

from .reader import (
    BLOCK_SIZE, HEADER_SIZE, ENTRY_SIZE, MAX_ENTRIES, OPCODES,
    SYSTEM_OPCODES, MARKER_OPCODE, FILETIME_UNIX_EPOCH,
    FormatError, Record, _ENTRY_VALUE, _VALUE_BITS,
)
from .index import IDXReader
from .query import to_filetime
from .model import BlockRef, ValidationReport, RawBatch, RawBlock

IDX_PAGE_SIZE = 1024
_IDX_HEADER_SIGNATURE = bytes.fromhex("0000000000000000000400fc")
_TYPE_TO_CODE = {"BOOL": 1, "INT": 2, "REAL": 3}
_CODE_TO_TYPE = {v: k for k, v in _TYPE_TO_CODE.items()}


class ArchiveChangedError(RuntimeError):
    """An opened archive file changed or was replaced during its lifetime."""


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, st: os.stat_result) -> "FileIdentity":
        return cls(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    def same_file(self, other: "FileIdentity") -> bool:
        """Compare stable file identity fields, independent of stat timestamps."""
        return self.device == other.device and self.inode == other.inode




class Archive:
    """A read-only LGH/IDX pair with a shared lifetime and query interface.

    ``validate='full'`` performs a full pair audit once at open. ``validate='none'``
    skips whole-file coverage validation, but every queried IDX page and LGH
    block is still structurally checked. Files are memory-mapped read-only.
    """

    def __init__(self, lgh_path: str | Path, idx_path: str | Path, *,
                 validate: str = "full", encoding: str = "utf-8"):
        if validate not in {"full", "none"}:
            raise ValueError("validate must be 'full' or 'none'")
        self.lgh_path = Path(lgh_path).resolve()
        self.idx_path = Path(idx_path).resolve()
        self.encoding = encoding
        self._closed = True
        self._lgh_file = None
        self._idx_file = None
        self._lgh_map = None
        self._idx_map = None
        self._lgh_identity = None
        self._idx_identity = None
        self._lgh_path_identity = None
        self._idx_path_identity = None
        self._idx_reader = IDXReader(self.idx_path, encoding=encoding)
        self._tag_counts: dict[str, int] | None = None
        self.validation_report: ValidationReport

        self._lgh_file = self.lgh_path.open("rb")
        self._idx_file = self.idx_path.open("rb")
        try:
            lgh_stat = os.fstat(self._lgh_file.fileno())
            idx_stat = os.fstat(self._idx_file.fileno())
            if lgh_stat.st_size < BLOCK_SIZE or lgh_stat.st_size % BLOCK_SIZE:
                raise FormatError(f"LGH size is not a multiple of {BLOCK_SIZE}")
            if idx_stat.st_size < 2 * IDX_PAGE_SIZE or idx_stat.st_size % IDX_PAGE_SIZE:
                raise FormatError(f"IDX size is not a multiple of {IDX_PAGE_SIZE}")
            self._lgh_identity = FileIdentity.from_stat(lgh_stat)
            self._idx_identity = FileIdentity.from_stat(idx_stat)

            # Keep path-stat baselines separate from descriptor-stat baselines.
            # On Windows, os.stat()/Path.stat() and os.fstat() can expose
            # timestamp metadata with different semantics/resolution. Comparing
            # a path stat directly to an fstat snapshot can therefore reject an
            # unchanged file. Each source is now compared with its own baseline;
            # device/inode additionally ensure the path still names the opened
            # file descriptor.
            self._lgh_path_identity = FileIdentity.from_stat(self.lgh_path.stat())
            self._idx_path_identity = FileIdentity.from_stat(self.idx_path.stat())
            if (not self._lgh_identity.same_file(self._lgh_path_identity) or
                    not self._idx_identity.same_file(self._idx_path_identity)):
                raise ArchiveChangedError(
                    "LGH/IDX path was replaced while the archive was opening"
                )

            self._lgh_map = mmap.mmap(self._lgh_file.fileno(), 0, access=mmap.ACCESS_READ)
            self._idx_map = mmap.mmap(self._idx_file.fileno(), 0, access=mmap.ACCESS_READ)
            self._closed = False

            if validate == "full":
                self.validation_report = self.validate_full()
            else:
                self.validation_report = ValidationReport(
                    level="none", lgh_size=lgh_stat.st_size, idx_size=idx_stat.st_size
                )
        except Exception:
            self.close()
            raise

    @classmethod
    def open(cls, lgh_path: str | Path, idx_path: str | Path, *,
             validate: str = "full", encoding: str = "utf-8") -> "Archive":
        """Open an immutable LGH/IDX pair. Use as a context manager."""
        return cls(lgh_path, idx_path, validate=validate, encoding=encoding)

    def __enter__(self) -> "Archive":
        self._check_open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        # Also clean up partially initialized instances when __init__ fails.
        # Each resource is optional until opening and mmap creation complete.
        for attr in ("_lgh_map", "_idx_map", "_lgh_file", "_idx_file"):
            resource = getattr(self, attr, None)
            if resource is not None:
                try:
                    resource.close()
                finally:
                    setattr(self, attr, None)
        self._closed = True

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("Archive is closed")

    def _ensure_unchanged(self) -> None:
        self._check_open()
        current_lgh_fd = FileIdentity.from_stat(os.fstat(self._lgh_file.fileno()))
        current_idx_fd = FileIdentity.from_stat(os.fstat(self._idx_file.fileno()))
        try:
            current_lgh_path = FileIdentity.from_stat(self.lgh_path.stat())
            current_idx_path = FileIdentity.from_stat(self.idx_path.stat())
        except FileNotFoundError as exc:
            raise ArchiveChangedError("An archive file was removed or replaced") from exc

        # Compare fstat with its fstat baseline and path.stat with its path.stat
        # baseline. Do not compare timestamps across the two APIs: Windows may
        # report their metadata timestamps differently. The file ID comparison
        # still detects path replacement relative to the open descriptors.
        if (current_lgh_fd != self._lgh_identity or
                current_idx_fd != self._idx_identity or
                current_lgh_path != self._lgh_path_identity or
                current_idx_path != self._idx_path_identity or
                not current_lgh_fd.same_file(current_lgh_path) or
                not current_idx_fd.same_file(current_idx_path)):
            raise ArchiveChangedError("LGH/IDX file changed after Archive.open()")

    @property
    def tag_counts(self) -> Mapping[str, int]:
        """Per-Tag numeric record counts collected during full validation.

        The mapping is available only after ``validate="full"`` (or an
        explicit successful call to :meth:`validate_full`). Counts are
        derived from validated LGH block headers; no individual observation
        records are decoded to build this catalogue.
        """
        self._ensure_unchanged()
        if self._tag_counts is None:
            raise RuntimeError(
                "Tag record counts are available only after full validation"
            )
        return MappingProxyType(self._tag_counts)

    def validate_full(self) -> ValidationReport:
        """Run a full LGH/IDX audit, retain Tag counts and phase diagnostics."""
        wall_started = time.perf_counter()
        thread_clock = getattr(time, "thread_time", time.process_time)
        thread_cpu_started = thread_clock()
        process_cpu_started = time.process_time()
        self._ensure_unchanged()

        # Validate IDX references and collect the Tag catalogue from indexed
        # LGH block headers in one pass. Only the fixed-size block headers are
        # needed for full structural validation; observation payloads are not
        # decoded here.
        index_report, tag_counts, numeric_records, numeric_blocks, index_diagnostics = (
            self._validate_idx_and_scan_headers()
        )
        lgh_block_scan_seconds = index_diagnostics["lgh_block_scan_seconds"]

        final_check_started = time.perf_counter()
        self._ensure_unchanged()
        final_identity_check_seconds = time.perf_counter() - final_check_started
        # Publish the catalogue only after every validation check has passed.
        self._tag_counts = tag_counts
        validation_wall_seconds = time.perf_counter() - wall_started
        validation_thread_cpu_seconds = thread_clock() - thread_cpu_started
        validation_process_cpu_seconds = time.process_time() - process_cpu_started
        self.validation_report = ValidationReport(
            level="full",
            lgh_size=len(self._lgh_map),
            idx_size=len(self._idx_map),
            numeric_records=numeric_records,
            numeric_blocks=numeric_blocks,
            indexed_blocks=index_report.get("indexed_blocks"),
            index_pages=index_report.get("index_pages"),
            idx_parse_seconds=float(index_diagnostics.get("idx_parse_seconds", 0.0)),
            idx_lgh_header_check_seconds=float(
                index_diagnostics.get("idx_lgh_header_check_seconds", 0.0)
            ),
            idx_coverage_check_seconds=float(
                index_diagnostics.get("idx_coverage_check_seconds", 0.0)
            ),
            final_identity_check_seconds=final_identity_check_seconds,
            idx_verify_seconds=float(index_diagnostics.get("idx_verify_seconds", 0.0)),
            lgh_block_scan_seconds=(
                float(lgh_block_scan_seconds)
                if lgh_block_scan_seconds is not None else None
            ),
            validation_wall_seconds=validation_wall_seconds,
            validation_thread_cpu_seconds=validation_thread_cpu_seconds,
            validation_process_cpu_seconds=validation_process_cpu_seconds,
            idx_pages_parsed=int(index_diagnostics.get("idx_pages_parsed", 0)),
            idx_entries_checked=int(index_diagnostics.get("idx_entries_checked", 0)),
            lgh_header_reads=int(index_diagnostics.get("lgh_header_reads", 0)),
            lgh_header_bytes_requested=int(
                index_diagnostics.get("lgh_header_bytes_requested", 0)
            ),
            lgh_block_header_bytes_read=int(
                index_diagnostics.get("lgh_block_header_bytes_read", 0)
            ),
            lgh_logical_bytes_scanned=int(
                index_diagnostics.get("lgh_logical_bytes_scanned", len(self._lgh_map))
            ),
            lgh_blocks_scanned=int(
                index_diagnostics.get("lgh_blocks_scanned", len(self._lgh_map) // BLOCK_SIZE)
            ),
        )
        return self.validation_report

    def _validate_idx_and_scan_headers(self):
        """Cross-check IDX entries and validate LGH block headers in one pass.

        The IDX is Tag-ordered, so this walks its leaf entries and performs
        fixed-size mmap header reads for the referenced LGH blocks. It avoids
        both the old per-entry seek/read calls and the subsequent full 512-byte
        sequential LGH scan. Observation payloads are not decoded by full
        archive validation and therefore do not need to be read.

        IDX errors and IDX/LGH Tag mismatches are raised while traversing the
        index. LGH block-format errors are deferred until coverage has passed,
        preserving the former verify_lgh() -> _scan_blocks() error precedence.
        """
        started = time.perf_counter()
        total_blocks = len(self._lgh_map) // BLOCK_SIZE
        seen = bytearray(total_blocks + 1)
        tag_counts: dict[str, int] = {}
        numeric_records = 0
        numeric_blocks = 0
        block_errors: dict[int, FormatError] = {}
        pages_parsed = 0
        indexed_count = 0
        idx_parse_seconds = 0.0
        idx_lgh_header_check_seconds = 0.0
        lgh_header_reads = 0
        lgh_header_bytes_requested = 0
        lgh_block_header_bytes_read = 0
        encoded_tags: dict[str, bytes] = {}
        page_iter = iter(self._idx_reader.pages())

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

            page_check_started = time.perf_counter()
            for entry in page.entries:
                number = entry.block_number
                if number == 0:
                    if entry.tag != "\x7f":
                        raise FormatError(f"Unexpected zero reference: {entry}")
                    continue
                if number < 2 or number > total_blocks:
                    raise FormatError(
                        f"IDX/LGH coverage mismatch; missing=[], extra={[number]}"
                    )
                if seen[number]:
                    raise FormatError("Duplicate IDX block references")
                seen[number] = 1
                indexed_count += 1

                offset = (number - 1) * BLOCK_SIZE
                header = self._lgh_map[offset:offset + HEADER_SIZE]
                lgh_header_reads += 1
                lgh_header_bytes_requested += 40
                lgh_block_header_bytes_read += len(header)

                raw_tag = header[16:56].split(b"\0", 1)[0]
                if self.encoding.lower().replace("_", "-") in {"utf-8", "utf8"}:
                    expected_tag_bytes = encoded_tags.get(entry.tag)
                    if expected_tag_bytes is None:
                        expected_tag_bytes = entry.tag.encode("utf-8")
                        encoded_tags[entry.tag] = expected_tag_bytes
                    tags_match = raw_tag == expected_tag_bytes
                    actual_tag = None
                else:
                    try:
                        actual_tag = raw_tag.decode(self.encoding)
                    except UnicodeDecodeError as exc:
                        raise FormatError(
                            f"Cannot decode LGH tag at block {number}"
                        ) from exc
                    tags_match = actual_tag == entry.tag
                if not tags_match:
                    if actual_tag is None:
                        try:
                            actual_tag = raw_tag.decode(self.encoding)
                        except UnicodeDecodeError as exc:
                            raise FormatError(
                                f"Cannot decode LGH tag at block {number}"
                            ) from exc
                    raise FormatError(
                        f"IDX/LGH tag mismatch at block {number}: "
                        f"{entry.tag!r} vs {actual_tag!r}"
                    )

                if header[:4] == b"\0" * 4:
                    lgh_block_header_bytes_read += BLOCK_SIZE - HEADER_SIZE
                try:
                    count = self._validate_indexed_lgh_block_header(
                        number, header, raw_tag
                    )
                except FormatError as exc:
                    block_errors[number] = exc
                else:
                    if count is not None:
                        numeric_blocks += 1
                        numeric_records += count
                        tag_counts[entry.tag] = tag_counts.get(entry.tag, 0) + count
            idx_lgh_header_check_seconds += time.perf_counter() - page_check_started

        coverage_started = time.perf_counter()
        if indexed_count != total_blocks - 1:
            missing = []
            for number in range(2, total_blocks + 1):
                if not seen[number]:
                    missing.append(number)
                    if len(missing) == 10:
                        break
            raise FormatError(
                f"IDX/LGH coverage mismatch; missing={missing}, extra=[]"
            )
        idx_coverage_check_seconds = time.perf_counter() - coverage_started

        # Block 1 is the LGH archive header and is intentionally not indexed.
        first_header = self._lgh_map[:HEADER_SIZE]
        lgh_block_header_bytes_read += len(first_header)
        if not (first_header[:4] == b"\0" * 4
                and first_header[8:12] == bytes.fromhex("000400fc")):
            lgh_block_header_bytes_read += BLOCK_SIZE - HEADER_SIZE
            block = self._parse_lgh_block(1)
            if block is not None:
                numeric_blocks += 1
                numeric_records += block.count
                tag_counts[block.tag] = tag_counts.get(block.tag, 0) + block.count

        if block_errors:
            raise block_errors[min(block_errors)]

        index_report = {
            "lgh_blocks": total_blocks,
            "indexed_blocks": indexed_count,
            "index_pages": len(self._idx_map) // 1024 - 1,
        }
        diagnostics = {
            "idx_pages_parsed": pages_parsed,
            "idx_entries_checked": indexed_count,
            "idx_parse_seconds": idx_parse_seconds,
            # Header cross-check and block-header parsing are fused in this
            # path; this field records their inclusive per-page work duration.
            "idx_lgh_header_check_seconds": idx_lgh_header_check_seconds,
            "idx_coverage_check_seconds": idx_coverage_check_seconds,
            "idx_verify_seconds": time.perf_counter() - started,
            "lgh_header_reads": lgh_header_reads,
            "lgh_header_bytes_requested": lgh_header_bytes_requested,
            "lgh_block_header_bytes_read": lgh_block_header_bytes_read,
            # A separate sequential LGH scan has been eliminated.
            "lgh_block_scan_seconds": 0.0,
            "lgh_logical_bytes_scanned": len(self._lgh_map),
            "lgh_blocks_scanned": total_blocks,
        }
        self._idx_reader.last_verify_diagnostics = diagnostics
        return index_report, tag_counts, numeric_records, numeric_blocks, diagnostics

    def _validate_indexed_lgh_block_header(
        self, block_number: int, header: bytes | memoryview,
        raw_tag: bytes,
    ) -> int | None:
        """Validate one indexed LGH block header and return its record count.

        ``None`` identifies a valid metadata/marker/blank block. The caller
        has already cross-checked the Tag, so this avoids decoding it a second
        time and avoids constructing a temporary BlockRef during validation.
        """
        offset = (block_number - 1) * BLOCK_SIZE
        if len(header) != HEADER_SIZE:
            raise FormatError(f"Truncated LGH block at 0x{offset:x}")
        opcode = header[:4]

        if opcode in SYSTEM_OPCODES:
            expected = SYSTEM_OPCODES[opcode].encode("ascii")
            if raw_tag != expected or struct.unpack_from("<I", header, 4)[0] != 1:
                raise FormatError(f"Unrecognized system block at 0x{offset:x}")
            return None
        if opcode == MARKER_OPCODE and header[16:17] == b"$" and header[4:8] == b"\0" * 4:
            return None
        if opcode == b"\0" * 4:
            # A zero header is a blank block only when its payload is also zero.
            tail = self._lgh_map[offset + HEADER_SIZE:offset + BLOCK_SIZE]
            if (header == bytes(HEADER_SIZE)
                    and tail == bytes(BLOCK_SIZE - HEADER_SIZE) and not raw_tag):
                return None
            raise FormatError(f"Unknown LGH block type {opcode.hex()} at 0x{offset:x}")
        if opcode not in OPCODES:
            raise FormatError(f"Unknown LGH block type {opcode.hex()} at 0x{offset:x}")
        count = struct.unpack_from("<I", header, 4)[0]
        if not 1 <= count <= MAX_ENTRIES:
            raise FormatError(f"Invalid LGH record count {count} at 0x{offset:x}")
        if not raw_tag:
            raise FormatError(f"Missing Tag at 0x{offset:x}")
        return count

    def _read_idx_page(self, logical_page_id: int):
        total_pages = len(self._idx_map) // IDX_PAGE_SIZE
        if not 2 <= logical_page_id <= total_pages:
            raise FormatError(f"IDX page out of bounds: {logical_page_id}")
        start = (logical_page_id - 1) * IDX_PAGE_SIZE
        raw = self._idx_map[start:start + IDX_PAGE_SIZE]
        if len(raw) != IDX_PAGE_SIZE:
            raise FormatError(f"Truncated IDX page {logical_page_id}")
        kind, count, next_id, prev_id = struct.unpack_from("<HHII", raw, 0)
        if kind not in (0, 1) or count > (IDX_PAGE_SIZE - 12) // 5:
            raise FormatError(f"Unsupported IDX page at {logical_page_id}")
        numbers = struct.unpack_from(f"<{count}I", raw, 12) if count else ()
        cursor = 12 + count * 4
        keys = []
        for _ in range(count):
            end = raw.find(b"\0", cursor)
            if end < 0:
                raise FormatError(f"Unterminated IDX key at page {logical_page_id}")
            try:
                keys.append(raw[cursor:end].decode(self.encoding))
            except UnicodeDecodeError as exc:
                raise FormatError(f"Invalid IDX key at page {logical_page_id}") from exc
            cursor = end + 1
        if raw[cursor:].strip(b"\0"):
            raise FormatError(f"Unexpected IDX payload at page {logical_page_id}")
        if keys != sorted(keys):
            raise FormatError(f"Unsorted IDX keys at page {logical_page_id}")
        return kind, next_id, prev_id, tuple(zip(keys, numbers, strict=True))

    def _blocks_for_tag_tree(self, tag: str) -> list[int]:
        self._ensure_unchanged()
        header = self._idx_map[:IDX_PAGE_SIZE]
        if header[:4] != b"\0" * 4 or header[8:12] != bytes.fromhex("000400fc"):
            raise FormatError("Unsupported IDX header signature")
        if header[BLOCK_SIZE:].strip(b"\0"):
            raise FormatError("Unexpected IDX header padding")
        total_pages = len(self._idx_map) // IDX_PAGE_SIZE
        declared_pages = struct.unpack_from("<I", header, 4)[0]
        root = struct.unpack_from("<I", header, 0x20)[0]
        if declared_pages != total_pages + 1:
            raise FormatError("IDX header page count mismatch")
        if not 2 <= root <= total_pages:
            raise FormatError("IDX root out of bounds")

        current = root
        visited: set[int] = set()
        while True:
            if current in visited:
                raise FormatError("IDX tree cycle detected")
            visited.add(current)
            kind, next_id, prev_id, entries = self._read_idx_page(current)
            keys = [key for key, _ in entries]
            pos = bisect_left(keys, tag)
            if kind == 1:
                break
            if pos == len(entries):
                return []
            current = entries[pos][1]

        result: list[int] = []
        current_leaf = current
        expected_prev = None
        while True:
            if current_leaf in visited and current_leaf != current:
                raise FormatError("IDX leaf chain cycle detected")
            if current_leaf != current:
                visited.add(current_leaf)
            kind, next_id, prev_id, entries = self._read_idx_page(current_leaf)
            if kind != 1:
                raise FormatError("IDX leaf link points to non-leaf page")
            if expected_prev is not None and prev_id != expected_prev:
                raise FormatError("IDX leaf previous pointer mismatch")
            stop = False
            for key, block_number in entries:
                if key == tag:
                    if block_number == 0:
                        raise FormatError("Zero LGH block reference for requested Tag")
                    result.append(block_number)
                elif key > tag:
                    stop = True
                    break
            if stop or next_id == 0:
                return sorted(result)
            if next_id in visited:
                raise FormatError("IDX leaf chain cycle detected")
            expected_prev = current_leaf
            current_leaf = next_id

    def _parse_lgh_block(self, block_number: int, *, expected_tag: str | None = None) -> BlockRef | None:
        if not 1 <= block_number <= len(self._lgh_map) // BLOCK_SIZE:
            raise FormatError(f"LGH block out of bounds: {block_number}")
        offset = (block_number - 1) * BLOCK_SIZE
        raw = self._lgh_map[offset:offset + BLOCK_SIZE]
        if len(raw) != BLOCK_SIZE:
            raise FormatError(f"Truncated LGH block at 0x{offset:x}")
        opcode = raw[:4]
        # IDX leaves also index system/marker blocks. They are legitimate
        # archive metadata, not numeric observations, and are omitted from
        # Record/NumPy result streams.
        if opcode in SYSTEM_OPCODES:
            actual = raw[16:56].split(b"\0", 1)[0]
            if actual != SYSTEM_OPCODES[opcode].encode("ascii") or struct.unpack_from("<I", raw, 4)[0] != 1:
                raise FormatError(f"Unrecognized system block at 0x{offset:x}")
            if expected_tag is not None and actual.decode("ascii") != expected_tag:
                raise FormatError(f"IDX/LGH metadata Tag mismatch at block {block_number}")
            return None
        if opcode == MARKER_OPCODE and raw[16:17] == b"$" and raw[4:8] == b"\0" * 4:
            return None
        if raw == bytes(BLOCK_SIZE):
            return None
        if opcode not in OPCODES:
            raise FormatError(f"Indexed block {block_number} has unknown opcode {opcode.hex()}")
        count = struct.unpack_from("<I", raw, 4)[0]
        if not 1 <= count <= MAX_ENTRIES:
            raise FormatError(f"Invalid LGH record count {count} at 0x{offset:x}")
        raw_tag = raw[16:56].split(b"\0", 1)[0]
        if not raw_tag:
            raise FormatError(f"Missing Tag at 0x{offset:x}")
        try:
            tag = raw_tag.decode(self.encoding)
        except UnicodeDecodeError as exc:
            raise FormatError(f"Invalid Tag at 0x{offset:x}") from exc
        if expected_tag is not None and tag != expected_tag:
            raise FormatError(f"IDX/LGH Tag mismatch at block {block_number}")
        return BlockRef(block_number, offset, tag, OPCODES[opcode], count)

    def _scan_blocks(self) -> Iterator[BlockRef]:
        total = len(self._lgh_map) // BLOCK_SIZE
        for block_number in range(1, total + 1):
            offset = (block_number - 1) * BLOCK_SIZE
            raw = self._lgh_map[offset:offset + BLOCK_SIZE]
            if raw == bytes(BLOCK_SIZE):
                continue
            opcode = raw[:4]
            if block_number == 1 and raw[:4] == b"\0" * 4 and raw[8:12] == bytes.fromhex("000400fc"):
                continue
            if opcode in SYSTEM_OPCODES:
                expected = SYSTEM_OPCODES[opcode].encode("ascii")
                if raw[16:56].split(b"\0", 1)[0] != expected or struct.unpack_from("<I", raw, 4)[0] != 1:
                    raise FormatError(f"Unrecognized system block at 0x{offset:x}")
                continue
            if opcode == MARKER_OPCODE and raw[16:17] == b"$" and raw[4:8] == b"\0" * 4:
                continue
            if opcode not in OPCODES:
                raise FormatError(f"Unknown LGH block type {opcode.hex()} at 0x{offset:x}")
            count = struct.unpack_from("<I", raw, 4)[0]
            if not 1 <= count <= MAX_ENTRIES:
                raise FormatError(f"Invalid LGH record count {count} at 0x{offset:x}")
            raw_tag = raw[16:56].split(b"\0", 1)[0]
            if not raw_tag:
                raise FormatError(f"Missing Tag at 0x{offset:x}")
            try:
                tag = raw_tag.decode(self.encoding)
            except UnicodeDecodeError as exc:
                raise FormatError(f"Invalid Tag at 0x{offset:x}") from exc
            yield BlockRef(block_number, offset, tag, OPCODES[opcode], count)

    def _candidate_blocks(self, tags: str | Iterable[str] | None) -> Iterator[BlockRef]:
        if tags is None:
            yield from self._scan_blocks()
            return
        if isinstance(tags, str):
            requested = (tags,)
        else:
            requested = tuple(dict.fromkeys(tags))
        if not requested:
            return
        refs: dict[int, str] = {}
        for tag in requested:
            seen_for_tag: set[int] = set()
            for number in self._blocks_for_tag_tree(tag):
                if number in seen_for_tag:
                    raise FormatError(f"Duplicate IDX reference for Tag {tag!r}: block {number}")
                seen_for_tag.add(number)
                previous = refs.get(number)
                if previous is not None:
                    raise FormatError(
                        f"IDX block {number} is referenced more than once "
                        f"({previous!r}, {tag!r})"
                    )
                refs[number] = tag
        for number in sorted(refs):
            block = self._parse_lgh_block(number, expected_tag=refs[number])
            if block is not None:
                yield block

    def iter_records(self, *, tags: str | Iterable[str] | None = None,
                     start=None, end=None, validate_types: bool = False) -> Iterator[Record]:
        """Yield Record conveniences in LGH physical order; no sorting/deduplication."""
        self._ensure_unchanged()
        lo = to_filetime(start) if start is not None else None
        hi = to_filetime(end) if end is not None else None
        if lo is not None and hi is not None and hi < lo:
            raise ValueError("end must be >= start")
        for block in self._candidate_blocks(tags):
            raw = self._lgh_map[block.block_offset:block.block_offset + BLOCK_SIZE]
            for index in range(block.count):
                pos = HEADER_SIZE + index * ENTRY_SIZE
                ticks, value = _ENTRY_VALUE.unpack_from(raw, pos)
                if lo is not None and ticks < lo or hi is not None and ticks >= hi:
                    continue
                bits = _VALUE_BITS.unpack_from(raw, pos + 8)[0]
                record = Record(
                    block.tag, block.value_type, value, ticks, str(self.lgh_path),
                    block.block_offset, block.block_offset + pos, index, bits,
                )
                if validate_types:
                    _ = record.logical_value
                yield record
        # A source can change after the generator's initial identity check.
        # Check on normal exhaustion without adding a stat() call per record.
        self._ensure_unchanged()

    def iter_batches(self, *, tags: str | Iterable[str] | None = None,
                     start=None, end=None, batch_size: int = 65_536,
                     validate_types: bool = False) -> Iterator[RawBatch]:
        """Yield detached, NumPy-independent batches of original LGH entries.

        Rows remain in LGH physical order. Each batch packs selected original
        16-byte record payloads into one immutable bytes buffer and stores
        Tag/type/provenance once per source block. ``batch_size`` is a strict
        maximum. No sorting, deduplication, interpolation or resampling occurs.
        """
        self._ensure_unchanged()
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        lo = to_filetime(start) if start is not None else None
        hi = to_filetime(end) if end is not None else None
        if lo is not None and hi is not None and hi < lo:
            raise ValueError("end must be >= start")

        entry_parts: list[memoryview] = []
        record_indices = bytearray()
        segments: list[RawBlock] = []
        source_file = str(self.lgh_path)

        def make_batch() -> RawBatch:
            parts = entry_parts
            try:
                # Join source fragments once, then split the interleaved on-disk
                # uint64 pairs using the stdlib array implementation. The final
                # RawBatch owns two compact little-endian column buffers.
                packed_entries = b"".join(parts)
            finally:
                # Release exported mmap views even if joining fails. Clearing
                # the list also makes the outer generator-finally idempotent.
                for part in parts:
                    try:
                        part.release()
                    except ValueError:
                        pass
                parts.clear()
            words = array("Q")
            if words.itemsize != 8:
                raise RuntimeError("This platform does not provide 8-byte array('Q')")
            words.frombytes(packed_entries)
            if len(words) != len(record_indices) * 2:
                raise RuntimeError("Internal packed-entry count mismatch")
            filetime_ticks = words[0::2].tobytes()
            value_bits = words[1::2].tobytes()
            del words, packed_entries
            return RawBatch(
                filetime_ticks=filetime_ticks,
                value_bits=value_bits,
                record_indices=bytes(record_indices),
                blocks=tuple(segments),
                source_file=source_file,
            )

        try:
            for block in self._candidate_blocks(tags):
                if lo is None and hi is None:
                    selected: range | list[int] = range(block.count)
                else:
                    selected = []
                    base = block.block_offset + HEADER_SIZE
                    for index in range(block.count):
                        ticks = struct.unpack_from("<Q", self._lgh_map, base + index * ENTRY_SIZE)[0]
                        if (lo is None or ticks >= lo) and (hi is None or ticks < hi):
                            selected.append(index)

                cursor = 0
                while cursor < len(selected):
                    room = batch_size - len(record_indices)
                    take = min(room, len(selected) - cursor)
                    chunk = selected[cursor:cursor + take]
                    if not chunk:
                        break

                    if validate_types:
                        for index in chunk:
                            bits = struct.unpack_from(
                                "<Q", self._lgh_map,
                                block.block_offset + HEADER_SIZE + index * ENTRY_SIZE + 8,
                            )[0]
                            value = struct.unpack("<d", struct.pack("<Q", bits))[0]
                            record = Record(
                                block.tag, block.value_type, value, 0, source_file,
                                block.block_offset, block.block_offset + HEADER_SIZE + index * ENTRY_SIZE,
                                index, bits,
                            )
                            _ = record.logical_value

                    row_start = len(record_indices)
                    first = chunk[0]
                    last = chunk[-1]
                    contiguous = (last - first + 1) == len(chunk)
                    source_start = block.block_offset + HEADER_SIZE + first * ENTRY_SIZE
                    if contiguous:
                        source_stop = source_start + len(chunk) * ENTRY_SIZE
                        entry_parts.append(memoryview(self._lgh_map)[source_start:source_stop])
                    else:
                        # Keep only contiguous runs as fragments; the final join
                        # performs one copy into the detached immutable RawBatch.
                        run_start = previous = chunk[0]
                        for index in chunk[1:]:
                            if index != previous + 1:
                                run_offset = block.block_offset + HEADER_SIZE + run_start * ENTRY_SIZE
                                run_stop = block.block_offset + HEADER_SIZE + (previous + 1) * ENTRY_SIZE
                                entry_parts.append(memoryview(self._lgh_map)[run_offset:run_stop])
                                run_start = index
                            previous = index
                        run_offset = block.block_offset + HEADER_SIZE + run_start * ENTRY_SIZE
                        run_stop = block.block_offset + HEADER_SIZE + (previous + 1) * ENTRY_SIZE
                        entry_parts.append(memoryview(self._lgh_map)[run_offset:run_stop])
                    record_indices.extend(bytes(chunk))

                    row_stop = len(record_indices)
                    segments.append(RawBlock(
                        block_number=block.block_number,
                        block_offset=block.block_offset,
                        tag=block.tag,
                        value_type=block.value_type,
                        row_start=row_start,
                        row_stop=row_stop,
                    ))
                    cursor += take

                    if len(record_indices) == batch_size:
                        self._ensure_unchanged()
                        yield make_batch()
                        entry_parts = []
                        record_indices = bytearray()
                        segments = []

            self._ensure_unchanged()
            if record_indices:
                yield make_batch()
            # Also check after the final batch has been consumed. The source
            # may have changed while the consumer processed that last batch.
            self._ensure_unchanged()
        finally:
            # A parse error, caller cancellation, or generator.close() must not
            # leave memoryviews exporting the read-only mmap. Otherwise
            # Archive.close() can raise BufferError and mask the real failure.
            for part in entry_parts:
                try:
                    part.release()
                except ValueError:
                    pass
            entry_parts.clear()

