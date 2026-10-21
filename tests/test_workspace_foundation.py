import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import master
import wallet_registry
from workspace_transfer import import_legacy, WorkspaceImportError

ROOT = Path(__file__).resolve().parents[1]


class WorkspaceTests(unittest.TestCase):
    def project(self, folder):
        folder.mkdir()
        shutil.copy2(ROOT/'project.json', folder/'project.json')
        return folder

    def legacy(self, folder):
        folder = self.project(folder)
        (folder/'runs/sample/raw').mkdir(parents=True)
        (folder/'runs/sample/raw/history_001.json').write_bytes(b'{"received_at":"2026-01-01Z"}\n')
        (folder/'.env').write_text('OKX_API_KEY=private-local-fixture\n', encoding='utf-8')
        (folder/'master.py').write_text('do not copy source code', encoding='utf-8')
        return folder

    def test_fresh_status_is_read_only_and_does_not_construct_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.project(Path(tmp)/'fresh')
            with patch('okx_reader.Client', side_effect=AssertionError('offline')), contextlib.redirect_stdout(io.StringIO()):
                result = master.status(root)
            self.assertEqual(result['status'], 'EMPTY_WORKSPACE')
            self.assertIsNone(result['report'])
            self.assertFalse(result['accounting_ready'])
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['project.json'])

    def test_initialization_is_offline_empty_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.project(Path(tmp)/'fresh')
            with patch('okx_reader.Client', side_effect=AssertionError('offline')), contextlib.redirect_stdout(io.StringIO()):
                first = master.initialize(root)
                second = master.initialize(root)
            self.assertEqual(first['registry_summary']['wallet_count'], 0)
            self.assertEqual(first, second)
            self.assertTrue((root/'watchlist/index.html').is_file())
            self.assertTrue((root/'data').is_dir())

    def test_demo_keeps_real_registry_and_active_report_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.project(Path(tmp)/'fresh')
            shutil.copytree(ROOT/'provider_fixtures', root/'provider_fixtures')
            with contextlib.redirect_stdout(io.StringIO()):
                master.initialize(root)
            pointer = root/'watchlist/latest_batch.json'
            pointer.write_text('{"run_path":"runs/real"}', encoding='utf-8')
            before = (root/'watchlist/registry.json').read_bytes()
            with patch('okx_reader.http_get', side_effect=AssertionError('offline')), contextlib.redirect_stdout(io.StringIO()):
                result = master.demo(root)
            self.assertEqual(result['network_requests'], 0)
            self.assertTrue(Path(result['report_path']).is_file())
            self.assertTrue(result['report_path'].endswith('report.html'))
            self.assertIn('SYNTHETIC', result['data_mode'])
            self.assertEqual((root/'watchlist/registry.json').read_bytes(), before)
            self.assertEqual(json.loads(pointer.read_text())['run_path'], 'runs/real')

    def test_pending_wallet_without_legacy_window_fails_before_api_or_job_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.project(Path(tmp)/'fresh')
            address = 'So11111111111111111111111111111111111111112'
            wallet_registry.add_manual(root, [address])
            with patch('okx_reader.Client', side_effect=AssertionError('no API')):
                with self.assertRaisesRegex(RuntimeError, 'HISTORY_WINDOW_REQUIRED'):
                    wallet_registry.sync(root)
            self.assertFalse((root/'runs').exists())
            self.assertEqual(wallet_registry.load(root)['wallets'][0]['history']['status'], 'NOT_FETCHED')

    def test_import_preserves_raw_source_and_current_code_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.legacy(Path(tmp)/'old with spaces')
            dest = self.project(Path(tmp)/'checkout')
            (dest/'master.py').write_text('new source', encoding='utf-8')
            before = {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
            first = import_legacy(source, dest)
            second = import_legacy(source, dest)
            self.assertEqual(first['files_to_copy'], 1)
            self.assertEqual(second['files_to_copy'], 0)
            self.assertEqual(second['identical_files_skipped'], 1)
            self.assertFalse((dest/'.env').exists())
            self.assertEqual((dest/'master.py').read_text(), 'new source')
            for path, data in before.items():
                self.assertEqual((source/path).read_bytes(), data)
            self.assertEqual((dest/'runs/sample/raw/history_001.json').read_bytes(), before[Path('runs/sample/raw/history_001.json')])

    def test_env_requires_explicit_flag_and_value_is_not_in_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.legacy(Path(tmp)/'old')
            dest = self.project(Path(tmp)/'new')
            result = import_legacy(source, dest, include_env=True)
            self.assertTrue(result['env_included'])
            self.assertEqual((source/'.env').read_bytes(), (dest/'.env').read_bytes())
            for receipt in (dest/'data/migration_receipts').glob('*.json'):
                self.assertNotIn('private-local-fixture', receipt.read_text())

    def test_all_conflicts_are_checked_before_any_file_is_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.legacy(Path(tmp)/'old')
            dest = self.project(Path(tmp)/'new')
            (dest/'.env').write_text('different-local-key', encoding='utf-8')
            with self.assertRaisesRegex(WorkspaceImportError, 'DESTINATION_CONFLICT'):
                import_legacy(source, dest, include_env=True)
            self.assertFalse((dest/'runs').exists())
            self.assertEqual((dest/'.env').read_text(), 'different-local-key')

    def test_runtime_file_conflict_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.legacy(Path(tmp)/'old')
            dest = self.project(Path(tmp)/'new')
            p = dest/'runs/sample/raw/history_001.json'
            p.parent.mkdir(parents=True)
            p.write_bytes(b'existing-different-evidence')
            with self.assertRaisesRegex(WorkspaceImportError, 'DESTINATION_CONFLICT'):
                import_legacy(source, dest)
            self.assertEqual(p.read_bytes(), b'existing-different-evidence')

    def test_dry_run_does_not_copy_or_create_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.legacy(Path(tmp)/'old')
            dest = self.project(Path(tmp)/'new')
            result = import_legacy(source, dest, include_env=True, dry_run=True)
            self.assertEqual(result['files_to_copy'], 2)
            self.assertEqual(result['status'], 'DRY_RUN')
            self.assertEqual(sorted(p.name for p in dest.iterdir()), ['project.json'])

    def test_overlapping_folders_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.legacy(Path(tmp)/'old')
            with self.assertRaisesRegex(WorkspaceImportError, 'OVERLAPPING'):
                import_legacy(source, source)
            dest = self.project(source/'nested')
            with self.assertRaisesRegex(WorkspaceImportError, 'OVERLAPPING'):
                import_legacy(source, dest)

    def test_custom_legacy_paths_are_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.legacy(Path(tmp)/'old')
            dest = self.project(Path(tmp)/'new')
            config = json.loads((source/'project.json').read_text())
            config['source_run'] = 'runs/custom-source'
            (source/'project.json').write_text(json.dumps(config), encoding='utf-8')
            with self.assertRaisesRegex(WorkspaceImportError, 'CUSTOM_PROJECT_PATHS'):
                import_legacy(source, dest)
            self.assertFalse((dest/'runs').exists())

    def test_active_worker_lock_blocks_import(self):
        for locked in ('source', 'destination'):
            with tempfile.TemporaryDirectory() as tmp:
                source = self.legacy(Path(tmp)/'old')
                dest = self.project(Path(tmp)/'new')
                root = source if locked == 'source' else dest
                (root/'watchlist').mkdir()
                (root/'watchlist/sync.lock').write_text('active', encoding='utf-8')
                with self.assertRaisesRegex(WorkspaceImportError, 'WORKSPACE_BUSY'):
                    import_legacy(source, dest)
                self.assertFalse((dest/'runs').exists())

    def test_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.legacy(Path(tmp)/'old')
            dest = self.project(Path(tmp)/'new')
            try:
                (source/'runs/linked.json').symlink_to(source/'project.json')
            except (OSError, NotImplementedError):
                self.skipTest('Symlink creation unavailable on this filesystem')
            with self.assertRaisesRegex(WorkspaceImportError, 'SYMLINK_NOT_ALLOWED'):
                import_legacy(source, dest)

    def test_source_change_during_copy_is_detected_without_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.legacy(Path(tmp)/'old')
            dest = self.project(Path(tmp)/'new')
            original = shutil.copy2
            def changing_copy(src, target):
                result = original(src, target)
                Path(src).write_bytes(b'changed-while-copying')
                return result
            with patch('workspace_transfer.shutil.copy2', side_effect=changing_copy):
                with self.assertRaisesRegex(WorkspaceImportError, 'SOURCE_CHANGED'):
                    import_legacy(source, dest)
            self.assertFalse((dest/'runs/sample/raw/history_001.json').exists())
            self.assertFalse((dest/'workspace-import.lock').exists())


if __name__ == '__main__':
    unittest.main()
