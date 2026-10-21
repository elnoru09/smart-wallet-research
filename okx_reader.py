"""Bounded, read-only OKX data intake. Credentials never belong in reports."""
import argparse
import base64
import csv
import getpass
import hashlib
import hmac
import html
import json
import math
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from walletlab import digest, export_csv, ts, write_json

HOST = 'https://web3.okx.com'
LEADERBOARD = '/api/v6/dex/market/leaderboard/list'
OVERVIEW = '/api/v6/dex/market/portfolio/overview'
HISTORY = '/api/v6/dex/market/portfolio/dex-history'
ENDPOINTS = {LEADERBOARD, OVERVIEW, HISTORY}
ENV_NAMES = ('OKX_API_KEY', 'OKX_SECRET_KEY', 'OKX_PASSPHRASE')
DOTENV_PATH = Path(__file__).resolve().parent / '.env'
PLAN = dict(chainIndex='501', timeFrame='5', sortBy='1', walletType='3',
            history_days=90, max_wallets=3, max_pages_per_wallet=3,
            page_limit=100, max_requests=32, min_interval_seconds=.8,
            max_retries=2, selection='provider order; no min win-rate/hold/profit filter')


class IntakeError(RuntimeError):
    def __init__(self,message,diagnostic=None):
        super().__init__(message)
        self.diagnostic=diagnostic or {}


class SchemaError(IntakeError):
    pass


def utc():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')


def scrub(value, secrets=()):
    if isinstance(value, dict):
        return {k: ('<redacted>' if re.search(r'(?i)(api.?key|secret|passphrase|authorization|signature|private.?key|access.?sign)',k)
                    else scrub(v,secrets)) for k,v in value.items()}
    if isinstance(value,list):return [scrub(v,secrets) for v in value]
    if isinstance(value,str):
        for s in sorted((s for s in secrets if s),key=len,reverse=True):
            value=value.replace(s,'<redacted>')
    return value


def wallet_address(value):
    alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    if not isinstance(value,str) or not 32 <= len(value) <= 44:
        raise SchemaError('Invalid Solana wallet address')
    n=0
    for c in value:
        if c not in alphabet:raise SchemaError('Invalid base58 wallet address')
        n=n*58+alphabet.index(c)
    size=(n.bit_length()+7)//8+len(value)-len(value.lstrip('1'))
    if size != 32:raise SchemaError('Wallet must decode to 32 bytes')
    return value


