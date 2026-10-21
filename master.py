"""Stable local entry point for Smart Wallet Research. Python standard library only."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid
import webbrowser
import zipfile

from okx_reader import IntakeError
from research_store import StorageError

ROOT = Path(__file__).resolve().parent


class ProjectError(RuntimeError):
    pass


class NoLocalReport(ProjectError):
    pass


def settings(root):
    return json.loads((Path(root)/'project.json').read_text(encoding='utf-8-sig'))


def location(root, cfg, name):
    root = Path(root).resolve()
    rel = Path(cfg[name])
    path = (root/rel).resolve()
    if rel.is_absolute() or path == root or root not in path.parents:
        raise ProjectError('Path project.json harus berada di dalam folder master')
    return path


def latest_run(root):
    cfg = settings(root)
    pointer = Path(root)/'watchlist/latest_batch.json'
    if pointer.is_file():
        value = json.loads(pointer.read_text(encoding='utf-8'))['run_path']
        folder = location(root, {'batch':value}, 'batch')
        if (folder/'intake_report.json').is_file(): return folder
    for key in ('followup_run', 'baseline_report'):
        folder = location(root, cfg, key)
        if (folder/'intake_report.json').is_file():
            return folder
    raise NoLocalReport('Laporan belum tersedia; impor data lama atau jalankan demo offline')


def status(root=ROOT):
    cfg = settings(root)
    try:
        folder = latest_run(root)
    except NoLocalReport:
        folder = None
    report = json.loads((folder/'intake_report.json').read_text(encoding='utf-8-sig')) if folder else dict(
        status='EMPTY_WORKSPACE', observation_count=0, accounting_ready=False, errors=[], audits=[])
    info = dict(project=cfg['project'], code_version=cfg['code_version'],
                folder=str(Path(root).resolve()), report=str(folder/'report.html') if folder else None,
                status=report['status'], observations=report['observation_count'],
                side_counts=report.get('side_counts', {}), accounting_ready=report['accounting_ready'],
                active_errors=len(report.get('errors', [])),
                coverage=[{k:a.get(k) for k in ('wallet', 'stored_pages', 'returned_records', 'stop_reason')}
                          for a in report['audits']])
    if (Path(root)/'watchlist/registry.json').is_file():
        import wallet_registry
        info['registry_summary'] = wallet_registry.summary(wallet_registry.load(root))
    if (Path(root)/'data/research.sqlite3').exists():
        from research_store import database_status
        info['database_summary'] = database_status(root)
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return info


def initialize(root=ROOT):
    root = Path(root).resolve()
    cfg = settings(root)
    for key in ('source_run', 'diagnosis', 'baseline_report', 'followup_run', 'exports'):
        location(root, cfg, key)
    import wallet_registry
    state = wallet_registry.ensure(root)
    for name in ('runs', 'exports', 'data'):
        location(root, {'path': name}, 'path').mkdir(parents=True, exist_ok=True)
    result = dict(status='WORKSPACE_READY', requests=0, registry_summary=wallet_registry.summary(state))
    print(json.dumps(result, indent=2))
    return result


def demo(root=ROOT):
    import okx_reader
    root = Path(root).resolve()
    out = location(root, {'path': 'runs/demo_'+uuid.uuid4().hex[:12]}, 'path')
    report = okx_reader.collect(okx_reader.read_fixture_client(root/'provider_fixtures'), out, fixture=True)
    result = okx_reader.cli_summary(report, out)
    result['intake_report_path'] = result['report_path']
    result['report_path'] = str(out/'intake_report.html')
    result['network_requests'] = 0
    result['scope'] = 'SYNTHETIC_DEMO_ONLY; registry and latest report pointer unchanged'
    print(json.dumps(result, indent=2))
    return result


def open_report(root=ROOT):
    path = latest_run(root)/'report.html'
    if not path.is_file():
        raise ProjectError('report.html belum tersedia; gunakan status untuk memeriksa run')
    print('Membuka: '+str(path))
    if not webbrowser.open(path.as_uri()):
        print('Browser tidak terbuka otomatis. Buka file tersebut melalui File Explorer.')
    return path


def continue_history(root=ROOT):
    root = Path(root).resolve(); cfg = settings(root)
    source = location(root, cfg, 'source_run')
    diagnosis = location(root, cfg, 'diagnosis')
    out = location(root, cfg, 'followup_run')
    if out.exists():
        print('Folder kelanjutan sudah ada. Tidak ada request baru yang dikirim.')
        print('Periksa laporan/status, kemudian ekspor hasil untuk audit: '+str(out))
        return 2
    if not (source/'intake_report.json').is_file() or not diagnosis.is_file():
        raise ProjectError('Run sumber atau diagnosis tidak ditemukan; pertahankan isi folder runs')
    entry = root/'history_followup.py'
    if not entry.is_file():
        raise ProjectError('history_followup.py tidak ditemukan')
    print('Melanjutkan wallet pertama: maksimal 2 HTTP attempt, tanpa retry otomatis.')
    print('Gunakan koneksi WARP yang sebelumnya berhasil. Kredensial dimasukkan di prompt lokal.')
    return subprocess.run([sys.executable, str(entry), 'resume', '--source-run', str(source),
        '--diagnosis', str(diagnosis), '--prompt', '--out', str(out)], cwd=str(root), check=False).returncode


def export_review(root=ROOT):
    root = Path(root).resolve(); cfg = settings(root)
    pointer = root/'watchlist/latest_batch.json'
    folder = latest_run(root) if pointer.is_file() else location(root, cfg, 'followup_run')
    if not folder.is_dir():
        raise ProjectError('Belum ada hasil kelanjutan. Jalankan menu 3 terlebih dahulu.')
    # Only files produced by the followup tool belong in its review archive.
    allowed = {'intake_report.json', 'observations.json', 'trade_observations.csv',
               'unverified_token_activity.csv', 'report.html', 'diagnosis_source.json', 'continuation_plan.json',
               'batch_plan.json', 'registry_before.json', 'registry_after.json'}
    allowed.update({'session_plan.json','session_progress.json'})
    files = sorted(p for p in folder.rglob('*') if p.is_file() and not p.is_symlink()
                   and (p.name in allowed
                        or 'raw' in p.relative_to(folder).parts and p.match('history_*.json')))
    if not files:
        raise ProjectError('Folder kelanjutan belum memiliki file hasil yang dapat diekspor')
    dest = location(root, cfg, 'exports'); dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = dest/(folder.name+'_review_'+stamp+'_'+uuid.uuid4().hex[:6]+'.zip')
    with zipfile.ZipFile(path, 'x', zipfile.ZIP_DEFLATED) as archive:
        for item in files:
            archive.write(item, folder.name+'/'+item.relative_to(folder).as_posix())
    print('ZIP siap dikirim: '+str(path))
    return path


def registry_action(command, root=ROOT, max_requests=50):
    root = Path(root).resolve()
    return subprocess.run([sys.executable, str(root/'wallet_registry.py'), command]+
        (['--prompt'] if command in ('collect', 'discover','sync') else [])+
        (['--max-requests',str(max_requests)] if command=='sync' else []), cwd=str(root), check=False).returncode


def add_wallets(root=ROOT):
    import wallet_registry
    values = input('Alamat wallet Solana (pisahkan dengan koma): ').split(',')
    label = input('Sumber alamat, misalnya OKX_manual / GMGN_manual: ').strip() or 'MANUAL_UNVERIFIED'
    try:
        count = wallet_registry.add_manual(root, values, label)
    except wallet_registry.m.IntakeError as exc:
        raise ProjectError(str(exc)) from None
    print(str(count)+' wallet baru ditambahkan. Buka menu 5 untuk melihat daftar.')


def database_action(command, root=ROOT, backup=None):
    import research_store as store
    if command == 'db-import': result = store.import_workspace(root)
    elif command == 'db-status': result = store.database_status(root)
    elif command == 'db-backup': result = store.backup_database(root)
    elif command == 'db-restore': result = store.restore_database(root, backup)
    elif command == 'db-dashboard':
        result = store.dashboard(root)
        webbrowser.open(Path(result['path']).as_uri())
    else: raise ProjectError('Unknown database command')
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def menu(root=ROOT):
    while True:
        print('\nSMART WALLET RESEARCH · MASTER')
        print('1. Buka laporan terbaru di browser')
        print('2. Lihat status dan cakupan data')
        print('3. Pilot lama: kelanjutan wallet pertama (sudah selesai)')
        print('4. Ekspor hasil untuk audit (opsional)')
        print('5. Buka daftar seluruh kandidat wallet')
        print('6. Ambil history maksimal 10 wallet baru')
        print('7. Tambah alamat wallet pilihan sendiri')
        print('8. Cari kandidat baru dari OKX (maksimal 2 request)')
        print('9. Sinkronkan antrean otomatis + ekspor (maksimal 50 request)')
        print('10. Siapkan workspace lokal (offline)')
        print('11. Jalankan demo sintetis (offline, tanpa API)')
        print('12. Impor/perbarui arsip SQLite (offline)')
        print('13. Buka dashboard status SQLite (offline)')
        print('14. Backup SQLite dan verifikasi (offline)')
        print('0. Keluar')
        try:
            choice = input('Pilih menu: ').strip()
            if choice == '0': return 0
            if choice == '1': open_report(root)
            elif choice == '2': status(root)
            elif choice == '3': continue_history(root)
            elif choice == '4': export_review(root)
            elif choice == '5': registry_action('report', root)
            elif choice == '6': registry_action('collect', root)
            elif choice == '7': add_wallets(root)
            elif choice == '8': registry_action('discover', root)
            elif choice == '9': registry_action('sync', root)
            elif choice == '10': initialize(root)
            elif choice == '11': demo(root)
            elif choice == '12': database_action('db-import', root)
            elif choice == '13': database_action('db-dashboard', root)
            elif choice == '14': database_action('db-backup', root)
            else: print('Pilih angka 0 sampai 14.')
        except (ProjectError, IntakeError, StorageError, sqlite3.Error, OSError, ValueError, KeyError) as exc:
            print('BELUM BERHASIL: '+(str(exc) if isinstance(exc, (ProjectError, IntakeError, StorageError)) else type(exc).__name__))
        except (EOFError, KeyboardInterrupt):
            print('\nMenu ditutup.'); return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=['status', 'report', 'continue-history', 'export-review',
                                                    'wallets', 'collect-wallets', 'discover-wallets', 'add-wallet', 'sync',
                                                    'init', 'demo', 'import-legacy', 'db-import', 'db-status',
                                                    'db-dashboard', 'db-backup', 'db-restore'])
    parser.add_argument('--max-requests',type=int,default=50)
    parser.add_argument('--source', help='Folder master lama untuk import-legacy')
    parser.add_argument('--include-env', action='store_true', help='Salin .env lokal tanpa mengganti file yang berbeda')
    parser.add_argument('--dry-run', action='store_true', help='Periksa rencana import tanpa menulis berkas')
    parser.add_argument('--backup', help='Path backup relatif terhadap workspace, untuk db-restore')
    args = parser.parse_args()
    if args.command == 'import-legacy' and not args.source:
        parser.error('import-legacy requires --source')
    if args.command != 'import-legacy' and (args.source or args.include_env or args.dry_run):
        parser.error('--source, --include-env and --dry-run are only valid for import-legacy')
    if (args.command == 'db-restore') != bool(args.backup):
        parser.error('db-restore requires --backup; --backup is only valid for db-restore')
    try:
        if args.command is None: return menu()
        if args.command == 'status': status()
        elif args.command == 'report': open_report()
        elif args.command == 'continue-history': return continue_history()
        elif args.command == 'export-review': export_review()
        elif args.command == 'wallets': return registry_action('report')
        elif args.command == 'collect-wallets': return registry_action('collect')
        elif args.command == 'discover-wallets': return registry_action('discover')
        elif args.command == 'add-wallet': add_wallets()
        elif args.command == 'sync': return registry_action('sync',max_requests=args.max_requests)
        elif args.command == 'init': initialize()
        elif args.command == 'demo': demo()
        elif args.command.startswith('db-'): database_action(args.command, backup=args.backup)
        elif args.command == 'import-legacy':
            from workspace_transfer import import_legacy, WorkspaceImportError
            try:
                result = import_legacy(args.source, ROOT, include_env=args.include_env, dry_run=args.dry_run)
            except WorkspaceImportError as exc:
                raise ProjectError(str(exc)) from None
            print(json.dumps(result, indent=2))
        return 0
    except (ProjectError, IntakeError, StorageError, sqlite3.Error, OSError, ValueError, KeyError) as exc:
        print('BELUM BERHASIL: '+(str(exc) if isinstance(exc, (ProjectError, IntakeError, StorageError)) else type(exc).__name__))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
