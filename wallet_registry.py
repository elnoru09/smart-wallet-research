"""Expandable Solana wallet candidate registry; bounded, read-only provider intake."""
import argparse
from collections import Counter
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import uuid
import webbrowser

import okx_reader as m
import history_followup as followup

ROOT = Path(__file__).resolve().parent
DISCOVERY_QUERIES = [dict(chainIndex='501', timeFrame='5', sortBy='1', walletType='3'),
                     dict(chainIndex='501', timeFrame='4', sortBy='3', walletType='3')]


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def local(root, value):
    root = Path(root).resolve(); path = (root/value).resolve()
    if Path(value).is_absolute() or path == root or root not in path.parents:
        raise m.IntakeError('Registry path must stay inside master folder')
    return path


def project_path(root, key, default):
    return local(root, read(Path(root)/'project.json').get(key, default))


def store(root):
    return Path(root)/'watchlist'


@contextmanager
def lock(root, filename='registry.lock'):
    folder = store(root); folder.mkdir(parents=True, exist_ok=True)
    path = folder/filename
    try:
        with path.open('x', encoding='utf-8') as f: f.write(m.utc())
    except FileExistsError:
        raise m.IntakeError('REGISTRY_BUSY: another update or interrupted process holds the lock') from None
    try: yield
    finally: path.unlink(missing_ok=True)


def save(root, state):
    state['updated_at'] = m.utc()
    path = store(root)/'registry.json'; temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state, indent=2, ensure_ascii=True)+'\n', encoding='utf-8')
    temp.replace(path)


def load(root):
    state = read(store(root)/'registry.json')
    if state.get('schema') != 'wallet-registry-0.2.6':
        raise m.SchemaError('Unsupported registry schema')
    wallets = [m.wallet_address(w['wallet']) for w in state['wallets']]
    if len(set(wallets)) != len(wallets): raise m.SchemaError('Registry contains duplicate wallet addresses')
    return state


def empty_history():
    return dict(status='NOT_FETCHED', stored_pages=0, parsed_observations=None,
                returned_records=None, summary=None, complete_history=False, accounting_ready=False)


def merge_leaderboard(state, envelope, source_file):
    if envelope.get('provider') != 'OKX' or envelope.get('path') != m.LEADERBOARD or str(envelope.get('response', {}).get('code')) != '0':
        raise m.SchemaError('Unexpected leaderboard source')
    if envelope.get('params', {}).get('chainIndex') != '501': raise m.SchemaError('Unexpected discovery chain')
    rows = m.leaderboard_rows(envelope)  # validate all addresses before mutation
    known = {w['wallet']: w for w in state['wallets']}
    if len(set(known)|{r['wallet'] for r in rows}) > 1000:
        raise m.IntakeError('REGISTRY_CAPACITY_1000: plan a larger registry explicitly')
    added = 0
    for rank, row in enumerate(rows, 1):
        wallet = row['wallet']
        provenance = dict(kind='OKX_LEADERBOARD', source_file=source_file,
            source_sha256=m.digest(envelope), received_at=envelope['received_at'],
            params=envelope['params'], provider_order=rank)
        if wallet not in known:
            entry = dict(wallet=wallet, first_discovered_at=envelope['received_at'],
                registered_at=m.utc(), sources=[], history=empty_history())
            state['wallets'].append(entry); known[wallet] = entry; added += 1
        if provenance not in known[wallet]['sources']: known[wallet]['sources'].append(provenance)
    return added, m.leaderboard_metadata(envelope, len(rows))