def dotenv_values(path=None):
    """Read only the three credential names, as literals. Never execute or expand text."""
    path = DOTENV_PATH if path is None else Path(path)
    try:
        content = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError):
        raise IntakeError('ENV_FILE_UNREADABLE: check local .env access and UTF-8 encoding') from None
    values = {}
    for number, raw in enumerate(content.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        name, sep, value = line.partition('=')
        name = name.strip()
        if name not in ENV_NAMES:
            continue
        value = value.strip()
        if not sep or name in values:
            raise IntakeError(f'ENV_FILE_INVALID: check assignment or duplicate key on line {number}')
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise IntakeError(f'ENV_FILE_INVALID: check quotes on line {number}')
            value = value[1:-1]
        # Quotes are optional; #, =, $, percent and backslashes are literal characters.
        # Inline comments and shell-style expansion are intentionally unsupported.
        values[name] = value
    return values


def credentials(prompt=False):
    vals=[os.environ.get(n,'') for n in ENV_NAMES]
    if not all(vals):
        local = dotenv_values()
        vals=[v or local.get(n,'') for n,v in zip(ENV_NAMES,vals)]
    if prompt and not all(vals):
        if not sys.stdin.isatty():
            raise IntakeError('PROMPT_REQUIRES_TERMINAL: run locally in an interactive terminal')
        vals=[v or getpass.getpass(n+' (hidden, current process only): ') for n,v in zip(ENV_NAMES,vals)]
    if not all(vals):
        raise IntakeError('CREDENTIALS_MISSING: fill the local .env, use --prompt, or configure environment variables')
    return vals


def sign(secret, timestamp, request_path):
    return base64.b64encode(hmac.new(secret.encode(),
        (timestamp+'GET'+request_path).encode(),hashlib.sha256).digest()).decode()


def finite_float(text):
    value=float(text)
    if not math.isfinite(value):raise ValueError('Nonfinite JSON number')
    return value


def response_diagnostic(status,body,secrets=()):
    """No provider messages, headers, URL query or response bodies in diagnostics."""
    result=dict(http_status=status,response_format='OTHER',provider_code=None)
    try:
        payload=json.loads(body.decode('utf-8'))
        result['response_format']='JSON'
        if isinstance(payload,dict) and isinstance(payload.get('code'),(str,int)):
            code=scrub(str(payload['code']),secrets)
            if re.fullmatch(r'[0-9]{1,12}|[A-Z][A-Z0-9_]{0,31}',code):
                result['provider_code']=code
            if code=='51000' and isinstance(payload.get('msg'),str):
                # Extract only documented parameter names; never echo arbitrary provider messages.
                message=scrub(payload['msg'],secrets)
                match=re.search(r'(?i)\bparameter\s+[\"\x27]?(chainIndex|walletAddress|begin|end|tokenContractAddress|type|cursor|limit)[\"\x27]?\s+error\b',message)
                if match:
                    names={s.lower():s for s in ('chainIndex','walletAddress','begin','end','tokenContractAddress','type','cursor','limit')}
                    result['parameter_name']=names[match.group(1).lower()]
    except (ValueError,UnicodeError):
        if body.lstrip().lower().startswith((b'<!doctype html',b'<html')):
            result['response_format']='HTML'
    return result


def exception_record(exc,stage,**extra):
    return dict(stage=stage,error=str(exc),diagnostic=getattr(exc,'diagnostic',{}),**extra)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def network_diagnostic(exc):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    result = dict(exception_type=type(exc).__name__, reason_type=type(reason).__name__, category='NETWORK_FAILURE')
    if isinstance(reason, ssl.SSLCertVerificationError):
        result['category'] = 'TLS_CERTIFICATE_VERIFY_FAILED'
        code = getattr(reason, 'verify_code', None)
        if isinstance(code, int): result['verify_code'] = code
    elif isinstance(reason, ssl.SSLError): result['category'] = 'TLS_ERROR'
    elif isinstance(reason, socket.gaierror): result['category'] = 'DNS_LOOKUP_FAILED'
    elif isinstance(reason, TimeoutError): result['category'] = 'TIMEOUT'
    elif isinstance(reason, ConnectionError): result['category'] = 'CONNECTION_FAILED'
    elif isinstance(reason, OSError): result['category'] = 'OS_NETWORK_ERROR'
    number = getattr(reason, 'errno', None)
    if isinstance(number, int): result['errno'] = number
    return result  # no URL, headers, certificate text or exception messages


def http_get(url,headers):
    request=urllib.request.Request(url,headers=headers,method='GET')
    opener=urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request,timeout=20) as response:
            body=response.read(8*1024*1024+1)
            if len(body)>8*1024*1024:raise IntakeError('RESPONSE_TOO_LARGE')
            return response.status,body
    except urllib.error.HTTPError as exc:
        # Only parse a bounded numeric/code diagnostic; caller discards all raw errors.
        return exc.code,exc.read(4096)
    except (urllib.error.URLError,TimeoutError,OSError) as exc:
        raise IntakeError('NETWORK_ERROR: request could not complete; no credentials logged',network_diagnostic(exc)) from None


