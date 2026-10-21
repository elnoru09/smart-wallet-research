"""Offline combined reports and bounded continuation of one recovered history."""
import argparse
import copy
import hashlib
import html
import json
import shutil
from collections import Counter
from pathlib import Path

import okx_reader as m


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def expected_params(audit):
    return dict(chainIndex='501', walletAddress=audit['wallet'],
                begin=str(audit['requested_begin_ms']), end=str(audit['requested_end_ms']), limit='100')


def validate_envelope(envelope, params):
    if envelope.get('provider') != 'OKX' or envelope.get('path') != m.HISTORY:
        raise m.SchemaError('Unexpected history provider or endpoint')
    if envelope.get('params') != params:
        raise m.SchemaError('History parameters differ from the recorded request')
    if str(envelope.get('response', {}).get('code')) != '0':
        raise m.SchemaError('History response did not report success')
    m.ts(envelope['received_at'])
    return m.history_page(envelope)


def inputs(source_run, diagnosis):
    source, diagnosis = Path(source_run).resolve(), Path(diagnosis).resolve()
    prior = read_json(source/'intake_report.json')
    failed = [a for a in prior['audits'] if a.get('request_failure') and a.get('returned_records') == 0]
    if len(failed) != 1:
        raise m.IntakeError('Expected one original history failure with zero records')
    audit = failed[0]
    d = read_json(diagnosis)
    if d.get('status') != 'HISTORY_REQUEST_SUCCEEDED' or d.get('wallet') != audit['wallet']:
        raise m.SchemaError('Diagnosis does not recover the original failed wallet')
    params = expected_params(audit)
    if d.get('params') != params or d.get('selection_changed') is not False or d.get('original_window_retained') is not True:
        raise m.SchemaError('Diagnosis changed candidate or requested window')
    rows, cursor = validate_envelope(d['response_envelope'], params)
    if len(rows) != d.get('returned_records'):
        raise m.SchemaError('Diagnosis row count mismatch')
    wallets = [c['wallet'] for c in prior['candidates']]
    if len(set(wallets)) != len(wallets) or audit['wallet'] not in wallets:
        raise m.SchemaError('Invalid original candidate list')
    pages = []
    hashes = {'original_intake_report.json': sha(source/'intake_report.json'), 'diagnosis.json': sha(diagnosis)}
    audit_map = {a['wallet']: a for a in prior['audits']}
    for path in sorted((source/'raw').glob('wallet_*/history_*.json')):
        env = read_json(path)
        wallet = env.get('params', {}).get('walletAddress')
        if wallet not in audit_map or wallet == audit['wallet']:
            raise m.SchemaError('Source histories conflict with original failure or candidates')
        ep = expected_params(audit_map[wallet])
        if 'cursor' in env['params']:
            ep['cursor'] = env['params']['cursor']
        validate_envelope(env, ep)
        label = 'original/'+path.relative_to(source).as_posix()
        pages.append((label, env)); hashes[label] = sha(path)
    for old_audit in prior['audits']:
        actual = sum(len(m.history_page(e)[0]) for _, e in pages if e['params']['walletAddress'] == old_audit['wallet'])
        if actual != old_audit['returned_records']:
            raise m.SchemaError('Stored source page count does not match original report')
    pages.append(('diagnosis.json', d['response_envelope']))
    return source, diagnosis, prior, audit, d, pages, hashes, cursor


def output_path(out, source, diagnosis):
    out = Path(out).resolve()
    if out.exists():
        raise m.IntakeError('OUTPUT_EXISTS: choose a new output directory')
    if out == source or source in out.parents or out in source.parents or out == diagnosis or out in diagnosis.parents:
        raise m.IntakeError('Output must be separate from input files and source run')
    return out


def parse_pages(pages):
    observations, errors = [], []
    counts, seen = {}, set()
    for label, env in pages:
        wallet = env['params']['walletAddress']
        count = counts.setdefault(wallet, dict(returned=0, parsed=0, malformed=0, duplicate_payload_occurrences=0, pages=0))
        count['pages'] += 1
        for row_index, row in enumerate(m.history_page(env)[0], 1):
            count['returned'] += 1
            fp = m.digest(row)
            if (wallet, fp) in seen:
                count['duplicate_payload_occurrences'] += 1
            seen.add((wallet, fp))
            try:
                o = m.observation(row, env, wallet, count['returned'])
                o.update(source_file=label, source_row=row_index)
                observations.append(o); count['parsed'] += 1
            except (m.SchemaError, ValueError, OverflowError, KeyError) as exc:
                count['malformed'] += 1
                errors.append(dict(wallet=wallet, stage='history', issue='INVALID_OR_UNSUPPORTED_RECORD',
                    provider_record_sha256=fp, reason=str(exc) if isinstance(exc, m.SchemaError) else type(exc).__name__))
    return observations, counts, errors