def set_history(entry, observations, audit, report_path):
    buys = [o for o in observations if o['side'] == 'buy']
    status = 'REQUEST_FAILED' if audit.get('request_failure') else 'PARTIAL_UNRECONCILED'
    if not audit.get('request_failure') and audit['returned_records'] == 0: status = 'EMPTY_REQUESTED_RANGE_UNVERIFIED'
    if not audit.get('request_failure') and audit.get('malformed_records', 0): status = 'PARTIAL_WITH_PARSE_ERRORS'
    entry['history'] = dict(status=status, stored_pages=audit.get('stored_pages', 0),
        parsed_observations=len(observations), returned_records=audit['returned_records'],
        oldest_record_at=min((o['timestamp'] for o in observations), default=None),
        latest_record_at=max((o['timestamp'] for o in observations), default=None),
        stop_reason=audit['stop_reason'], summary=dict(side_counts=dict(Counter(o['side'] for o in observations)),
            buy_mc_below_6k=sum(o['provider_event_mc_usd'] is not None and o['provider_event_mc_usd'] < 6000 for o in buys),
            buy_mc_below_10k=sum(o['provider_event_mc_usd'] is not None and o['provider_event_mc_usd'] < 10000 for o in buys)),
        report_path=report_path, retryable_systemic=bool(audit.get('request_failure') and systemic_failure(audit['request_failure'])),
        complete_history=False, accounting_ready=False)


def bootstrap(root=ROOT):
    root = Path(root).resolve()
    with lock(root):
        path = store(root)/'registry.json'
        state = load(root) if path.exists() else dict(schema='wallet-registry-0.2.6', created_at=m.utc(), wallets=[])
        source = project_path(root, 'source_run', 'runs/okx_002')
        added = 0
        if (source/'raw/leaderboard.json').is_file():
            envelope = read(source/'raw/leaderboard.json')
            added, metadata = merge_leaderboard(state, envelope, (source/'raw/leaderboard.json').relative_to(root).as_posix())
            state['snapshot_discovery_metadata'] = metadata
        known = {w['wallet']: w for w in state['wallets']}
        for key, default in [('followup_run', 'runs/okx_003_continued'), ('baseline_report', 'runs/okx_700_offline')]:
            report_dir = project_path(root, key, default)
            if not (report_dir/'intake_report.json').exists(): continue
            r = read(report_dir/'intake_report.json'); observations = read(report_dir/'observations.json')
            for a in r['audits']:
                entry = known.get(a['wallet'])
                if entry and entry['history']['status'] == 'NOT_FETCHED':
                    set_history(entry, [o for o in observations if o['wallet'] == a['wallet']], a,
                                (report_dir/'report.html').relative_to(root).as_posix())
            break
        save(root, state); render(root, state)
        return state, added


def ensure(root=ROOT):
    return load(root) if (store(root)/'registry.json').exists() else bootstrap(root)[0]


def summary(state):
    statuses = Counter(w['history']['status'] for w in state['wallets'])
    return dict(wallet_count=len(state['wallets']), history_status_counts=dict(statuses),
        wallets_with_records=sum((w['history']['parsed_observations'] or 0) > 0 for w in state['wallets']),
        parsed_observations=sum(w['history']['parsed_observations'] or 0 for w in state['wallets']), accounting_ready=False)


