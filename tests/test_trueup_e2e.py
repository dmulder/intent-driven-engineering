"""True-up writes portable, append-only canonical history that agents can query directly."""
import argparse
import copy
import json
import os
import shutil
from pathlib import Path
from unittest import mock

from trueup_support import TrueUpTest, GitRepo, provider_spec
from intentlib import cli
from intentlib.core import Store as CanonicalStore, canonical, snapshot, validate
from intentlib.trueup import importer, graph as graph_mod
from intentlib.trueup.util import TrueUpError


class EndToEnd(TrueUpTest):
    import_results = True

    def canonical(self):
        return CanonicalStore(snapshot(self.repo.path, working=True))

    def query(self, command, location):
        return cli.query(self.repo.path, argparse.Namespace(command=command, location=location, at=None))

    def records(self):
        root = self.repo.path / '.intent'
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()
                if not any(part in ('cache','drafts') for part in p.relative_to(root).parts)}

    def start(self):
        commits = self.small_history()
        self.init(providers={'fake': provider_spec()})
        result = self.run_ai()
        self.assertIn('import', result)
        self.assertTrue(result['complete'], result)
        return commits

    def test_import_serializes_with_ordinary_metadata_writers(self):
        self.small_history()
        self.init(providers={'fake': provider_spec()})
        with cli.writer_lock(self.repo.path):
            result = self.run_ai(ok=4)
        self.assertIn('another intentctl writer', result['import_error'])
        resumed = self.run_ai()
        self.assertTrue(resumed['complete'])
        self.assertEqual(resumed['ai_calls'], 0)

    def test_default_run_imports_actual_records_and_historical_revisions(self):
        c1, c2, c3, c4 = self.start()
        store = self.canonical()
        self.assertFalse(store.errors)
        self.assertFalse(validate(snapshot(self.repo.path, working=True), snapshot(self.repo.path, revision='HEAD')))
        auth = next(v for v in store.current('intent').values() if v['title']=='Add auth check')
        self.assertEqual(auth['status'], 'retired')
        self.assertGreaterEqual(auth['revision'], 3)
        self.assertEqual(auth['reconstruction']['source_commits'], [c1,c2])
        self.assertEqual(auth['reconstruction']['history']['ended_by'][0]['commit'], c3)
        self.assertEqual(auth['reconstruction']['verification'], 'pending')
        bindings = [v for v in store.current('binding').values() if v['intent_id']==auth['id']]
        self.assertEqual(len(bindings), 2)
        self.assertTrue(all(v['status']=='historical' and v['verification']['status']=='pending' for v in bindings))
        self.assertIn(b"return t == 'x'", store.objects[bindings[0]['target']['source']])
        answer = self.query('explain', 'auth.py:2')
        self.assertEqual(answer['applicable'], [])
        self.assertIn(auth['id'], {v['intent']['id'] for v in answer['historical']})
        self.assertNotIn('retrospective', answer)
        history = self.query('history','auth.py:2')
        self.assertIn(c1, json.dumps(history))
        self.assertIn(c2, json.dumps(history))
        self.assertTrue(history['intent_revisions'])

    def test_results_survive_cache_deletion_and_clone(self):
        self.start()
        before = self.query('history','auth.py:2')
        shutil.rmtree(self.state)
        # No second store is required by any normal intent query.
        self.assertEqual(before, self.query('history','auth.py:2'))
        self.repo.git('add','.intent')
        self.repo.git('commit','-qm','import reconstructed history')
        clone = self.tmp / 'clone'
        self.repo.git('clone','-q',str(self.repo.path),str(clone))
        result = cli.query(clone,argparse.Namespace(command='history',location='auth.py:2',at=None))
        self.assertEqual(result['intent_revisions'], before['intent_revisions'])
        self.assertFalse((clone/'.intent/cache').exists())
        self.assertFalse(validate(snapshot(clone,working=True), snapshot(clone,revision='HEAD')))

    def test_repeated_run_is_idempotent_and_reanalysis_appends(self):
        c1,*_ = self.start()
        before = self.records()
        calls = len(self.calls())
        result = self.run_ai()
        self.assertTrue(result['import']['unchanged'])
        self.assertEqual(before,self.records())
        self.assertEqual(len(self.calls()),calls)
        self.repo.git('add','.intent')
        self.repo.git('commit','-qm','import')
        # A changed analysis adds revisions rather than rewriting prior records.
        config = self.store().config()
        old = next(iter(self.graph().nodes.values()))
        changed = copy.deepcopy(old)
        changed['findings_data'][0]['statement'] += ' Clarified by reanalysis.'
        graph = self.graph()
        graph.nodes[old['id']] = changed
        result = importer.import_history(__import__('intentlib.trueup.gitscan',fromlist=['Repo']).Repo(self.repo.path),graph,
                                         self.store().manifest(),self.tuj('status')['coverage'])
        self.assertTrue(result['intents'])
        after = self.records()
        for p,data in before.items():
            if p!='manifests/current.json': self.assertEqual(after[p],data)

    def test_import_and_queries_isolate_ambient_git_locations_and_external_programs(self):
        other=GitRepo(self.tmp/'other')
        other_sha=other.commit('Outside',{'a.py':'outside'})
        sha=self.repo.commit('Inside',{'a.py':'inside'})
        marker=self.tmp/'executed'
        script=self.tmp/'fsmonitor.sh'
        script.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
        script.chmod(0o755)
        self.repo.git('config','core.fsmonitor',str(script))
        env={'GIT_DIR':str(other.path/'.git'),'GIT_WORK_TREE':str(other.path),
             'GIT_COMMON_DIR':str(other.path/'.git'),'GIT_INDEX_FILE':str(other.path/'.git/index'),
             'GIT_OBJECT_DIRECTORY':str(other.path/'.git/objects'),
             'GIT_ALTERNATE_OBJECT_DIRECTORIES':str(other.path/'.git/objects')}
        with mock.patch.dict(os.environ,env):
            self.init(providers={'fake':provider_spec()})
            result=self.run_ai()
            self.assertTrue(result['import']['intents'])
            self.assertEqual(self.tuj('list')['intents'][0]['title'],'Inside')
            self.assertEqual(self.tuj('query','a.py:1')['historical'][0]['intent']['title'],'Inside')
        self.assertFalse(marker.exists(),'canonical Git calls must keep true-up external-program isolation')
        self.repo.git('config','core.fsmonitor','false')
        self.assertEqual(other.head(),other_sha)
        intent=next(iter(self.canonical().current('intent').values()))
        self.assertEqual(intent['reconstruction']['source_commits'],[sha])
        from intentlib.core import _GIT_CONTEXT
        self.assertIsNone(_GIT_CONTEXT.get(),'the ordinary canonical CLI must retain its normal Git environment')

    def test_default_processing_cache_lives_under_intent_and_results_are_versioned(self):
        self.repo.commit('Add module',{'module.py':'x=1\n'})
        self.init(providers={'fake':provider_spec()}, state=False)
        result = self.tuj('run','--provider','fake','--acknowledge-data-sharing','--backoff-base','0',state=False)
        self.assertTrue(result['import']['intents'])
        self.assertTrue((self.repo.path/'.intent/cache/trueup/meta.json').exists())
        status = self.repo.git('status','--porcelain','--untracked-files=all').stdout.decode()
        self.assertIn('.intent/records/',status)
        self.assertNotIn('.intent/cache/',status)
        self.assertFalse((self.repo.path/'.git/intent-trueup').exists())

    def test_import_never_changes_source_index_or_git_history(self):
        self.small_history()
        before = self.repo.head(), self.repo.git('ls-files','-s').stdout, self.repo.git('diff','HEAD').stdout
        self.init(providers={'fake':provider_spec()})
        self.run_ai()
        after = self.repo.head(), self.repo.git('ls-files','-s').stdout, self.repo.git('diff','HEAD').stdout
        self.assertEqual(before,after)

    def test_partial_failure_imports_supported_context_and_records_coverage(self):
        self.repo.commit('Good',{'a.py':'x=1\n'})
        self.repo.commit('Bad [ai-fail]',{'b.py':'x=1\n'})
        self.init(providers={'fake':provider_spec()})
        result = self.run_ai(ok=4)
        self.assertFalse(result['complete'])
        self.assertTrue(result['import']['intents'])
        store = self.canonical()
        event = store.events[store.manifest['events'][-1]]
        coverage = json.loads(event['verification'][0]['result'])
        self.assertFalse(coverage['complete'])
        self.assertEqual(coverage['ai_analysis']['failed'],1)
        self.assertTrue(self.query('explain','a.py:1')['historical'])

    def test_dry_run_has_no_canonical_records_and_no_ai_only_inspects(self):
        self.repo.commit('Initial',{'a.py':'1\n'})
        self.init(providers={'fake':provider_spec()})
        self.run_ai('--dry-run')
        self.assertFalse((self.repo.path/'.intent/config.json').exists())
        self.tuj('run','--no-ai','--network','off')
        self.assertFalse((self.repo.path/'.intent/config.json').exists())

    def test_dirty_source_refuses_import_without_losing_analysis(self):
        self.repo.commit('Initial',{'a.py':'1\n'})
        self.init(providers={'fake':provider_spec()})
        self.repo.write('a.py','2\n')
        result = self.run_ai(ok=4)
        self.assertIn('clean source worktree',result['import_error'])
        self.assertFalse(result['complete'])
        self.assertTrue(self.graph().nodes)
        self.repo.git('checkout','--','a.py')
        result = self.run_ai()
        self.assertTrue(result['import']['intents'])

    def test_interrupted_canonical_publish_recovers_without_duplicate_records(self):
        self.repo.commit('Initial',{'a.py':'1\n'})
        self.init(providers={'fake':provider_spec()})
        fired = []
        def crash(name):
            if name=='import:after_record_write' and not fired:
                fired.append(True)
                raise RuntimeError('simulated crash')
        with mock.patch.object(importer,'fault',side_effect=crash):
            with self.assertRaises(RuntimeError): self.run_ai()
        self.assertTrue((self.repo.path/'.intent/drafts/trueup-import.json').exists())
        result = self.run_ai()
        self.assertTrue(result['complete'])
        store = self.canonical()
        self.assertEqual(len(store.intents),1)
        self.assertFalse(store.errors)
        self.assertFalse((self.repo.path/'.intent/drafts/trueup-import.json').exists())

    def test_existing_manual_records_and_baseline_are_immutable(self):
        self.repo.commit('Initial',{'a.py':'1\n'})
        cli.initialize(self.repo.path,no_hooks=True)
        self.repo.git('add','.intent')
        self.repo.git('commit','-qm','adopt')
        cli.capture(self.repo.path,argparse.Namespace(key='manual',kind='manual',description='Human reason',rationale='Existing context',provenance='unknown'))
        intent=cli.add_intent(self.repo.path,argparse.Namespace(command='intent-create',event='manual',key='manual',status='active',kind='requirement',severity=None,
            title='Manual intent',description='Keep a stable',rationale='Human supplied',requirement=[],constraint=[],related=[]))
        cli.declare(self.repo.path,argparse.Namespace(event='manual',intent=intent['id'],binding=None,action='introduce',provenance=None))
        cli.finalize(self.repo.path,argparse.Namespace(key='manual'))
        self.repo.git('add','.intent'); self.repo.git('commit','-qm','manual context')
        before = self.records()
        self.init(providers={'fake':provider_spec()})
        self.run_ai()
        for p,data in before.items():
            if p!='manifests/current.json': self.assertEqual(self.records()[p],data)

    def test_without_import_normal_commands_never_open_analysis_cache(self):
        self.repo.commit('Initial',{'a.py':'1\n'})
        cli.initialize(self.repo.path,no_hooks=True)
        self.assertNotIn('historical',self.query('explain','a.py:1'))
        with mock.patch('intentlib.trueup.store.Store.open',side_effect=AssertionError('must not read cache')):
            self.query('history','a.py:1')