class Client:
    def __init__(self, creds, transport=http_get, sleeper=time.sleep,
                 clock=utc, monotonic=time.monotonic, budget=32, interval=.8, retries=2):
        self.creds=creds
        self.transport,self.sleep,self.clock,self.monotonic=transport,sleeper,clock,monotonic
        self.budget,self.interval,self.retries=budget,interval,retries
        self.requests=0
        self.last=None

    def get(self,path,params):
        if path not in ENDPOINTS:raise IntakeError('ENDPOINT_NOT_ALLOWLISTED')
        query=urllib.parse.urlencode(sorted((k,str(v)) for k,v in params.items()))
        request_path=path+('?' + query if query else '')
        for attempt in range(self.retries+1):
            if self.requests >= self.budget:raise IntakeError('REQUEST_BUDGET_EXHAUSTED')
            if self.last is not None:self.sleep(max(0,self.interval-(self.monotonic()-self.last)))
            now=self.clock()
            headers={'OK-ACCESS-KEY':self.creds[0], 'OK-ACCESS-TIMESTAMP':now,
                     'OK-ACCESS-PASSPHRASE':self.creds[2],
                     'OK-ACCESS-SIGN':sign(self.creds[1],now,request_path),
                     'Accept':'application/json','User-Agent':'WalletResearch/0.2'}
            self.requests+=1
            self.last=self.monotonic()
            status,body=self.transport(HOST+request_path,headers)
            if status==429 or 500 <= status <= 599:
                if attempt < self.retries:
                    self.sleep(min(2**attempt,4))
                    continue
            diagnostic=response_diagnostic(status,body,self.creds)
            if status!=200:raise IntakeError(f'HTTP_{status}: verify network, key permission, quota and provider availability',diagnostic)
            try:
                data=json.loads(body.decode('utf-8'),parse_float=finite_float,
                                parse_constant=lambda x:(_ for _ in ()).throw(ValueError(x)))
            except (ValueError,UnicodeError):raise SchemaError('INVALID_JSON_RESPONSE',diagnostic) from None
            if not isinstance(data,dict) or str(data.get('code'))!='0':
                raise IntakeError('PROVIDER_ERROR: response did not report success; inspect entitlement in developer portal',diagnostic)
            if 'data' not in data:raise SchemaError('MISSING_DATA',diagnostic)
            return dict(provider='OKX',path=path,params=params,requested_at=now,
                        received_at=self.clock(),response=scrub(data,self.creds),
                        original_response_sha256=hashlib.sha256(body).hexdigest(),
                        sanitized=True)
        raise IntakeError('RETRY_EXHAUSTED')


def leaderboard_rows(envelope):
    data=envelope['response'].get('data')
    if not isinstance(data,list):raise SchemaError('Leaderboard data must be a list')
    result,seen=[],set()
    for row in data:
        if not isinstance(row,dict):raise SchemaError('Invalid leaderboard row')
        address=wallet_address(row.get('walletAddress'))
        if address not in seen:
            result.append(dict(wallet=address,source='OKX',discovered_at=envelope['received_at'],
                               history_complete='false',provider_summary=row))
            seen.add(address)
    return result


def leaderboard_metadata(envelope,unique_count):
    n=len(envelope['response']['data'])
    return dict(provider_record_count=n,unique_wallet_count=unique_count,
        documented_max_records=20,
        warnings=([dict(code='LEADERBOARD_COUNT_EXCEEDS_DOCUMENTATION',observed=n,documented=20)]
                  if n>20 else []))


def history_page(envelope):
    data=envelope['response'].get('data')
    if not isinstance(data,dict) or not isinstance(data.get('transactionList'),list):
        raise SchemaError('History data must contain transactionList array')
    if 'cursor' not in data:raise SchemaError('History missing cursor; cannot determine pagination state')
    if not isinstance(data['cursor'],(str,int)):raise SchemaError('Invalid cursor')
    if len(data['transactionList'])>100:raise SchemaError('History page exceeds documented limit')
    return data['transactionList'],str(data['cursor'])


def scalar_number(value, positive=False):
    try:v=float(value)
    except (ValueError,TypeError):raise SchemaError('Invalid or missing numeric field') from None
    if not math.isfinite(v) or v<0 or (positive and v<=0):raise SchemaError('Invalid numeric field')
    return v


