import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from okx_reader import (observation, SchemaError, IntakeError, replay, retry_failed_history,
                        response_diagnostic, HISTORY)

ROOT=Path(__file__).resolve().parents[1]
ADDRESS='So11111111111111111111111111111111111111112'
TOKEN='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'


class TransferReplayTests(unittest.TestCase):
    def row(self,side='3',value=''):
        return dict(type=side,chainIndex='501',tokenContractAddress=TOKEN,
                    amount='10',valueUsd=value,price='',marketCap='',time='1791763200000')

    def envelope(self):
        return dict(received_at='2026-10-20T00:00:00Z',params=dict(chainIndex='501',
            walletAddress=ADDRESS,begin='1784700000000',end='1792470000000',limit='100'),
            response={'code':'0','data':{'transactionList':[self.row()],'cursor':'0'}})

    def test_transfer_in_missing_usd_is_unknown_not_zero(self):
        r=observation(self.row(),self.envelope(),ADDRESS,1)
        self.assertIsNone(r['provider_usd_value'])
        self.assertEqual(r['usd_value_status'],'UNKNOWN')
        self.assertEqual(r['side'],'transfer_in')
        self.assertEqual(r['quantity'],10)

    def test_transfer_out_null_usd_is_unknown(self):
        r=observation(self.row('4',None),self.envelope(),ADDRESS,1)
        self.assertIsNone(r['provider_usd_value'])
        self.assertEqual(r['side'],'transfer_out')

    def test_explicit_zero_is_distinct_from_unknown(self):
        r=observation(self.row('3','0'),self.envelope(),ADDRESS,1)
        self.assertEqual(r['provider_usd_value'],0)
        self.assertEqual(r['usd_value_status'],'PROVIDER_REPORTED_UNRECONCILED')

    def test_buy_and_sell_still_require_usd(self):
        for side in ('1','2'):
            with self.assertRaisesRegex(SchemaError,'MISSING_TRADE_USD_VALUE'):
                observation(self.row(side),self.envelope(),ADDRESS,1)

    def test_invalid_transfer_usd_not_silently_unknown(self):
        with self.assertRaises(SchemaError):
            observation(self.row('3','not-a-number'),self.envelope(),ADDRESS,1)

    def write_run(self,source):
        source.mkdir()
        raw=source/'raw/wallet_001';raw.mkdir(parents=True)
        env=self.envelope()
        (raw/'history_001.json').write_text(json.dumps(env))
        failure={'stage':'history','error':'HTTP_400: historical failure',
                 'diagnostic':{'http_status':400,'provider_code':'51000'},'wallet':ADDRESS}
        report=dict(candidates=[dict(wallet=ADDRESS,source='OKX',
            discovered_at='2026-10-20T00:00:00Z',history_complete='false')],
            audits=[dict(wallet=ADDRESS,returned_records=1,parsed_observations=0,
               malformed_records=1,stop_reason='PAGE_BUDGET_REACHED',request_failure=None)],
            errors=[dict(wallet=ADDRESS,stage='history',issue='INVALID_OR_UNSUPPORTED_RECORD')],
            data_mode='SYNTHETIC_FIXTURE',started_at='2026-10-20T00:00:00Z',
            finished_at='2026-10-20T00:00:00Z',requests=1,config_sha256='fixture',
            observation_count=0)
        (source/'intake_report.json').write_text(json.dumps(report))
        return env,report,failure

    def test_replay_no_network_preserves_source_and_original_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'original';out=Path(tmp)/'replay'
            env,_,_=self.write_run(source)
            before={str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()}
            with patch('okx_reader.Client',side_effect=AssertionError('No client in replay')):
                r=replay(source,out)
            self.assertEqual(r['parsed_after'],1)
            self.assertEqual(r['unknown_usd_transfers'],1)
            self.assertEqual(r['requests'],0)
            self.assertFalse(r['accounting_ready'])
            self.assertFalse(r['audits'][0]['complete_history'])
            self.assertEqual(before,{str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()})
            import csv
            with (out/'trade_observations.csv').open(encoding='utf-8-sig') as f:
                rows=list(csv.DictReader(f))
            self.assertEqual(rows[0]['observed_at'],env['received_at'])
            self.assertEqual(rows[0]['provider_usd_value'],'')

    def test_replay_refuses_nested_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'original';self.write_run(source)
            with self.assertRaises(IntakeError):replay(source,source/'replay')

    def test_safe_parameter_name_extraction(self):
        d=response_diagnostic(400,b'{"code":"51000","msg":"Parameter walletAddress error: sensitive"}')
        self.assertEqual(d['parameter_name'],'walletAddress')
        self.assertNotIn('sensitive',str(d))

    def test_unknown_parameter_and_credentials_not_echoed(self):
        d=response_diagnostic(400,b'{"code":"51000","msg":"Parameter APIKEY_SECRET error"}',['APIKEY_SECRET'])
        self.assertNotIn('parameter_name',d)
        self.assertNotIn('APIKEY_SECRET',str(d))

    def test_targeted_retry_keeps_original_window_and_one_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'original'
            env,report,failure=self.write_run(source)
            report['audits'][0].update(returned_records=0,request_failure=failure,
                requested_begin_ms=int(env['params']['begin']),requested_end_ms=int(env['params']['end']))
            (source/'intake_report.json').write_text(json.dumps(report))
            client=Mock();client.requests=1
            client.get.side_effect=IntakeError('HTTP_400',{'http_status':400,'provider_code':'51000','parameter_name':'begin'})
            with patch('okx_reader.Client',return_value=client) as constructor, contextlib.redirect_stdout(io.StringIO()):
                code=retry_failed_history(source,Path(tmp)/'diagnosis.json',['fixture-key','fixture-secret','fixture-pass'])
            self.assertEqual(code,2)
            self.assertEqual(constructor.call_args.kwargs['budget'],1)
            client.get.assert_called_once_with(HISTORY,env['params'])
            r=json.loads((Path(tmp)/'diagnosis.json').read_text())
            self.assertFalse(r['selection_changed'])
            self.assertTrue(r['original_window_retained'])


if __name__=='__main__':unittest.main()
