"""Read reconstructed history from actual .intent records, without any processing cache."""
import argparse

from .. import cli
from ..core import Store, git_environment, identity, snapshot
from .gitscan import GIT_GLOBAL_ARGS, git_env
from .util import TrueUpError, iso, write_json


def write_coverage_cache(store, report):
    write_json(store.path / 'coverage.json', {'as_of': iso(), **report})


def _canonical(repo, at=None):
    if repo.toplevel is None:
        raise TrueUpError('intent queries require a working tree')
    with git_environment(git_env(), GIT_GLOBAL_ARGS):
        entries = snapshot(repo.toplevel, revision=at) if at else snapshot(repo.toplevel, working=True)
    store = Store(entries)
    if store.errors:
        raise TrueUpError(str(store.errors))
    return store


def show(repo, ident, at=None):
    store = _canonical(repo, at)
    if ident.startswith('N-'):
        ident = identity(store.config, 'intent', 'trueup:' + ident)
    matches = [v for i, v in store.current('intent').items() if i.startswith(ident)]
    if len(matches) != 1:
        raise TrueUpError(f'no unique canonical intent matches {ident!r}')
    return matches[0]


def query(repo, location, at=None):
    with git_environment(git_env(), GIT_GLOBAL_ARGS):
        return cli.query(repo.toplevel, argparse.Namespace(command='explain', location=location, at=at))


def list_intents(repo, args):
    store = _canonical(repo)
    rows = []
    for intent in store.current('intent').values():
        reconstruction = intent.get('reconstruction')
        if not reconstruction:
            continue
        node = reconstruction['history']
        if args.path and args.path not in node['paths']:
            continue
        if args.kind and node['kind'] != args.kind:
            continue
        if args.lifecycle and node['lifecycle'] != args.lifecycle:
            continue
        if args.commit and not any(c.startswith(args.commit) for c in reconstruction['source_commits']):
            continue
        if args.reference and args.reference not in node['resource_keys']:
            continue
        if args.contains and args.contains.lower() not in (intent['title'] + ' ' + ' '.join(f['statement'] for f in node['findings_data'])).lower():
            continue
        rows.append(intent)
    rows.sort(key=lambda v: (v['reconstruction']['history']['timeline'][0]['index'], v['id']))
    return {'total': len(rows), 'intents': rows[:args.limit]}


def tree(repo, limit=100):
    args = argparse.Namespace(path=None, kind=None, lifecycle=None, commit=None, reference=None, contains=None, limit=limit)
    lines = []
    for intent in list_intents(repo, args)['intents']:
        node = intent['reconstruction']['history']
        lines.append(f"{intent['id']} revision {intent['revision']} [{intent['status']}; verification pending] {intent['title']}")
        for entry in node['timeline']:
            lines.append(f"    {entry['commit']} {entry['role']} {entry.get('relation') or ''}")
        for ending in node['ended_by']:
            lines.append(f"    {ending['relation']} in {ending['commit']}")
    return '\n'.join(lines)