def observation(row,envelope,wallet,ordinal):
    if not isinstance(row,dict):raise SchemaError('Invalid history row')
    sides={'1':'buy','2':'sell','3':'transfer_in','4':'transfer_out'}
    if str(row.get('chainIndex'))!='501':raise SchemaError('Unexpected chain')
    side=sides.get(str(row.get('type')))
    if side is None:raise SchemaError('Unknown transaction type')
    token=wallet_address(row.get('tokenContractAddress'))
    quantity=scalar_number(row.get('amount'),positive=True)
    raw_value=row.get('valueUsd')
    if raw_value in (None,'') and side.startswith('transfer'):
        value=None
    else:
        if raw_value in (None,''):raise SchemaError('MISSING_TRADE_USD_VALUE')
        value=scalar_number(raw_value)
    millis=int(row['time'])
    if millis<1000000000000:raise SchemaError('Transaction time must be milliseconds')
    when=datetime.fromtimestamp(millis/1000,timezone.utc).isoformat().replace('+00:00','Z')
    if ts(when)>ts(envelope['received_at']):raise SchemaError('Future transaction time')
    params=envelope['params']
    if not int(params['begin']) <= millis <= int(params['end']):raise SchemaError('Transaction outside requested window')
    mc=row.get('marketCap')
    mc=scalar_number(mc) if mc not in (None,'') else None
    if mc==0:mc=None
    return dict(observation_id=f'{wallet}:{ordinal}',wallet=wallet,token=token,
        timestamp=when,observed_at=envelope['received_at'],side=side,quantity=quantity,
        provider_usd_value=value,provider_event_mc_usd=mc,
        usd_value_status='UNKNOWN' if value is None else 'PROVIDER_REPORTED_UNRECONCILED',
        provider_record_sha256=digest(row),fee_status='UNKNOWN',
        identity_status='ONCHAIN_EVENT_ID_NOT_IN_DOCUMENTED_SCHEMA',
        ordering_status='PROVIDER_ORDER_NOT_ONCHAIN_ORDER',
        mc_status='PROVIDER_HISTORICAL_MC_UNRECONCILED' if mc is not None else 'MISSING',
        history_status='UNVERIFIED',observation_only=True)


def collect_history(client,wallet,begin,end,max_pages,raw_dir):
    cursor=None;seen_cursors=set();fingerprints=set();observations=[];issues=[]
    stop='PAGE_BUDGET_REACHED';count=0;duplicates=0;malformed=0;failure=None
    for page_no in range(1,max_pages+1):
        params=dict(chainIndex='501',walletAddress=wallet,begin=str(begin),end=str(end),limit='100')
        if cursor:params['cursor']=cursor
        try:
            envelope=client.get(HISTORY,params)
            write_json(Path(raw_dir)/f'history_{page_no:03}.json',envelope,exclusive=True)
            rows,next_cursor=history_page(envelope)
        except IntakeError as exc:
            stop=str(exc);failure=exception_record(exc,'history',wallet=wallet);break
        count+=len(rows)
        for row in rows:
            fp=digest(row)
            if fp in fingerprints:duplicates+=1
            fingerprints.add(fp)
            # Keep repeated payloads: no documented onchain identity to dedupe safely.
            try:observations.append(observation(row,envelope,wallet,len(observations)+1))
            except (SchemaError,ValueError,OverflowError,KeyError) as exc:
                malformed+=1
                # These messages originate in our parser, not in provider payloads.
                reason=(str(exc) if isinstance(exc,SchemaError) else type(exc).__name__)
                issues.append(dict(provider_record_sha256=fp,issue='INVALID_OR_UNSUPPORTED_RECORD',reason=reason))
        if count>=1000:
            stop='PROVIDER_RECORD_CAP_REACHED';break
        if not next_cursor or next_cursor=='0':
            stop='PROVIDER_CURSOR_EXHAUSTED';break
        if next_cursor in seen_cursors or next_cursor==cursor:
            stop='REPEATED_CURSOR';break
        if not rows:
            stop='EMPTY_PAGE_WITH_CURSOR';break
        seen_cursors.add(next_cursor);cursor=next_cursor
    audit=dict(wallet=wallet,returned_records=count,parsed_observations=len(observations),
        stop_reason=stop,duplicate_payload_occurrences=duplicates,malformed_records=malformed,
        requested_begin_ms=begin,requested_end_ms=end,
        provider_cursor_exhausted=stop=='PROVIDER_CURSOR_EXHAUSTED',complete_history=False,
        request_failure=failure,
        basis_status='NOT_RECONCILED',eligible_for_accounting=False,
        note='Cursor exhaustion means requested provider range ended, not lifetime inventory completeness.')
    return observations,audit,issues