def render(root, state):
    esc = html.escape; rows = []
    for i, w in enumerate(state['wallets'], 1):
        h = w['history']; s = h['summary']
        values = [i, w['wallet'], w['first_discovered_at'], h['status'], h['parsed_observations'],
                  s['side_counts'].get('buy', 0) if s else None,
                  s['buy_mc_below_6k'] if s else None, s['buy_mc_below_10k'] if s else None,
                  h.get('oldest_record_at'), h.get('latest_record_at')]
        row = '<tr>'+''.join('<td>'+esc(str(v) if v is not None else 'UNKNOWN')+'</td>' for v in values)
        path = h.get('report_path')
        if path:
            local(root, path)
            row += '<td><a href="../'+esc(path, quote=True)+'">Lihat data</a></td>'
        else: row += '<td>Belum tersedia</td>'
        rows.append(row+'</tr>')
    s = summary(state)
    text = '''<!doctype html><html lang="id"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Daftar kandidat wallet</title>
<style>:root{color-scheme:dark}body{font:15px system-ui;background:#0d1520;color:#e9f0f9;margin:28px}p{line-height:1.65}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}td,th{padding:12px;text-align:left;border-bottom:1px solid #33465e}td{max-width:220px;overflow-wrap:anywhere}input{padding:12px;width:300px;max-width:80%;background:#182435;color:#fff;border:1px solid #49617d}a{color:#8dc6ff}.notice{padding:18px;background:#342b17}</style>
<h1>Daftar kandidat wallet</h1><p>''' + esc(str(s['wallet_count'])) + ' wallet terdaftar · '+str(s['wallets_with_records'])+' memiliki record · '+str(s['parsed_observations'])+''' observasi tersimpan.</p>
<p class="notice">Kandidat belum berarti wallet berkualitas atau layak diikuti. UNKNOWN berarti history belum tersedia. Peringkat sumber mengandung bias seleksi leaderboard. BUY MC kecil belum membuktikan hold lama, posisi sangkut, atau win rate. Halaman ini snapshot lokal, belum monitor live.</p>
<input id="q" placeholder="Cari alamat atau status"><p>Seluruh waktu UTC. Nomor baris mengikuti urutan registrasi, bukan skor kualitas.</p><div class="scroll"><table><thead><tr>'''
    headers = ['No', 'Wallet', 'Ditemukan', 'Status history', 'Record parsed', 'BUY', 'BUY MC <6k', 'BUY MC <10k', 'Record tertua', 'Record terbaru', 'Detail']
    text += ''.join('<th>'+esc(h)+'</th>' for h in headers)+'</tr></thead><tbody id="rows">'+''.join(rows)+'''</tbody></table></div><script>
document.getElementById('q').addEventListener('input',function(){const q=this.value.toLowerCase();document.querySelectorAll('#rows tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q))});
</script></html>'''
    (store(root)/'index.html').write_text(text, encoding='utf-8')


def add_manual(root, addresses, source='MANUAL_UNVERIFIED'):
    addresses = list(dict.fromkeys(m.wallet_address(a.strip()) for a in addresses if a.strip()))
    if not addresses: raise m.IntakeError('NO_ADDRESSES_RECEIVED')
    ensure(root)
    with lock(root):
        state = load(root); known = {w['wallet']: w for w in state['wallets']}; added = 0
        if len(set(known)|set(addresses)) > 1000: raise m.IntakeError('REGISTRY_CAPACITY_1000')
        for address in addresses:
            provenance = dict(kind='USER_PROVIDED', label=source, received_at=m.utc())
            if address not in known:
                w = dict(wallet=address, first_discovered_at=m.utc(), registered_at=m.utc(), sources=[], history=empty_history())
                known[address] = w; state['wallets'].append(w); added += 1
            known[address]['sources'].append(provenance)
        save(root, state); render(root, state)
        return added


def unique_run(root, prefix):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = Path(root)/'runs'/(prefix+'_'+stamp+'_'+uuid.uuid4().hex[:6])
    path.mkdir(parents=True, exist_ok=False)
    return path


def discover(root=ROOT, client=None, creds=None):
    ensure(root)
    with lock(root):
        state = load(root); out = unique_run(root, 'discovery')
        m.write_json(out/'discovery_plan.json', dict(queries=DISCOVERY_QUERIES, max_attempts=2, retries=0,
            selection='fixed strata; all unique returned addresses; no win-rate or PnL cutoff'), exclusive=True)
        client = client if client is not None else m.Client(creds, budget=2, retries=0)
        errors = []; added = 0; metadata = []
        for i, params in enumerate(DISCOVERY_QUERIES, 1):
            try:
                env = client.get(m.LEADERBOARD, params)
                path = out/f'leaderboard_{i:03}.json'; m.write_json(path, env, exclusive=True)
                n, meta = merge_leaderboard(state, env, path.relative_to(Path(root)).as_posix())
                added += n; metadata.append(meta); save(root, state)
            except m.IntakeError as exc:
                errors.append(m.exception_record(exc, 'discovery')); break
        report = dict(status='DISCOVERY_PARTIAL' if errors else 'DISCOVERY_RECEIVED', new_wallets=added,
            requests=client.requests, errors=errors, metadata=metadata, **summary(state))
        m.write_json(out/'discovery_report.json', report, exclusive=True)
        render(root, state)
        return report