class ImportValidation(TrueUpTest):
    import_results = True

    def prepared(self):
        self.repo.commit('Initial', {'a.py':'x=1\n'})
        cli.initialize(self.repo.path, no_hooks=True)
        self.repo.git('add','.intent')
        self.repo.git('commit','-qm','adopt')
        before = snapshot(self.repo.path, revision='HEAD')
        self.init(providers={'fake':provider_spec()})
        self.run_ai()
        after = snapshot(self.repo.path, working=True)
        return before, after, CanonicalStore(after)

    def change(self, entries, path, mutate):
        from intentlib.core import seal
        value = json.loads(entries[path][1])
        mutate(value)
        if 'content_hash' in value:
            value = seal(value)
        entries[path] = ('100644', canonical(value)+b'\n')

    def test_import_cannot_create_active_or_verified_bindings(self):
        before, after, store = self.prepared()
        binding = next(iter(store.current('binding').values()))
        path = f".intent/bindings/{binding['id']}/000001.json"
        for mutate in [lambda b:b.update(status='active'), lambda b:b.update(verification={'status':'verified','method':'AI','evidence':'AI says so'})]:
            proposed = dict(after)
            self.change(proposed,path,mutate)
            self.assertIn('HISTORICAL_IMPORT',{e['code'] for e in validate(proposed,before)})

    def test_import_must_be_explicit_unknown_provenance_and_metadata_only(self):
        before, after, store = self.prepared()
        event = next(e for e in store.events.values() if e['kind']=='retrospective-import')
        path = f".intent/events/{event['id']}.json"
        for mutate in [lambda e:e.update(kind='manual'), lambda e:e.update(provenance='known')]:
            proposed = dict(after)
            self.change(proposed,path,mutate)
            self.assertIn('HISTORICAL_IMPORT',{e['code'] for e in validate(proposed,before)})
        proposed = dict(after)
        proposed['a.py']=('100644',b'x=2\n')
        self.assertTrue(validate(proposed,before),'historical import cannot account for a new source edit')

    def test_generated_metadata_and_blob_snapshots_are_not_model_evidence(self):
        self.repo.commit('Real source',{'a.py':'x=1\n'})
        marker='ENCODED_PRIVATE_SOURCE_SNAPSHOT'
        sha=self.repo.commit('Record imported history', {'.intent/objects/fake.json':marker})
        self.init(providers={'fake':provider_spec()})
        self.run_ai('--dry-run')
        rec=self.store().commit(sha)
        self.assertEqual(rec['analysis']['status'],'no_intent_found')
        self.assertEqual(len(self.calls()),1,'metadata-only commits never invoke the model')
        from intentlib.trueup.gitscan import Repo, inspect_commit
        from intentlib.trueup.analysis import DEFAULT_LIMITS
        info=inspect_commit(Repo(self.repo.path),sha,DEFAULT_LIMITS)
        self.assertEqual(info['files'],[])
        self.assertIsNone(info.get('unattributed_patch'))

    def test_out_of_range_historical_model_target_is_recorded_as_a_gap(self):
        sha=self.repo.commit('Real source',{'a.py':'one\ntwo'})
        from intentlib.trueup.gitscan import Repo
        limitations=[]
        target=importer._binding_target(Repo(self.repo.path),{'commit':sha},'a.py',[1,999],1000,limitations)
        self.assertIsNone(target)
        self.assertTrue(limitations)
        self.assertIsNotNone(importer._binding_target(Repo(self.repo.path),{'commit':sha},'a.py',[1,2],1000,[]))

    def test_failed_import_is_visible_in_status_separately_from_analysis(self):
        self.repo.commit('Real source',{'a.py':'one'})
        self.init(providers={'fake':provider_spec()})
        self.repo.write('a.py','dirty')
        self.run_ai(ok=4)
        status=self.tuj('status')
        self.assertTrue(status['coverage']['analysis_complete'])
        self.assertEqual(status['import']['status'],'failed')
        self.assertFalse(status['complete'])


class CacheSafety(TrueUpTest):
    def test_repository_symlink_cannot_redirect_default_cache_writes(self):
        outside=self.tmp/'outside'
        outside.mkdir()
        (self.repo.path/'.intent').symlink_to(outside,target_is_directory=True)
        self.repo.commit('Initial',{'a.py':'one'})
        self.tu('init',state=False,ok=1)
        self.assertEqual(list(outside.iterdir()),[])

    def test_writer_lock_symlink_cannot_truncate_another_file(self):
        from intentlib.trueup.store import Store
        victim=self.tmp/'keep'
        victim.write_bytes(b'untouched')
        cache=self.tmp/'cache'
        cache.mkdir()
        (cache/'lock').symlink_to(victim)
        with self.assertRaises(TrueUpError): Store(cache).acquire()
        self.assertEqual(victim.read_bytes(),b'untouched')
