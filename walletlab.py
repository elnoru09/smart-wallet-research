"""Dependency-free wallet profiling. No signing, trading or network requests."""
import argparse
import csv
import hashlib
import html
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

EPS = 1e-8


def ts(value):
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Timestamp must include timezone: ' + value)
    return d.timestamp()


def number(value):
    n = float(value)
    if not math.isfinite(n) or n < 0:
        raise ValueError('Expected finite nonnegative number: ' + str(value))
    return n


def read_csv(path, required):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        r = csv.DictReader(f)
        missing = set(required) - set(r.fieldnames or [])
        if missing:
            raise ValueError(f'{path}: missing columns {sorted(missing)}')
        return list(r)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x' if exclusive else 'w', encoding='utf-8') as f:
        json.dump(value, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write('\n')


def load_inputs(folder, asof):
    folder = Path(folder)
    wallets = read_csv(folder/'wallets.csv', ['wallet', 'source', 'discovered_at', 'history_complete'])
    trades = read_csv(folder/'trades.csv', ['event_id', 'wallet', 'token', 'timestamp',
        'observed_at', 'sequence', 'side', 'quantity', 'usd_value', 'fee_usd', 'entry_mc_usd'])
    quotes = read_csv(folder/'quotes.csv', ['wallet', 'token', 'timestamp', 'observed_at',
        'quantity', 'liquidation_value_usd', 'executable', 'source'])
    known_wallets = {}
    for r in wallets:
        t = ts(r['discovered_at'])
        if r['history_complete'] not in ('true', 'false'):
            raise ValueError('history_complete must be true or false')
        if t <= asof:
            if r['wallet'] in known_wallets:
                raise ValueError('Duplicate wallet: merge sources in one row')
            known_wallets[r['wallet']] = r
    accepted, ignored = [], 0
    seen = set()
    for r in trades:
        if r['event_id'] in seen:
            raise ValueError('Duplicate event_id: ' + r['event_id'])
        seen.add(r['event_id'])
        r = dict(r)
        r['_t'], r['_seen'] = ts(r['timestamp']), ts(r['observed_at'])
        if r['_seen'] < r['_t']:
            raise ValueError('observed_at precedes transaction')
        if max(r['_t'], r['_seen']) > asof or r['wallet'] not in known_wallets:
            ignored += 1
            continue
        r['sequence'] = int(r['sequence'])
        if r['side'] not in ('buy', 'sell', 'transfer_in', 'transfer_out'):
            raise ValueError('Unknown side')
        for k in ('quantity', 'usd_value', 'fee_usd'):
            r[k] = number(r[k])
        if r['quantity'] <= 0:
            raise ValueError('quantity must be positive')
        r['entry_mc_usd'] = number(r['entry_mc_usd']) if r['entry_mc_usd'] else None
        if r['side'] == 'buy' and r['usd_value'] <= 0:
            raise ValueError('Buy cost must be positive')
        if r['entry_mc_usd'] is not None and r['entry_mc_usd'] <= 0:
            raise ValueError('Market cap must be positive if provided')
        accepted.append(r)
    order_keys = set()
    for r in accepted:
        key = (r['wallet'], r['token'], r['_t'], r['sequence'])
        if key in order_keys:
            raise ValueError('Ambiguous order: use distinct sequence for same timestamp')
        order_keys.add(key)
    for r in quotes:
        r['_t'], r['_seen'] = ts(r['timestamp']), ts(r['observed_at'])
        if r['_seen'] < r['_t']:
            raise ValueError('Quote observed_at precedes quote timestamp')
        for k in ('quantity', 'liquidation_value_usd'):
            r[k] = number(r[k])
        if r['executable'] not in ('true', 'false'):
            raise ValueError('Quote executable must be true or false')
    return known_wallets, accepted, quotes, ignored


def new_episode(wallet, token, idx, event):
    return dict(wallet=wallet, token=token, episode=idx, first_buy=event['_t'],
        last_buy=event['_t'], first_entry_mc_usd=event['entry_mc_usd'],
        quantity=0., bought=0., sold=0., buy_cost=0., cost_remaining=0.,
        net_sale_proceeds=0., realized_pnl=0., first_sell=None, half_sold_at=None,
        closed_at=None, last_event=event['_t'], mc_weighted_sum=0., mc_cost=0.,
        primary_entry_spend_usd=0., secondary_entry_spend_usd=0., flags=[])


def profile(folder, as_of, config):
    if config['minimum_hold_days'] is not None or config['minimum_win_rate'] is not None:
        raise ValueError('V0.2 profiles hold/win rate descriptively; threshold filters are not implemented')
    if not 0 < config['secondary_entry_mc_usd'] < config['primary_entry_mc_usd']:
        raise ValueError('Require 0 < secondary MC < primary MC')
    if config['max_quote_age_seconds'] < 0:
        raise ValueError('Quote age must be nonnegative')
    asof = ts(as_of)
    wallets, trades, quotes, ignored = load_inputs(folder, asof)
    grouped = defaultdict(list)
    for r in trades:
        grouped[(r['wallet'], r['token'])].append(r)
    episodes, exclusions = [], []
    for (wallet, token), events in sorted(grouped.items()):
        events.sort(key=lambda r: (r['_t'], r['sequence'], r['event_id']))
        # Transfers contaminate basis / custody. Keep evidence, never assume zero cost.
        reason = None
        if any(r['side'].startswith('transfer') for r in events):
            reason = 'TRANSFER_PRESENT_UNRESOLVED_BASIS_OR_CUSTODY'
        elif wallets[wallet]['history_complete'] != 'true':
            reason = 'HISTORY_NOT_DECLARED_COMPLETE'
        if reason:
            exclusions.append(dict(wallet=wallet, token=token, reason=reason))
            continue
        built, e = [], None
        for r in events:
            q = r['quantity']
            if r['side'] == 'buy':
                if e is None:
                    e = new_episode(wallet, token, len(built)+1, r)
                    built.append(e)
                cost = r['usd_value'] + r['fee_usd']
                e['quantity'] += q
                e['bought'] += q
                e['buy_cost'] += cost
                e['cost_remaining'] += cost
                e['last_buy'] = r['_t']
                if r['entry_mc_usd'] is not None:
                    e['mc_weighted_sum'] += r['entry_mc_usd'] * cost
                    e['mc_cost'] += cost
                    if r['entry_mc_usd'] < config['primary_entry_mc_usd']:
                        e['primary_entry_spend_usd'] += cost
                    if r['entry_mc_usd'] < config['secondary_entry_mc_usd']:
                        e['secondary_entry_spend_usd'] += cost
            else:
                if e is None or q > e['quantity'] + EPS:
                    reason = 'SELL_EXCEEDS_RECONSTRUCTED_INVENTORY'
                    break
                q = min(q, e['quantity'])
                allocated = e['cost_remaining'] * q/e['quantity']
                proceeds = r['usd_value'] - r['fee_usd']
                e['quantity'] -= q
                e['sold'] += q
                e['cost_remaining'] -= allocated
                e['net_sale_proceeds'] += proceeds
                e['realized_pnl'] += proceeds - allocated
                if e['first_sell'] is None:
                    e['first_sell'] = r['_t']
                if e['half_sold_at'] is None and e['sold'] >= e['bought']/2:
                    e['half_sold_at'] = r['_t']
                if e['quantity'] <= EPS:
                    e['quantity'] = e['cost_remaining'] = 0.
                    e['closed_at'] = r['_t']
                    e = None
            if e is not None:
                e['last_event'] = r['_t']
        if reason:
            exclusions.append(dict(wallet=wallet, token=token, reason=reason))
        else:
            episodes.extend(built)
    for e in episodes:
        first, close = e['first_buy'], e['closed_at']
        e['age_days'] = ((close if close is not None else asof)-first)/86400
        e['days_since_last_buy'] = (asof-e['last_buy'])/86400 if close is None else None
        e['retained_fraction'] = e['quantity']/e['bought']
        e['first_sell_days'] = (e['first_sell']-first)/86400 if e['first_sell'] is not None else None
        e['half_sold_days'] = (e['half_sold_at']-first)/86400 if e['half_sold_at'] is not None else None
        e['cost_weighted_entry_mc_usd'] = e['mc_weighted_sum']/e['mc_cost'] if e['mc_cost'] else None
        e['mc_cost_coverage'] = e['mc_cost']/e['buy_cost']
        e['primary_entry_spend_fraction'] = e['primary_entry_spend_usd']/e['buy_cost']
        e['secondary_entry_spend_fraction'] = e['secondary_entry_spend_usd']/e['buy_cost']
        mc = e['first_entry_mc_usd']
        e['entry_group'] = ('UNKNOWN' if mc is None else 'BELOW_SECONDARY' if mc < config['secondary_entry_mc_usd']
                            else 'SECONDARY_TO_PRIMARY' if mc < config['primary_entry_mc_usd'] else 'OUTSIDE')
        e['liquidation_value_usd'] = None
        e['open_pnl_usd'] = None
        e['quote_timestamp'] = e['quote_source'] = None
        if close is not None:
            e['position_status'] = 'CLOSED'
            e['total_pnl_usd'] = e['realized_pnl']
        else:
            eligible = [q for q in quotes if q['wallet'] == e['wallet'] and q['token'] == e['token']
                        and max(q['_t'], q['_seen']) <= asof and q['_t'] >= e['last_event']
                        and asof-q['_t'] <= config['max_quote_age_seconds']
                        and q['executable'] == 'true' and q['source'].strip()
                        and math.isclose(q['quantity'], e['quantity'], rel_tol=1e-8, abs_tol=EPS)]
            if not eligible:
                e['position_status'] = 'OPEN_UNVALUED'
                e['total_pnl_usd'] = None
            else:
                q = max(eligible, key=lambda q: (q['_t'], q['_seen']))
                e['quote_timestamp'], e['quote_source'] = q['timestamp'], q['source']
                e['liquidation_value_usd'] = q['liquidation_value_usd']
                e['open_pnl_usd'] = q['liquidation_value_usd']-e['cost_remaining']
                e['total_pnl_usd'] = e['realized_pnl']+e['open_pnl_usd']
                e['position_status'] = ('OPEN_UNDERWATER' if e['open_pnl_usd'] < -EPS else
                                        'OPEN_PROFIT' if e['open_pnl_usd'] > EPS else 'OPEN_FLAT')
        e['cash_recovered_ratio'] = e['net_sale_proceeds']/e['buy_cost']
        e['residual_diagnostic'] = e['quantity'] > 0 and e['retained_fraction'] < .1
    summaries = []
    for wallet, meta in sorted(wallets.items()):
        es = [e for e in episodes if e['wallet'] == wallet]
        small = [e for e in es if e['entry_group'] in ('BELOW_SECONDARY', 'SECONDARY_TO_PRIMARY')]
        closed = [e for e in small if e['closed_at'] is not None]
        open_es = [e for e in small if e['closed_at'] is None]
        token_pnl = defaultdict(float)
        for e in closed:
            token_pnl[e['token']] += e['total_pnl_usd']
        best = max(token_pnl.values()) if token_pnl else None
        summaries.append(dict(wallet=wallet, sources=meta['source'], discovered_at=meta['discovered_at'],
            history_declared_complete=meta['history_complete'] == 'true',
            analyzable_episodes=len(es), small_entry_episodes=len(small),
            small_entry_distinct_tokens=len({e['token'] for e in small}),
            closed_small_episodes=len(closed), open_small_episodes=len(open_es),
            closed_small_win_rate=sum(e['total_pnl_usd'] > EPS for e in closed)/len(closed) if closed else None,
            realized_small_pnl_usd=sum(e['realized_pnl'] for e in small),
            closed_small_pnl_usd=sum(e['total_pnl_usd'] for e in closed),
            closed_pnl_without_best_token_usd=sum(token_pnl.values())-best if best is not None else None,
            median_closed_hold_days=statistics.median(e['age_days'] for e in closed) if closed else None,
            open_small_remaining_cost_usd=sum(e['cost_remaining'] for e in open_es),
            open_underwater_positions=sum(e['position_status'] == 'OPEN_UNDERWATER' for e in open_es),
            open_unvalued_positions=sum(e['position_status'] == 'OPEN_UNVALUED' for e in open_es),
            unresolved_tokens=sum(e['wallet'] == wallet for e in exclusions),
            evidence_status=('INSUFFICIENT_DESCRIPTION_SAMPLE' if len(small) < config['minimum_episodes_for_description']
                             else 'DESCRIPTIVE_ONLY_NOT_VALIDATED'),
            label='WATCH_ONLY'))
    return dict(version=config['version'], data_mode='SYNTHETIC_DEMO' if (Path(folder)/'SYNTHETIC_DEMO.txt').exists()
                else 'IMPORTED_UNVERIFIED', as_of=as_of, config=config, config_sha256=digest(config),
                input_sha256={name:file_hash(Path(folder)/name) for name in ('wallets.csv','trades.csv','quotes.csv')},
                ignored_future_or_unknown_wallet_events=ignored, wallets=summaries, episodes=episodes,
                unresolved=exclusions, limitations=[
                    'No prospective profitability claim; no copy trading simulation in V0.2.',
                    'Closed-only win rate excludes open positions and MUST NOT be used alone.',
                    'MC, complete-history declarations and executable liquidation quotes are supplied, not independently verified.',
                    'Wallets from current leaderboards introduce retrospective selection bias.',
                    'Transfers quarantine the entire wallet-token history; no zero-cost basis inference.',
                    'Open positions are censored; median closed duration is not typical duration of all positions.',
                    'No minimum hold or win-rate filter; 20-episode count is a display flag, not statistical proof.'
                ])


def export_csv(path, rows):
    if not rows:
        Path(path).write_text('', encoding='utf-8')
        return
    with Path(path).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), extrasaction='raise')
        w.writeheader()
        w.writerows(rows)