def make_report(prior, recovered, pages, hashes, stop, cursor, requests, active_failure=None):
    observations, counts, errors = parse_pages(pages)
    wallet = recovered['wallet']
    historical = [copy.deepcopy(e) for e in prior.get('errors', [])
                  if e.get('wallet') == wallet and e.get('stage') == 'history' and not e.get('issue')]
    errors += [copy.deepcopy(e) for e in prior.get('errors', [])
               if e.get('issue') != 'INVALID_OR_UNSUPPORTED_RECORD'
               and not (e.get('wallet') == wallet and e.get('stage') == 'history' and not e.get('issue'))]
    if active_failure:
        errors.append(active_failure)
    audits = copy.deepcopy(prior['audits'])
    for a in audits:
        c = counts.get(a['wallet'], dict(returned=0, parsed=0, malformed=0, duplicate_payload_occurrences=0, pages=0))
        a.update(returned_records=c['returned'], parsed_observations=c['parsed'], malformed_records=c['malformed'],
                 duplicate_payload_occurrences=c['duplicate_payload_occurrences'], stored_pages=c['pages'],
                 complete_history=False, eligible_for_accounting=False)
        if a['wallet'] == wallet:
            a.update(stop_reason=stop, next_cursor=cursor, request_failure=active_failure,
                     provider_cursor_exhausted=stop == 'PROVIDER_CURSOR_EXHAUSTED',
                     recovered_from_diagnosis=True)
    report = m.intake_report(copy.deepcopy(prior['candidates']), observations, audits, errors)
    report.update(schema='history-followup-0.2.4', status='PARTIAL_WITH_ERRORS' if errors else 'PARTIAL_UNRECONCILED',
        data_mode='COMBINED_PROVIDER_OBSERVATIONS_UNRECONCILED', processed_at=m.utc(),
        original_started_at=prior['started_at'], requests=requests,
        original_run_requests=prior['requests'], historical_errors=historical, source_files_sha256=hashes,
        selection_changed=False, original_window_retained=True, side_counts=dict(Counter(o['side'] for o in observations)),
        discovery=copy.deepcopy(prior.get('discovery', {})),
        warning='All counts are provider observations. Holding periods, balances, win rates and profitability remain unverified.')
    for a in report['audits']:
        own = [o for o in observations if o['wallet'] == a['wallet']]
        a['oldest_record_at'] = min((o['timestamp'] for o in own), default=None)
        a['latest_record_at'] = max((o['timestamp'] for o in own), default=None)
    return report, observations