def eligible(entry, retry_systemic=False):
    h = entry['history']
    return h['status'] == 'NOT_FETCHED' or (retry_systemic and h['status'] == 'REQUEST_FAILED'
        and (h.get('retryable_systemic',False) or h.get('stop_reason','').startswith('NETWORK_ERROR')))


def systemic_failure(failure):
    d = failure.get('diagnostic', {})
    return d.get('http_status') != 400 or d.get('provider_code') in {'50111','50113','50114','50116','50011'}


def historical_window(root):
    source = project_path(root, 'source_run', 'runs/okx_002')/'intake_report.json'
    if not source.is_file():
        raise m.IntakeError('HISTORY_WINDOW_REQUIRED: import legacy data first; rolling history is planned for M2')
    prior = read(source)
    ranges = {(a['requested_begin_ms'], a['requested_end_ms']) for a in prior['audits']}
    if len(ranges) != 1:
        raise m.IntakeError('Original requested windows differ; define a new collection plan')
    return next(iter(ranges))


def collect(root=ROOT, client=None, creds=None, max_wallets=10, selected_addresses=None,
            retry_systemic=False, out=None):
    if not isinstance(max_wallets, int) or not 1 <= max_wallets <= 10:
        raise m.IntakeError('Batch size must be between 1 and 10')
    root = Path(root).resolve(); ensure(root)
    with lock(root):
        state = load(root)
        pool = state['wallets']
        if selected_addresses is not None:
            known = {w['wallet']: w for w in pool}
            if len(set(selected_addresses)) != len(selected_addresses) or any(a not in known for a in selected_addresses):
                raise m.IntakeError('Invalid frozen batch selection')
            pool = [known[a] for a in selected_addresses]
        chosen = [w for w in pool if eligible(w, retry_systemic)][:max_wallets]
        if not chosen: return dict(status='NO_PENDING_WALLETS', requests=0, selected_wallets=0)
        begin, end = historical_window(root); started = m.utc()
        if out is None: out = unique_run(root, 'wallet_batch')
        else:
            out = Path(out).resolve()
            if root not in out.parents: raise m.IntakeError('Batch output must be inside master')
            out.mkdir(parents=True, exist_ok=False)
        m.write_json(out/'registry_before.json', state, exclusive=True)
        m.write_json(out/'batch_plan.json', dict(wallets=[w['wallet'] for w in chosen], begin_ms=begin, end_ms=end,
            max_wallets=max_wallets, pages_per_wallet=1, page_limit=100, max_attempts=max_wallets, retries=0,
            retry_prior_systemic_failures_once=retry_systemic,
            selection='first NOT_FETCHED in registry order; failures retained; no outcome filters',
            history_window='original frozen requested window; not live monitoring', recorded_at=m.utc()), exclusive=True)
        client = client if client is not None else m.Client(creds, budget=max_wallets, retries=0)
        initial_requests = client.requests
        observations = []; audits = []; candidates = []; errors = []; halted = False
        def persist(complete=False):
            report = m.intake_report(candidates, observations, audits, errors)
            report.update(schema='wallet-batch-0.2.7', status='PARTIAL_WITH_ERRORS' if errors else 'PARTIAL_UNRECONCILED',
                batch_complete=complete, data_mode='PROVIDER_OBSERVATIONS_UNRECONCILED', started_at=started,
                finished_at=m.utc(), requests=client.requests-initial_requests,
                side_counts=dict(Counter(o['side'] for o in observations)), selected_wallets=len(chosen),
                attempted_wallets=len(candidates), registry_summary=summary(state), report_scope='THIS_BATCH_ONLY')
            followup.save_report(out, report, observations, exclusive=False)
            m.write_json(out/'registry_after.json', state)
            save_pointer(root, out); render(root, state)
            return report
        for i, entry in enumerate(chosen, 1):
            wallet = entry['wallet']; raw = out/'raw'/f'wallet_{i:03}'; raw.mkdir(parents=True)
            entry['history'] = dict(empty_history(), status='IN_PROGRESS', report_path=(out/'report.html').relative_to(root).as_posix())
            save(root, state)  # interruption is visible and does not automatically reselect this wallet
            obs, audit, issues = m.collect_history(client, wallet, begin, end, 1, raw)
            audit['stored_pages'] = len(list(raw.glob('history_*.json')))
            for o in obs: o['source_file'] = (raw/'history_001.json').relative_to(root).as_posix()
            audit['oldest_record_at'] = min((o['timestamp'] for o in obs), default=None)
            audit['latest_record_at'] = max((o['timestamp'] for o in obs), default=None)
            set_history(entry, obs, audit, (out/'report.html').relative_to(root).as_posix())
            save(root, state)
            candidates.append(dict(wallet=wallet, source='REGISTRY', discovered_at=entry['first_discovered_at'], history_complete='false'))
            observations.extend(obs); audits.append(audit)
            errors.extend(dict(stage='history', wallet=wallet, **issue) for issue in issues)
            failure = audit.get('request_failure')
            if failure:
                errors.append(failure)
                if systemic_failure(failure): halted = True
            persist()
            if halted: break
        report = persist(complete=True)
        return dict(status=report['status'], requests=client.requests-initial_requests, selected_wallets=len(chosen),
            attempted_wallets=len(candidates), observation_count=len(observations), registry_summary=summary(state),
            errors=errors, halted_systemic=halted, report_path=str(out/'report.html'))


