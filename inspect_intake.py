"""Inspect an existing intake report without a new API call or credential input."""
import argparse
import json
import re
from pathlib import Path


def safe_summary(report):
    # Output fixed error categories and bounded numeric provider codes only.
    # Do not echo untrusted error strings, provider messages or payloads from disk.
    errors=[]
    allowed={'PROVIDER_ERROR','NETWORK_ERROR','INVALID_JSON_RESPONSE','MISSING_DATA',
             'REQUEST_BUDGET_EXHAUSTED','RETRY_EXHAUSTED'}
    for e in report.get('errors',[]):
        text=str(e.get('error',''))
        category=text.split(':',1)[0]
        if category not in allowed and not re.fullmatch(r'HTTP_[0-9]{3}',category):
            category='SCHEMA_OR_OTHER_ERROR'
        stage=e.get('stage')
        if stage not in ('leaderboard','history','overview'):stage='OTHER'
        d=e.get('diagnostic') or {}
        code=d.get('provider_code')
        if code is not None and not re.fullmatch(r'[0-9]{1,12}',str(code)):code=None
        http=d.get('http_status')
        if not isinstance(http,int) or not 100<=http<=599:http=None
        errors.append(dict(stage=stage,category=category,http_status=http,provider_code=code))
    status=report.get('status')
    known={'NO_CANDIDATES_RECEIVED','LEADERBOARD_REQUEST_OR_SCHEMA_FAILED','LEADERBOARD_EMPTY_RESPONSE',
           'INTAKE_RECEIVED_RECONCILIATION_REQUIRED','INTAKE_PARTIAL_REQUIRES_REVIEW',
           'REPLAY_PARTIAL_REQUIRES_REVIEW','REPLAY_REQUIRES_RECONCILIATION'}
    if status not in known:status='UNKNOWN_REPORT_STATUS'
    return dict(status=status,candidate_count=len(report.get('candidates',[])),
        error_count=len(errors),errors=errors,network_requests_made_by_inspector=0,
        note='V0.2 discarded provider code on failure; a generic PROVIDER_ERROR cannot be decoded retrospectively.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('report',type=Path)
    args=p.parse_args()
    try:
        r=json.loads(args.report.read_text(encoding='utf-8-sig'))
        print(json.dumps(safe_summary(r),indent=2))
    except (OSError,ValueError):raise SystemExit('Could not read intake JSON report')


if __name__=='__main__':main()
