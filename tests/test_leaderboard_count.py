import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from okx_reader import Client, LEADERBOARD, OVERVIEW, collect, leaderboard_rows, leaderboard_metadata


def b58(n):
    alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    data=n.to_bytes(32,'big');prefix='1'*(len(data)-len(data.lstrip(b'\0')))
    text=''
    while n:n,r=divmod(n,58);text=alphabet[r]+text
    return prefix+text


class CountTests(unittest.TestCase):
    def envelope(self,n=25):
        return dict(response={'code':'0','data':[{'walletAddress':b58(i)} for i in range(1,n+1)]},received_at='2026-10-20T00:00:00Z')

    def test_more_than_twenty_is_warning_not_schema_failure(self):
        env=self.envelope()
        rows=leaderboard_rows(env)
        self.assertEqual(len(rows),25)
        meta=leaderboard_metadata(env,len(rows))
        self.assertEqual(meta['warnings'][0]['observed'],25)

    def test_twenty_records_no_warning(self):
        env=self.envelope(20)
        self.assertEqual(leaderboard_metadata(env,20)['warnings'],[])

    def test_audit_still_collects_only_first_three_wallets(self):
        env=self.envelope()
        def transport(url,headers):
            if LEADERBOARD in url:payload=env['response']
            elif OVERVIEW in url:payload={'code':'0','data':{}}
            else:payload={'code':'0','data':{'transactionList':[],'cursor':'0'}}
            return 200,json.dumps(payload).encode()
        cl=Client(['fixture-long-key','fixture-long-secret','fixture-long-pass'],transport=transport,
                  clock=lambda:'2026-10-20T00:00:00Z',sleeper=lambda x:None,interval=0)
        with tempfile.TemporaryDirectory() as tmp:
            r=collect(cl,Path(tmp)/'run',fixture=True)
        self.assertEqual(len(r['candidates']),3)
        self.assertEqual([c['wallet'] for c in r['candidates']],[b58(i) for i in range(1,4)])
        self.assertEqual(r['discovery']['provider_record_count'],25)
        self.assertEqual(cl.requests,7)


if __name__=='__main__':unittest.main()