def sync(root=ROOT, client=None, creds=None, max_requests=50, export=True):
    """Drain a frozen candidate queue, keeping a single global request budget."""
    if not isinstance(max_requests, int) or not 1 <= max_requests <= 50:
        raise m.IntakeError('Sync request budget must be between 1 and 50')
    root = Path(root).resolve(); ensure(root)
    with lock(root, 'sync.lock'):
        initial = load(root)
        queue = [w['wallet'] for w in initial['wallets'] if eligible(w, retry_systemic=True)]
        if not queue: return dict(status='NO_PENDING_WALLETS', requests=0, registry_summary=summary(initial))
        historical_window(root)
        target = queue[:max_requests]
        out = unique_run(root, 'wallet_sync')
        source = project_path(root, 'source_run', 'runs/okx_002')/'intake_report.json'
        source_hash = followup.sha(source)
        m.write_json(out/'session_plan.json', dict(wallets=target, queued_before=len(queue), max_attempts=max_requests,
            page_limit=100, pages_per_wallet=1, retries=0, retry_prior_systemic_failures_once=True,
            source_report_sha256=source_hash, recorded_at=m.utc(),
            selection='frozen registry order; NOT_FETCHED and prior systemic request failures only'), exclusive=True)
        m.write_json(out/'registry_before.json', initial, exclusive=True)
        client = client if client is not None else m.Client(creds, budget=max_requests, retries=0)
        initial_requests = client.requests; stopped = 'QUEUE_PROCESSED'; extra_errors = []
        def persist():
            candidates=[]; audits=[]; observations=[]; errors=list(extra_errors)
            for batch in sorted((out/'batches').glob('batch_*')):
                if not (batch/'intake_report.json').exists(): continue
                report=read(batch/'intake_report.json')
                candidates.extend(report['candidates']);audits.extend(report['audits'])
                observations.extend(read(batch/'observations.json'));errors.extend(report['errors'])
            state=load(root)
            report=m.intake_report(candidates, observations, audits, errors)
            report.update(schema='wallet-sync-0.2.7', status=stopped, data_mode='PROVIDER_OBSERVATIONS_UNRECONCILED',
                requests=client.requests-initial_requests, side_counts=dict(Counter(o['side'] for o in observations)),
                registry_summary=summary(state), selected_wallets=len(target), attempted_wallets=len(candidates),
                report_scope='THIS_SYNC_ONLY', processed_at=m.utc())
            followup.save_report(out, report, observations, exclusive=False)
            m.write_json(out/'registry_after.json', state)
            m.write_json(out/'session_progress.json', dict(status=stopped,requests=report['requests'],
                attempted_wallets=len(candidates), observation_count=len(observations), registry_summary=summary(state)))
            save_pointer(root,out);render(root,state)
            return report
        for offset in range(0,len(target),10):
            if followup.sha(source) != source_hash:
                stopped='SOURCE_CHANGED';extra_errors.append(dict(stage='sync',error='SOURCE_CHANGED'));break
            chunk=target[offset:offset+10]
            try:
                result=collect(root,client=client,max_wallets=len(chunk),selected_addresses=chunk,retry_systemic=True,
                               out=out/'batches'/f'batch_{offset//10+1:03}')
            except KeyboardInterrupt:
                stopped='INTERRUPTED';extra_errors.append(dict(stage='sync',error='USER_INTERRUPTED'));break
            except (m.IntakeError,OSError,ValueError,KeyError) as exc:
                stopped='LOCAL_ERROR';extra_errors.append(dict(stage='sync',error=str(exc) if isinstance(exc,m.IntakeError) else type(exc).__name__));break
            print('Progres: '+str(client.requests-initial_requests)+' / '+str(max_requests)+' request; '+
                  str(summary(load(root))['wallets_with_records'])+' wallet memiliki record.',flush=True)
            stopped='SYSTEMIC_FAILURE' if result.get('halted_systemic') else 'RUNNING'
            persist()
            if result.get('halted_systemic'):break
        if stopped == 'RUNNING':
            stopped='REQUEST_BUDGET_REACHED' if len(queue)>len(target) else 'QUEUE_PROCESSED'
        report=persist()
        if stopped=='QUEUE_PROCESSED' and report['errors']:
            stopped='QUEUE_PROCESSED_WITH_ERRORS';report=persist()
        result=dict(status=report['status'],requests=report['requests'],observation_count=report['observation_count'],
            registry_summary=report['registry_summary'],errors=report['errors'],report_path=str(out/'report.html'))
        if export:
            import master
            result['review_zip']=str(master.export_review(root))
        return result


