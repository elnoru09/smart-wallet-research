import contextlib
import io
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import parse_qs,urlsplit
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_wallet_registry as registry_fixtures
address=registry_fixtures.address
import wallet_registry as w
import okx_reader as m


class SyncTests(unittest.TestCase):
    setup_project=registry_fixtures.RegistryTests.setup_project
    client=registry_fixtures.RegistryTests.client
    row=registry_fixtures.RegistryTests.row

    def test_one_sync_drains_multiple_batches_with_per_batch_request_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            client,calls=self.client(limit=50)
            with contextlib.redirect_stdout(io.StringIO()):r=w.sync(root,client=client,export=False)
            self.assertEqual(r['requests'],12);self.assertEqual(len(calls),12)
            self.assertEqual(r['status'],'QUEUE_PROCESSED')
            self.assertEqual(r['registry_summary']['wallets_with_records'],15)
            folder=Path(r['report_path']).parent
            reports=[w.read(p) for p in sorted((folder/'batches').glob('*/intake_report.json'))]
            self.assertEqual([x['requests'] for x in reports],[10,2])
            self.assertEqual(w.read(folder/'intake_report.json')['observation_count'],12)

    def test_budget_is_global_and_next_sync_skips_successes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            client,calls=self.client(limit=50)
            with contextlib.redirect_stdout(io.StringIO()):r=w.sync(root,client=client,max_requests=11,export=False)
            self.assertEqual(r['requests'],11);self.assertEqual(r['status'],'REQUEST_BUDGET_REACHED')
            client,calls=self.client(limit=50)
            with contextlib.redirect_stdout(io.StringIO()):r=w.sync(root,client=client,max_requests=11,export=False)
            self.assertEqual(r['requests'],1)
            self.assertEqual(parse_qs(urlsplit(calls[0][0]).query)['walletAddress'],[address(15)])

    def test_network_failure_stops_after_one_attempt_and_can_recover_on_next_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            from unittest.mock import Mock
            failed=Mock();failed.requests=0
            def fail(*args):
                failed.requests+=1;raise m.IntakeError('NETWORK_ERROR: fixture',dict(category='TIMEOUT'))
            failed.get.side_effect=fail
            with contextlib.redirect_stdout(io.StringIO()):r=w.sync(root,client=failed,export=False)
            self.assertEqual(r['requests'],1);self.assertEqual(r['status'],'SYSTEMIC_FAILURE')
            self.assertEqual(r['registry_summary']['history_status_counts']['REQUEST_FAILED'],1)
            client,calls=self.client(limit=50)
            with contextlib.redirect_stdout(io.StringIO()):r=w.sync(root,client=client,export=False)
            self.assertEqual(r['requests'],12)
            self.assertEqual(parse_qs(urlsplit(calls[0][0]).query)['walletAddress'],[address(4)])

    def test_auth_error_halts_cycle_and_is_retryable_after_user_fixes_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            client,calls=self.client(lambda _: (401,b'{"code":"50111"}'),limit=50)
            with contextlib.redirect_stdout(io.StringIO()):r=w.sync(root,client=client,export=False)
            self.assertEqual(len(calls),1);self.assertEqual(r['status'],'SYSTEMIC_FAILURE')
            self.assertTrue(w.eligible(w.load(root)['wallets'][3],True))

    def test_wallet_specific_400_is_retained_without_stopping_other_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            def response(n):
                if n==1:return 400,b'{"code":"51000"}'
                return 200,json.dumps(dict(code='0',data=dict(transactionList=[self.row()],cursor='more'))).encode()
            client,calls=self.client(response,limit=50)
            with contextlib.redirect_stdout(io.StringIO()):r=w.sync(root,client=client,export=False)
            self.assertEqual(r['status'],'QUEUE_PROCESSED_WITH_ERRORS');self.assertEqual(r['requests'],12)
            self.assertFalse(w.eligible(w.load(root)['wallets'][3],True))

    def test_interrupt_keeps_last_wallet_checkpoint_and_excludes_uncertain_request_from_auto_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            def response(n):
                if n==2:raise KeyboardInterrupt()
                return 200,json.dumps(dict(code='0',data=dict(transactionList=[self.row()],cursor='more'))).encode()
            client,calls=self.client(response,limit=50)
            with contextlib.redirect_stdout(io.StringIO()):r=w.sync(root,client=client,export=False)
            self.assertEqual(r['status'],'INTERRUPTED');self.assertEqual(r['observation_count'],1)
            self.assertEqual(w.load(root)['wallets'][4]['history']['status'],'IN_PROGRESS')
            self.assertFalse(w.eligible(w.load(root)['wallets'][4],True))
            self.assertFalse((root/'watchlist/sync.lock').exists())

    def test_one_export_contains_all_nested_batches_and_never_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root);(root/'.env').write_text('fixture-secret')
            client,_=self.client(limit=50)
            with contextlib.redirect_stdout(io.StringIO()):r=w.sync(root,client=client)
            with zipfile.ZipFile(r['review_zip']) as z:
                names=z.namelist()
                self.assertEqual(len([n for n in names if n.endswith('/history_001.json')]),12)
                self.assertEqual(len([n for n in names if n.endswith('/batch_plan.json')]),2)
                self.assertTrue(any(n.endswith('/session_plan.json') for n in names))
                self.assertFalse(any(Path(n).name=='.env' for n in names))
            self.assertEqual(len(list((root/'exports').glob('*.zip'))),1)

    def test_empty_queue_does_not_create_client_or_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root,n=3);w.bootstrap(root)
            with patch('wallet_registry.m.Client',side_effect=AssertionError('no API')):
                r=w.sync(root,export=False)
            self.assertEqual(r['requests'],0)

    def test_overlarge_budget_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('wallet_registry.m.Client',side_effect=AssertionError('no API')):
                with self.assertRaises(m.IntakeError):w.sync(Path(tmp),max_requests=51)

    def test_default_sync_client_has_global_cap_and_no_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            with patch('wallet_registry.m.Client') as constructor,contextlib.redirect_stdout(io.StringIO()):
                constructor.return_value.requests=1
                constructor.return_value.get.side_effect=m.IntakeError('NETWORK_ERROR')
                w.sync(root,creds=['fixture']*3,export=False,max_requests=17)
            self.assertEqual(constructor.call_args.kwargs,dict(budget=17,retries=0))


