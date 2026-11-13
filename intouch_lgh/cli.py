"""Read-only command-line inspection of experimental LGH/IDX layouts."""
import argparse
from pathlib import Path
from .reader import LGHReader, FormatError
from .index import IDXReader
from .query import filter_records
from .audit import audit_archive_pair

def main():
    p = argparse.ArgumentParser(description='Read-only InTouch LGH/IDX inspector (experimental layout)')
    p.add_argument('input', help='Path to an archived .lgh file')
    p.add_argument('--idx', help='Matching IDX file path; validates index before querying')
    p.add_argument('--mode', choices=['blocks','records','audit'], default='blocks')
    p.add_argument('--tag', action='append', help='Tag filter; may repeat')
    p.add_argument('--start', help='Inclusive ISO-8601 timestamp with timezone')
    p.add_argument('--end', help='Exclusive ISO-8601 timestamp with timezone')
    p.add_argument('--encoding', default='utf-8')
    p.add_argument('--skip-unknown', action='store_true', help='Exploration only: may omit data')
    ns = p.parse_args()
    try:
        reader = LGHReader(ns.input, encoding=ns.encoding, skip_unknown=ns.skip_unknown)
        if ns.mode == 'audit':
            idx_file = ns.idx or str(Path(ns.input).with_suffix('.idx'))
            print(audit_archive_pair(ns.input, idx_file))
            return
        if ns.idx:
            print(f'IDX verification: {IDXReader(ns.idx, encoding=ns.encoding).verify_lgh(ns.input)}')
        if ns.mode == 'blocks':
            for b in reader.blocks():
                if ns.tag and b.tag not in ns.tag:
                    continue
                print(f'0x{b.offset:08x}\t{b.tag}\t{b.value_type}\t{b.count}\t{b.header_filetime}')
            return
        records = filter_records(reader.records(tags=set(ns.tag) if ns.tag else None), start=ns.start, end=ns.end)
        for r in records:
            # Do not use JSON here: the repr of IEEE-754 specials is intentional.
            print(f'{r.record_offset}\t{r.tag}\t{r.value_type}\t{r.timestamp_iso}\t{r.value!r}\t{r.value_bits:016x}')
    except (FormatError, ValueError, OSError) as e:
        p.exit(2, f'Inspection error: {e}\n')

if __name__ == '__main__':
    main()