def save_pointer(root, out):
    path = store(root)/'latest_batch.json'; temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(dict(run_path=Path(out).relative_to(Path(root)).as_posix()))+'\n', encoding='utf-8')
    temp.replace(path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('init', 'list', 'report', 'collect', 'discover','sync'):
        command = sub.add_parser(name)
        if name in ('collect', 'discover','sync'): command.add_argument('--prompt', action='store_true')
        if name=='sync': command.add_argument('--max-requests',type=int,default=50)
    command = sub.add_parser('add'); command.add_argument('--wallet', action='append', required=True)
    command.add_argument('--source', default='MANUAL_UNVERIFIED')
    args = p.parse_args()
    try:
        if args.command == 'init': result = summary(bootstrap()[0])
        elif args.command == 'list': result = summary(ensure())
        elif args.command == 'report':
            state = ensure(); render(ROOT, state)
            webbrowser.open((store(ROOT)/'index.html').as_uri()); result = summary(state)
        elif args.command == 'add': result = dict(added=add_manual(ROOT, args.wallet, args.source))
        else:
            state = ensure()
            if args.command == 'sync' and not 1<=args.max_requests<=50:
                raise m.IntakeError('Sync request budget must be between 1 and 50')
            if args.command in ('collect','sync') and not any(eligible(w,args.command=='sync') for w in state['wallets']):
                result = dict(status='NO_PENDING_WALLETS', requests=0)
            else:
                if args.command in ('collect', 'sync'):
                    historical_window(ROOT)
                creds = m.credentials(args.prompt)
                if args.command=='sync':
                    result=sync(creds=creds,max_requests=args.max_requests)
                    webbrowser.open((store(ROOT)/'index.html').as_uri())
                else: result = collect(creds=creds) if args.command == 'collect' else discover(creds=creds)
        print(json.dumps(result, indent=2))
        return 2 if result.get('errors') else 0
    except (m.IntakeError, OSError, ValueError, KeyError) as exc:
        print(json.dumps(dict(status='BLOCKED', error=str(exc) if isinstance(exc, m.IntakeError) else type(exc).__name__)))
        return 2


if __name__ == '__main__': raise SystemExit(main())
