"""Physical storage for canonical history: raw blobs and immutable Merkle batches.

The logical record schema, identities and hashes are unchanged. All decoding is
verified before exposing logical paths; storage paths never override source files.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import re
import tempfile

_READ_SESSION = ContextVar('intentctl_storage_read_session', default=None)


@contextmanager
def read_session():
    # Only callers holding a canonical writer lock use a consistent read session.
    # Ordinary readers always read and verify current bytes.
    token = _READ_SESSION.set({})
    try:
        yield
    finally:
        _READ_SESSION.reset(token)


FORMAT = 'intentctl-record-pack-v2'
FANOUT = 32
INLINE = 192
TEXT_CHUNK = 1024
MAX_RECORD_BYTES = 64 * 1024 * 1024
MAX_DEPTH = 128
RECORD = re.compile(r'\.intent/(?:(?:intents/I|bindings/B)-[0-9a-f]{64}/[0-9]{6,}\.json|events/E-[0-9a-f]{64}\.json)\Z')
PACK = re.compile(r'\.intent/records/([0-9a-f]{64})\.json\Z')
BLOB = re.compile(r'\.intent/objects/([0-9a-f]{64})\.blob\Z')


def is_record(path):
    return bool(RECORD.fullmatch(str(path)))


def _core():
    from . import core
    return core


def _fail(message):
    raise _core().IntentError('invalid packed storage: ' + message)


class Encoder:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.nodes = {}

    def node(self, value):
        key = _core().digest(_core().canonical(value))
        if key not in self.existing:
            self.nodes[key] = value
        return key

    def tree(self, refs, branch):
        while len(refs) > 1:
            refs = [self.node({'kind': branch, 'children': refs[i:i + FANOUT]})
                    for i in range(0, len(refs), FANOUT)]
        return refs[0]

    def value(self, value):
        raw = _core().canonical(value)
        if len(raw) > MAX_RECORD_BYTES:
            _fail('expanded record exceeds 64 MiB safety limit')
        if len(raw) <= INLINE or not isinstance(value, (dict, list, str)):
            return [0, _core().loads(raw) if isinstance(value, (dict, list)) else value]
        if isinstance(value, str):
            refs = [self.node({'kind': 'text', 'value': value[i:i + TEXT_CHUNK]})
                    for i in range(0, len(value), TEXT_CHUNK)]
            return [1, self.tree(refs, 'text-branch')]
        if isinstance(value, list):
            refs = [self.node({'kind': 'list', 'items': [self.value(v) for v in value[i:i + FANOUT]]})
                    for i in range(0, len(value), FANOUT)]
            return [1, self.tree(refs, 'list-branch')]
        return [1, self.node({'kind': 'object', 'fields': {k: self.value(v) for k, v in value.items()}})]


def encode_records(records, existing=()):
    """Return one immutable batch, sharing values with earlier batches by digest."""
    encoder = Encoder(existing)
    roots = {}
    for path, value in sorted(records.items()):
        if not is_record(path):
            _fail('non-record path in batch')
        roots[path] = encoder.value(value)
    return {'format': FORMAT, 'records': roots, 'values': encoder.nodes}


class Decoder:
    def __init__(self, nodes):
        self.nodes = nodes
        self.cache = {}
        self.active = set()
        self.used = set()

    def size_check(self, size):
        if size > MAX_RECORD_BYTES:
            _fail('expanded record exceeds 64 MiB safety limit')

    def atom(self, atom, depth=0):
        if not isinstance(atom, list) or len(atom) != 2 or type(atom[0]) is not int:
            _fail('malformed value reference')
        if atom[0] == 0:
            value = atom[1]
            size = len(_core().canonical(value))
            self.size_check(size)
            return value, size
        if atom[0] != 1 or not isinstance(atom[1], str):
            _fail('unknown value reference')
        return self.node(atom[1], depth + 1)

    def node(self, key, depth):
        if depth > MAX_DEPTH or key in self.active:
            _fail('cyclic or excessively deep value graph')
        self.used.add(key)
        if key in self.cache:
            return self.cache[key]
        if key not in self.nodes:
            _fail('missing value ' + key)
        self.active.add(key)
        node = self.nodes[key]
        kind = node.get('kind')
        if kind == 'text' and set(node) == {'kind', 'value'} and isinstance(node['value'], str):
            if not 0 < len(node['value']) <= TEXT_CHUNK:
                _fail('invalid text chunk')
            value = node['value']
            size = len(_core().canonical(value))
        elif kind == 'object' and set(node) == {'kind', 'fields'} and isinstance(node['fields'], dict):
            children = {k: self.atom(v, depth) for k, v in node['fields'].items()}
            size = 2 + max(0, len(children) - 1) + sum(len(_core().canonical(k)) + 1 + v[1] for k, v in children.items())
            self.size_check(size)
            value = {k: v[0] for k, v in children.items()}
        elif kind == 'list' and set(node) == {'kind', 'items'} and isinstance(node['items'], list):
            if not 1 <= len(node['items']) <= FANOUT:
                _fail('invalid list leaf')
            children = [self.atom(v, depth) for v in node['items']]
            size = 2 + max(0, len(children) - 1) + sum(v[1] for v in children)
            self.size_check(size)
            value = [v[0] for v in children]
        elif kind in ('text-branch', 'list-branch') and set(node) == {'kind', 'children'} and isinstance(node['children'], list):
            refs = node['children']
            if not 1 <= len(refs) <= FANOUT or not all(isinstance(k, str) for k in refs):
                _fail('invalid branch')
            children = [self.node(k, depth + 1) for k in refs]
            expected = str if kind == 'text-branch' else list
            if not all(isinstance(v[0], expected) for v in children):
                _fail('mixed branch types')
            size = 2 + sum(v[1] - 2 for v in children) + (len(children) - 1 if expected is list else 0)
            self.size_check(size)
            value = ''.join(v[0] for v in children) if expected is str else [x for v in children for x in v[0]]
        else:
            _fail('unknown or malformed value node')
        self.size_check(size)
        self.active.remove(key)
        self.cache[key] = value, size
        return value, size


def unpack(entries):
    """Expand physical packs/blobs to byte-identical logical v1 record paths."""
    result = {}
    nodes, roots = {}, {}

    def add(path, entry):
        if path in result and result[path] != entry:
            _fail('conflicting logical record ' + path)
        result[path] = entry

    for path, entry in entries.items():
        match = PACK.fullmatch(path)
        blob = BLOB.fullmatch(path)
        if not match and not blob:
            add(path, entry)
            continue
        mode, raw = entry
        if mode != '100644':
            _fail('storage containers must be ordinary files')
        if blob:
            if _core().digest(raw) != blob[1]:
                _fail('source blob digest mismatch')
            value = _core().object_record(raw)
            add('.intent/objects/' + blob[1] + '.json', ('100644', _core().canonical(value) + b'\n'))
            continue
        batch = _core().loads(raw)
        if (not isinstance(batch, dict) or set(batch) != {'format', 'records', 'values'}
                or batch['format'] != FORMAT or not isinstance(batch['records'], dict)
                or not isinstance(batch['values'], dict)):
            _fail('unknown batch format')
        canonical = _core().canonical(batch)
        if raw != canonical + b'\n' or _core().digest(canonical) != match[1]:
            _fail('batch digest or canonical encoding mismatch')
        for key, value in batch['values'].items():
            if not isinstance(value, dict) or _core().digest(_core().canonical(value)) != key:
                _fail('value digest mismatch')
            if key in nodes and nodes[key] != value:
                _fail('conflicting shared value')
            nodes[key] = value
        for key, value in batch['records'].items():
            if not is_record(key):
                _fail('non-record path in batch')
            if key in roots and roots[key] != value:
                _fail('conflicting record roots')
            roots[key] = value
    decoder = Decoder(nodes)
    for path, root in roots.items():
        value, _ = decoder.atom(root)
        if not isinstance(value, dict):
            _fail('record root is not an object')
        add(path, ('100644', _core().canonical(value) + b'\n'))
    if set(nodes) != decoder.used:
        _fail('unreferenced values in batch storage')
    return result


def batch_entry(batch):
    raw = _core().canonical(batch)
    return '.intent/records/' + _core().digest(raw) + '.json', ('100644', raw + b'\n')


def existing_values(root):
    values = set()
    directory = Path(root) / '.intent/records'
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        _fail('batch directory is a symlink')
    for path in directory.glob('*.json'):
        if path.is_symlink():
            _fail('batch file is a symlink')
        # Full verification is performed by snapshot/Store before publication.
        value = _core().loads(path.read_bytes())
        if value.get('format') != FORMAT:
            _fail('unknown existing batch format')
        values.update(value['values'])
    return values


def write_records(root, records):
    if not records:
        return None
    ensure_attributes(root)
    path, (_, raw) = batch_entry(encode_records(records, existing_values(root)))
    _write_bytes(Path(root) / path, raw)
    if _READ_SESSION.get() is not None:
        _READ_SESSION.get().clear()
    return path


def _write_bytes(path, data, immutable=True):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        _fail('storage write through symlink')
    if path.exists():
        old = path.read_bytes()
        if old == data:
            return
        if immutable:
            _fail('conflicting immutable storage file')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.intentctl-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass  # Directory fsync is unavailable on some supported filesystems.
    finally:
        if temporary.exists():
            temporary.unlink()


def ensure_attributes(root):
    path = Path(root) / '.intent/.gitattributes'
    if any(part.is_symlink() for part in (path, *path.parents)):
        _fail('attributes file is a symlink')
    data = path.read_bytes() if path.exists() else b''
    for rule in (b'objects/*.blob -text', b'records/*.json -text'):
        if rule not in data.splitlines():
            data = data.rstrip(b'\n') + (b'\n' if data else b'') + rule + b'\n'
    _write_bytes(path, data, immutable=False)


def put_source(root, data):
    """New snapshots are raw blobs; existing JSON snapshots remain authoritative."""
    root = Path(root)
    key = _core().digest(data)
    legacy = root / '.intent/objects' / (key + '.json')
    if any(p.is_symlink() for p in (legacy, *legacy.parents)):
        _fail('source snapshot path is a symlink')
    if legacy.exists():
        expected = _core().canonical(_core().object_record(data)) + b'\n'
        if legacy.read_bytes() != expected:
            _fail('conflicting existing source snapshot')
        return key
    ensure_attributes(root)
    _write_bytes(root / '.intent/objects' / (key + '.blob'), data)
    return key


def _disk_records(paths):
    entries = {}
    for path in paths:
        if any(p.is_symlink() for p in (path, *path.parents)):
            _fail('batch read through symlink')
        entries['.intent/records/' + path.name] = ('100644', path.read_bytes())
    return unpack(entries)


def read_record(path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        _fail('record read through symlink')
    if path.exists():
        return path.read_bytes()
    intent = next((p for p in path.parents if p.name == '.intent'), None)
    if intent is None:
        raise FileNotFoundError(str(path))
    relative = '.intent/' + path.relative_to(intent).as_posix()
    object_match = re.fullmatch(r'\.intent/objects/([0-9a-f]{64})\.json', relative)
    if object_match:
        blob = intent / 'objects' / (object_match[1] + '.blob')
        if blob.exists():
            raw = blob.read_bytes()
            if _core().digest(raw) != object_match[1]:
                _fail('source blob digest mismatch')
            return _core().canonical(_core().object_record(raw)) + b'\n'
    if not is_record(relative):
        raise FileNotFoundError(str(path))
    cache = _READ_SESSION.get()
    key = str(intent.absolute())
    if cache is None or key not in cache:
        records = _disk_records(sorted((intent / 'records').glob('*.json')))
        if cache is not None:
            cache[key] = records
    else:
        records = cache[key]
    entry = records.get(relative)
    if entry is None:
        raise FileNotFoundError(str(path))
    return entry[1]


def record_exists(path):
    try:
        read_record(path)
        return True
    except FileNotFoundError:
        return False
