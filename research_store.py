"""Offline, versioned SQLite archive of legacy provider evidence (standard library)."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid

import okx_reader as provider

SCHEMA_VERSION = 1
APPLICATION_ID = 0x53575231  # SWR1
PARSER_VERSION = 'okx-observation-0.3.1'
DATABASE = 'data/research.sqlite3'


class StorageError(RuntimeError):
    pass


def utc():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def local(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    path = root / relative
    if relative.is_absolute() or path == root or root not in path.resolve().parents:
        raise StorageError('PATH_OUTSIDE_WORKSPACE')
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root):
        raise StorageError('SYMLINK_NOT_ALLOWED')
    return path


@contextmanager
def writer_lock(root, legacy=False):
    names = (['watchlist/sync.lock', 'watchlist/registry.lock', 'workspace-import.lock'] if legacy else [])
    names += ['data/research.lock']
    owned = []
    try:
        for name in names:
            path = local(root, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open('x', encoding='utf-8') as stream:
                    stream.write(str(os.getpid()))
            except FileExistsError:
                raise StorageError('WORKSPACE_BUSY: stop workers and review existing locks') from None
            owned.append(path)
        yield
    finally:
        for path in reversed(owned):
            path.unlink(missing_ok=True)


SCHEMA = '''
CREATE TABLE artifacts (
 id INTEGER PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL,
 kind TEXT NOT NULL, content BLOB NOT NULL, imported_at TEXT NOT NULL,
 UNIQUE(path, sha256)
);
CREATE TABLE current_artifacts (
 path TEXT PRIMARY KEY, artifact_id INTEGER NOT NULL REFERENCES artifacts(id)
);
CREATE TABLE import_state (
 id INTEGER PRIMARY KEY CHECK(id=1), imported_at TEXT NOT NULL, active_report_path TEXT
);
CREATE TABLE wallet_snapshots (
 artifact_id INTEGER NOT NULL REFERENCES artifacts(id), wallet TEXT NOT NULL,
 first_discovered_at TEXT, status TEXT NOT NULL, parsed_observations INTEGER,
 data_json TEXT NOT NULL, PRIMARY KEY(artifact_id, wallet)
);
CREATE TABLE payloads (
 payload_id TEXT PRIMARY KEY, provider TEXT NOT NULL, chain_index TEXT NOT NULL,
 wallet TEXT NOT NULL, record_sha256 TEXT NOT NULL, raw_json TEXT NOT NULL,
 UNIQUE(provider, chain_index, wallet, record_sha256)
);
CREATE TABLE occurrences (
 artifact_id INTEGER NOT NULL REFERENCES artifacts(id), envelope_key TEXT NOT NULL,
 row_number INTEGER NOT NULL, payload_id TEXT NOT NULL REFERENCES payloads(payload_id),
 received_at TEXT NOT NULL, received_epoch REAL NOT NULL, requested_at TEXT,
 endpoint TEXT NOT NULL, params_json TEXT NOT NULL,
 parsed_json TEXT, parse_error TEXT, parser_version TEXT NOT NULL,
 PRIMARY KEY(artifact_id, envelope_key, row_number)
);
CREATE INDEX occurrences_payload ON occurrences(payload_id);
CREATE VIEW payload_provenance AS
SELECT p.*, MIN(o.received_epoch) AS first_received_epoch,
 COUNT(*) AS archived_occurrences,
 MAX(o.parsed_json IS NOT NULL) AS has_parsed_observation
FROM payloads p JOIN occurrences o ON o.payload_id=p.payload_id
GROUP BY p.payload_id;
CREATE TABLE legacy_rows (
 artifact_id INTEGER NOT NULL REFERENCES artifacts(id), row_number INTEGER NOT NULL,
 wallet TEXT NOT NULL, record_sha256 TEXT, data_json TEXT NOT NULL,
 PRIMARY KEY(artifact_id, row_number)
);
CREATE TABLE reports (
 artifact_id INTEGER PRIMARY KEY REFERENCES artifacts(id), status TEXT NOT NULL,
 observation_count INTEGER NOT NULL, data_json TEXT NOT NULL
);
'''


def validate_database(conn):
    if conn.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
        raise StorageError('NOT_A_RESEARCH_DATABASE')
    if conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
        raise StorageError('UNSUPPORTED_DATABASE_VERSION')


def open_readonly(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise StorageError('DATABASE_NOT_INITIALIZED: run db-import first')
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA query_only=ON')
        validate_database(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def initialize_database(path):
    """Create schema only in a new file; reject unrelated or newer databases."""
    created = not path.exists()
    conn = sqlite3.connect(path, timeout=5)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        if created:
            conn.executescript('BEGIN IMMEDIATE;\n' + SCHEMA +
                               f'PRAGMA application_id={APPLICATION_ID};\n'
                               f'PRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;')
        validate_database(conn)
    except BaseException:
        conn.close()
        if created:
            path.unlink(missing_ok=True)
        raise
    return conn


def sources(root):
    """Read only allowlisted JSON; credentials, exports, demos and databases are excluded."""
    registry = local(root, 'watchlist/registry.json')
    if not registry.is_file():
        raise StorageError('REGISTRY_REQUIRED: import legacy data or initialize the workspace first')
    project = local(root, 'project.json')
    cfg = json.loads(project.read_text(encoding='utf-8-sig'))
    selected = [(project, 'project'), (registry, 'registry')]
    pointer = local(root, 'watchlist/latest_batch.json')
    if pointer.exists():
        selected.append((pointer, 'pointer'))
    runs = local(root, 'runs')
    if runs.exists():
        for base, dirs, files in os.walk(runs, followlinks=False):
            base = Path(base)
            dirs[:] = sorted(d for d in dirs if not (base == runs and d.startswith('demo_')))
            for name in dirs:
                local(root, (base/name).relative_to(root))
            for name in sorted(files):
                path = base/name
                rel = path.relative_to(root)
                if path.suffix != '.json':
                    continue
                kind = ('report' if name == 'intake_report.json' else
                        'legacy_observations' if name == 'observations.json' else
                        'raw' if 'raw' in rel.parts or name == 'diagnosis_source.json' else None)
                # Diagnosis contains its provider envelope instead of a separate raw page.
                if rel.as_posix() == cfg.get('diagnosis'):
                    kind = 'raw'
                if kind:
                    selected.append((local(root, rel), kind))
    items = []
    for path, kind in sorted(selected):
        content = path.read_bytes()
        try:
            document = json.loads(content.decode('utf-8-sig'), parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
        except (ValueError, UnicodeError):
            raise StorageError('INVALID_SOURCE_JSON: ' + path.relative_to(root).as_posix()) from None
        if kind == 'pointer':
            local(root, document['run_path'])
        items.append((path.relative_to(root).as_posix(), kind, content, document))
    return items


def active_report_path(items, cfg):
    available = {rel for rel, kind, _, _ in items if kind == 'report'}
    pointers = [d['run_path'] for _, kind, _, d in items if kind == 'pointer']
    for folder in pointers + [cfg.get('followup_run'), cfg.get('baseline_report')]:
        if folder:
            relative = folder.replace('\\', '/').rstrip('/') + '/intake_report.json'
            if relative in available:
                return relative
    return None


def envelopes(document):
    if not isinstance(document, dict):
        raise StorageError('INVALID_RAW_ENVELOPE')
    if 'response_envelope' in document:
        return [('response_envelope', document['response_envelope'])]
    if 'provider' in document:
        return [('', document)]
    # A failed diagnosis has no raw response. Preserve its original bytes only.
    if document.get('schema', '').startswith('targeted-history-diagnosis-'):
        return []
    raise StorageError('UNSUPPORTED_RAW_DOCUMENT')


def import_raw(conn, artifact_id, document):
    for key, env in envelopes(document):
        if env.get('provider') != 'OKX' or env.get('path') not in (provider.HISTORY, provider.LEADERBOARD, provider.OVERVIEW):
            raise StorageError('UNSUPPORTED_PROVIDER_ENDPOINT')
        params = env['params']
        if str(params.get('chainIndex')) != '501':
            raise StorageError('UNSUPPORTED_CHAIN')
        received = env['received_at']
        epoch = provider.ts(received)
        if env['path'] != provider.HISTORY:
            continue  # Discovery/overview bytes remain available in artifacts.
        if str(env['response'].get('code')) != '0':
            raise StorageError('UNSUCCESSFUL_HISTORY_ENVELOPE')
        wallet = provider.wallet_address(params['walletAddress'])
        rows, _ = provider.history_page(env)
        for number, row in enumerate(rows, 1):
            fingerprint = provider.digest(row)
            payload_id = sha(encode(['OKX', '501', wallet, fingerprint]).encode())
            conn.execute('INSERT OR IGNORE INTO payloads VALUES (?,?,?,?,?,?)',
                         (payload_id, 'OKX', '501', wallet, fingerprint, encode(row)))
            parsed, error = None, None
            try:
                parsed = encode(provider.observation(row, env, wallet, number))
            except (provider.SchemaError, ValueError, OverflowError, KeyError, TypeError) as exc:
                error = str(exc) if isinstance(exc, provider.SchemaError) else type(exc).__name__
            conn.execute('INSERT INTO occurrences VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                         (artifact_id, key, number, payload_id, received, epoch, env.get('requested_at'),
                          env['path'], encode(params), parsed, error, PARSER_VERSION))


def import_document(conn, artifact_id, kind, document):
    if kind == 'registry':
        if document.get('schema') != 'wallet-registry-0.2.6':
            raise StorageError('UNSUPPORTED_REGISTRY_SCHEMA')
        for entry in document['wallets']:
            wallet = provider.wallet_address(entry['wallet'])
            history = entry['history']
            conn.execute('INSERT INTO wallet_snapshots VALUES (?,?,?,?,?,?)',
                         (artifact_id, wallet, entry.get('first_discovered_at'), history['status'],
                          history.get('parsed_observations'), encode(entry)))
    elif kind == 'raw':
        import_raw(conn, artifact_id, document)
    elif kind == 'report':
        if not isinstance(document.get('observation_count'), int) or document['observation_count'] < 0:
            raise StorageError('INVALID_REPORT_COUNT')
        conn.execute('INSERT INTO reports VALUES (?,?,?,?)',
                     (artifact_id, document['status'], document['observation_count'], encode(document)))
    elif kind == 'legacy_observations':
        if not isinstance(document, list):
            raise StorageError('INVALID_LEGACY_OBSERVATIONS')
        for number, row in enumerate(document, 1):
            conn.execute('INSERT INTO legacy_rows VALUES (?,?,?,?,?)',
                         (artifact_id, number, provider.wallet_address(row['wallet']),
                          row.get('provider_record_sha256'), encode(row)))
    elif kind == 'pointer':
        if not isinstance(document.get('run_path'), str):
            raise StorageError('INVALID_REPORT_POINTER')


def import_workspace(root):
    root = Path(root).resolve()
    with writer_lock(root, legacy=True):
        items = sources(root)
        path = local(root, DATABASE)
        with closing(initialize_database(path)) as conn:
            before = conn.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0]
            before_payloads = conn.execute('SELECT COUNT(*) FROM payloads').fetchone()[0]
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('DELETE FROM current_artifacts')
                for rel, kind, content, document in items:
                    fingerprint = sha(content)
                    row = conn.execute('SELECT id FROM artifacts WHERE path=? AND sha256=?', (rel, fingerprint)).fetchone()
                    if row:
                        artifact_id = row[0]
                    else:
                        cursor = conn.execute('INSERT INTO artifacts(path,sha256,kind,content,imported_at) VALUES (?,?,?,?,?)',
                                              (rel, fingerprint, kind, content, utc()))
                        artifact_id = cursor.lastrowid
                        try:
                            import_document(conn, artifact_id, kind, document)
                        except (AttributeError, TypeError, KeyError, ValueError):
                            raise StorageError('INVALID_SOURCE_SCHEMA: ' + rel) from None
                    conn.execute('INSERT INTO current_artifacts VALUES (?,?)', (rel, artifact_id))
                # Check source bytes and membership again before the single database commit.
                current = sources(root)
                if [(p, sha(b)) for p, _, b, _ in current] != [(p, sha(b)) for p, _, b, _ in items]:
                    raise StorageError('SOURCE_CHANGED: stop writers and repeat db-import')
                cfg = json.loads(local(root, 'project.json').read_text(encoding='utf-8-sig'))
                conn.execute('INSERT OR REPLACE INTO import_state VALUES (1,?,?)', (utc(), active_report_path(items, cfg)))
            result = summary_connection(conn)
            result.update(status='DATABASE_IMPORTED', network_requests=0,
                          artifacts_added=result['artifact_versions'] - before,
                          payloads_added=result['provider_payloads'] - before_payloads,
                          source_files_modified=False)
            return result


def summary_connection(conn):
    count = lambda table: conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
    registry = conn.execute("SELECT artifact_id FROM current_artifacts WHERE path='watchlist/registry.json'").fetchone()
    wallets = [] if registry is None else conn.execute(
        'SELECT wallet,status,parsed_observations,data_json FROM wallet_snapshots WHERE artifact_id=? ORDER BY wallet', registry).fetchall()
    statuses = {}
    for _, state, _, _ in wallets:
        statuses[state] = statuses.get(state, 0) + 1
    active = None
    pointer = conn.execute('SELECT active_report_path FROM import_state WHERE id=1').fetchone()
    if pointer and pointer[0]:
        rel = pointer[0]
        record = conn.execute('SELECT r.status,r.observation_count,r.data_json FROM current_artifacts c JOIN reports r ON r.artifact_id=c.artifact_id WHERE c.path=?', (rel,)).fetchone()
        if record:
            report = json.loads(record[2])
            active = dict(report_path=rel, status=record[0], observations=record[1], errors=len(report.get('errors', [])))
    parsed = conn.execute('SELECT COUNT(DISTINCT payload_id) FROM occurrences WHERE parsed_json IS NOT NULL').fetchone()[0]
    unknown = conn.execute('''SELECT COUNT(*) FROM legacy_rows l WHERE NOT EXISTS
        (SELECT 1 FROM payloads p WHERE p.provider='OKX' AND p.chain_index='501'
         AND p.wallet=l.wallet AND p.record_sha256=l.record_sha256)''').fetchone()[0]
    imported = conn.execute('SELECT imported_at FROM import_state WHERE id=1').fetchone()
    return dict(schema_version=SCHEMA_VERSION, last_imported_at=imported[0] if imported else None,
                artifact_versions=count('artifacts'), current_source_files=count('current_artifacts'),
                wallet_count=len(wallets), history_status_counts=statuses,
                registry_observations=sum(w[2] or 0 for w in wallets), provider_payloads=count('payloads'),
                parsed_provider_payloads=parsed, raw_row_occurrences=count('occurrences'),
                parse_error_occurrences=conn.execute('SELECT COUNT(*) FROM occurrences WHERE parse_error IS NOT NULL').fetchone()[0],
                legacy_observation_rows=count('legacy_rows'), legacy_rows_without_raw_payload=unknown,
                active_batch=active, accounting_ready=False, unique_onchain_events=None)


def database_status(root):
    path = local(root, DATABASE)
    if not path.exists():
        return dict(status='DATABASE_NOT_INITIALIZED', network_requests=0)
    with closing(open_readonly(path)) as conn:
        with conn:
            conn.execute('BEGIN')
            result = summary_connection(conn)
            return dict(status='DATABASE_READY' if result['last_imported_at'] else 'DATABASE_EMPTY',
                        network_requests=0, **result)


def verify_connection(conn):
    validate_database(conn)
    if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise StorageError('DATABASE_INTEGRITY_FAILED')
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise StorageError('DATABASE_FOREIGN_KEY_FAILED')
    for content, fingerprint in conn.execute('SELECT content,sha256 FROM artifacts'):
        if sha(content) != fingerprint:
            raise StorageError('ARCHIVED_ARTIFACT_HASH_MISMATCH')
    return summary_connection(conn)


def copy_database(source, destination):
    """SQLite online backup into an exclusively published file, then verify archived bytes."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.database-', suffix='.tmp', dir=destination.parent)
    os.close(fd)
    temp = Path(temp)
    try:
        with closing(open_readonly(source)) as src, closing(sqlite3.connect(temp)) as dest:
            src.backup(dest)
            summary = verify_connection(dest)
        with temp.open('rb+') as stream:
            os.fsync(stream.fileno())
        os.link(temp, destination)  # Exclusive: existing files are never replaced.
        return summary
    finally:
        temp.unlink(missing_ok=True)


