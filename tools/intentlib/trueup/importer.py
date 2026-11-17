"""Import reconstructed Git history as ordinary, append-only .intent records.

The cache supplies analysis checkpoints, never the durable result. Import events are
recorded now with unknown provenance; historical bindings pin the original Git blobs
and remain pending verification rather than silently imposing present-day constraints.
"""
import copy
import json

from .. import cli
from ..core import (IntentError, ValidationError, canonical, changes_between, digest, git_environment, head,
                    identity, metadata, put_object, record_path, safe_path, seal, snapshot,
                    source, validate, write_json)
from ..schema import published
from . import graph as graph_mod
from .gitscan import GIT_GLOBAL_ARGS, git_env
from .store import Store
from .util import TrueUpError, fault


def _records(root):
    return cli.manifest(root)


def _projections(node, order):
    """Retain the successive historical statements as actual intent revisions."""
    findings = node['findings_data']
    for commit in dict.fromkeys(f['commit'] for f in findings):
        cutoff = order[commit]
        current = copy.deepcopy(node)
        current['findings_data'] = [copy.deepcopy(f) for f in findings if order[f['commit']] <= cutoff]
        current['supporting_data'] = [f for f in current['supporting_data'] if order[f['commit']] <= cutoff]
        current['timeline'] = [t for t in current['timeline'] if t['index'] <= cutoff]
        current['edges'] = [e for e in current['edges'] if order[e['commit']] <= cutoff]
        current['ended_by'] = [e for e in current['ended_by'] if order[e['commit']] <= cutoff]
        current['contested_by'] = [e for e in current['contested_by'] if order[e['commit']] <= cutoff]
        current['lifecycle'] = (graph_mod.ENDING[current['ended_by'][0]['relation']] if current['ended_by'] else graph_mod.ACTIVE)
        current['last_commit'] = current['timeline'][-1]['commit']
        current['paths'] = sorted({p['path'] for f in current['findings_data'] + current['supporting_data'] for p in f['paths']})
        evidence = current['findings_data'] + current['supporting_data']
        current['resource_keys'] = sorted(set().union(*(graph_mod.evidence_resources(f) for f in evidence)))
        current['tags'] = sorted({tag for f in evidence for tag in f['tags']})
        yield current
    # Endings, supporting evidence and contested relationships can arrive later.
    yield node


def _binding_target(repo, finding, path, lines, cap, limitations):
    if not safe_path(path) or metadata(path):
        limitations.append(f"omitted non-source binding path {path!r}")
        return None
    revisions = [finding['commit']]
    revisions += repo.run('rev-list', '--parents', '-n', '1', finding['commit']).decode().split()[1:]
    for revision in revisions:
        raw = repo.run('ls-tree', '-z', revision, '--', ':(literal)' + path)
        if not raw:
            continue
        fields = raw.split(b'\t', 1)[0].decode().split()
        if len(fields) != 3 or fields[1] != 'blob' or fields[0] not in ('100644', '100755', '120000'):
            continue
        mode, _, oid = fields
        data, truncated = repo.run_capped('cat-file', 'blob', oid, cap=cap)
        if truncated:
            limitations.append(f"historical snapshot omitted for {path}: exceeds {cap} bytes")
            return None
        start, end = 0, len(data)
        if lines and mode != '120000' and b'\0' not in data:
            starts = [0] + [i + 1 for i, b in enumerate(data) if b == 10]
            first, last = lines
            if first < 1 or last < first or last > len(starts):
                limitations.append(f"historical line range omitted for {path} at {revision}: {first}-{last}")
                return None
            start = starts[first - 1]
            end = starts[last] if last < len(starts) else len(data)
        return mode, data, start, end, revision
    limitations.append(f"historical source unavailable for {path} at {finding['commit']}")
    return None


def _publish(root, journal, transaction):
    """Validate the entire proposed tree, write immutable records, publish manifest last.

    An ignored transaction journal lets a retry finish identical writes after a crash;
    a different manifest or conflicting immutable file is never overwritten.
    """
    current = cli.manifest(root)
    writes = transaction['writes']
    target = writes['.intent/manifests/current.json']
    if current != target and digest(canonical(current)) != transaction['base_manifest']:
        raise TrueUpError('canonical manifest changed during historical import; inspect the saved import transaction')
    for path, value in writes.items():
        if not safe_path(path) or not metadata(path) or path.startswith(('.intent/cache/', '.intent/drafts/')):
            raise TrueUpError('invalid historical import transaction path')
        existing = root / path
        mutable = path == '.intent/manifests/current.json' or path.startswith('.intent/schema/')
        if existing.exists() and not mutable and existing.read_bytes() != canonical(value) + b'\n':
            raise TrueUpError(f'conflicting canonical record during historical import: {path}')
    proposed = snapshot(root, staged=True)
    proposed = {p: v for p, v in proposed.items() if not metadata(p)}
    proposed.update({p: v for p, v in snapshot(root, working=True).items() if metadata(p)})
    proposed.update({p: ('100644', canonical(v) + b'\n') for p, v in writes.items()})
    revision = head(root)
    errors = validate(proposed, snapshot(root, revision=revision) if revision else {})
    if errors:
        raise ValidationError(errors)
    write_json(journal, transaction)
    for path, value in writes.items():
        if path != '.intent/manifests/current.json':
            write_json(root / path, value, immutable=not path.startswith('.intent/schema/'))
            fault('import:after_record_write')
    write_json(root / '.intent/manifests/current.json', target)
    fault('import:after_manifest_write')
    journal.unlink()


