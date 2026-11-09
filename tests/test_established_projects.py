"""Independent normative tests from user WIP/established-project requirements.

No implementation or inherited tests were consulted. Public APIs/schema supplied
source-free by Coder/Validator. Real SQLite and local Git repos; no provider doubles.
Test stores are retained, never deleted. Expected project/document absence comes
from transient opening/discovery not constituting promotion or consent.
Later parent clarification permits automatic indexing of ordinary primary checkouts;
linked worktrees and explicitly marked transient clones remain unconditionally WIP.
"""
import json
import io
import os
from pathlib import Path
import subprocess
import tempfile
import functools
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import sys
import unittest

from kindex.config import Config
from kindex.store import Store
from kindex.ingest import scan_projects, scan_kin_files
from kindex.project_store import register_project_graph
from kindex.integrations import open_project_store
from kindex.vectors import enqueue_embedding


@contextmanager
def working_directory(path):
    original = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(original)


def isolated_cases(cls):
    """Ordinary pytest delegates each case to a sterile, network-denied interpreter."""
    for name, method in list(vars(cls).items()):
        if not name.startswith('test_'):
            continue
        def wrapped(self, _method=method, _name=name):
            if os.environ.get('KINDEX_BOUNDARY_ISOLATED_HOME'):
                return _method(self)
            runner = Path(__file__).with_name('independent_boundary_runner.py')
            result = subprocess.run(
                [sys.executable, str(runner), '-v', Path(__file__).stem + '.' + cls.__name__ + '.' + _name],
                env={'PATH': '/opt/homebrew/bin:/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'},
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        setattr(cls, name, functools.wraps(method)(wrapped))
    return cls


@isolated_cases

class EstablishedProjectBoundary(unittest.TestCase):
    def setUp(self):
        self.stores = []
        isolated = os.environ.get('KINDEX_BOUNDARY_ISOLATED_HOME')
        if not isolated:
            return
        self.assertEqual(Path(isolated).resolve(), Path.home().resolve())
        os.environ['KIN_NO_SCHEDULER_WRITES'] = '1'
        self.base = Path(tempfile.mkdtemp(prefix='boundary-case-', dir=isolated))

    def tearDown(self):
        for store in self.stores:
            store.close()

    def repo(self, relative, marker, transient=False, primary=True):
        root = self.base / relative
        root.mkdir(parents=True)
        if primary:
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
        (root / 'CLAUDE.md').write_text('# ' + marker + '\n\nProject-specific durable knowledge sentinel.\n')
        (root / '.kin').mkdir()
        (root / '.kin' / 'config').write_text('name: ' + marker + '\ndescription: Independent project fixture\ndomains: [testing]\n')
        if transient:
            (root / '.kindex-no-index').write_text('arbitrary marker content; presence is the contract')
        return root

    def central(self, parent, name="central"):
        config = Config(data_dir=str(self.base / name), project_dirs=[str(parent)])
        store = Store(config)
        self.stores.append(store)
        return config, store

    def local(self, root):
        store = open_project_store({'project_path': str(root), 'session_id': 'independent-test', 'agent': 'independent-tester'})
        self.stores.append(store)
        return store

    def durable(self, store):
        return store.all_nodes(node_type='project') + store.all_nodes(node_type='document')

    def test_discovery_is_not_promotion(self):
        """Falsifier: ordinary-name unapproved repo becomes project/document knowledge."""
        root = self.repo('clients/stable-office', 'Transient ordinary-name workspace', transient=True)
        from kindex.project_store import registered_project_graphs
        os.environ['KIN_NO_SCHEDULER_WRITES'] = '0'
        registry_before = registered_project_graphs()
        local = self.local(root)
        local.add_node(node_id='local-transient', title='Transient task context', content='Still available locally', node_type='concept')
        self.assertEqual(local.get_node('local-transient')['content'], 'Still available locally')
        config, store = self.central(root.parent)
        project_count = scan_projects(config, store)
        kin_count = scan_kin_files(config, store)
        self.assertEqual(self.durable(store), [])
        self.assertEqual((project_count, kin_count), (0, 0))
        self.assertEqual(registered_project_graphs(), registry_before)
        # A canonical-looking entry must not import bytes resolved into a protected source.
        alias = self.repo('clients/canonical-alias', 'Canonical alias to protected files')
        for relative in ('CLAUDE.md', '.kin/config'):
            entry = alias / relative
            entry.rename(entry.with_name(entry.name + '.saved'))
            entry.symlink_to(root / relative)
        self.assertEqual(scan_projects(config, store), 0)
        self.assertEqual(scan_kin_files(config, store), 0)
        self.assertEqual(self.durable(store), [])
        self.assertEqual(registered_project_graphs(), registry_before)

    def test_legacy_registration_does_not_consent_or_destroy_data(self):
        """Falsifier: legacy observation enables maintenance or migration loses local state."""
        from kindex.project_store import project_is_established, project_knowledge_state
        root = self.repo('legacy/service', 'Legacy observed workspace', primary=False)
        data = root / '.kin' / 'local' / 'kindex'
        config = Config(data_dir=str(data))
        store = Store(config)
        self.stores.append(store)
        store.add_node(node_id='legacy-fact', title='Existing retained fact', content='Must survive policy adoption', node_type='concept')
        enqueue_embedding(store, 'legacy-fact')
        queue_before = store.get_meta('embed.queue')
        self.assertTrue(queue_before, 'fixture must contain pending queue metadata')
        from kindex.project_store import registered_project_graphs
        registry_before = registered_project_graphs()
        os.environ['KIN_NO_SCHEDULER_WRITES'] = '0'
        register_project_graph(root, data)
        self.assertNotEqual(registered_project_graphs(), registry_before, 'fixture must really contain a legacy observation')
        self.assertEqual(project_knowledge_state(root)['state'], 'wip')
        self.assertFalse(project_is_established(root, data))
        config2, central = self.central(root.parent)
        self.assertEqual(scan_projects(config2, central), 0)
        self.assertEqual(scan_kin_files(config2, central), 0)
        self.assertEqual(self.durable(central), [])
        self.assertIsNotNone(store.get_node('legacy-fact'))
        self.assertEqual(store.get_meta('embed.queue'), queue_before)

    def test_opt_in_works_independent_of_directory_names_and_exact_binding(self):
        """Falsifier: folder heuristic rejects consent or approval leaks to sibling/store."""
        from kindex.project_store import set_project_knowledge_state, project_is_established
        root = self.repo('factory/worktrees/run-123', 'Explicit established project')
        sibling = self.repo('factory/worktrees/run-124', 'Unapproved sibling')
        child = self.repo('factory/worktrees/run-123/child', 'Unapproved nested project')
        data = root / '.kin' / 'local' / 'kindex'
        set_project_knowledge_state(root, 'wip')
        set_project_knowledge_state(sibling, 'wip')
        set_project_knowledge_state(child, 'wip')
        set_project_knowledge_state(root, 'established')
        self.assertTrue(project_is_established(root, data))
        self.assertFalse(project_is_established(sibling))
        self.assertFalse(project_is_established(child))
        self.assertFalse(project_is_established(root, self.base / 'different-storage'))
        config, store = self.central(root.parent)
        self.assertGreater(scan_projects(config, store), 0, 'explicit opt-in must permit CLAUDE ingestion')
        self.assertIn('Project-specific durable knowledge sentinel', json.dumps(self.durable(store)))
        kin_config, kin_store = self.central(root.parent, 'kin-central')
        self.assertGreater(scan_kin_files(kin_config, kin_store), 0, 'explicit opt-in must permit .kin ingestion')
        self.assertTrue(self.durable(kin_store))
        self.assertEqual({str(Path(n['extra']['path']).resolve()) for n in kin_store.all_nodes(node_type='project')}, {str(root.resolve())})
        nodes = self.durable(store) + self.durable(kin_store)
        self.assertTrue(nodes, 'opt-in must persist usable knowledge')
        serialized = json.dumps(nodes)
        self.assertNotIn('Unapproved sibling', serialized)
        self.assertNotIn('Unapproved nested project', serialized)

    def test_local_memory_survives_open_establish_revoke_reopen(self):
        """Falsifier: local writes unavailable, implicit promotion, or revocation deletes state."""
        from kindex.project_store import set_project_knowledge_state, project_is_established
        root = self.repo('ordinary-repo', 'Local context workspace')
        set_project_knowledge_state(root, 'wip')
        store = self.local(root)
        self.assertFalse(project_is_established(root))
        self.assertEqual(self.durable(store), [])
        store.add_node(node_id='task-memory', title='Task-local discovery', content='Retained task context', node_type='concept')
        enqueue_embedding(store, 'task-memory')
        queue_before = store.get_meta('embed.queue')
        self.assertTrue(queue_before, 'fixture must contain a real queued node')
        self.assertEqual(store.get_node('task-memory')['content'], 'Retained task context')
        set_project_knowledge_state(root, 'established')
        self.assertTrue(project_is_established(root))
        set_project_knowledge_state(root, 'wip')
        self.assertFalse(project_is_established(root))
        reopened = self.local(root)
        self.assertEqual(reopened.get_node('task-memory')['content'], 'Retained task context')
        self.assertEqual(reopened.get_meta('embed.queue'), queue_before)
        self.assertEqual(self.durable(reopened), [])

    def test_revoked_maintenance_preserves_legitimate_pending_queue(self):
        """Falsifier: automatic maintenance drains/clears a revoked project's queued node."""
        from kindex.daemon import _project_maintenance
        from kindex.project_store import set_project_knowledge_state
        root = self.repo('maintenance-project', 'Maintenance workspace')
        data = root / '.kin' / 'local' / 'kindex'
        config = Config(data_dir=str(data), embedding={'provider': 'voyage'})
        config._project_path = root
        store = Store(config)
        self.stores.append(store)
        store.add_node(node_id='pending-fact', title='Pending retained knowledge', content='Queue must survive revocation', node_type='concept')
        enqueue_embedding(store, 'pending-fact')
        before = store.get_meta('embed.queue')
        self.assertTrue(before)
        set_project_knowledge_state(root, 'established')
        set_project_knowledge_state(root, 'wip')
        result = _project_maintenance(config, store)
        self.assertEqual(result.get('paid_maintenance_skipped'), 'project_not_established')
        self.assertEqual(store.get_meta('embed.queue'), before)
        self.assertEqual(store.get_node('pending-fact')['content'], 'Queue must survive revocation')

    def test_session_ingestion_requires_explicit_project_consent(self):
        """Falsifier: WIP/unknown/nested WIP session text enters durable automatic ingestion."""
        from kindex.ingest import scan_codex_sessions
        from kindex.project_store import set_project_knowledge_state
        root = self.repo('session-project', 'Session workspace')
        nested = root / 'temporary-area'
        nested.mkdir()
        session_dir = self.base / 'codex' / 'sessions' / '2026' / '11' / '09'
        session_dir.mkdir(parents=True)
        def transcript(name, cwd):
            metadata = {'id': name}
            if cwd is not None:
                metadata['cwd'] = str(cwd)
            records = [
                {'type': 'session_meta', 'payload': metadata},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Python and SQLite implement a Knowledge Graph with a Transaction Boundary. The "transaction boundary" preserves durable knowledge through explicit transactions, reliable storage, and carefully scoped local session boundaries.'}]}}
            ]
            (session_dir / (name + '.jsonl')).write_text('\n'.join(json.dumps(r) for r in records) + '\n')
        set_project_knowledge_state(root, 'wip')
        transcript('known', root)
        transcript('unknown', None)
        config = Config(data_dir=str(self.base / 'session-store'), codex_dir=str(self.base / 'codex'))
        store = Store(config)
        self.stores.append(store)
        self.assertEqual(scan_codex_sessions(config, store), 0)
        self.assertEqual(store.all_nodes(), [])
        set_project_knowledge_state(root, 'established')
        self.assertGreater(scan_codex_sessions(config, store), 0)
        before = store.all_nodes()
        self.assertTrue(before)
        self.assertEqual({str(Path(n['extra']['cwd']).resolve()) for n in store.all_nodes(node_type='session')}, {str(root.resolve())})
        set_project_knowledge_state(nested, 'wip')
        transcript('nested-wip', nested)
        marked = root / 'unclassified-child'
        deep = marked / 'deep'
        deep.mkdir(parents=True)
        (marked / '.kindex-no-index').write_text('')
        transcript('marked-child', deep)
        self.assertEqual(scan_codex_sessions(config, store), 0)
        self.assertEqual(store.all_nodes(), before)

    def test_linked_git_worktree_cannot_be_established(self):
        """Falsifier: real linked worktree or its subdirectory obtains indexing consent."""
        from kindex.project_store import set_project_knowledge_state, project_is_established
        source = Path(os.environ.get('CANDIDATE_SRC', str(Path(__file__).parents[1] / 'src'))).parent
        clone = self.base / 'canonical-clone'
        linked = self.base / 'branch-checkout'
        subprocess.run(['git', 'clone', '--quiet', '--no-hardlinks', '--no-checkout', str(source), str(clone)], check=True)
        subprocess.run(['git', '-C', str(clone), 'worktree', 'add', '--quiet', '--detach', str(linked), 'HEAD'], check=True)
        child = linked / 'ordinary-subdirectory'
        child.mkdir()
        for root in (linked, child):
            with self.assertRaises(ValueError):
                set_project_knowledge_state(root, 'established')
            self.assertFalse(project_is_established(root))

    def test_primary_checkout_auto_indexing_remains_available(self):
        """Latest parent clarification: primary checkout is canonical unless explicitly WIP.
        Falsifier: blanket opt-in requirement breaks ordinary canonical repositories.
        """
        from kindex.project_store import project_is_established
        root = self.repo('ordinary-primary', 'Canonical primary repository')
        self.assertTrue(project_is_established(root))
        config, store = self.central(root.parent)
        self.assertGreater(scan_projects(config, store), 0)
        self.assertIn('Project-specific durable knowledge sentinel', json.dumps(self.durable(store)))
        kin_config, kin_store = self.central(root.parent, 'primary-kin')
        self.assertGreater(scan_kin_files(kin_config, kin_store), 0)
        self.assertEqual({str(Path(n['extra']['path']).resolve()) for n in kin_store.all_nodes(node_type='project')}, {str(root.resolve())})

    def test_transient_clone_marker_cannot_be_overridden_by_establish(self):
        """Falsifier: Factory independent clone marker is treated as promotable canonical code."""
        from kindex.project_store import set_project_knowledge_state, project_is_established
        root = self.repo('innocent-location', 'Factory temporary clone', transient=True)
        child = root / 'nested-workspace'
        child.mkdir()
        for path in (root, child):
            self.assertFalse(project_is_established(path))
            with self.assertRaises(ValueError):
                set_project_knowledge_state(path, 'established')
            self.assertFalse(project_is_established(path))

    def test_explicit_file_ingestion_cannot_import_transient_sources(self):
        """Strict requirement: explicit indexing cannot bypass hard transient boundaries.
        Falsifier: transient document is created or previously indexed content is refreshed.
        """
        from kindex.adapters.files import ingest_directory, scan_registered_files
        root = self.repo('canonical-content', 'Canonical file source')
        allowed = root / 'knowledge.md'
        allowed.write_text('CANONICAL-CONTENT-SENTINEL: retained, indexable project knowledge.')
        nested = root / 'temporary-child'
        nested.mkdir()
        (nested / '.kindex-no-index').write_text('')
        (nested / 'hidden.md').write_text('TRANSIENT-CONTENT-SENTINEL: must never be imported.')
        config, store = self.central(root.parent, 'content-target')
        with working_directory(root):
            self.assertGreater(ingest_directory(store, root, extensions=['.md']), 0)
            documents = store.all_nodes(node_type='document')
            self.assertIn('CANONICAL-CONTENT-SENTINEL', json.dumps(documents))
            self.assertNotIn('TRANSIENT-CONTENT-SENTINEL', json.dumps(documents))
            before = {n['id']: n['content'] for n in store.all_nodes()}
            queue_before = store.get_meta('embed.queue')
            with self.assertRaises(ValueError):
                ingest_directory(store, nested, extensions=['.md'])
            (root / '.kindex-no-index').write_text('present')
            allowed.write_text('ALTERED-TRANSIENT-CONTENT: refreshing this is forbidden.')
            with self.assertRaises(ValueError):
                ingest_directory(store, root, extensions=['.md'])
            self.assertEqual(scan_registered_files(store), 0)
            self.assertEqual({n['id']: n['content'] for n in store.all_nodes()}, before)
            self.assertEqual(store.get_meta('embed.queue'), queue_before)

    def test_explicit_content_ingestion_cannot_write_transient_target(self):
        """Falsifier: canonical source is imported into a hard-transient content index."""
        from kindex.adapters.files import ingest_directory
        root = self.repo('canonical-source', 'Canonical allowed input')
        (root / 'knowledge.md').write_text('CANONICAL-INPUT-SENTINEL with stable content for indexing.')
        target = self.repo('temporary-destination', 'Transient target', transient=True)
        store = Store(Config(data_dir=str(target / '.kin' / 'local' / 'kindex')))
        self.stores.append(store)
        store.add_node(node_id='local-note', title='Local note', content='Local capture remains usable', node_type='concept')
        before = {n['id']: n['content'] for n in store.all_nodes()}
        queue_before = store.get_meta('embed.queue')
        from kindex.ingest import scan_sessions, scan_codex_sessions
        from kindex.daemon import incremental_ingest
        scan_config = Config(data_dir=str(self.base / 'canonical-scan-context'), project_dirs=[str(root.parent)])
        with working_directory(root):
            with self.assertRaises(ValueError):
                ingest_directory(store, root, extensions=['.md'])
            for scanner in (scan_projects, scan_kin_files, scan_sessions, scan_codex_sessions, incremental_ingest):
                arguments = (scan_config, store, '2000-01-01T00:00:00') if scanner is incremental_ingest else (scan_config, store)
                with self.subTest(scanner=scanner.__name__):
                    with self.assertRaises(ValueError):
                        scanner(*arguments)
                    self.assertEqual({n['id']: n['content'] for n in store.all_nodes()}, before)
                    self.assertEqual(store.get_meta('embed.queue'), queue_before)
        self.assertEqual({n['id']: n['content'] for n in store.all_nodes()}, before)
        self.assertEqual(store.get_meta('embed.queue'), queue_before)
        self.assertEqual(store.get_node('local-note')['content'], 'Local capture remains usable')

    def test_explicit_embedding_cli_refuses_transient_writes_preserving_reads_and_export(self):
        """Falsifier: explicit embedding changes transient queue or disables local/read/export use."""
        from kindex.cli import build_parser
        root = self.repo('embedding-project', 'Embedding boundary fixture')
        data = root / '.kin' / 'local' / 'kindex'
        config_file = self.base / 'embedding-config.json'
        config_file.write_text(json.dumps({'data_dir': str(data)}))
        store = Store(Config(data_dir=str(data)))
        self.stores.append(store)
        store.add_node(node_id='canonical-queued', title='Canonical embedding candidate', content='Stable canonical content', node_type='concept', queue_embedding=False)
        self.assertFalse(store.get_meta('embed.queue'))
        def parsed(command):
            return build_parser().parse_args(command + ['--config', str(config_file), '--json'])
        def call(args):
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                args.func(args)
            return output.getvalue()
        with working_directory(root):
            self.assertTrue(call(parsed(['embed', 'enqueue'])).strip())
            seeded_queue = store.get_meta('embed.queue')
            self.assertTrue(seeded_queue)
            self.assertIn('canonical-queued', seeded_queue)
            (root / '.kindex-no-index').write_text('')
            store.add_node(node_id='local-capture', title='Local capture must stay visible', content='Useful task-local context', node_type='concept', queue_embedding=False)
            before = {n['id']: n['content'] for n in store.all_nodes()}
            for command in (['embed'], ['embed', 'enqueue'], ['embed', 'drain'], ['embed', 'reindex']):
                args = parsed(command)
                with self.subTest(command=command):
                    with self.assertRaises(SystemExit) as refusal:
                        call(args)
                    self.assertEqual(refusal.exception.code, 2)
                    self.assertEqual(store.get_meta('embed.queue'), seeded_queue)
                    self.assertEqual({n['id']: n['content'] for n in store.all_nodes()}, before)
            for command in (['embed', 'status'], ['embed', 'plan'], ['embed', 'calibrate', '--show']):
                with self.subTest(read_command=command):
                    self.assertTrue(call(parsed(command)).strip())
            artifact = root / '.kin' / 'index.json'
            self.assertFalse(artifact.exists())
            call(parsed(['index', '--output-dir', str(root), '--no-merge-driver']))
            exported = json.loads(artifact.read_text())
            # Public exporter selects active repo code nodes, not arbitrary local notes.
            self.assertIsInstance(exported, dict)
            self.assertIsInstance(exported.get('nodes'), list)
            self.assertEqual(store.get_node('local-capture')['content'], 'Useful task-local context')
            self.assertEqual(store.get_meta('embed.queue'), seeded_queue)

    def test_explicit_ingest_cli_and_pipeline_preserve_boundary(self):
        """Falsifier: CLI or shared adapter pipeline bypasses the direct file source boundary."""
        from kindex.cli import build_parser
        from kindex.adapters.files import FilesAdapter
        from kindex.adapters.pipeline import run_adapter, IngestConfig
        root = self.repo('canonical-pipeline-source', 'Canonical pipeline source')
        marked = self.repo('transient-pipeline-source', 'Transient pipeline source', transient=True)
        (marked / 'private.md').write_text('EXCLUDED-PIPELINE-CONTENT must not become durable document knowledge.')
        config, store = self.central(root.parent, 'pipeline-target')
        config_file = self.base / 'pipeline-config.json'
        config_file.write_text(json.dumps({'data_dir': str(config.data_dir)}))
        before = {n['id']: n['content'] for n in store.all_nodes()}
        queue_before = store.get_meta('embed.queue')
        with working_directory(root):
            args = build_parser().parse_args(['ingest', 'files', '--directory', str(marked), '--config', str(config_file), '--json'])
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                with self.assertRaises(SystemExit) as refusal:
                    args.func(args)
            self.assertEqual(refusal.exception.code, 2)
            result = run_adapter(FilesAdapter(), store, IngestConfig(), directory=marked)
            self.assertTrue(result.errors, 'pipeline must signal source refusal')
            self.assertEqual({n['id']: n['content'] for n in store.all_nodes()}, before)
            self.assertEqual(store.get_meta('embed.queue'), queue_before)

    def test_mixed_registered_sources_preserve_protected_excerpt(self):
        """Falsifier: canonical sibling refresh erases a previously indexed transient excerpt."""
        from kindex.adapters.files import scan_registered_files
        first = self.repo('first-source', 'First canonical source')
        second = self.repo('second-source', 'Second canonical source')
        file_one = first / 'retained.md'
        file_two = second / 'changing.md'
        file_one.write_text('FIRST-EXCERPT-SENTINEL: preserve this prior knowledge.')
        file_two.write_text('SECOND-EXCERPT-SENTINEL: canonical sibling knowledge.')
        config, store = self.central(first.parent, 'mixed-content-target')
        store.add_node(node_id='mixed-document', title='Combined registered document', content='Original notes', node_type='document', extra={'file_paths': [str(file_one), str(file_two)]})
        with working_directory(first):
            self.assertGreater(scan_registered_files(store), 0)
            before = store.get_node('mixed-document')['content']
            self.assertIn('FIRST-EXCERPT-SENTINEL', before)
            self.assertIn('SECOND-EXCERPT-SENTINEL', before)
            queue_before = store.get_meta('embed.queue')
            (first / '.kindex-no-index').write_text('')
            file_two.write_text('CHANGED-CANONICAL-SIBLING: this must not cause prior protected content loss.')
            self.assertEqual(scan_registered_files(store), 0)
            self.assertEqual(store.get_node('mixed-document')['content'], before)
            self.assertEqual(store.get_meta('embed.queue'), queue_before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
