import copy
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import history_followup as f
import okx_reader as m

W='So11111111111111111111111111111111111111112'
T='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'


class FollowupTests(unittest.TestCase):
    def seed(self, base, cursor='seed-cursor'):
        source=base/'source';source.mkdir();(source/'raw').mkdir()
        audit=dict(wallet=W,returned_records=0,requested_begin_ms=1784700000000,
                   requested_end_ms=1792470000000,request_failure=dict(stage='history',wallet=W,error='HTTP_400'))
        prior=dict(candidates=[dict(wallet=W,source='OKX',discovered_at='2026-10-19T00:00:00Z',history_complete='false')],
                   audits=[audit],errors=[audit['request_failure']],started_at='2026-10-19T00:00:00Z',requests=2)
        (source/'intake_report.json').write_text(json.dumps(prior))
        row=dict(type='3',chainIndex='501',tokenContractAddress=T,amount='5',valueUsd='',marketCap='',time='1791763200000')
        env=dict(provider='OKX',path=m.HISTORY,params=f.expected_params(audit),received_at='2026-10-20T08:00:00Z',
                 response=dict(code='0',data=dict(transactionList=[row],cursor=cursor)))
        diagnosis=base/'diagnosis.json'
        d=dict(wallet=W,status='HISTORY_REQUEST_SUCCEEDED',params=env['params'],selection_changed=False,
               original_window_retained=True,returned_records=1,response_envelope=env,requests=1)
        diagnosis.write_text(json.dumps(d))
        return source,diagnosis,env

    def client(self, env, replies):
        calls=[]
        def transport(url, headers):
            calls.append(url)
            reply=replies[len(calls)-1]
            if isinstance(reply,Exception):raise reply
            return 200,json.dumps(reply).encode()
        c=m.Client(['fixture-key','fixture-secret','fixture-pass'],transport=transport,
                   sleeper=lambda _:None,interval=0,retries=0,budget=2,clock=lambda:'2026-10-20T09:00:00Z')
        return c,calls

    def reply(self, env, cursor, side='1'):
        row=copy.deepcopy(env['response']['data']['transactionList'][0])
        row.update(type=side,valueUsd='20',marketCap='5500')
        return dict(code='0',data=dict(transactionList=[row],cursor=cursor))

    def test_merge_is_offline_preserves_input_and_received_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);source,d,env=self.seed(b)
            before={str(p):p.read_bytes() for p in b.rglob('*.json')}
            with patch('history_followup.m.Client',side_effect=AssertionError('offline')):
                r=f.run(source,d,b/'out')
            self.assertEqual(r['requests'],0);self.assertEqual(r['observation_count'],1)
            self.assertEqual(len(r['historical_errors']),1);self.assertEqual(r['errors'],[])
            for p,data in before.items():self.assertEqual(Path(p).read_bytes(),data)
            rows=json.loads((b/'out/observations.json').read_text())
            self.assertEqual(rows[0]['observed_at'],env['received_at'])
            self.assertIsNone(rows[0]['provider_usd_value'])

    def test_two_requests_follow_cursor_and_original_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);source,d,env=self.seed(b)
            client,calls=self.client(env,[self.reply(env,'next'),self.reply(env,'more','2')])
            r=f.run(source,d,b/'out',resume=True,client=client)
            self.assertEqual(len(calls),2);self.assertEqual(r['observation_count'],3)
            from urllib.parse import urlsplit,parse_qs
            params=[parse_qs(urlsplit(u).query) for u in calls]
            self.assertEqual([p['cursor'][0] for p in params],['seed-cursor','next'])
            for p in params:
                for k,v in env['params'].items():self.assertEqual(p[k],[v])
            self.assertEqual(r['audits'][0]['stop_reason'],'PAGE_BUDGET_REACHED')
            self.assertEqual(r['side_counts'],dict(transfer_in=1,buy=1,sell=1))

    def test_repeated_cursor_stops_after_one_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);source,d,env=self.seed(b)
            client,calls=self.client(env,[self.reply(env,'seed-cursor')])
            r=f.run(source,d,b/'out',resume=True,client=client)
            self.assertEqual(len(calls),1)
            self.assertEqual(r['audits'][0]['stop_reason'],'REPEATED_CURSOR')

    def test_empty_page_with_cursor_does_not_claim_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);source,d,env=self.seed(b)
            client,calls=self.client(env,[dict(code='0',data=dict(transactionList=[],cursor='next'))])
            r=f.run(source,d,b/'out',resume=True,client=client)
            self.assertEqual(len(calls),1)
            self.assertEqual(r['audits'][0]['stop_reason'],'EMPTY_PAGE_WITH_CURSOR')
            self.assertFalse(r['audits'][0]['complete_history'])

    def test_cursor_exhausted_still_not_accounting_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);source,d,env=self.seed(b)
            client,calls=self.client(env,[self.reply(env,'0')])
            r=f.run(source,d,b/'out',resume=True,client=client)
            self.assertEqual(len(calls),1);self.assertTrue(r['audits'][0]['provider_cursor_exhausted'])
            self.assertFalse(r['accounting_ready']);self.assertFalse(r['audits'][0]['complete_history'])

    def test_exhausted_seed_does_not_create_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);source,d,env=self.seed(b,'0')
            with patch('history_followup.m.Client',side_effect=AssertionError('no request')):
                r=f.run(source,d,b/'out',resume=True)
            self.assertEqual(r['requests'],0)

    def test_failure_keeps_prior_page_and_records_active_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);source,d,env=self.seed(b)
            client,calls=self.client(env,[self.reply(env,'next'),m.IntakeError('NETWORK_ERROR')])
            r=f.run(source,d,b/'out',resume=True,client=client)
            self.assertEqual(r['requests'],2);self.assertEqual(r['observation_count'],2)
            self.assertEqual(len(r['errors']),1);self.assertEqual(len(r['historical_errors']),1)
            self.assertTrue((b/'out/raw/history_002.json').exists())

    def test_schema_mismatch_keeps_raw_response_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);source,d,env=self.seed(b)
            client,calls=self.client(env,[dict(code='0',data=dict(transactionList=[]))])
            r=f.run(source,d,b/'out',resume=True,client=client)
            self.assertEqual(len(calls),1);self.assertEqual(r['observation_count'],1)
            self.assertEqual(r['status'],'PARTIAL_WITH_ERRORS')
            self.assertTrue((b/'out/raw/history_002.json').exists())

    def test_altered_window_rejected_before_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);source,d,env=self.seed(b)
            data=json.loads(d.read_text());data['params']['begin']='1784700000001';d.write_text(json.dumps(data))
            with patch('history_followup.m.Client',side_effect=AssertionError('no request')):
                with self.assertRaises(m.SchemaError):f.run(source,d,b/'out',resume=True)
            self.assertFalse((b/'out').exists())

    def test_nested_output_rejected_before_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);source,d,env=self.seed(b)
            with self.assertRaises(m.IntakeError):f.run(source,d,source/'out',resume=True)

    def test_duplicates_are_flagged_and_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);source,d,env=self.seed(b)
            client,calls=self.client(env,[copy.deepcopy(env['response'])])
            r=f.run(source,d,b/'out',resume=True,client=client)
            self.assertEqual(r['observation_count'],2)
            self.assertEqual(r['audits'][0]['duplicate_payload_occurrences'],1)

    def test_html_payload_cannot_close_script_element(self):
        text=f.report_html({'warning':'</script><img src=x onerror=alert(1)>'},[])
        self.assertNotIn('</script><img',text)
        self.assertIn('\\u003c/script>',text)

    def test_default_resume_client_disables_retries_and_caps_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);source,d,env=self.seed(b)
            with patch('history_followup.m.Client') as constructor:
                constructor.return_value.get.side_effect=m.IntakeError('HTTP_429')
                constructor.return_value.requests=1
                f.run(source,d,b/'out',resume=True,creds=['fixture']*3)
            self.assertEqual(constructor.call_args.kwargs,dict(budget=2,retries=0))
            self.assertEqual(constructor.return_value.get.call_count,1)


if __name__=='__main__':unittest.main()
