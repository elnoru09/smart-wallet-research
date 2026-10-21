import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wallet_registry as w
import okx_reader as m
import master


def address(i):
    alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    n=int.from_bytes(bytes([i])*32,'big');out=''
    while n:n,r=divmod(n,58);out=alphabet[r]+out
    return out


class RegistryTests(unittest.TestCase):
    def setup_project(self, root, n=15):
        cfg=dict(project='test',code_version='0.2.6',source_run='runs/source',followup_run='runs/old',
                 baseline_report='runs/old',exports='exports')
        (root/'project.json').write_text(json.dumps(cfg))
        source=root/'runs/source';(source/'raw').mkdir(parents=True)
        env=dict(provider='OKX',path=m.LEADERBOARD,params=w.DISCOVERY_QUERIES[0],received_at='2026-10-20T06:01:05Z',
                 response=dict(code='0',data=[dict(walletAddress=address(i)) for i in range(1,n+1)]))
        (source/'raw/leaderboard.json').write_text(json.dumps(env))
        audits=[dict(wallet=address(i),requested_begin_ms=1784700065104,requested_end_ms=1792476065104,
                     returned_records=1,stored_pages=1,stop_reason='PAGE_BUDGET_REACHED',request_failure=None) for i in (1,2,3)]
        (source/'intake_report.json').write_text(json.dumps(dict(audits=audits)))
        old=root/'runs/old';old.mkdir()
        report=dict(candidates=[],audits=audits,status='PARTIAL_UNRECONCILED',observation_count=3,
                    accounting_ready=False,side_counts=dict(buy=3),errors=[])
        (old/'intake_report.json').write_text(json.dumps(report))
        obs=[dict(wallet=address(i),side='buy',provider_event_mc_usd=5500,timestamp='2026-10-12T00:00:00Z') for i in (1,2,3)]
        (old/'observations.json').write_text(json.dumps(obs));(old/'report.html').write_text('old')
        return env

    def row(self):
        return dict(type='1',chainIndex='501',tokenContractAddress=address(90),amount='10',valueUsd='20',
                    marketCap='5500',time='1791763200000')

    def client(self, response=None, limit=10):
        calls=[]
        def transport(url, headers):
            calls.append((url,headers))
            return response(len(calls)) if response else (200,json.dumps(dict(code='0',data=dict(transactionList=[self.row()],cursor='more'))).encode())
        return m.Client(['fixture-key','fixture-secret','fixture-pass'],transport=transport,budget=limit,retries=0,
            sleeper=lambda _:None,interval=0,clock=lambda:'2026-10-20T09:00:00Z'),calls

    def test_bootstrap_all_candidates_is_offline_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root)
            with patch('wallet_registry.m.Client',side_effect=AssertionError('offline')):
                state,added=w.bootstrap(root);again,n=w.bootstrap(root)
            self.assertEqual(added,15);self.assertEqual(n,0)
            self.assertEqual(w.summary(again)['history_status_counts'],dict(PARTIAL_UNRECONCILED=3,NOT_FETCHED=12))
            self.assertEqual(again['wallets'][0]['first_discovered_at'],'2026-10-20T06:01:05Z')

    def test_batch_ten_skips_old_wallets_and_retains_frozen_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            before={str(p):p.read_bytes() for p in (root/'runs').rglob('*') if p.is_file()}
            client,calls=self.client();r=w.collect(root,client=client)
            self.assertEqual(r['attempted_wallets'],10);self.assertEqual(len(calls),10)
            params=[parse_qs(urlsplit(c[0]).query) for c in calls]
            self.assertEqual([p['walletAddress'][0] for p in params],[address(i) for i in range(4,14)])
            for p in params:
                self.assertEqual(p['begin'],['1784700065104']);self.assertEqual(p['end'],['1792476065104'])
                self.assertNotIn('cursor',p)
            for p,data in before.items():self.assertEqual(Path(p).read_bytes(),data)
            state=w.load(root);self.assertEqual(w.summary(state)['wallets_with_records'],13)
            self.assertFalse(r['registry_summary']['accounting_ready'])

    def test_next_batch_only_takes_remaining_unfetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            client,_=self.client();w.collect(root,client=client)
            client,calls=self.client();r=w.collect(root,client=client)
            self.assertEqual(r['attempted_wallets'],2)
            self.assertEqual([parse_qs(urlsplit(c[0]).query)['walletAddress'][0] for c in calls],[address(14),address(15)])
            with patch('wallet_registry.m.Client',side_effect=AssertionError('empty queue')):
                self.assertEqual(w.collect(root)['requests'],0)

    def test_auth_failure_stops_batch_and_failed_wallet_is_not_erased(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            client,calls=self.client(lambda _: (401,b'{"code":"50111","msg":"fixture-secret"}'))
            r=w.collect(root,client=client)
            self.assertEqual(len(calls),1);self.assertEqual(r['attempted_wallets'],1)
            state=w.load(root);self.assertEqual(state['wallets'][3]['history']['status'],'REQUEST_FAILED')
            self.assertEqual(w.summary(state)['history_status_counts']['NOT_FETCHED'],11)
            self.assertNotIn('fixture-secret',json.dumps(r))

    def test_wallet_specific_400_does_not_select_replacement_or_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            def response(n):
                if n==1:return 400,b'{"code":"51000"}'
                return 200,json.dumps(dict(code='0',data=dict(transactionList=[self.row()],cursor='0'))).encode()
            client,calls=self.client(response);r=w.collect(root,client=client)
            self.assertEqual(len(calls),10);self.assertEqual(r['observation_count'],9)
            self.assertEqual(w.load(root)['wallets'][3]['history']['status'],'REQUEST_FAILED')

    def test_auth_code_stops_even_when_http_status_is_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            client,calls=self.client(lambda _: (400,b'{"code":"50111"}'))
            w.collect(root,client=client)
            self.assertEqual(len(calls),1)

    def test_default_clients_cap_budget_and_disable_retries(self):
        for method,budget in [(w.collect,10),(w.discover,2)]:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);self.setup_project(root);w.bootstrap(root)
                with patch('wallet_registry.m.Client') as constructor:
                    constructor.return_value.requests=1
                    constructor.return_value.get.side_effect=m.IntakeError('HTTP_401',dict(http_status=401))
                    method(root,creds=['fixture']*3)
                self.assertEqual(constructor.call_args.kwargs,dict(budget=budget,retries=0))

    def test_empty_response_is_distinct_from_unfetched_and_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            client,_=self.client(lambda _: (200,b'{"code":"0","data":{"transactionList":[],"cursor":"0"}}'))
            w.collect(root,client=client);h=w.load(root)['wallets'][3]['history']
            self.assertEqual(h['status'],'EMPTY_REQUESTED_RANGE_UNVERIFIED');self.assertEqual(h['parsed_observations'],0)
            self.assertFalse(h['complete_history'])

    def test_malformed_records_remain_visible_as_parse_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            row=self.row();row['valueUsd']=''
            client,_=self.client(lambda _: (200,json.dumps(dict(code='0',data=dict(transactionList=[row],cursor='0'))).encode()))
            r=w.collect(root,client=client)
            self.assertEqual(r['observation_count'],0);self.assertEqual(len(r['errors']),10)
            self.assertEqual(w.load(root)['wallets'][3]['history']['status'],'PARTIAL_WITH_PARSE_ERRORS')

    def test_manual_add_validates_entire_input_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            before=(root/'watchlist/registry.json').read_bytes()
            with self.assertRaises(m.SchemaError):w.add_manual(root,[address(200),'invalid-address'])
            self.assertEqual((root/'watchlist/registry.json').read_bytes(),before)
            self.assertEqual(w.add_manual(root,[address(200),address(200)],'GMGN_manual'),1)
            entry=w.load(root)['wallets'][-1]
            self.assertEqual(entry['sources'][0]['kind'],'USER_PROVIDED')
            self.assertEqual(entry['history']['status'],'NOT_FETCHED')

    def test_existing_address_preserves_first_discovery_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            before=copy.deepcopy(w.load(root)['wallets'][0])
            self.assertEqual(w.add_manual(root,[address(1)],'OKX_manual'),0)
            after=w.load(root)['wallets'][0]
            self.assertEqual(after['first_discovered_at'],before['first_discovered_at'])
            self.assertEqual(after['history'],before['history'])

    def test_discovery_two_fixed_queries_deduplicate_and_do_not_fetch_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            body=dict(code='0',data=[dict(walletAddress=address(1)),dict(walletAddress=address(200))])
            client,calls=self.client(lambda _: (200,json.dumps(body).encode()),2)
            r=w.discover(root,client=client)
            self.assertEqual(r['new_wallets'],1);self.assertEqual(len(calls),2)
            for call,expected in zip(calls,w.DISCOVERY_QUERIES):
                self.assertEqual(urlsplit(call[0]).path,m.LEADERBOARD)
                self.assertEqual(parse_qs(urlsplit(call[0]).query),{k:[v] for k,v in expected.items()})

    def test_failed_second_discovery_preserves_first_response_additions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            body=dict(code='0',data=[dict(walletAddress=address(200))])
            client,calls=self.client(lambda n: (200,json.dumps(body).encode()) if n==1 else (429,b'{"code":"50011"}'),2)
            r=w.discover(root,client=client)
            self.assertEqual(r['new_wallets'],1);self.assertEqual(len(calls),2)
            self.assertEqual(w.load(root)['wallets'][-1]['wallet'],address(200))

    def test_lock_prevents_concurrent_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            with w.lock(root):
                with self.assertRaisesRegex(m.IntakeError,'REGISTRY_BUSY'):w.add_manual(root,[address(200)])
                self.assertTrue((root/'watchlist/registry.lock').exists())
            self.assertFalse((root/'watchlist/registry.lock').exists())

    def test_registry_report_uses_unknown_for_unfetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            text=(root/'watchlist/index.html').read_text()
            self.assertIn('15 wallet terdaftar',text);self.assertIn('NOT_FETCHED',text);self.assertIn('UNKNOWN',text)

    def test_master_exports_new_batch_nested_raw_without_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_project(root);w.bootstrap(root)
            client,_=self.client();r=w.collect(root,client=client)
            folder=Path(r['report_path']).parent;(folder/'.env').write_text('fixture-secret')
            (folder/'raw/wallet_001/.env').write_text('fixture-secret')
            with contextlib.redirect_stdout(io.StringIO()):archive=master.export_review(root)
            self.assertEqual(master.latest_run(root),folder)
            with zipfile.ZipFile(archive) as z:
                self.assertEqual(len([n for n in z.namelist() if n.endswith('/history_001.json')]),10)
                self.assertTrue(any(n.endswith('/registry_after.json') for n in z.namelist()))
                self.assertFalse(any(Path(n).name=='.env' for n in z.namelist()))


if __name__=='__main__':unittest.main()
