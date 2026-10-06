"""Real temporary Git commits; no network, push, fake release or Windows claim."""
from __future__ import annotations
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import ExitStack
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module
probe = load('verify_installed_recovery_r64')
spec = importlib.util.spec_from_file_location('ci_binding', ROOT / 'scripts/ci_source_binding.py')
ci = importlib.util.module_from_spec(spec); spec.loader.exec_module(ci)
safe = ci.safe


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


@unittest.skipUnless(shutil.which('git'), 'Git is required for real commit identity tests')
class RealGitBindingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='ci-flat-source-中文-')))
        self.local = self.directory / 'outside'; self.local.mkdir()
        self.root = self.directory / 'flat'; self.root.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.update(GITHUB_ACTIONS='', GITHUB_SHA='', GITHUB_REPOSITORY=ci.REPOSITORY['full_name'],
            GITHUB_REPOSITORY_ID=ci.REPOSITORY['id'], GITHUB_REPOSITORY_OWNER_ID=ci.REPOSITORY['owner_id'])
        self.stack.enter_context(patch.dict(os.environ, env, clear=True))
        files = {'package.json': encoded({'name':'fixture', 'version':'3.0.4'}),
            'BUILD_REVISION.txt': b'Source revision: stability-r94\n',
            '.gitattributes': b'* -text\n', 'backend/app/main.py': b'print("repaired")\n'}
        for name, data in files.items():
            self.put(self.root, name, data)
            self.put(self.local, name, data)
        manifest = {name: safe._sha256(data) for name, data in files.items()}
        manifest_bytes = encoded(manifest)
        self.put(self.root, ci.MANIFEST, manifest_bytes)
        marker = {'schema':2, 'mode':ci.MODE, 'representation':ci.REPRESENTATION,
            'repository':dict(ci.REPOSITORY), 'product_version':'3.0.4', 'source_revision':'stability-r94',
            'source_manifest_sha256':safe._sha256(manifest_bytes)}
        self.put(self.root, ci.MARKER, encoded(marker))
        self.git('init', '--quiet')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Local provenance unit test')
        self.git('config', 'core.autocrlf', 'false')
        self.commit()

    @staticmethod
    def put(root, name, data):
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.STDOUT).decode().strip()

    def commit(self):
        self.git('add', '--all'); self.git('commit', '--quiet', '-m', 'Synthetic local fixture only')
        self.head = self.git('rev-parse', 'HEAD')
        os.environ['GITHUB_ACTIONS'] = 'true'; os.environ['GITHUB_SHA'] = self.head

    def reject(self, pattern):
        with self.assertRaisesRegex(safe.SourceBindingError, pattern):
            ci.verify_ci_source_binding(self.root)

    def test_real_commit_is_bound_to_every_manifest_path_and_blob(self):
        proof = ci.verify_ci_source_binding(self.root)
        self.assertEqual(self.head, proof['source_commit'])
        self.assertEqual(2, proof['schema'])
        self.assertEqual(ci.REPOSITORY, proof['repository'])
        self.assertEqual(ci.REPRESENTATION, proof['representation'])
        self.assertFalse(any('archive' in field or 'carrier' in field for field in proof))
        self.assertEqual('flat-git-ci', proof['mode'])
        self.assertEqual(len(self.git('ls-files').splitlines()), proof['tracked_files_verified'])
        self.assertEqual(self.git('rev-parse', 'HEAD^{tree}'), proof['git_tree'])
        self.assertEqual(proof, ci.verify_ci_source_binding(self.root))

    def test_wrong_real_commit_and_unsupported_execution_modes_rejected(self):
        prior = self.head
        self.git('commit', '--allow-empty', '--quiet', '-m', 'Second real synthetic commit')
        self.assertNotEqual(prior, self.git('rev-parse', 'HEAD'))
        self.reject('Git HEAD differs')
        for values in ({'GITHUB_ACTIONS':''}, {'GITHUB_SHA':''}, {'GITHUB_SHA':'not-a-sha'}):
            with patch.dict(os.environ, values):
                self.reject('CI mode requires')

    def test_missing_own_git_cannot_adopt_ancestor_repository(self):
        nested = self.root / 'nested'; shutil.copytree(self.root, nested, ignore=shutil.ignore_patterns('.git', 'nested'))
        with self.assertRaisesRegex(safe.SourceBindingError, 'own Git checkout'):
            ci.verify_ci_source_binding(nested)

    def test_dirty_bytes_and_dirty_index_rejected(self):
        self.put(self.root, 'backend/app/main.py', b'print("dirty")\n')
        self.reject('checksum mismatch|Git index differs')
        self.git('add', 'backend/app/main.py'); self.reject('checksum mismatch|Git index differs')

    def test_assume_unchanged_index_cannot_hide_changed_blob(self):
        self.git('update-index', '--assume-unchanged', 'backend/app/main.py')
        self.put(self.root, 'backend/app/main.py', b'print("hidden dirty")\n')
        self.reject('checksum mismatch|differs from Git blob')

    def test_ignored_or_untracked_executable_rejected_including_root(self):
        for name in ('extra.exe', 'backend/app/extra.py', 'scripts/build/extra.js', 'extras/extensionless'):
            self.put(self.root, name, b'#!/bin/sh\n')
            self.reject('Unsealed executable')
            (self.root / name).unlink()
        self.put(self.root, '.git/info/exclude', b'ignored.exe\n')
        self.put(self.root, 'ignored.exe', b'ignored binary')
        self.reject('Unsealed executable')

    def test_known_build_outputs_remain_allowed_but_not_source_named_build(self):
        for name in ('installer-output/build.log', 'build/browsers/chrome.exe', 'backend/dist/core.exe',
                     'dist-electron/main.js', 'dist/collector_core/collector_core.exe', 'collector_core.spec',
                     'backend/app/__pycache__/module.pyc'):
            self.put(self.root, name, b'fixture output')
        ci.verify_ci_source_binding(self.root)
        self.put(self.root, 'backend/app/build/sneak.exe', b'fixture unsealed source')
        self.reject('Unsealed executable')

    def test_checkout_preserves_exact_lf_and_bom_despite_autocrlf(self):
        self.git('config', 'core.autocrlf', 'true')
        target = self.root / 'backend/app/main.py'; expected = target.read_bytes()
        target.unlink(); self.git('checkout-index', '--force', '--', 'backend/app/main.py')
        self.assertEqual(expected, target.read_bytes())
        ci.verify_ci_source_binding(self.root)
        self.git('update-index', '--assume-unchanged', 'backend/app/main.py')
        for altered in (expected.replace(b'\n', b'\r\n'), b'\xef\xbb\xbf' + expected):
            target.write_bytes(altered)
            self.reject('checksum mismatch|differs from Git blob')
        target.write_bytes(expected)

    def test_info_attributes_cannot_hide_checkout_eol_rewriting(self):
        self.put(self.root, '.git/info/attributes', b'backend/app/main.py text eol=crlf\n')
        target = self.root / 'backend/app/main.py'; target.unlink()
        self.git('checkout-index', '--force', '--', 'backend/app/main.py')
        self.assertIn(b'\r\n', target.read_bytes())
        self.assertEqual('', self.git('diff', '--name-only'))
        self.git('update-index', '--assume-unchanged', 'backend/app/main.py')
        self.reject('checksum mismatch|differs from Git blob')

    def test_clean_smudge_filters_cannot_hide_changed_working_bytes(self):
        helper = self.root / '.git/filter_fixture.py'
        helper.write_text('import sys\ndata=sys.stdin.buffer.read()\n'
            'data=data.replace(b"FILTERED",b"repaired") if sys.argv[1]=="clean" else data.replace(b"repaired",b"FILTERED")\n'
            'sys.stdout.buffer.write(data)\n')
        command = '"' + sys.executable + '" "' + str(helper) + '" '
        self.git('config', 'filter.fixture.clean', command + 'clean')
        self.git('config', 'filter.fixture.smudge', command + 'smudge')
        self.put(self.root, '.git/info/attributes', b'backend/app/main.py filter=fixture\n')
        target = self.root / 'backend/app/main.py'; target.unlink()
        self.git('checkout-index', '--force', '--', 'backend/app/main.py')
        self.assertIn(b'FILTERED', target.read_bytes())
        self.assertEqual('', self.git('diff', '--name-only'))
        self.reject('checksum mismatch|differs from Git blob')

    def configure_sentinels(self):
        sentinel = self.root / '.git/EXTERNAL_HELPER_EXECUTED'
        helper = self.root / '.git/sentinel_helper.py'
        helper.write_text('from pathlib import Path\nimport sys\n'
            + 'Path(' + repr(str(sentinel)) + ').write_text("executed")\n'
            + 'sys.stdout.buffer.write(sys.stdin.buffer.read() if len(sys.argv)>1 and sys.argv[1]=="filter" else b"\\0")\n')
        command = '"' + sys.executable + '" "' + str(helper) + '" '
        self.git('config', 'core.fsmonitor', command + 'fsmonitor')
        self.git('config', 'filter.audit.clean', command + 'filter')
        self.git('config', 'filter.audit.smudge', command + 'filter')
        self.put(self.root, '.git/info/attributes', b'backend/app/main.py filter=audit\n')
        return sentinel

    def test_configured_fsmonitor_and_filters_never_execute_on_success_or_rejection(self):
        sentinel = self.configure_sentinels()
        index_before = (self.root / '.git/index').read_bytes()
        ci.verify_ci_source_binding(self.root)
        self.assertFalse(sentinel.exists(), 'Read-only verification invoked a configured helper')
        self.assertEqual(index_before, (self.root / '.git/index').read_bytes())
        self.put(self.root, 'backend/app/main.py', b'changed working bytes')
        self.reject('checksum mismatch')
        self.assertFalse(sentinel.exists(), 'Rejected source still invoked a configured helper')
        self.assertEqual(index_before, (self.root / '.git/index').read_bytes())

    def test_staged_blob_change_rejects_without_running_configured_helpers(self):
        target = 'backend/app/main.py'
        original = (self.root / target).read_bytes()
        self.put(self.root, target, b'staged change'); self.git('add', target)
        self.put(self.root, target, original)
        sentinel = self.configure_sentinels()
        self.reject('Git index differs')
        self.assertFalse(sentinel.exists())

    def test_conflicted_index_rejects_all_nonzero_stages(self):
        name = 'backend/app/main.py'; oid = self.git('rev-parse', 'HEAD:' + name)
        self.git('update-index', '--force-remove', '--', name)
        subprocess.run(['git', '-C', str(self.root), 'update-index', '--index-info'],
            input=('100644 ' + oid + ' 1\t' + name + '\n100644 ' + oid + ' 2\t' + name + '\n').encode(),
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        sentinel = self.configure_sentinels()
        self.reject('Unmerged Git index entry')
        self.assertFalse(sentinel.exists())

    def test_staged_path_addition_and_deletion_are_not_hidden_by_working_bytes(self):
        self.put(self.root, 'extra.txt', b'staged new path'); self.git('add', 'extra.txt')
        self.reject('Git index differs')
        self.git('reset', '--quiet', 'HEAD', '--', 'extra.txt'); (self.root / 'extra.txt').unlink()
        self.git('update-index', '--force-remove', '--', 'backend/app/main.py')
        self.reject('Git index differs')

    def test_staged_executable_mode_must_match_head(self):
        self.git('update-index', '--chmod=+x', 'backend/app/main.py')
        self.reject('Git index differs')

    @unittest.skipIf(os.name == 'nt', 'Windows does not preserve Unix executable mode')
    def test_unix_working_executable_mode_ignores_core_filemode_bypass(self):
        self.git('config', 'core.filemode', 'false')
        target = self.root / 'backend/app/main.py'
        target.chmod(0o755)
        self.reject('executable mode differs')
        target.chmod(0o644); ci.verify_ci_source_binding(self.root)

    def test_missing_promisor_tree_cannot_execute_configured_transport(self):
        tree = self.git('rev-parse', 'HEAD:backend')
        sentinel = self.root / '.git/TRANSPORT_EXECUTED'
        helper = self.root / '.git/transport_helper.py'
        helper.write_text('from pathlib import Path\nPath(' + repr(str(sentinel)) + ').write_text("executed")\n')
        self.git('config', 'extensions.partialClone', 'audit')
        self.git('config', 'remote.audit.promisor', 'true')
        self.git('config', 'remote.audit.url', 'ext::' + sys.executable + ' ' + str(helper))
        self.git('config', 'protocol.allow', 'always')
        self.git('config', 'protocol.ext.allow', 'always')
        (self.root / '.git/objects' / tree[:2] / tree[2:]).unlink()
        self.reject('Git source inspection failed')
        self.assertFalse(sentinel.exists(), 'Missing object triggered a configured transport')

    def test_every_git_subprocess_uses_internal_read_only_controls(self):
        original = ci.subprocess.run
        with patch.object(ci.subprocess, 'run', wraps=original) as run:
            ci.verify_ci_source_binding(self.root)
        self.assertGreater(len(run.call_args_list), 1)
        for call in run.call_args_list:
            command = call.args[0]
            self.assertIn('--no-pager', command)
            self.assertIn('--no-replace-objects', command)
            self.assertIn('--no-optional-locks', command)
            self.assertIn('core.fsmonitor=false', command)
            self.assertIn('protocol.allow=never', command)
            self.assertIn('protocol.ext.allow=never', command)
            self.assertNotIn('status', command)
            environment = call.kwargs['env']
            self.assertEqual('1', environment['GIT_NO_LAZY_FETCH'])
            self.assertEqual('', environment['GIT_ALLOW_PROTOCOL'])
            self.assertEqual('0', environment['GIT_OPTIONAL_LOCKS'])
            self.assertEqual('0', environment['GIT_TERMINAL_PROMPT'])

    def test_caller_cannot_override_internal_read_only_git_environment(self):
        for key, value in (('GIT_NO_LAZY_FETCH','0'), ('GIT_ALLOW_PROTOCOL','ext'), ('GIT_OPTIONAL_LOCKS','1')):
            with self.subTest(key=key), patch.dict(os.environ, {key:value}), patch.object(ci.subprocess, 'run') as run:
                self.reject('Unsupported Git environment override')
                run.assert_not_called()

    def test_manifest_tamper_rejected_even_if_committed(self):
        self.put(self.root, ci.MANIFEST, b'{}\n')
        self.reject('manifest digest mismatch')
        self.commit(); self.reject('manifest digest mismatch')

    def test_extra_committed_file_cannot_escape_manifest(self):
        self.put(self.root, 'extra.txt', b'new committed file'); self.commit()
        self.reject('Git blob paths differ')

    def test_removed_manifest_entry_cannot_remove_git_coverage(self):
        manifest = json.loads((self.root / ci.MANIFEST).read_text())
        del manifest['backend/app/main.py']
        data = encoded(manifest); self.put(self.root, ci.MANIFEST, data)
        marker = json.loads((self.root / ci.MARKER).read_text()); marker['source_manifest_sha256'] = safe._sha256(data)
        self.put(self.root, ci.MARKER, encoded(marker)); self.commit()
        self.reject('Git blob paths differ')

    def test_symlink_and_hardlink_source_rejected(self):
        target = self.root / 'backend/app/main.py'; target.unlink()
        try:
            target.symlink_to(self.local / 'backend/app/main.py')
        except (OSError, NotImplementedError):
            self.skipTest('Symlink creation unavailable')
        self.reject('Symlink|Git index differs')
        self.commit(); self.reject('Git symlinks/submodules')

    def test_hardlinked_source_rejected_even_with_matching_bytes(self):
        target = self.root / 'backend/app/main.py'; target.unlink()
        os.link(self.local / 'backend/app/main.py', target)
        self.reject('Hardlinked source')

    def test_untracked_symlink_and_generated_root_symlink_rejected(self):
        for name in ('extra.txt', 'build'):
            path = self.root / name
            try:
                path.symlink_to(self.local, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest('Symlink creation unavailable')
            self.reject('Symlink'); path.unlink()

    def test_inventory_rejects_directory_symlink_before_following_query(self):
        target = self.root / 'untracked-link'
        try:
            target.symlink_to(self.local, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Symlink creation unavailable')
        self._assert_no_follow_inventory_rejection(target)

    def test_inventory_rejects_windows_reparse_metadata_before_following_query(self):
        target = self.root / 'untracked-reparse'; target.mkdir()
        original_lstat = Path.lstat
        def reparse_metadata(path, *args, **kwargs):
            value = original_lstat(path, *args, **kwargs)
            if path == target:
                fields = {name: getattr(value, name) for name in dir(value) if name.startswith('st_')}
                fields['st_file_attributes'] = getattr(value, 'st_file_attributes', 0) | safe.REPARSE_POINT
                return SimpleNamespace(**fields)
            return value
        with patch.object(Path, 'lstat', reparse_metadata):
            self._assert_no_follow_inventory_rejection(target)

    def _assert_no_follow_inventory_rejection(self, target):
        original_dir, original_file, original_stat = Path.is_dir, Path.is_file, Path.stat
        def guarded_dir(path, *args, **kwargs):
            if path == target: raise AssertionError('FOLLOWING_DIRECTORY_QUERY_BEFORE_LINK_REJECTION')
            return original_dir(path, *args, **kwargs)
        def guarded_file(path, *args, **kwargs):
            if path == target: raise AssertionError('FOLLOWING_FILE_QUERY_BEFORE_LINK_REJECTION')
            return original_file(path, *args, **kwargs)
        def guarded_stat(path, *args, **kwargs):
            if path == target and kwargs.get('follow_symlinks', True):
                raise AssertionError('FOLLOWING_STAT_BEFORE_LINK_REJECTION')
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, 'is_dir', guarded_dir), patch.object(Path, 'is_file', guarded_file), \
                patch.object(Path, 'stat', guarded_stat):
            self.reject('Symlink/reparse')

    def test_git_environment_redirection_rejected(self):
        with patch.dict(os.environ, {'GIT_DIR':str(self.root / '.git')}):
            self.reject('Unsupported Git environment override')

    def test_version_template_cannot_be_confused_with_release_status(self):
        marker = json.loads((self.root / ci.MARKER).read_text()); marker['status'] = 'PASSED'
        self.put(self.root, ci.MARKER, encoded(marker)); self.commit(); self.reject('fields are missing or unexpected')

    def test_installed_probe_uses_the_same_real_flat_git_authority(self):
        spec = importlib.util.spec_from_file_location('installed_probe_ci_test', ROOT / 'scripts/verify_installed_recovery_r64.py')
        probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)
        with patch.object(probe, 'SOURCE_ROOT', self.root), \
                patch.object(probe, 'PROOF_FILES', ('backend/app/main.py',)), \
                patch.object(probe, 'load_sibling', return_value=ci):
            identity = probe.source_binding()
            self.assertEqual(self.head, identity['source_commit'])
            self.assertEqual(ci.verify_ci_source_binding(self.root), identity['source_provenance'])
            self.assertEqual({'backend/app/main.py'}, set(identity['proof_files_sha256']))
            self.put(self.root, 'backend/app/main.py', b'changed after verified receipt')
            with self.assertRaisesRegex(safe.SourceBindingError, 'checksum mismatch|Git index differs'):
                probe.source_binding()



    def make_proof(self):
        executable = self.root.parent / 'installed/desktop.exe'
        executable.parent.mkdir()
        executable.write_bytes(b'synthetic desktop')
        core = executable.parent / 'resources/backend/collector_core/collector_core.exe'
        core.parent.mkdir(parents=True)
        core.write_bytes(b'synthetic core')
        (executable.parent / 'resources/app.asar').write_bytes(b'synthetic asar')
        f = probe.fixture
        state = {'verified': True, 'no_submission': True, 'historical_receipts': 1,
            'protected_tables': len(f.PROTECTED), 'login_files': 8, 'protected_sha256': 'p', 'material_sha256': 'm'}
        proof = {**probe.source_binding(), 'verified': True, 'contract': f.CONTRACT, 'platform': 'win32',
            'synthetic': True, 'live_accounts_tested': False, 'user_data_touched': False,
            'desktop': {'pid': 11, 'executable': str(executable), 'sha256': safe._sha256(executable.read_bytes())},
            'core': {'pid': 12, 'parent_pid': 11, 'executable': str(core), 'sha256': safe._sha256(core.read_bytes())},
            'core_frozen': True, 'core_upgrade_manifest_sha256': 'a' * 64, 'seed_pid': 10,
            'seed_completed_perf_ns': 1, 'launch_perf_ns': 2, 'ready_perf_ns': 3,
            'chronology_clock': f.legacy.CHRONOLOGY_CLOCK, 'manifest_sha256': 'b' * 64,
            'input_database_sha256': 'c' * 64, 'nonce': 'd' * 64,
            'normal_shutdown': True, 'manifest_unchanged': True,
            'checks': dict.fromkeys(probe.FLAGS, True),
            'startup': {**state, 'posting_queue_revision': 0, 'withdrawal_audits': 0},
            'persisted_state': {**state, 'posting_queue_revision': 2, 'withdrawal_audits': 1},
            'api_calls': [{'path': '/api/posting/snapshot', 'action': None,
                'expected_status': 200, 'observed_status': None, 'transport': 'desktop-ipc'}],
            'fresh_review': {'id': f.POST, 'caption': f.CAPTION, 'asset_id': f.ASSET,
                'profile_id': f.PROFILES['posting'], 'expected_username': f.USERNAME, 'queue_revision': 1},
            'located_task': {'task_id': f.TASK, 'version': 7, 'status': 'paused',
                'dismissed': True, 'can_stop': True, 'window_ids': [f.PROFILES['collection']]},
            'safety_fence': 'invalid-synthetic-render-digest-before-PlaywrightWorker',
            'desktop_app_asar_sha256': safe._sha256(b'synthetic asar')}
        return proof, executable, core

    def test_public_receipt_requires_current_exact_provenance_and_all_existing_guards(self):
        with patch.object(probe, 'SOURCE_ROOT', self.root), patch.object(probe, 'PROOF_FILES', ('backend/app/main.py',)), \
                patch.object(probe, 'load_sibling', return_value=ci):
            proof, executable, core = self.make_proof()
            def validate(value):
                return probe.validate_proof(value, executable=executable, core_executable=core)
            self.assertIs(proof, validate(proof))
            for key in proof:
                changed = copy.deepcopy(proof); changed.pop(key)
                with self.subTest(missing=key), self.assertRaises((RuntimeError, KeyError)):
                    validate(changed)
            for field in proof['source_provenance']:
                changed = copy.deepcopy(proof)
                changed['source_provenance'][field] = 'changed'
                with self.subTest(provenance=field), self.assertRaises(RuntimeError):
                    validate(changed)
            for field, value in (('schema', True), ('git_checkout', 0), ('github_actions', 0)):
                changed = copy.deepcopy(proof); changed['source_provenance'][field] = value
                with self.subTest(type=field), self.assertRaises(RuntimeError):
                    validate(changed)
            for flag in probe.FLAGS:
                changed = copy.deepcopy(proof); changed['checks'][flag] = False
                with self.subTest(flag=flag), self.assertRaises(RuntimeError):
                    validate(changed)
            for path, value in ((('source_commit',), 'a' * 40), (('core', 'parent_pid'), 999),
                    (('core', 'pid'), 11), (('desktop', 'sha256'), '0' * 64),
                    (('synthetic',), False), (('live_accounts_tested',), True),
                    (('user_data_touched',), True), (('core_frozen',), False),
                    (('platform',), 'linux'), (('normal_shutdown',), False),
                    (('manifest_unchanged',), False), (('safety_fence',), 'missing'),
                    (('launch_perf_ns',), 1), (('persisted_state', 'withdrawal_audits'), 0)):
                changed = copy.deepcopy(proof); target = changed
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.subTest(path=path), self.assertRaises(RuntimeError):
                    validate(changed)
            with patch.dict(os.environ, {'GITHUB_SHA': 'a' * 40}):
                with self.assertRaisesRegex(RuntimeError, 'Git HEAD differs'):
                    validate(proof)
            self.put(self.root, 'backend/app/main.py', b'tampered after receipt')
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                validate(proof)

    def test_public_repository_environment_cannot_be_omitted_or_substituted(self):
        for key in ('GITHUB_REPOSITORY', 'GITHUB_REPOSITORY_ID', 'GITHUB_REPOSITORY_OWNER_ID'):
            for value in ('', 'other', '1406784622'):
                with self.subTest(key=key,value=value), patch.dict(os.environ,{key:value}), patch.object(ci.subprocess,'run') as run:
                    self.reject('repository identity mismatch');run.assert_not_called()

    def test_marker_schema_representation_and_repository_are_exact(self):
        original = (self.root / ci.MARKER).read_bytes()
        for changes in ({'schema':True}, {'schema':1}, {'mode':'other'}, {'representation':'other'},
                        {'repository':{**ci.REPOSITORY,'id':'different'}}, {'repository':{**ci.REPOSITORY,'extra':True}},
                        {'product_version':None}, {'source_revision':None}):
            marker=json.loads(original);marker.update(changes);self.put(self.root,ci.MARKER,encoded(marker))
            self.reject('schema/mode/representation|repository identity|version/revision')
        self.put(self.root,ci.MARKER,original)

    def test_private_source_controls_are_rejected_before_git(self):
        for name in ci.FORBIDDEN_CONTROLS:
            self.put(self.root,name,b'{}')
            with patch.object(ci.subprocess,'run') as run:
                self.reject('must not be mixed');run.assert_not_called()
            (self.root/name).unlink()

    def test_manifest_and_marker_json_are_strict(self):
        for name in (ci.MANIFEST,ci.MARKER):
            original=(self.root/name).read_bytes()
            for bad in (b'{"x":1,"x":2}',b'{"x":NaN}',b'{"x":1e999}',b'[]',b'null'):
                with self.subTest(name=name,data=bad):
                    self.put(self.root,name,bad);self.reject('Duplicate|Non-finite|object')
            self.put(self.root,name,original)

    def test_marker_is_bound_to_raw_git_blob_even_with_valid_semantics(self):
        self.put(self.root,ci.MARKER,(self.root/ci.MARKER).read_bytes()+b'\n')
        self.reject('Working source differs from Git blob')

    def test_symlinked_git_directory_and_git_metadata_reject_before_queries(self):
        outside=self.directory/'actual-git';(self.root/'.git').rename(outside)
        try: (self.root/'.git').symlink_to(outside,target_is_directory=True)
        except (OSError,NotImplementedError):
            outside.rename(self.root/'.git');self.skipTest('Symlink creation unavailable')
        with patch.object(ci.subprocess,'run') as run:
            self.reject('Symlink/reparse');run.assert_not_called()
        (self.root/'.git').unlink();outside.rename(self.root/'.git')
        target=self.root/'.git/info/external';target.symlink_to(self.local,target_is_directory=True)
        with patch.object(ci.subprocess,'run') as run:
            self.reject('Symlink/reparse');run.assert_not_called()

    def test_git_file_shared_worktree_and_alternate_object_store_rejected(self):
        for name in ('commondir','objects/info/alternates','objects/info/http-alternates','info/grafts'):
            self.put(self.root,'.git/'+name,b'outside')
            with patch.object(ci.subprocess,'run') as run:
                self.reject('External Git metadata');run.assert_not_called()
            (self.root/'.git'/name).unlink()
        outside=self.directory/'actual-git';(self.root/'.git').rename(outside)
        self.put(self.root,'.git',('gitdir: '+str(outside)+'\n').encode())
        with patch.object(ci.subprocess,'run') as run:
            self.reject('not a directory');run.assert_not_called()

    def test_source_root_parent_and_nonregular_source_rejected(self):
        target=self.root/'backend/app/main.py'
        if hasattr(os,'mkfifo'):
            target.unlink();os.mkfifo(target);self.reject('not a regular file')
            target.unlink();self.put(self.root,'backend/app/main.py',(self.local/'backend/app/main.py').read_bytes())
        linked=self.directory/'linked-root'
        try: linked.symlink_to(self.root,target_is_directory=True)
        except (OSError,NotImplementedError): self.skipTest('Symlink creation unavailable')
        with patch.object(ci.subprocess,'run') as run, self.assertRaisesRegex(safe.SourceBindingError,'Symlink/reparse'):
            ci.verify_ci_source_binding(linked)
        run.assert_not_called()

    def test_cli_returns_only_json_and_never_writes_reports(self):
        import io
        from contextlib import redirect_stdout,redirect_stderr
        out,err=io.StringIO(),io.StringIO()
        index=(self.root/'.git/index').read_bytes()
        with redirect_stdout(out),redirect_stderr(err): self.assertEqual(0,ci.main(['--root',str(self.root)]))
        self.assertEqual(ci.verify_ci_source_binding(self.root),json.loads(out.getvalue()));self.assertEqual('',err.getvalue())
        self.assertEqual(index,(self.root/'.git/index').read_bytes())
        self.assertFalse((self.root/'installer-output').exists())
        self.put(self.root,'backend/app/main.py',b'changed')
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()): self.assertEqual(1,ci.main(['--root',str(self.root)]))

if __name__ == '__main__':
    unittest.main()