def replay(source_run,out):
    """Re-parse the exact saved rows without a network client or modified source files."""
    import copy
    source=Path(source_run).resolve();out=Path(out).resolve()
    if out==source or source in out.parents or out in source.parents:
        raise IntakeError('Replay output must be a separate sibling directory')
    if out.exists():raise IntakeError('OUTPUT_EXISTS: choose a new replay directory')
    prior=json.loads((source/'intake_report.json').read_text(encoding='utf-8'))
    candidates=copy.deepcopy(prior['candidates'])
    known={c['wallet'] for c in candidates}
    obs=[];issues=[];counts={};source_files={}
    for path in sorted((source/'raw').glob('wallet_*/history_*.json')):
        envelope=json.loads(path.read_text(encoding='utf-8'))
        wallet=envelope['params']['walletAddress']
        if wallet not in known:raise SchemaError('Replay history contains wallet outside recorded candidates')
        rows,cursor=history_page(envelope)
        source_files[str(path.relative_to(source))]=hashlib.sha256(path.read_bytes()).hexdigest()
        count=counts.setdefault(wallet,dict(returned=0,parsed=0,malformed=0))
        for row in rows:
            count['returned']+=1
            try:
                parsed=observation(row,envelope,wallet,count['returned'])
                # Stable pointer to the stored row; no chain identity is implied.
                parsed['source_file']=str(path.relative_to(source))
                obs.append(parsed);count['parsed']+=1
            except (SchemaError,ValueError,OverflowError,KeyError) as exc:
                count['malformed']+=1
                issues.append(dict(wallet=wallet,stage='history',provider_record_sha256=digest(row),
                    issue='INVALID_OR_UNSUPPORTED_RECORD',reason=str(exc) if isinstance(exc,SchemaError) else type(exc).__name__))
    audits=copy.deepcopy(prior['audits'])
    errors=[]
    for e in prior.get('errors',[]):
        if e.get('issue')!='INVALID_OR_UNSUPPORTED_RECORD':errors.append(e)
    errors.extend(issues)
    for audit in audits:
        count=counts.get(audit['wallet'],dict(returned=0,parsed=0,malformed=0))
        audit.update(returned_records=count['returned'],parsed_observations=count['parsed'],
                     malformed_records=count['malformed'],complete_history=False,eligible_for_accounting=False)
    report=intake_report(candidates,obs,audits,errors)
    report.update(schema='okx-intake-0.2.3',data_mode='OFFLINE_REPLAY_OF_'+prior['data_mode'],
        status='REPLAY_PARTIAL_REQUIRES_REVIEW' if errors else 'REPLAY_REQUIRES_RECONCILIATION',
        started_at=prior['started_at'],finished_at=prior['finished_at'],replayed_at=utc(),
        requests=0,original_run_requests=prior['requests'],
        discovery=copy.deepcopy(prior.get('discovery',{})),warnings=copy.deepcopy(prior.get('warnings',[])),
        config_sha256=prior['config_sha256'],source_files_sha256=source_files,
        prior_report_sha256=hashlib.sha256((source/'intake_report.json').read_bytes()).hexdigest(),
        parsed_before=prior['observation_count'],parsed_after=len(obs),
        unknown_usd_transfers=sum(r['side'].startswith('transfer') and r['provider_usd_value'] is None for r in obs),
        observation_timestamp_policy='Original received_at retained; replayed_at is processing time, not a backdated discovery time.')
    out.mkdir(parents=True)
    write_json(out/'intake_report.json',report,exclusive=True)
    (out/'intake_report.html').write_text(intake_html(report),encoding='utf-8')
    export_csv(out/'trade_observations.csv',obs)
    export_csv(out/'unverified_token_activity.csv',report['unverified_token_activity'])
    export_csv(out/'candidate_wallets.csv',[{k:c[k] for k in ('wallet','source','discovered_at','history_complete')} for c in candidates])
    return report