def report_html(report, observations):
    # JSON cannot close its script element; dynamic content is assigned with textContent.
    payload = json.dumps(dict(report=report, observations=observations), ensure_ascii=True).replace('<', '\\u003c')
    return '''<!doctype html><html lang="id"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smart Wallet · Observasi</title><style>
:root{color-scheme:dark}body{font:15px system-ui;margin:0;background:#0d1520;color:#e9f0f9}main{max-width:1450px;margin:auto;padding:32px 24px}h1{font-size:30px;margin-bottom:8px}p{line-height:1.65}.muted{color:#a6b6ca}.cards{display:flex;gap:12px;flex-wrap:wrap}.card{flex:1;min-width:135px;padding:20px;background:#182435;border:1px solid #304158;border-radius:12px}.card strong{display:block;font-size:28px}.notice{padding:16px;background:#342b17;border-left:3px solid #e5b34d;margin:22px 0}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:12px 10px;text-align:left;border-bottom:1px solid #304158;vertical-align:top}th{color:#a9c5e8}code{word-break:break-all}select,input,button{font:inherit;padding:10px;background:#182435;color:#e9f0f9;border:1px solid #49617d;border-radius:7px}input{min-width:250px}.filters{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}.address{max-width:240px;overflow-wrap:anywhere}button{cursor:pointer}footer{margin-top:30px;color:#a6b6ca}</style>
<main><p class="muted">SMART WALLET RESEARCH · V0.2.4</p><h1>Jelajahi aktivitas wallet</h1><p class="muted">Laporan lokal dari snapshot tersimpan. Tidak memperbarui data otomatis.</p>
<div class="cards" id="cards"></div><div class="notice">Data belum direkonsiliasi. Jumlah BUY/SELL bukan win rate. Rentang timestamp bukan durasi hold. Posisi terbuka dan posisi sangkut belum dapat dipastikan.</div>
<h2>Cakupan tiap wallet</h2><div class="scroll"><table><thead><tr><th>Wallet</th><th>Record</th><th>Halaman</th><th>Record tertua (UTC)</th><th>Record terbaru (UTC)</th><th>Status</th></tr></thead><tbody id="coverage"></tbody></table></div>
<h2>Detail observasi</h2><div class="filters"><select id="wallet"><option value="">Semua wallet</option></select><select id="side"><option value="">Semua aktivitas</option><option>buy</option><option>sell</option><option>transfer_in</option><option>transfer_out</option></select><select id="mc"><option value="">Semua MC</option><option value="6000">MC di bawah US$6k</option><option value="10000">MC di bawah US$10k</option><option value="missing">MC belum diketahui</option></select><input id="query" placeholder="Cari alamat token atau wallet"><button id="download">Unduh hasil filter (JSON)</button></div><p id="counter" class="muted"></p>
<div class="scroll"><table><thead><tr><th>Waktu aktivitas (UTC)</th><th>Wallet</th><th>Token</th><th>Jenis</th><th>Quantity</th><th>USD provider</th><th>MC provider (USD)</th><th>Diterima (UTC)</th></tr></thead><tbody id="rows"></tbody></table></div>
<p><button id="prev">Sebelumnya</button> <span id="page"></span> <button id="next">Berikutnya</button></p>
<footer>USD kosong ditampilkan UNKNOWN. Fee dan basis inventory belum tersedia. MC berasal dari provider. Error historis dipertahankan pada intake_report.json. Laporan ini tidak memberi rekomendasi transaksi.</footer></main>
<script id="dataset" type="application/json">''' + payload + '''</script><script>
const data=JSON.parse(document.getElementById('dataset').textContent),r=data.report;
const el=id=>document.getElementById(id);let page=0,filtered=[];const size=50;
function cell(row,value,cls){const td=document.createElement('td');td.textContent=value??'UNKNOWN';if(cls)td.className=cls;row.appendChild(td)}
for(const [label,value] of [['Wallet',r.candidates.length],['Observasi',r.observation_count],['BUY',r.side_counts.buy||0],['SELL',r.side_counts.sell||0],['Transfer',(r.side_counts.transfer_in||0)+(r.side_counts.transfer_out||0)]]){const c=document.createElement('div');c.className='card';const n=document.createElement('strong');n.textContent=value;c.append(n,document.createTextNode(label));el('cards').appendChild(c)}
r.candidates.forEach((c,i)=>{const o=document.createElement('option');o.value=c.wallet;o.textContent='Wallet '+(i+1)+' · '+c.wallet.slice(0,8)+'…';el('wallet').appendChild(o)});
r.audits.forEach(a=>{const tr=document.createElement('tr');[a.wallet,a.returned_records,a.stored_pages,a.oldest_record_at,a.latest_record_at,a.stop_reason].forEach(v=>cell(tr,v,'address'));el('coverage').appendChild(tr)});
function draw(){el('rows').replaceChildren();filtered.slice(page*size,(page+1)*size).forEach(o=>{const tr=document.createElement('tr');[o.timestamp,o.wallet,o.token,o.side,o.quantity,o.provider_usd_value,o.provider_event_mc_usd,o.observed_at].forEach((v,i)=>cell(tr,v,i===1||i===2?'address':''));el('rows').appendChild(tr)});const n=Math.max(1,Math.ceil(filtered.length/size));el('counter').textContent=filtered.length+' dari '+data.observations.length+' observasi · seluruh waktu UTC';el('page').textContent=(page+1)+' / '+n;el('prev').disabled=page===0;el('next').disabled=page+1>=n}
function filter(){const w=el('wallet').value,s=el('side').value,mc=el('mc').value,q=el('query').value.trim().toLowerCase();filtered=data.observations.filter(o=>(!w||o.wallet===w)&&(!s||o.side===s)&&(!q||(o.wallet+' '+o.token).toLowerCase().includes(q))&&(!mc||(mc==='missing'?o.provider_event_mc_usd===null:o.provider_event_mc_usd!==null&&o.provider_event_mc_usd<Number(mc))));page=0;draw()}
['wallet','side','mc','query'].forEach(id=>el(id).addEventListener('input',filter));el('prev').onclick=()=>{page--;draw()};el('next').onclick=()=>{page++;draw()};el('download').onclick=()=>{const u=URL.createObjectURL(new Blob([JSON.stringify(filtered,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=u;a.download='filtered_observations.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000)};filter();
</script></html>'''


