#!/usr/bin/env python3
"""Synthetic cumulative-history storage and exact round-trip benchmark."""
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from intentlib.core import canonical, loads
from intentlib.storage import encode_records, batch_entry, unpack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revisions', type=int, nargs='+', default=[40, 80, 160])
    args = parser.parse_args()
    if any(n < 2 or n > 512 for n in args.revisions):
        parser.error('revision counts must be between 2 and 512; the legacy input itself is quadratic')
    print('revisions logical_bytes packed_bytes values encode_seconds exact_round_trip')
    for count in args.revisions:
        records = {}
        for revision in range(1, count + 1):
            findings = [{'id': number, 'statement': f'Requirement {number}: '+'evidence '*80}
                        for number in range(revision)]
            records[f'.intent/intents/I-{"a"*64}/{revision:06d}.json'] = {
                'id': 'I-'+'a'*64, 'revision': revision,
                'requirements': [f['statement'] for f in findings],
                'reconstruction': {'history': {'findings_data': findings}},
                'rationale': 'Source commits: '+''.join(f'{n:040x}, ' for n in range(revision))}
        start = time.monotonic()
        batch = encode_records(records)
        elapsed = time.monotonic() - start
        path, entry = batch_entry(batch)
        decoded = unpack({path: entry})
        if {p: canonical(v)+b'\n' for p, v in records.items()} != {p: v[1] for p, v in decoded.items()}:
            raise RuntimeError('round-trip mismatch')
        print(count, sum(len(canonical(v))+1 for v in records.values()), len(entry[1]),
              len(batch['values']), f'{elapsed:.3f}', True, flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