def retry_failed_history(source_run,out,creds):
    """One signed GET, same candidate/window as the original failed request, no pagination."""
    source=Path(source_run);out=Path(out)
    if out.exists():raise IntakeError('OUTPUT_EXISTS: choose a new diagnosis file')
    prior=json.loads((source/'intake_report.json').read_text(encoding='utf-8'))
    failed=[a for a in prior['audits'] if a.get('request_failure') and a.get('returned_records')==0]
    if len(failed)!=1:raise IntakeError('Expected exactly one failed history with zero rows; no request sent')
    a=failed[0]
    params=dict(chainIndex='501',walletAddress=a['wallet'],begin=str(a['requested_begin_ms']),
                end=str(a['requested_end_ms']),limit='100')
    cl=Client(creds,budget=1)
    report=dict(schema='targeted-history-diagnosis-0.2.3',wallet=a['wallet'],params=params,
                original_window_retained=True,selection_changed=False,payment_enabled=False)
    try:
        envelope=cl.get(HISTORY,params)
        rows,cursor=history_page(envelope)
        report.update(status='HISTORY_REQUEST_SUCCEEDED',returned_records=len(rows),response_envelope=envelope)
    except IntakeError as exc:
        report.update(status='HISTORY_REQUEST_FAILED',error=exception_record(exc,'history'))
    report.update(requests=cl.requests,diagnosed_at=utc())
    write_json(out,report,exclusive=True)
    summary={k:v for k,v in report.items() if k!='response_envelope'}
    print(json.dumps(summary,indent=2))
    return 0 if report['status']=='HISTORY_REQUEST_SUCCEEDED' else 2


def intake_report(candidates,observations,audits,errors):
    grouped=[]
    for c in candidates:
        rows=[r for r in observations if r['wallet']==c['wallet']]
        buys=[r for r in rows if r['side']=='buy']
        groups={}
        for r in rows:
            g=groups.setdefault(r['token'],dict(wallet=c['wallet'],token=r['token'],
                observed_buy_quantity=0.,observed_sell_quantity=0.,
                transfer_present=False,first_observed_buy=None,last_observed_buy=None,
                open_position_status='UNKNOWN_REQUIRES_BALANCE_AND_BASIS_RECONCILIATION'))
            if r['side']=='buy':
                g['observed_buy_quantity']+=r['quantity']
                g['first_observed_buy']=min(g['first_observed_buy'] or r['timestamp'],r['timestamp'])
                g['last_observed_buy']=max(g['last_observed_buy'] or r['timestamp'],r['timestamp'])
            elif r['side']=='sell':g['observed_sell_quantity']+=r['quantity']
            else:g['transfer_present']=True
        grouped.extend(groups.values())
        # Rows are observations, not guaranteed unique fills. Name counts accordingly.
        c['intake_summary']=dict(buy_observations=len(buys),
            provider_mc_below_6k=sum(r['provider_event_mc_usd'] is not None and r['provider_event_mc_usd']<6000 for r in buys),
            provider_mc_6k_to_10k=sum(r['provider_event_mc_usd'] is not None and 6000<=r['provider_event_mc_usd']<10000 for r in buys),
            missing_buy_mc=sum(r['provider_event_mc_usd'] is None for r in buys))
    return dict(schema='okx-intake-0.2',candidates=candidates,audits=audits,
        observation_count=len(observations),errors=errors,
        accounting_ready=False,copy_simulation_ready=False,
        unverified_token_activity=grouped,
        required_next=['Validate token amount decimals and USD/fee conventions against chain records.',
                       'Recover stable transaction/event identity and event order.',
                       'Reconcile inventory basis, transfers and current balances.',
                       'Obtain executable full-position liquidation quotes.'],
        warning='Provider MC observations are not verified entry episodes. No claim of stuck positions, hold duration or net profit.')