def save_report(out, report, observations, exclusive=True):
    m.write_json(out/'intake_report.json', report, exclusive=exclusive)
    m.write_json(out/'observations.json', observations, exclusive=exclusive)
    m.export_csv(out/'trade_observations.csv', observations)
    m.export_csv(out/'unverified_token_activity.csv', report['unverified_token_activity'])
    (out/'report.html').write_text(report_html(report, observations), encoding='utf-8')


def run(source_run, diagnosis, out, resume=False, creds=None, client=None):
    source, diagnosis, prior, recovered, d, pages, hashes, cursor = inputs(source_run, diagnosis)
    out = output_path(out, source, diagnosis)
    out.mkdir(parents=True)
    shutil.copyfile(diagnosis, out/'diagnosis_source.json')
    stop = 'DIAGNOSTIC_PAGE_ONLY' if cursor and cursor != '0' else 'PROVIDER_CURSOR_EXHAUSTED'
    failure = None; requests = 0
    if resume and cursor and cursor != '0':
        if client is None:
            client = m.Client(creds, budget=2, retries=0)
        # Even an injected client cannot send more than these two get calls.
        raw = out/'raw'; raw.mkdir()
        seen = {cursor}
        m.write_json(out/'continuation_plan.json', dict(wallet=recovered['wallet'], params=expected_params(recovered),
            starting_cursor=cursor, max_additional_pages=2, max_http_attempts=2, automatic_retries=0,
            selection_changed=False, original_window_retained=True, recorded_at=m.utc()), exclusive=True)
        stop = 'PAGE_BUDGET_REACHED'
        for page_no in (2, 3):
            params = dict(expected_params(recovered), cursor=cursor)
            try:
                env = client.get(m.HISTORY, params)
                path = raw/f'history_{page_no:03}.json'
                m.write_json(path, env, exclusive=True)
                rows, next_cursor = validate_envelope(env, params)
                label = 'continuation/'+path.relative_to(out).as_posix()
                pages.append((label, env)); hashes[label] = sha(path)
            except m.IntakeError as exc:
                stop = 'REQUEST_OR_SCHEMA_ERROR'
                failure = m.exception_record(exc, 'history', wallet=recovered['wallet'])
                break
            cursor = next_cursor
            if not cursor or cursor == '0':
                stop = 'PROVIDER_CURSOR_EXHAUSTED'; break
            if cursor in seen:
                stop = 'REPEATED_CURSOR'; break
            if not rows:
                stop = 'EMPTY_PAGE_WITH_CURSOR'; break
            seen.add(cursor)
        requests = client.requests
    report, observations = make_report(prior, recovered, pages, hashes, stop, cursor, requests, failure)
    report['diagnosis_requests'] = d.get('requests')
    save_report(out, report, observations)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('merge', 'resume'):
        p = sub.add_parser(name)
        p.add_argument('--source-run', required=True)
        p.add_argument('--diagnosis', required=True)
        p.add_argument('--out', required=True)
        if name == 'resume': p.add_argument('--prompt', action='store_true')
    args = parser.parse_args()
    try:
        source, diagnosis, _, _, _, _, _, cursor = inputs(args.source_run, args.diagnosis)
        output_path(args.out, source, diagnosis)
        creds = m.credentials(args.prompt) if args.command == 'resume' and cursor and cursor != '0' else None
        r = run(source, diagnosis, args.out, resume=args.command == 'resume', creds=creds)
        summary = {k:r[k] for k in ('status', 'observation_count', 'side_counts', 'requests', 'accounting_ready')}
        summary['errors'] = r['errors']
        summary['coverage'] = [{k:a.get(k) for k in ('wallet', 'stored_pages', 'returned_records', 'stop_reason')} for a in r['audits']]
        print(json.dumps(summary, indent=2))
        print('Open: '+str(Path(args.out)/'report.html'))
        return 2 if r['errors'] else 0
    except (m.IntakeError, OSError, ValueError, KeyError) as exc:
        # Never echo arbitrary external file content or paths in parser errors.
        print(json.dumps(dict(status='BLOCKED', error=str(exc) if isinstance(exc, m.IntakeError) else type(exc).__name__)))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
