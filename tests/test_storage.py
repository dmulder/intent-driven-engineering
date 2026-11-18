"""Storage layout round trips, bounded sharing, tamper rejection and Git portability."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from support import RepoTest
from intentlib import cli, storage
from intentlib.core import IntentError, canonical, digest, object_record, put_object, snapshot, Store


def record_path(revision=1, identity='a'):
    return f'.intent/intents/I-{identity * 64}/{revision:06d}.json'


def prefixes(count):
    return {record_path(r): {'id': 'I-'+'a'*64, 'revision': r,
                             'history': {'findings': [{'id': n, 'statement': str(n)+' evidence '*80} for n in range(r)]},
                             'rationale': 'Source commits: '+''.join(f'{n:040x}, ' for n in range(r))}
            for r in range(1, count + 1)}


def packed(records, existing=()):
    batch = storage.encode_records(records, existing)
    path, entry = storage.batch_entry(batch)
    return batch, {path: entry}


class CodecTests(unittest.TestCase):
    def test_all_revisions_round_trip_and_share_prefixes(self):
        records = prefixes(100)
        batch, physical = packed(records)
        logical = storage.unpack(physical)
        self.assertEqual({k: canonical(v)+b'\n' for k, v in records.items()}, {k: v[1] for k, v in logical.items()})
        self.assertLess(sum(len(v[1]) for v in physical.values()), sum(len(v[1]) for v in logical.values()) // 3)

    def test_doubling_long_lived_intent_does_not_quadruple_packed_bytes(self):
        _, small = packed(prefixes(64))
        _, large = packed(prefixes(128))
        self.assertLess(sum(len(v[1]) for v in large.values()), 3 * sum(len(v[1]) for v in small.values()))

    def test_shared_values_across_batches(self):
        records = prefixes(8)
        first, a = packed({k: v for k, v in records.items() if v['revision'] < 8})
        second, b = packed({record_path(8): records[record_path(8)]}, first['values'])
        self.assertFalse(set(first['values']) & set(second['values']))
        self.assertEqual(len(storage.unpack({**a, **b})), 8)
        with self.assertRaises(IntentError):
            storage.unpack(b)

    def test_inline_reference_looking_data_is_literal(self):
        record = {'r': {'kind': 'list', 'items': [[1, 'not-a-reference']]}, 'text': '\r\n"\\☃'*800}
        _, physical = packed({record_path(): record})
        self.assertEqual(json.loads(storage.unpack(physical)[record_path()][1]), record)

    def test_invalid_batch_value_missing_reference_and_source_override_rejected(self):
        batch, physical = packed(prefixes(4))
        for change in ('digest', 'missing', 'source', 'unused'):
            bad = copy.deepcopy(batch)
            if change == 'digest':
                next(iter(bad['values'].values()))['tampered'] = True
            elif change == 'missing':
                bad['values'].pop(next(iter(bad['values'])))
            elif change == 'source':
                bad['records']['src.py'] = next(iter(bad['records'].values()))
            else:
                value = {'kind': 'text', 'value': 'unreferenced'}
                bad['values'][digest(canonical(value))] = value
            path, entry = storage.batch_entry(bad)
            with self.subTest(change=change), self.assertRaises(IntentError):
                storage.unpack({path: entry})
        path, (mode, raw) = next(iter(physical.items()))
        with self.assertRaises(IntentError):
            storage.unpack({path: (mode, raw+b' ')})

    def test_conflicting_representations_rejected(self):
        records = {record_path(): {'hello': 'world'}}
        _, physical = packed(records)
        same = {record_path(): ('100644', canonical(records[record_path()])+b'\n')}
        self.assertEqual(len(storage.unpack({**physical, **same})), 1)
        with self.assertRaises(IntentError):
            storage.unpack({**physical, record_path(): ('100644', b'{}\n')})

    def test_expansion_bomb_rejected_before_joining(self):
        text = {'kind': 'text', 'value': 'x'*1024}
        text_key = digest(canonical(text))
        branch = {'kind': 'text-branch', 'children': [text_key]*32}
        branch_key = digest(canonical(branch))
        root = {'kind': 'object', 'fields': {'payload': [1, branch_key]}}
        root_key = digest(canonical(root))
        batch = {'format': storage.FORMAT, 'records': {record_path(): [1, root_key]},
                 'values': {text_key: text, branch_key: branch, root_key: root}}
        path, entry = storage.batch_entry(batch)
        with mock.patch.object(storage, 'MAX_RECORD_BYTES', 2048), self.assertRaisesRegex(IntentError, 'safety limit'):
            storage.unpack({path: entry})

    def test_encoder_does_not_alias_mutable_input(self):
        value = {'short': {'hello': []}}
        _, physical = packed({record_path(): value})
        value['short']['hello'].append('changed')
        self.assertEqual(json.loads(storage.unpack(physical)[record_path()][1]), {'short': {'hello': []}})


class SourceStorageTests(RepoTest):
    def test_raw_blob_exact_bytes_git_identity_and_logical_reader(self):
        raw = b'first\r\nsecond\r\n\x00binary\xff'
        key = put_object(self.root, raw)
        path = '.intent/objects/'+key+'.blob'
        self.assertEqual((self.root/path).read_bytes(), raw)
        self.assertEqual(cli.read_json(self.root/'.intent/objects'/f'{key}.json'), object_record(raw))
        self.git('config', 'core.autocrlf', 'true')
        self.git('add', '.intent')
        self.assertEqual(self.git('show', ':'+path).stdout, raw)
        self.assertEqual(Store(snapshot(self.root, staged=True)).objects[key], raw)
        oid = self.git('hash-object', '--stdin', input=raw).stdout.strip()
        self.assertEqual(self.git('rev-parse', ':'+path).stdout.strip(), oid)

    def test_legacy_objects_reused_and_corruption_rejected(self):
        raw = b'legacy payload'
        key = digest(raw)
        path = self.root/'.intent/objects'/f'{key}.json'
        path.write_bytes(canonical(object_record(raw))+b'\n')
        self.assertEqual(put_object(self.root, raw), key)
        self.assertFalse(path.with_suffix('.blob').exists())
        other = put_object(self.root, b'another payload')
        (self.root/'.intent/objects'/f'{other}.blob').write_bytes(b'corrupt')
        with self.assertRaisesRegex(IntentError, 'digest mismatch'):
            snapshot(self.root, working=True)

    def test_reads_detect_tampering_even_when_size_and_mtime_are_preserved(self):
        storage.write_records(self.root, {record_path(): {'text': 'evidence '*100}})
        path = next((self.root/'.intent/records').glob('*.json'))
        cli.read_json(self.root/record_path())
        before = path.stat()
        data = path.read_bytes()
        path.write_bytes(data.replace(b'evidence', b'EVidence', 1))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(IntentError):
            cli.read_json(self.root/record_path())

    def test_disk_record_reader_and_collision_checks(self):
        records = {record_path(): {'test': 'evidence '*100}}
        storage.write_records(self.root, records)
        path = self.root/record_path()
        self.assertTrue(storage.record_exists(path))
        self.assertEqual(cli.read_json(path), records[record_path()])
        self.assertFalse(storage.record_exists(self.root/record_path(2)))
        self.assertFalse(path.exists())