def intake_html(report):
    def esc(value):return html.escape(str(value))
    rows=[]
    for c in report['candidates']:
        summary=c['intake_summary']
        audit=next((a for a in report['audits'] if a['wallet']==c['wallet']),{})
        rows.append('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in
            [c['wallet'],summary['buy_observations'],summary['provider_mc_below_6k'],
             summary['provider_mc_6k_to_10k'],summary['missing_buy_mc'],audit.get('stop_reason','NOT_FETCHED')])+'</tr>')
    headers=['Alamat kandidat','BUY observations','Provider MC <6k','Provider MC 6k–10k','BUY tanpa MC','Status pagination']
    return '<!doctype html><html lang="id"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OKX Intake Audit</title><style>body{font:15px system-ui;max-width:1300px;margin:40px auto;padding:0 24px;background:#101923;color:#e4edf5}p,li{line-height:1.7}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:12px;text-align:left;border-bottom:1px solid #42505d}.scroll{overflow:auto}.status{background:#544018;padding:16px;border-radius:8px}</style><h1>OKX · Data Intake V0.2</h1><p class="status">'+esc(report['data_mode'])+' · BELUM DIREKONSILIASI · Bukan daftar rekomendasi</p><p>Pengambilan dimulai: '+esc(report['started_at'])+'<br>Request: '+esc(report['requests'])+' · Observasi: '+esc(report['observation_count'])+'</p><div class="scroll"><table><thead><tr>'+''.join('<th>'+esc(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join(rows)+'</tbody></table></div><h2>Gate data</h2><p>Accounting ready: false · Copy simulation ready: false. Angka tabel adalah observasi provider, bukan jumlah transaksi unik yang sudah diverifikasi. Wallet/token fixture bukan bukti aktivitas nyata.</p><ul>'+''.join('<li>'+esc(s)+'</li>' for s in report['required_next'])+'</ul><p>'+esc(report['warning'])+'</p></html>'


def read_fixture_client(folder):
    # Each fixture explicitly synthetic; endpoint contracts still validated.
    folder=Path(folder)
    if not (folder/'SYNTHETIC_FIXTURE.txt').exists():raise IntakeError('Fixture must be labelled synthetic')
    def transport(url,headers):
        parsed=urllib.parse.urlsplit(url)
        params=urllib.parse.parse_qs(parsed.query)
        if parsed.path==LEADERBOARD:name='leaderboard.json'
        elif parsed.path==OVERVIEW:name='overview.json'
        else:
            cursor=params.get('cursor',[''])[0]
            name='history_2.json' if cursor else 'history_1.json'
        return 200,(folder/name).read_bytes()
    return Client(['fixture-api','fixture-secret','fixture-pass'],transport=transport,
                  sleeper=lambda x:None,interval=0,clock=lambda:'2026-10-20T00:00:00Z')


def collect(client,out,fixture=False):
    out=Path(out)
    if out.exists():raise IntakeError('OUTPUT_EXISTS: choose a new run directory')
    out.mkdir(parents=True)
    raw=out/'raw';raw.mkdir()
    start=client.clock()
    end=int(ts(start)*1000)
    begin=end-PLAN['history_days']*86400*1000
    plan=dict(PLAN,recorded_at=start,data_mode='SYNTHETIC_FIXTURE' if fixture else 'LIVE_PROVIDER_UNRECONCILED')
    write_json(out/'intake_plan.json',plan,exclusive=True)
    candidates=[];observations=[];audits=[];errors=[];discovery={}
    try:
        env=client.get(LEADERBOARD,{k:PLAN[k] for k in ('chainIndex','timeFrame','sortBy','walletType')})
        write_json(raw/'leaderboard.json',env,exclusive=True)
        all_candidates=leaderboard_rows(env)
        discovery=leaderboard_metadata(env,len(all_candidates))
        candidates=all_candidates[:PLAN['max_wallets']]
        for i,c in enumerate(candidates,1):
            wallet=c['wallet'];wd=raw/f'wallet_{i:03}';wd.mkdir()
            try:
                env=client.get(OVERVIEW,dict(chainIndex='501',walletAddress=wallet,timeFrame='5'))
                write_json(wd/'overview.json',env,exclusive=True)
                if not isinstance(env['response']['data'],dict):raise SchemaError('Overview data must be object')
                c['provider_overview_status']='RECEIVED_UNRECONCILED'
            except IntakeError as exc:
                errors.append(exception_record(exc,'overview',wallet=wallet))
            obs,audit,issues=collect_history(client,wallet,begin,end,PLAN['max_pages_per_wallet'],wd)
            observations.extend(obs);audits.append(audit)
            if audit.get('request_failure'):errors.append(audit['request_failure'])
            for issue in issues:errors.append(dict(wallet=wallet,stage='history',**issue))
    except IntakeError as exc:
        errors.append(exception_record(exc,'leaderboard'))
    r=intake_report(candidates,observations,audits,errors)
    r.update(data_mode=plan['data_mode'],started_at=start,finished_at=client.clock(),requests=client.requests,
             status=('INTAKE_PARTIAL_REQUIRES_REVIEW' if candidates and errors else
                     'INTAKE_RECEIVED_RECONCILIATION_REQUIRED' if candidates else
                     'LEADERBOARD_REQUEST_OR_SCHEMA_FAILED' if errors else 'LEADERBOARD_EMPTY_RESPONSE'),
             config_sha256=digest(plan))
    r['discovery']=discovery
    r['warnings']=discovery.get('warnings',[])
    write_json(out/'intake_report.json',r,exclusive=True)
    (out/'intake_report.html').write_text(intake_html(r),encoding='utf-8')
    export_csv(out/'candidate_wallets.csv',[{k:c[k] for k in ('wallet','source','discovered_at','history_complete')} for c in candidates])
    export_csv(out/'trade_observations.csv',observations)
    export_csv(out/'unverified_token_activity.csv',r['unverified_token_activity'])
    # This receipt does not lock a trading strategy or pass profitability gates.
    write_json(out/'intake_receipt.json',dict(data_mode=r['data_mode'],candidates=[c['wallet'] for c in candidates],
        recorded_at=start,plan_sha256=digest(plan),report_sha256=digest(r),
        selection='first up to 3 returned by fixed OKX query, no outcome tuning'),exclusive=True)
    return r


def cli_summary(report,out=None):
    summary={k:report[k] for k in ('status','data_mode','requests','observation_count','accounting_ready')}
    summary['candidate_count']=len(report.get('candidates',[]))
    summary['errors']=report.get('errors',[])
    summary['warnings']=report.get('warnings',[])
    summary['discovery']=report.get('discovery',{})
    if out is not None:summary['report_path']=str(Path(out)/'intake_report.json')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('doctor','collect','demo'):
        p=sub.add_parser(name)
        p.add_argument('--out',required=True)
        if name!='demo':p.add_argument('--prompt',action='store_true')
        else:p.add_argument('--fixtures',default='provider_fixtures')
    for name in ('replay','retry-history'):
        p=sub.add_parser(name)
        p.add_argument('--source-run',required=True)
        p.add_argument('--out',required=True)
        if name=='retry-history':p.add_argument('--prompt',action='store_true')
    args=parser.parse_args()
    try:
        if args.command=='replay':
            result=replay(args.source_run,args.out)
            print(json.dumps({k:result[k] for k in ('status','parsed_before','parsed_after','unknown_usd_transfers','requests','accounting_ready')},indent=2))
            return 0
        if args.command=='retry-history':
            return retry_failed_history(args.source_run,args.out,credentials(args.prompt))
        if args.command=='doctor':
            out=Path(args.out)
            report=dict(checked_at=utc(),credential_presence={k:bool(os.environ.get(k)) for k in ENV_NAMES},
                        authenticated_api_tested=False,status='NOT_TESTED')
            try:
                creds=credentials(args.prompt)
                report['credential_presence']={k:bool(v) for k,v in zip(ENV_NAMES,creds)}
                report['credential_presence_scope']='loaded in current process; values not recorded'
                cl=Client(creds,budget=1)
                env=cl.get(LEADERBOARD,{k:PLAN[k] for k in ('chainIndex','timeFrame','sortBy','walletType')})
                report.update(authenticated_api_tested=True,http_status=200,provider_code='0',requests=cl.requests)
                rows=leaderboard_rows(env)
                report.update(status='AUTHENTICATED_LEADERBOARD_RECEIVED',authenticated_api_tested=True,
                              returned_candidates=len(rows),requests=cl.requests,
                              discovery=leaderboard_metadata(env,len(rows)))
            except IntakeError as exc:report.update(status='BLOCKED',reason=str(exc),diagnostic=exc.diagnostic)
            write_json(out,report,exclusive=True)
            print(json.dumps(report,indent=2));return 0 if report['status'].startswith('AUTHENTICATED') else 2
        client=read_fixture_client(args.fixtures) if args.command=='demo' else Client(credentials(args.prompt))
        r=collect(client,args.out,fixture=args.command=='demo')
        print(json.dumps(cli_summary(r,args.out),indent=2))
        return 0 if r['candidates'] and not r['errors'] else 2
    except (IntakeError,FileExistsError) as exc:
        print(json.dumps(dict(status='BLOCKED',reason=str(exc)),indent=2));return 2


if __name__=='__main__':raise SystemExit(main())
