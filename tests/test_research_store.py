import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import master
import okx_reader as m
import research_store as store

ROOT = Path(__file__).resolve().parents[1]
WALLET = 'So11111111111111111111111111111111111111112'
TOKEN = '11111111111111111111111111111111'


class ResearchStoreTests(unittest.TestCase):
    def write(self, root, relative, document):
        path = root/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding='utf-8')
        return path

    def workspace(self, root):
        root = root.resolve()
        shutil.copy2(ROOT/'project.json', root/'project.json')
        self.write(root, 'watchlist/registry.json', dict(schema='wallet-registry-0.2.6',wallets=[
            dict(wallet=WALLET,first_discovered_at='2026-10-01T00:00:00Z',sources=[],history=dict(
                status='PARTIAL_UNRECONCILED',parsed_observations=1,complete_history=False,accounting_ready=False))]))
        self.write(root, 'watchlist/latest_batch.json', dict(run_path='runs/failed'))
        self.write(root, 'runs/failed/intake_report.json', dict(status='PARTIAL_WITH_ERRORS',observation_count=0,
            accounting_ready=False,audits=[],errors=[dict(error='NETWORK_ERROR')]))
        row = dict(chainIndex='501',type='1',tokenContractAddress=TOKEN,amount='1.00000001',valueUsd='2',
                   marketCap='',time='1791763200000')
        env = dict(provider='OKX',path=m.HISTORY,params=dict(chainIndex='501',walletAddress=WALLET,
            begin='1784700065104',end='1792476065104',limit='100'),requested_at='2026-10-20T06:00:00Z',
            received_at='2026-10-20T06:01:00Z',response=dict(code='0',data=dict(transactionList=[row],cursor='more')))
        self.write(root, 'runs/original/raw/history_001.json', env)
        obs = m.observation(row, env, WALLET, 1)
        self.write(root, 'runs/replay/observations.json', [obs])
        return env

    def test_import_is_offline_idempotent_and_keeps_source_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.workspace(root)
            (root/'.env').write_text('OKX_SECRET_KEY=never-read-secret-marker',encoding='utf-8')
            before={p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with patch('okx_reader.Client',side_effect=AssertionError('offline')):
                first=store.import_workspace(root);second=store.import_workspace(root)
            self.assertEqual(first['wallet_count'],1)
            self.assertEqual(first['registry_observations'],1)
            self.assertEqual(first['provider_payloads'],1)
            self.assertEqual(second['artifacts_added'],0)
            self.assertEqual(second['payloads_added'],0)
            self.assertEqual(second['raw_row_occurrences'],1)
            self.assertEqual(first['active_batch']['observations'],0)
            self.assertFalse(first['accounting_ready'])
            self.assertIsNone(first['unique_onchain_events'])
            for path,content in before.items():self.assertEqual((root/path).read_bytes(),content)
            with contextlib.closing(store.open_readonly(root/store.DATABASE)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM artifacts WHERE path LIKE '%.env%'").fetchone()[0],0)
                self.assertNotIn(b'never-read-secret-marker', b''.join(r[0] for r in conn.execute('SELECT content FROM artifacts')))
                raw=json.loads(conn.execute('SELECT raw_json FROM payloads').fetchone()[0])
                self.assertEqual(raw['amount'],'1.00000001')

    def test_replays_duplicate_rows_and_repeated_captures_are_distinguished(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);env=self.workspace(root)
            env['response']['data']['transactionList']*=2
            self.write(root,'runs/original/raw/history_001.json',env)
            self.write(root,'runs/copy/raw/history_001.json',env)
            later=copy.deepcopy(env);later['received_at']='2026-10-21T00:00:00Z'
            self.write(root,'runs/later/raw/history_001.json',later)
            result=store.import_workspace(root)
            self.assertEqual(result['provider_payloads'],1)
            self.assertEqual(result['raw_row_occurrences'],6)
            with contextlib.closing(store.open_readonly(root/store.DATABASE)) as conn:
                first,n=conn.execute('SELECT first_received_epoch,archived_occurrences FROM payload_provenance').fetchone()
                self.assertEqual(first,m.ts('2026-10-20T06:01:00Z'))
                self.assertEqual(n,6)

    def test_embedded_diagnosis_and_original_received_time_are_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);env=self.workspace(root)
            self.write(root,'runs/history_diagnosis_003.json',dict(schema='targeted-history-diagnosis-0.2.3',response_envelope=env))
            self.write(root,'runs/continued/diagnosis_source.json',dict(schema='targeted-history-diagnosis-0.2.3',response_envelope=env))
            result=store.import_workspace(root)
            self.assertEqual(result['provider_payloads'],1)
            self.assertEqual(result['raw_row_occurrences'],3)
            with contextlib.closing(store.open_readonly(root/store.DATABASE)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM occurrences WHERE envelope_key='response_envelope'").fetchone()[0],2)

    def test_malformed_rows_and_missing_mc_are_not_converted_to_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);env=self.workspace(root)
            invalid=copy.deepcopy(env['response']['data']['transactionList'][0]);invalid['amount']='broken'
            env['response']['data']['transactionList'].append(invalid)
            self.write(root,'runs/original/raw/history_001.json',env)
            result=store.import_workspace(root)
            self.assertEqual(result['provider_payloads'],2)
            self.assertEqual(result['parsed_provider_payloads'],1)
            self.assertEqual(result['parse_error_occurrences'],1)
            with contextlib.closing(store.open_readonly(root/store.DATABASE)) as conn:
                row=json.loads(conn.execute('SELECT parsed_json FROM occurrences WHERE parsed_json IS NOT NULL').fetchone()[0])
                self.assertIsNone(row['provider_event_mc_usd'])
                self.assertEqual(row['mc_status'],'MISSING')

    def test_legacy_without_raw_remains_visible_and_unlinked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.workspace(root)
            (root/'runs/original/raw/history_001.json').unlink()
            result=store.import_workspace(root)
            self.assertEqual(result['legacy_observation_rows'],1)
            self.assertEqual(result['legacy_rows_without_raw_payload'],1)
            self.assertEqual(result['provider_payloads'],0)

    def test_changed_source_creates_new_version_and_preserves_old_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.workspace(root);store.import_workspace(root)
            path=root/'watchlist/registry.json';old=path.read_bytes();doc=json.loads(old)
            doc['wallets'][0]['history']['status']='REQUEST_FAILED';doc['wallets'][0]['history']['parsed_observations']=0
            self.write(root,'watchlist/registry.json',doc)
            result=store.import_workspace(root)
            self.assertEqual(result['artifacts_added'],1)
            self.assertEqual(result['history_status_counts'],dict(REQUEST_FAILED=1))
            self.assertEqual(result['registry_observations'],0)
            with contextlib.closing(store.open_readonly(root/store.DATABASE)) as conn:
                rows=conn.execute("SELECT content FROM artifacts WHERE path='watchlist/registry.json'").fetchall()
                self.assertIn((old,),rows);self.assertEqual(len(rows),2)

    def test_invalid_source_rolls_back_the_entire_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.workspace(root);store.import_workspace(root)
            before=store.database_status(root)
            self.write(root,'runs/new/intake_report.json',dict(status='x',observation_count=-1))
            with self.assertRaisesRegex(store.StorageError,'INVALID_REPORT_COUNT'):store.import_workspace(root)
            self.assertEqual(store.database_status(root),before)
            self.assertFalse((root/'data/research.lock').exists())

    def test_source_mutation_before_commit_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.workspace(root);store.import_workspace(root)
            before=store.database_status(root);original=store.sources;calls=[]
            def changed(base):
                calls.append(1)
                if len(calls)==2:
                    p=root/'watchlist/registry.json';p.write_bytes(p.read_bytes()+b'\n')
                return original(base)
            with patch('research_store.sources',side_effect=changed):
                with self.assertRaisesRegex(store.StorageError,'SOURCE_CHANGED'):store.import_workspace(root)
            self.assertEqual(store.database_status(root),before)

    def test_active_report_falls_back_to_config_without_batch_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.workspace(root)
            (root/'watchlist/latest_batch.json').unlink()
            self.write(root,'runs/okx_003_continued/intake_report.json',dict(status='PARTIAL_UNRECONCILED',observation_count=1))
            result=store.import_workspace(root)
            self.assertEqual(result['active_batch']['observations'],1)

    def test_removed_source_is_archived_but_not_listed_as_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.workspace(root);first=store.import_workspace(root)
            (root/'runs/original/raw/history_001.json').unlink()
            second=store.import_workspace(root)
            self.assertEqual(second['artifact_versions'],first['artifact_versions'])
            self.assertEqual(second['current_source_files'],first['current_source_files']-1)
            self.assertEqual(second['provider_payloads'],1)

    def test_source_nan_and_path_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.workspace(root)
            self.write(root,'watchlist/latest_batch.json',dict(run_path='../outside'))
            with self.assertRaisesRegex(store.StorageError,'PATH_OUTSIDE_WORKSPACE'):store.import_workspace(root)
            self.write(root,'watchlist/latest_batch.json',dict(run_path='runs/failed'))
            (root/'runs/failed/intake_report.json').write_text('{"observation_count":NaN}',encoding='utf-8')
            with self.assertRaisesRegex(store.StorageError,'INVALID_SOURCE_JSON'):store.import_workspace(root)

    def test_active_locks_block_import_without_removing_other_owner_lock(self):
        for name in ['watchlist/registry.lock','watchlist/sync.lock','workspace-import.lock','data/research.lock']:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);self.workspace(root);p=root/name;p.parent.mkdir(exist_ok=True);p.write_text('owner')
                with self.assertRaisesRegex(store.StorageError,'WORKSPACE_BUSY'):store.import_workspace(root)
                self.assertEqual(p.read_text(),'owner')
                self.assertFalse((root/store.DATABASE).exists())

    def test_status_is_readonly_and_missing_database_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.assertEqual(store.database_status(root)['status'],'DATABASE_NOT_INITIALIZED')
            self.assertEqual(list(root.iterdir()),[])
            self.workspace(root);store.import_workspace(root)
            db=root/store.DATABASE;before=db.read_bytes()
            store.database_status(root)
            self.assertEqual(db.read_bytes(),before)
            with contextlib.closing(store.open_readonly(db)) as conn:
                with self.assertRaises(sqlite3.OperationalError):conn.execute('DELETE FROM artifacts')

    def test_backup_restore_preserves_bytes_counts_and_active_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.workspace(root);store.import_workspace(root)
            db=root/store.DATABASE;before=db.read_bytes()
            backup=store.backup_database(root)
            shutil.rmtree(root/'runs');shutil.rmtree(root/'watchlist')
            restored=store.restore_database(root,Path(backup['path']).relative_to(root))
            self.assertEqual(backup['summary'],restored['summary'])
            self.assertFalse(restored['active_database_replaced'])
            self.assertEqual(db.read_bytes(),before)
            with contextlib.closing(store.open_readonly(restored['path'])) as conn:
                self.assertEqual(store.verify_connection(conn)['provider_payloads'],1)
                self.assertGreater(len(conn.execute("SELECT content FROM artifacts WHERE kind='raw'").fetchone()[0]),0)

    def test_backup_rejects_corruption_and_exclusive_publish_preserves_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.workspace(root);store.import_workspace(root)
            existing=root/'data/existing.sqlite3';existing.write_bytes(b'do-not-replace')
            with self.assertRaises(FileExistsError):store.copy_database(root/store.DATABASE,existing)
            self.assertEqual(existing.read_bytes(),b'do-not-replace')
            with contextlib.closing(sqlite3.connect(root/store.DATABASE)) as conn, conn:
                conn.execute("UPDATE artifacts SET content=? WHERE id=1",(b'tampered',))
            with self.assertRaisesRegex(store.StorageError,'ARCHIVED_ARTIFACT_HASH_MISMATCH'):store.backup_database(root)
            self.assertEqual(list((root/'data/backups').glob('*.sqlite3')),[])

    def test_foreign_database_and_future_schema_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.workspace(root);(root/'data').mkdir()
            with contextlib.closing(sqlite3.connect(root/store.DATABASE)) as conn, conn:conn.execute('CREATE TABLE other(x)')
            with self.assertRaisesRegex(store.StorageError,'NOT_A_RESEARCH_DATABASE'):store.import_workspace(root)
            (root/store.DATABASE).unlink();store.import_workspace(root)
            with contextlib.closing(sqlite3.connect(root/store.DATABASE)) as conn, conn:conn.execute('PRAGMA user_version=999')
            with self.assertRaisesRegex(store.StorageError,'UNSUPPORTED_DATABASE_VERSION'):store.database_status(root)

    def test_demo_export_and_env_are_excluded_and_dashboard_escapes_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.workspace(root)
            self.write(root,'runs/demo_example/raw/history_001.json',dict(unsupported='demo'))
            self.write(root,'exports/private.json',dict(unsupported='export'))
            self.write(root,'runs/failed/intake_report.json',dict(status='<script>alert(1)</script>',observation_count=0))
            store.import_workspace(root)
            before=(root/store.DATABASE).read_bytes()
            result=store.dashboard(root);text=Path(result['path']).read_text()
            self.assertNotIn('<script>alert(1)</script>',text)
            self.assertIn('&lt;script&gt;',text)
            self.assertIn('UNKNOWN',text)
            self.assertIn('runs/original/raw/history_001.json',text)
            self.assertEqual((root/store.DATABASE).read_bytes(),before)

    def test_symlink_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.workspace(root)
            link=root/'runs/linked'
            try:link.symlink_to(root/'watchlist',target_is_directory=True)
            except (OSError,NotImplementedError):self.skipTest('Symlink creation unavailable')
            with self.assertRaisesRegex(store.StorageError,'SYMLINK_NOT_ALLOWED'):store.import_workspace(root)

    def test_master_status_and_cli_actions_keep_batch_and_archive_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.workspace(root)
            with contextlib.redirect_stdout(io.StringIO()):
                master.database_action('db-import',root)
                info=master.status(root)
                with patch('master.webbrowser.open') as browser:
                    result=master.database_action('db-dashboard',root)
            self.assertEqual(info['observations'],0)
            self.assertEqual(info['database_summary']['registry_observations'],1)
            browser.assert_called_once_with(Path(result['path']).as_uri())


if __name__=='__main__':unittest.main()