def import_history(repo, graph, scope, coverage, max_snapshot_bytes=1_500_000):
    with git_environment(git_env(), GIT_GLOBAL_ARGS):
        return _import_history(repo, graph, scope, coverage, max_snapshot_bytes)


def _import_history(repo, graph, scope, coverage, max_snapshot_bytes):
    root = repo.toplevel
    if root is None:
        raise TrueUpError('historical import requires a working tree')
    # One canonical writer even when operators choose different processing caches.
    with Store(root / '.intent/cache/trueup/canonical-writer'):
        journal = root / '.intent/drafts/trueup-import.json'
        if journal.exists():
            _publish(root, journal, json.loads(journal.read_text()))
        staged = snapshot(root, staged=True)
        working = snapshot(root, working=True)
        if changes_between(source(staged), source(working)):
            raise TrueUpError('historical import requires a clean source worktree; commit source edits or use --dry-run')
        if (root / '.intent/config.json').exists():
            if changes_between(cli.pending_source(root), source(staged)):
                raise TrueUpError('historical import is metadata-only; commit staged source changes or use --dry-run')
        else:
            revision = head(root)
            if revision and changes_between(source(snapshot(root, revision=revision)), source(staged)):
                raise TrueUpError('commit staged source changes before adopting .intent, or use --dry-run')
            cli.initialize(root, no_hooks=True)
        manifest = _records(root)
        cfg = cli.config(root)
        ids = {n: identity(cfg, 'intent', 'trueup:' + n) for n in graph.nodes}
        order = {c['sha']: c['index'] for c in scope['commits']}
        fingerprint = digest(canonical({'nodes': {n: graph_mod.node_hash(v) for n, v in sorted(graph.nodes.items())},
                                        'coverage': coverage}))
        previous_import = None
        for event_id in reversed(manifest['events']):
            event = cli.read_json(root / record_path('event', event_id))
            if event['kind'] == 'retrospective-import':
                previous_import = event
                break
        if previous_import and any(v['kind'] == 'trueup-import-state' and v['reference'] == fingerprint for v in previous_import['verification']):
            return {'intents': [], 'bindings': 0, 'events': [], 'unchanged': True, 'path': '.intent'}
        key = 'trueup-import:' + digest(canonical([fingerprint, manifest['events'][-1:]]))
        event_id = identity(cfg, 'event', key)
        intents, bindings, transitions, limitations = [], [], [], []
        writes = {}
        next_manifest = copy.deepcopy(manifest)
        for node_id, node in sorted(graph.nodes.items(), key=lambda kv: (kv[1]['timeline'][0]['index'], kv[0])):
            iid = ids[node_id]
            old = (cli.read_json(root / record_path('intent', iid, manifest['intents'][iid])) if iid in manifest['intents'] else None)
            if old:
                origin = cli.read_json(root / record_path('event', old['created_by_event']))
                if not old.get('reconstruction') or origin['kind'] != 'retrospective-import':
                    raise TrueUpError(f'historical import will not overwrite manually authored intent {iid}')
            current = old
            known = set()
            for revision in range(1, (old['revision'] if old else 0) + 1):
                record = cli.read_json(root / record_path('intent', iid, revision))
                if record.get('reconstruction'):
                    known.add(record['reconstruction']['content_hash'])
            owned, finding_revisions = [], {}
            projections = list(_projections(node, order))
            for number, projection in enumerate(projections):
                content_hash = graph_mod.node_hash(projection)
                if current and (current['reconstruction']['content_hash'] == content_hash or (number < len(projections)-1 and content_hash in known)):
                    continue
                findings = projection['findings_data'] + projection['supporting_data']
                bases = {f['basis'] for f in findings}
                basis = next(iter(bases)) if len(bases) == 1 else 'mixed'
                uncertainty = max((f['uncertainty'] for f in findings), key=['low','medium','high'].index)
                latest = projection['findings_data'][-1]
                revision = current['revision'] + 1 if current else 1
                value = seal({'schema_version': 1, 'id': iid, 'revision': revision,
                    'previous_hash': current['content_hash'] if current else None, 'created_by_event': event_id,
                    'status': {'superseded':'superseded', 'reversed':'retired', 'retired':'retired'}.get(projection['lifecycle'], 'active'),
                    'kind': 'retrospective', 'title': projection['title'], 'description': latest['statement'],
                    'rationale': f"Reconstructed from Git history after the fact; basis={basis}, uncertainty={uncertainty}. Not captured contemporaneously and not independently verified. Source commits: " + ', '.join(dict.fromkeys(f['commit'] for f in findings)),
                    'requirements': [f['statement'] for f in projection['findings_data'] if f['kind'] in ('requirement','invariant')],
                    'constraints': [f['statement'] for f in projection['findings_data'] if f['kind'] in ('constraint','rejected-alternative')],
                    'related_intents': sorted({ids[e['to']] for e in projection['edges'] if e['to'] in ids and e['to'] != node_id}),
                    'reconstruction': {'method':'git-history-ai', 'node_id':node_id, 'content_hash':content_hash,
                        'source_commits':list(dict.fromkeys(f['commit'] for f in findings)), 'basis':basis, 'uncertainty':uncertainty,
                        'verification':'pending', 'history':projection, 'limitations':list(coverage['manifest']['notes'])}})
                owned.append(value)
                writes[record_path('intent', iid, revision)] = value
                next_manifest['intents'][iid] = revision
                current = value
                known.add(content_hash)
                finding_revisions[latest['id']] = revision
            owned_bindings = []
            if current is None:
                continue
            for finding in node['findings_data'] + node['supporting_data']:
                for candidate in finding['paths']:
                    path, lines = candidate['path'], candidate.get('lines')
                    binding_id = identity(cfg, 'binding', 'trueup:' + digest(canonical([node_id, finding['id'], path, lines])))
                    if binding_id in manifest['bindings']:
                        continue
                    try:
                        target = _binding_target(repo, finding, path, lines, max_snapshot_bytes, limitations)
                    except TrueUpError as exc:
                        limitations.append(f"historical source unavailable for {path}: {exc}")
                        target = None
                    if target is None:
                        continue
                    mode, data, start, end, snapshot_commit = target
                    source_id = put_object(root, data)
                    value = seal({'schema_version':1, 'id':binding_id, 'revision':1, 'previous_hash':None, 'created_by_event':event_id,
                        'intent_id':iid, 'intent_revision':finding_revisions.get(finding['id'], current['revision']),
                        'relationship':'historically-derived-from', 'status':'historical', 'predecessors':[],
                        'rationale':f"Reconstructed association from {finding['commit']}; historical snapshot {snapshot_commit}. Current applicability is unverified.",
                        'target':{'path':path,'start':start,'end':end,'source':source_id,'mode':mode},
                        'verification':{'status':'pending','method':'AI reconstruction from Git history',
                                        'evidence':f"finding {finding['id']} at commit {finding['commit']}; basis {finding['basis']}; uncertainty {finding['uncertainty']}"}})
                    writes[record_path('binding', binding_id, 1)] = value
                    next_manifest['bindings'][binding_id] = 1
                    owned_bindings.append(value)
            if not owned and owned_bindings:
                # A new association must explicitly carry the current reconstructed intent.
                value = seal({**current,'revision':current['revision']+1,'previous_hash':current['content_hash'],'created_by_event':event_id})
                owned.append(value)
                writes[record_path('intent',iid,value['revision'])] = value
                next_manifest['intents'][iid] = value['revision']
                current = value
            if owned:
                for binding in owned_bindings or [None]:
                    transitions.append({'intent_id':iid,'binding_id':binding['id'] if binding else None,'action':'import',
                        'from_revision':old['revision'] if old else None,'to_revision':current['revision'],
                        'binding_revisions':[1] if binding else []})
                intents.extend(owned)
                bindings.extend(owned_bindings)
        event = seal({'schema_version':1,'id':event_id,'kind':'retrospective-import',
            'description':'Reconstruct project intent from Git history into .intent',
            'rationale':'Record evidence-backed historical context for agents, including uncertainty and incomplete coverage; no claim of contemporaneous capture or independent verification.',
            'changes':[],'intent_transitions':transitions,'provenance':'unknown',
            'verification':[{'kind':'trueup-import-state','reference':fingerprint,'result':canonical(coverage).decode()},
                            {'kind':'trueup-limitations','reference':'historical-source-bindings','result':json.dumps(limitations)}],
            'records':[{'type':'intent','id':v['id'],'revision':v['revision']} for v in intents] +
                      [{'type':'binding','id':v['id'],'revision':v['revision']} for v in bindings]})
        writes[record_path('event',event_id)] = event
        next_manifest['events'].append(event_id)
        writes['.intent/manifests/current.json'] = next_manifest
        for name in ('intent','event'):
            writes[f'.intent/schema/{name}.schema.json'] = published(name)
        transaction = {'base_manifest':digest(canonical(manifest)), 'writes':writes}
        _publish(root, journal, transaction)
        return {'intents':sorted({v['id'] for v in intents}),'revisions':len(intents),'bindings':len(bindings),
                'events':[event_id],'path':'.intent','limitations':limitations,'verification':'pending',
                'next':'git add .intent && intentctl validate --staged, then commit the imported history'}
