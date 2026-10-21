import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from okx_reader import Client, LEADERBOARD, IntakeError, collect, cli_summary, response_diagnostic
from inspect_intake import safe_summary


class DiagnosticTests(unittest.TestCase):
    def client(self,status,body):
        return Client(['dummy-key','dummy-secret','dummy-pass'],transport=lambda u,h:(status,body),
            clock=lambda:'2026-10-20T00:00:00Z',sleeper=lambda t:None,interval=0)

    def test_http_failure_distinct_from_empty(self):
        with tempfile.TemporaryDirectory() as d:
            r=collect(self.client(401,b'{"code":"50111","msg":"dummy-secret"}'),Path(d)/'run')
            self.assertEqual(r['status'],'LEADERBOARD_REQUEST_OR_SCHEMA_FAILED')
            s=cli_summary(r)
            self.assertEqual(s['errors'][0]['diagnostic']['provider_code'],'50111')
            self.assertNotIn('dummy-secret',json.dumps(s))

    def test_success_empty_leaderboard_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            r=collect(self.client(200,b'{"code":"0","data":[]}'),Path(d)/'run')
            self.assertEqual(r['status'],'LEADERBOARD_EMPTY_RESPONSE')
            self.assertEqual(r['errors'],[])

    def test_provider_error_retains_numeric_code_not_message(self):
        c=self.client(200,b'{"code":"50011","msg":"dummy-pass"}')
        with self.assertRaises(IntakeError) as cm:c.get(LEADERBOARD,{})
        self.assertEqual(cm.exception.diagnostic['provider_code'],'50011')
        self.assertNotIn('dummy-pass',json.dumps(cm.exception.diagnostic))

    def test_html_failure_no_body_echo(self):
        d=response_diagnostic(403,b'<html>dummy-secret</html>',['dummy-secret'])
        self.assertEqual(d['response_format'],'HTML')
        self.assertNotIn('dummy-secret',json.dumps(d))

    def test_malicious_code_not_printed(self):
        d=response_diagnostic(401,b'{"code":"dummy-secret","msg":"password"}',['dummy-secret'])
        self.assertIsNone(d['provider_code'])

    def test_old_report_can_be_inspected_without_credential(self):
        r={'status':'NO_CANDIDATES_RECEIVED','candidates':[],
           'errors':[{'stage':'leaderboard','error':'HTTP_403: verify network'}]}
        s=safe_summary(r)
        self.assertEqual(s['errors'][0]['category'],'HTTP_403')
        self.assertEqual(s['network_requests_made_by_inspector'],0)

    def test_inspector_does_not_echo_unknown_fields_or_raw_messages(self):
        s=safe_summary({'status':'secret','candidates':[],
            'api_key':'sensitive','errors':[{'stage':'secret','error':'secret-body','msg':'secret-body'}]})
        self.assertNotIn('secret',json.dumps(s))


if __name__=='__main__':unittest.main()
