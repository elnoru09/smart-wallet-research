import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from okx_reader import (Client, IntakeError, SchemaError, LEADERBOARD, HISTORY,
    sign, scrub, credentials, leaderboard_rows, history_page, observation,
    collect_history, collect, read_fixture_client, wallet_address, NoRedirect)

ROOT=Path(__file__).resolve().parents[1]
ADDRESS='So11111111111111111111111111111111111111112'
FIXED='2026-10-20T00:00:00Z'


class ConnectorTests(unittest.TestCase):
    def client(self,transport,**kwargs):
        return Client(['secret-key-123','sign-secret-456','pass-secret-789'],transport=transport,
            clock=lambda:FIXED,sleeper=lambda t:None,interval=0,**kwargs)

    def test_signature_known_hmac_vector(self):
        # RFC 4231 test case 1, mapped directly to the client prehash format separately below.
        import hmac,hashlib
        self.assertEqual(hmac.new(bytes.fromhex('0b'*20),b'Hi There',hashlib.sha256).hexdigest(),
                         'b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7')
        from unittest.mock import patch
        with patch('okx_reader.hmac.new', wraps=hmac.new) as mocked:
            sign('sample-secret',FIXED,HISTORY+'?cursor=a%2Fb')
            self.assertEqual(mocked.call_args.args[1],(FIXED+'GET'+HISTORY+'?cursor=a%2Fb').encode())

    def test_signature_matches_exact_encoded_request(self):
        seen=[]
        def transport(url,headers):
            seen.append((url,headers))
            return 200,b'{"code":"0","data":[]}'
        cl=self.client(transport)
        cl.get(HISTORY,{'cursor':'a/b+','chainIndex':'501'})
        url,h=seen[0]
        path=url.removeprefix('https://web3.okx.com')
        self.assertIn('a%2Fb%2B',path)
        self.assertEqual(h['OK-ACCESS-SIGN'],sign('sign-secret-456',FIXED,path))

    def test_only_read_endpoints_allowlisted(self):
        cl=self.client(lambda u,h: self.fail('must not call network'))
        with self.assertRaises(IntakeError):cl.get('/api/v6/dex/aggregator/swap',{})
        self.assertEqual(cl.requests,0)

    def test_missing_credentials_no_values_printed(self):
        with patch.dict('os.environ',{},clear=True), patch('okx_reader.dotenv_values',return_value={}):
            with self.assertRaisesRegex(IntakeError,'CREDENTIALS_MISSING'):credentials()

    def test_auth_failure_does_not_leak_body(self):
        cl=self.client(lambda u,h:(401,b'echo secret-key-123 pass-secret-789'))
        with self.assertRaises(IntakeError) as e:cl.get(LEADERBOARD,{})
        self.assertNotIn('secret',str(e.exception))
        self.assertEqual(cl.requests,1)

    def test_redaction_of_echoed_credentials(self):
        cl=self.client(lambda u,h:(200,b'{"code":"0","data":[],"echo":"secret-key-123","api_key":"other"}'))
        env=cl.get(LEADERBOARD,{})
        self.assertNotIn('secret-key-123',json.dumps(env))
        self.assertEqual(env['response']['api_key'],'<redacted>')

    def test_redirect_not_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,None,None,None))
        cl=self.client(lambda u,h:(302,b''))
        with self.assertRaisesRegex(IntakeError,'HTTP_302'):cl.get(LEADERBOARD,{})

    def test_rate_limit_retry_bounded(self):
        calls=[]
        def response(u,h):
            calls.append(1)
            return (429,b'') if len(calls)<3 else (200,b'{"code":"0","data":[]}')
        cl=self.client(response)
        cl.get(LEADERBOARD,{})
        self.assertEqual(cl.requests,3)

    def test_request_budget_counts_retries(self):
        cl=self.client(lambda u,h:(429,b''),budget=1)
        with self.assertRaisesRegex(IntakeError,'REQUEST_BUDGET'):cl.get(LEADERBOARD,{})
        self.assertEqual(cl.requests,1)

    def test_provider_error_not_empty_success(self):
        cl=self.client(lambda u,h:(200,b'{"code":"50011","msg":"secret-key-123"}'))
        with self.assertRaisesRegex(IntakeError,'PROVIDER_ERROR'):cl.get(LEADERBOARD,{})

    def test_invalid_json_and_nan_rejected(self):
        for b in (b'html page',b'{"code":"0","data":[NaN]}',b'{"code":"0","data":[1e999]}'):
            with self.subTest(body=b):
                cl=self.client(lambda u,h:(200,b))
                with self.assertRaises(SchemaError):cl.get(LEADERBOARD,{})

    def test_bad_leaderboard_shape_rejected(self):
        with self.assertRaises(SchemaError):leaderboard_rows({'response':{'data':{}}})

    def test_candidates_deduplicated_in_original_order(self):
        r=leaderboard_rows({'response':{'data':[{'walletAddress':ADDRESS}]*2},'received_at':FIXED})
        self.assertEqual(len(r),1)
        self.assertEqual(r[0]['history_complete'],'false')

    def test_address_decodes_32_bytes(self):
        self.assertEqual(wallet_address(ADDRESS),ADDRESS)
        for bad in ('O'*44,'1'*33,'not-a-wallet'):
            with self.assertRaises(SchemaError):wallet_address(bad)

    def test_history_cursor_required(self):
        with self.assertRaises(SchemaError):history_page({'response':{'data':{'transactionList':[]}}})

    def test_pagination_and_fixture_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=collect(read_fixture_client(ROOT/'provider_fixtures'),Path(tmp)/'run',fixture=True)
            self.assertEqual(r['observation_count'],2)
            self.assertEqual(r['requests'],4)
            self.assertEqual(r['data_mode'],'SYNTHETIC_FIXTURE')
            self.assertFalse(r['accounting_ready'])
            self.assertFalse(r['audits'][0]['complete_history'])
            self.assertEqual(r['audits'][0]['stop_reason'],'PROVIDER_CURSOR_EXHAUSTED')
            self.assertEqual(r['candidates'][0]['intake_summary']['provider_mc_below_6k'],1)
            self.assertEqual(r['candidates'][0]['intake_summary']['missing_buy_mc'],1)

    def test_repeated_cursor_does_not_loop_or_drop_duplicates(self):
        body={'code':'0','data':{'transactionList':[],'cursor':'same'}}
        # empty page with continuing cursor stops explicitly
        cl=self.client(lambda u,h:(200,json.dumps(body).encode()))
        with tempfile.TemporaryDirectory() as tmp:
            obs,audit,_=collect_history(cl,ADDRESS,0,9999999999999,3,tmp)
            self.assertEqual(audit['stop_reason'],'EMPTY_PAGE_WITH_CURSOR')

    def test_duplicate_payload_kept_and_flagged(self):
        record=json.loads((ROOT/'provider_fixtures/history_1.json').read_text())['data']['transactionList'][0]
        body={'code':'0','data':{'transactionList':[record,record],'cursor':'0'}}
        cl=self.client(lambda u,h:(200,json.dumps(body).encode()))
        with tempfile.TemporaryDirectory() as tmp:
            obs,audit,_=collect_history(cl,ADDRESS,0,9999999999999,3,tmp)
            self.assertEqual(len(obs),2)
            self.assertEqual(audit['duplicate_payload_occurrences'],1)
            self.assertFalse(audit['eligible_for_accounting'])

    def test_page_budget_not_marked_complete(self):
        body=json.loads((ROOT/'provider_fixtures/history_1.json').read_text())
        cl=self.client(lambda u,h:(200,json.dumps(body).encode()))
        with tempfile.TemporaryDirectory() as tmp:
            obs,audit,_=collect_history(cl,ADDRESS,0,9999999999999,1,tmp)
            self.assertEqual(audit['stop_reason'],'PAGE_BUDGET_REACHED')
            self.assertFalse(audit['complete_history'])

    def test_invalid_rows_quarantined_without_zero_fee_assumption(self):
        record=json.loads((ROOT/'provider_fixtures/history_1.json').read_text())['data']['transactionList'][0]
        record['amount']='not-a-number'
        cl=self.client(lambda u,h:(200,json.dumps({'code':'0','data':{'transactionList':[record],'cursor':'0'}}).encode()))
        with tempfile.TemporaryDirectory() as tmp:
            obs,audit,issues=collect_history(cl,ADDRESS,0,9999999999999,1,tmp)
            self.assertEqual(obs,[])
            self.assertEqual(audit['malformed_records'],1)
            self.assertEqual(len(issues),1)

    def test_run_cannot_overwrite_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(IntakeError,'OUTPUT_EXISTS'):
                collect(read_fixture_client(ROOT/'provider_fixtures'),tmp,fixture=True)

    def test_record_cap_never_claims_lifetime_completeness(self):
        record=json.loads((ROOT/'provider_fixtures/history_1.json').read_text())['data']['transactionList'][0]
        def transport(url,headers):
            rows=[dict(record,time=str(int(record['time'])-i)) for i in range(100)]
            # Unique cursor each page, overlapping record payload intentionally retained.
            transport.n+=1
            return 200,json.dumps({'code':'0','data':{'transactionList':rows,'cursor':str(transport.n)}}).encode()
        transport.n=0
        cl=self.client(transport)
        with tempfile.TemporaryDirectory() as tmp:
            obs,audit,_=collect_history(cl,ADDRESS,0,9999999999999,15,tmp)
            self.assertEqual(audit['stop_reason'],'PROVIDER_RECORD_CAP_REACHED')
            self.assertEqual(cl.requests,10)
            self.assertFalse(audit['complete_history'])

    def test_actual_repeated_cursor_stops(self):
        body=json.loads((ROOT/'provider_fixtures/history_1.json').read_text())
        cl=self.client(lambda u,h:(200,json.dumps(body).encode()))
        with tempfile.TemporaryDirectory() as tmp:
            obs,audit,_=collect_history(cl,ADDRESS,0,9999999999999,5,tmp)
            self.assertEqual(audit['stop_reason'],'REPEATED_CURSOR')
            self.assertEqual(cl.requests,2)

    def test_zero_mc_is_missing_not_lowcap(self):
        row=json.loads((ROOT/'provider_fixtures/history_1.json').read_text())['data']['transactionList'][0]
        row['marketCap']='0'
        env=dict(received_at=FIXED,params={'begin':'0','end':'9999999999999'})
        record=observation(row,env,ADDRESS,1)
        self.assertIsNone(record['provider_event_mc_usd'])
        self.assertEqual(record['fee_status'],'UNKNOWN')

    def test_timestamp_seconds_not_milliseconds_rejected(self):
        row=json.loads((ROOT/'provider_fixtures/history_1.json').read_text())['data']['transactionList'][0]
        row['time']='1760000000'
        with self.assertRaises(SchemaError):
            observation(row,dict(received_at=FIXED,params={'begin':'0','end':'9999999999999'}),ADDRESS,1)

    def test_noninteractive_prompt_rejected(self):
        with patch.dict('os.environ',{},clear=True), patch('okx_reader.dotenv_values',return_value={}), patch('sys.stdin.isatty',return_value=False):
            with self.assertRaisesRegex(IntakeError,'PROMPT_REQUIRES_TERMINAL'):credentials(prompt=True)


if __name__=='__main__':unittest.main()