def html_report(result):
    def cell(v):
        if v is None:
            return '—'
        return html.escape(f'{v:.4f}' if isinstance(v, float) else str(v))
    def table(rows, columns):
        return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+html.escape(label)+'</th>' for key,label in columns)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+cell(r.get(key))+'</td>' for key,label in columns)+'</tr>' for r in rows)+'</tbody></table></div>'
    wallet_cols = [('wallet','Wallet'),('small_entry_episodes','Entry <10k'),('closed_small_win_rate','Win rate CLOSED ONLY (0–1)'),('open_underwater_positions','Sangkut'),('open_unvalued_positions','Belum dinilai'),('realized_small_pnl_usd','Realized PnL USD'),('closed_pnl_without_best_token_usd','Closed PnL tanpa token terbaik'),('evidence_status','Status bukti')]
    positions = [e for e in result['episodes'] if e['closed_at'] is None]
    pos_cols = [('wallet','Wallet'),('token','Token'),('entry_group','MC entry pertama'),('age_days','Umur (hari)'),('days_since_last_buy','Sejak tambah terakhir'),('retained_fraction','Porsi token tersisa (0–1)'),('cash_recovered_ratio','Modal kembali (rasio)'),('cost_remaining','Modal tersisa USD'),('open_pnl_usd','Open PnL USD'),('position_status','Status')]
    return '<!doctype html><html lang="id"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Wallet Research</title><style>body{font:15px system-ui;background:#101923;color:#e4edf5;margin:32px auto;max-width:1400px;padding:0 24px}h1{font-size:32px}h2{margin-top:32px}.badge{background:#554019;color:#ffe6a1;padding:14px;border-radius:8px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:12px;border-bottom:1px solid #354350;text-align:left;white-space:nowrap}th{background:#233244}p,li{line-height:1.7;color:#bdcddd}</style><h1>Smart Wallet Research · V0.2</h1><div class="badge">'+html.escape(result['data_mode'])+' · WATCH ONLY · Bukan rekomendasi entry</div><p>Snapshot: '+html.escape(result['as_of'])+' · US$20/token adalah konfigurasi rencana, belum hasil simulasi.</p><p>MC secondary: US$'+str(result['config']['secondary_entry_mc_usd'])+' · primary: US$'+str(result['config']['primary_entry_mc_usd'])+'. Label entry pertama belum menjamin mayoritas modal dibeli pada MC tersebut; periksa cost-weighted MC di CSV.</p><h2>Profil wallet</h2>'+table(result['wallets'],wallet_cols)+'<h2>Posisi terbuka dan sangkut</h2>'+table(positions,pos_cols)+'<h2>Kualitas dan batas data</h2><p>Token unresolved: '+str(len(result['unresolved']))+'</p><ul>'+''.join('<li>'+html.escape(s)+'</li>' for s in result['limitations'])+'</ul></html>'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    s = sub.add_parser('screen')
    s.add_argument('--input', default='examples')
    s.add_argument('--as-of', required=True)
    s.add_argument('--config', default='config.json')
    s.add_argument('--out', default='output')
    f = sub.add_parser('freeze')
    f.add_argument('--report', required=True)
    f.add_argument('--out', required=True)
    args = p.parse_args()
    if args.command == 'screen':
        cfg = json.loads(Path(args.config).read_text(encoding='utf-8'))
        if cfg['execution_enabled'] or cfg['mode'] != 'research_only':
            raise ValueError('Only research_only mode supported')
        result = profile(args.input, args.as_of, cfg)
        out = Path(args.out)
        if out.exists() and any(out.iterdir()):
            raise ValueError('Output folder must be empty: preserve prior experiment artifacts')
        out.mkdir(parents=True, exist_ok=True)
        write_json(out/'report.json', result)
        export_csv(out/'wallet_profiles.csv', result['wallets'])
        export_csv(out/'position_episodes.csv', result['episodes'])
        export_csv(out/'unresolved.csv', result['unresolved'])
        (out/'report.html').write_text(html_report(result), encoding='utf-8')
        print(json.dumps(dict(report=str(out/'report.html'), data_mode=result['data_mode'], wallets=len(result['wallets']), episodes=len(result['episodes']), unresolved=len(result['unresolved'])), indent=2))
    else:
        r = json.loads(Path(args.report).read_text(encoding='utf-8'))
        now = datetime.now(timezone.utc)
        if ts(r['as_of']) > now.timestamp():
            raise ValueError('Cannot freeze a future snapshot')
        record = dict(schema='candidate-registry-0.1', recorded_at=now.isoformat(),
            snapshot_as_of=r['as_of'], data_mode=r['data_mode'], config_sha256=r['config_sha256'],
            report_sha256=file_hash(args.report), source_code_sha256=file_hash(__file__), input_sha256=r['input_sha256'],
            wallets=[w['wallet'] for w in r['wallets']], selection_rule='all wallets in report; no performance ranking',
            status='DEMO_ONLY' if r['data_mode'] == 'SYNTHETIC_DEMO' else 'CANDIDATES_RECORDED_NOT_STRATEGY_VALIDATED',
            note='Prospective evaluation starts no earlier than recorded_at. Local hash is not an external timestamp authority.')
        write_json(args.out, record, exclusive=True)
        print(str(args.out))


if __name__ == '__main__':
    main()