class NetworkDiagnosticTests(unittest.TestCase):
    def test_tls_code_retained_without_error_message(self):
        exc=ssl.SSLCertVerificationError(1,'fixture-secret');exc.verify_code=62
        d=m.network_diagnostic(urllib.error.URLError(exc))
        self.assertEqual(d['category'],'TLS_CERTIFICATE_VERIFY_FAILED');self.assertEqual(d['verify_code'],62)
        self.assertNotIn('fixture-secret',json.dumps(d))

    def test_dns_timeout_and_connection_have_distinct_categories(self):
        for exc,expected in [(socket.gaierror(-2,'fixture-secret'),'DNS_LOOKUP_FAILED'),
                             (TimeoutError('fixture-secret'),'TIMEOUT'),
                             (ConnectionRefusedError(111,'fixture-secret'),'CONNECTION_FAILED')]:
            d=m.network_diagnostic(urllib.error.URLError(exc))
            self.assertEqual(d['category'],expected);self.assertNotIn('fixture-secret',json.dumps(d))

    def test_http_transport_attaches_only_safe_network_diagnostic(self):
        with patch('okx_reader.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=urllib.error.URLError(TimeoutError('fixture-secret'))
            with self.assertRaises(m.IntakeError) as cm:m.http_get('https://web3.okx.com/test',{})
        self.assertEqual(cm.exception.diagnostic['category'],'TIMEOUT')
        self.assertNotIn('fixture-secret',str(cm.exception)+json.dumps(cm.exception.diagnostic))


if __name__=='__main__':unittest.main()