def backup_database(root):
    root = Path(root).resolve()
    with writer_lock(root):
        destination = local(root, 'data/backups/research_' + uuid.uuid4().hex + '.sqlite3')
        summary = copy_database(local(root, DATABASE), destination)
    return dict(status='BACKUP_VERIFIED', path=str(destination), summary=summary, network_requests=0)


def restore_database(root, backup):
    root = Path(root).resolve()
    # Restore into a separate verified database, preserving the active database.
    source = local(root, backup)
    with writer_lock(root):
        destination = local(root, 'data/restored/research_' + uuid.uuid4().hex + '.sqlite3')
        summary = copy_database(source, destination)
    return dict(status='RESTORE_VERIFIED', path=str(destination), active_database_replaced=False,
                summary=summary, network_requests=0)


def dashboard(root):
    """Generate a local read-only HTML snapshot entirely from the database."""
    root = Path(root).resolve()
    with closing(open_readonly(local(root, DATABASE))) as conn:
        conn.execute('BEGIN')
        info = summary_connection(conn)
        registry = conn.execute("SELECT artifact_id FROM current_artifacts WHERE path='watchlist/registry.json'").fetchone()
        rows = [] if registry is None else conn.execute('''SELECT wallet,status,parsed_observations FROM wallet_snapshots
            WHERE artifact_id=? ORDER BY wallet''', registry).fetchall()
        evidence = conn.execute('''SELECT p.wallet,p.record_sha256,a.path,o.envelope_key,o.row_number,o.received_at,o.parse_error
            FROM occurrences o JOIN payloads p ON p.payload_id=o.payload_id JOIN artifacts a ON a.id=o.artifact_id
            ORDER BY a.path,o.row_number''').fetchall()
    esc = lambda value: html.escape('UNKNOWN' if value is None else str(value), quote=True)
    cards = [('Wallet', info['wallet_count']), ('Observasi registry', info['registry_observations']),
             ('Payload provider berbeda', info['provider_payloads']),
             ('Observasi batch terakhir', (info['active_batch'] or {}).get('observations'))]
    text = '''<!doctype html><html lang="id"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Status riset wallet</title><style>:root{color-scheme:dark}body{font:15px system-ui;margin:24px;background:#101923;color:#edf3fb}
.cards{display:flex;flex-wrap:wrap;gap:12px}.card{padding:18px;background:#203045;border-radius:8px;min-width:170px}.card strong{display:block;font-size:28px}
table{border-collapse:collapse;width:100%;font-size:12px}th,td{text-align:left;padding:10px;border-bottom:1px solid #33465e;overflow-wrap:anywhere;max-width:300px}
.scroll{overflow:auto}p{line-height:1.6}input{padding:10px;width:300px;max-width:90%}</style>
<h1>Status riset wallet</h1><p>Snapshot lokal dari SQLite · ''' + esc(utc()) + ' · waktu UTC</p><div class="cards">'
    text += ''.join('<div class="card"><strong>' + esc(value) + '</strong>' + esc(label) + '</div>' for label, value in cards) + '</div>'
    text += '<p>Data diimpor ke SQLite pada ' + esc(info['last_imported_at']) + '. Setelah pengumpulan baru, jalankan db-import untuk memperbarui snapshot.</p>'
    text += '<p>Jumlah registry dan batch terakhir mempunyai cakupan berbeda. Payload berbeda dihitung berdasarkan isi record provider; jumlah transaksi on-chain unik masih UNKNOWN. Riwayat parsial belum membuktikan hold, win rate atau posisi sangkut.</p>'
    text += '<p>Batch terakhir: ' + esc(encode(info['active_batch'])) + '</p>'
    text += '<p>Baris raw tersimpan: ' + esc(info['raw_row_occurrences']) + '; parse error: ' + esc(info['parse_error_occurrences']) + '; baris observasi lama tanpa raw yang cocok: ' + esc(info['legacy_rows_without_raw_payload']) + '.</p>'
    text += '<h2>Seluruh wallet</h2><input id="q" placeholder="Cari alamat atau status"><div class="scroll"><table><thead><tr><th>Wallet</th><th>Status riwayat</th><th>Observasi registry</th></tr></thead><tbody id="wallets">'
    text += ''.join('<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in row) + '</tr>' for row in rows) + '</tbody></table></div>'
    text += '<h2>Jejak sumber raw</h2><p>Nomor baris mulai dari 1. Salinan sumber dan versi lama tetap tersedia dalam arsip SQLite. Halaman ini tidak mengambil data API.</p><div class="scroll"><table><thead><tr>'
    text += ''.join('<th>' + label + '</th>' for label in ['Wallet', 'SHA256 record', 'File sumber', 'Envelope', 'Baris', 'Diterima UTC', 'Parse error']) + '</tr></thead><tbody>'
    text += ''.join('<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in row) + '</tr>' for row in evidence) + '</tbody></table></div>'
    text += '''<script>document.getElementById('q').addEventListener('input',function(){const q=this.value.toLowerCase();document.querySelectorAll('#wallets tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q))});</script></html>'''
    target = local(root, 'data/status.html')
    target.write_text(text, encoding='utf-8')
    return dict(status='DASHBOARD_READY', path=str(target), network_requests=0)
