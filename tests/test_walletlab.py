import csv
import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from walletlab import profile, html_report, write_json

ROOT = Path(__file__).resolve().parents[1]
ASOF = '2026-10-20T12:00:00Z'


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        self.cfg = json.loads((ROOT/'config.json').read_text())
        self.wallets = [dict(wallet='A',source='test',discovered_at='2026-10-01T00:00:00Z',history_complete='true')]
        self.trades = []
        self.quotes = []

    def add(self, side, q, usd, day=1, mc='', fee=0):
        self.trades.append(dict(event_id=str(len(self.trades)),wallet='A',token='T',
            timestamp=f'2026-10-{day:02}T00:00:00Z',observed_at=f'2026-10-{day:02}T00:00:01Z',
            sequence=len(self.trades),side=side,quantity=q,usd_value=usd,fee_usd=fee,entry_mc_usd=mc))

    def quote(self, q, value, time='2026-10-20T11:59:00Z', executable='true'):
        self.quotes.append(dict(wallet='A',token='T',timestamp=time,observed_at=time,
            quantity=q,liquidation_value_usd=value,executable=executable,source='test'))

    def run_profile(self):
        fields = {
            'wallets.csv':['wallet','source','discovered_at','history_complete'],
            'trades.csv':['event_id','wallet','token','timestamp','observed_at','sequence','side','quantity','usd_value','fee_usd','entry_mc_usd'],
            'quotes.csv':['wallet','token','timestamp','observed_at','quantity','liquidation_value_usd','executable','source']}
        for name, rows in [('wallets.csv',self.wallets),('trades.csv',self.trades),('quotes.csv',self.quotes)]:
            with (self.p/name).open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=fields[name]);w.writeheader();w.writerows(rows)
        return profile(self.p,ASOF,self.cfg)

    def test_partial_sale_cost_and_liquidation(self):
        self.add('buy',100,20,mc=5000,fee=1)
        self.add('sell',50,15,day=5,fee=1)
        self.quote(50,5)
        e=self.run_profile()['episodes'][0]
        self.assertAlmostEqual(e['realized_pnl'],3.5)
        self.assertAlmostEqual(e['cost_remaining'],10.5)
        self.assertAlmostEqual(e['total_pnl_usd'],-2)
        self.assertEqual(e['position_status'],'OPEN_UNDERWATER')

    def test_missing_quote_not_counted_as_win(self):
        self.add('buy',100,10,mc=5000)
        r=self.run_profile()
        self.assertEqual(r['episodes'][0]['position_status'],'OPEN_UNVALUED')
        self.assertIsNone(r['wallets'][0]['closed_small_win_rate'])

    def test_dust_and_recovered_capital(self):
        self.add('buy',100,10,mc=5000)
        self.add('sell',99,15)
        e=self.run_profile()['episodes'][0]
        self.assertTrue(e['residual_diagnostic'])
        self.assertAlmostEqual(e['cash_recovered_ratio'],1.5)
        self.assertEqual(e['first_sell_days'],0)

    def test_transfers_quarantined_not_free_profit(self):
        self.add('transfer_in',100,0)
        self.add('sell',100,100)
        r=self.run_profile()
        self.assertFalse(r['episodes'])
        self.assertEqual(len(r['unresolved']),1)

    def test_future_trade_ignored_by_observed_time(self):
        self.add('buy',100,10,mc=5000)
        self.add('sell',100,100,day=19)
        self.trades[-1]['observed_at']='2026-10-21T00:00:00Z'
        r=self.run_profile()
        self.assertEqual(r['episodes'][0]['quantity'],100)
        self.assertEqual(r['ignored_future_or_unknown_wallet_events'],1)

    def test_future_quote_not_used(self):
        self.add('buy',100,10,mc=5000)
        self.quote(100,100,time='2026-10-21T00:00:00Z')
        self.assertIsNone(self.run_profile()['episodes'][0]['total_pnl_usd'])

    def test_stale_quote_not_used(self):
        self.add('buy',100,10,mc=5000)
        self.quote(100,100,time='2026-10-19T00:00:00Z')
        self.assertIsNone(self.run_profile()['episodes'][0]['total_pnl_usd'])

    def test_wrong_quantity_quote_not_scaled(self):
        self.add('buy',100,10,mc=5000)
        self.quote(1,1)
        self.assertEqual(self.run_profile()['episodes'][0]['position_status'],'OPEN_UNVALUED')

    def test_nonexecutable_quote_not_used(self):
        self.add('buy',100,10,mc=5000)
        self.quote(100,100,executable='false')
        self.assertIsNone(self.run_profile()['episodes'][0]['total_pnl_usd'])

    def test_reentry_separate_episodes(self):
        self.add('buy',100,10,mc=5000)
        self.add('sell',100,12,day=5)
        self.add('buy',100,10,day=10,mc=15000)
        r=self.run_profile()
        self.assertEqual(len(r['episodes']),2)
        self.assertEqual(r['wallets'][0]['small_entry_episodes'],1)
        self.assertEqual(r['wallets'][0]['closed_small_win_rate'],1)

    def test_oversell_quarantines_entire_token(self):
        self.add('buy',100,10,mc=5000)
        self.add('sell',101,10,day=5)
        self.assertFalse(self.run_profile()['episodes'])

    def test_duplicate_event_rejected(self):
        self.add('buy',100,10,mc=5000)
        self.trades.append(dict(self.trades[0]))
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            self.run_profile()

    def test_nonfinite_money_rejected(self):
        self.add('buy',100,'nan',mc=5000)
        with self.assertRaises(ValueError):self.run_profile()

    def test_incomplete_history_quarantined(self):
        self.wallets[0]['history_complete']='false'
        self.add('buy',100,10,mc=5000)
        self.assertEqual(len(self.run_profile()['unresolved']),1)

    def test_mc_first_buy_does_not_hide_late_accumulation(self):
        self.add('buy',1,1,mc=5000)
        self.add('buy',100,100,day=2,mc=50000)
        e=self.run_profile()['episodes'][0]
        self.assertEqual(e['entry_group'],'BELOW_SECONDARY')
        self.assertGreater(e['cost_weighted_entry_mc_usd'],49000)
        self.assertAlmostEqual(e['primary_entry_spend_fraction'],1/101)

    def test_exact_thresholds_are_strict(self):
        self.add('buy',100,10,mc=10000)
        self.assertEqual(self.run_profile()['episodes'][0]['entry_group'],'OUTSIDE')

    def test_quote_older_than_latest_event_not_used(self):
        self.add('buy',100,10,day=20,mc=5000)
        self.trades[0]['timestamp']='2026-10-20T11:59:30Z'
        self.trades[0]['observed_at']='2026-10-20T11:59:31Z'
        self.quote(100,100,time='2026-10-20T11:59:00Z')
        self.assertIsNone(self.run_profile()['episodes'][0]['total_pnl_usd'])

    def test_closed_profit_and_open_loss_both_visible(self):
        self.add('buy',100,10,mc=5000)
        self.add('sell',100,20,day=5)
        self.add('buy',100,10,day=10,mc=5000)
        self.quote(100,1)
        r=self.run_profile()
        self.assertEqual(r['wallets'][0]['closed_small_win_rate'],1)
        self.assertEqual(r['wallets'][0]['open_underwater_positions'],1)
        self.assertAlmostEqual(sum(e['total_pnl_usd'] for e in r['episodes']),1)

    def test_output_escaped(self):
        self.wallets[0]['source']='<script>alert(1)</script>'
        self.add('buy',100,10,mc=5000)
        r=self.run_profile()
        r['wallets'][0]['wallet']='<script>alert(1)</script>'
        self.assertNotIn('<script>',html_report(r))

    def test_registry_file_cannot_overwrite(self):
        p=self.p/'lock.json'
        write_json(p,{'a':1},exclusive=True)
        with self.assertRaises(FileExistsError):write_json(p,{'a':2},exclusive=True)


if __name__ == '__main__':unittest.main()
