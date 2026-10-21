"""Copy selected local runtime data into a checkout without replacing files."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid


RUNTIME_DIRS = ('runs', 'watchlist', 'exports', 'data')
PROJECT_PATHS = ('source_run', 'diagnosis', 'baseline_report', 'followup_run', 'exports')


class WorkspaceImportError(RuntimeError):
    pass


def _sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _check_path(root, relative):
    path = root / relative
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root):
        raise WorkspaceImportError('SYMLINK_NOT_ALLOWED: ' + relative.as_posix())
    resolved = path.resolve()
    if root not in resolved.parents:
        raise WorkspaceImportError('PATH_OUTSIDE_WORKSPACE')
    return path


def _check_idle(root):
    for relative in ('watchlist/registry.lock', 'watchlist/sync.lock', 'workspace-import.lock'):
        if (root / relative).exists():
            raise WorkspaceImportError('WORKSPACE_BUSY: stop the worker and review existing locks')


@contextmanager
def _import_lock(root):
    path = root / 'workspace-import.lock'
    try:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(str(os.getpid()))
    except FileExistsError:
        raise WorkspaceImportError('WORKSPACE_BUSY: another import holds the lock') from None
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _plan(source, destination, include_env):
    for root in (source, destination):
        _check_idle(root)
    if not (source / 'project.json').is_file():
        raise WorkspaceImportError('LEGACY_PROJECT_REQUIRED: select the folder containing project.json')
    configs = [json.loads((root / 'project.json').read_text(encoding='utf-8-sig'))
               for root in (source, destination)]
    if any(configs[0].get(key) != configs[1].get(key) for key in PROJECT_PATHS):
        raise WorkspaceImportError('CUSTOM_PROJECT_PATHS: review local project paths before importing')
    paths = []
    for name in RUNTIME_DIRS:
        folder = _check_path(source, Path(name))
        if not folder.exists():
            continue
        if not folder.is_dir():
            raise WorkspaceImportError('RUNTIME_PATH_NOT_DIRECTORY: ' + name)
        for path in sorted(folder.rglob('*')):
            relative = path.relative_to(source)
            _check_path(source, relative)
            if any(part in ('.git', '__pycache__') for part in relative.parts):
                continue
            if path.name.endswith(('.lock', '.tmp')) or not path.is_file():
                continue
            paths.append(relative)
    if include_env and (source / '.env').exists():
        _check_path(source, Path('.env'))
        if not (source / '.env').is_file():
            raise WorkspaceImportError('ENV_NOT_A_FILE')
        paths.append(Path('.env'))
    plan = []
    for relative in paths:
        original = _check_path(source, relative)
        target = _check_path(destination, relative)
        fingerprint = _sha(original)
        exists = target.exists()
        if exists and (not target.is_file() or _sha(target) != fingerprint):
            raise WorkspaceImportError('DESTINATION_CONFLICT: ' + relative.as_posix())
        plan.append((relative, fingerprint, exists))
    return plan


def _copy_exclusive(source, destination, fingerprint):
    """Publish a verified temporary file atomically, never replacing a target."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.import-', suffix='.tmp', dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        shutil.copy2(source, temporary)
        if _sha(temporary) != fingerprint or _sha(source) != fingerprint:
            raise WorkspaceImportError('SOURCE_CHANGED: stop the legacy worker before importing')
        with temporary.open('rb+') as stream:
            os.fsync(stream.fileno())
        # Hard-link creation is exclusive and atomic on supported local filesystems.
        # Failure leaves the existing destination untouched; no overwrite fallback.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def import_legacy(source, destination, include_env=False, dry_run=False):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise WorkspaceImportError('OVERLAPPING_WORKSPACES: use separate folders')
    if not source.is_dir() or not destination.is_dir():
        raise WorkspaceImportError('WORKSPACE_DIRECTORY_REQUIRED')
    plan = _plan(source, destination, include_env)
    result = dict(status='DRY_RUN' if dry_run else 'IMPORTED', files_to_copy=sum(not p[2] for p in plan),
                  identical_files_skipped=sum(p[2] for p in plan),
                  env_included=any(p[0] == Path('.env') for p in plan), source_modified=False)
    if dry_run:
        return result
    with _import_lock(destination):
        # Recheck conflicts after taking the import lock, before any destination write.
        _check_idle(source)
        if any((destination / 'watchlist' / name).exists() for name in ('registry.lock', 'sync.lock')):
            raise WorkspaceImportError('WORKSPACE_BUSY: stop the destination worker')
        for relative, fingerprint, existed in plan:
            original = _check_path(source, relative)
            target = _check_path(destination, relative)
            if _sha(original) != fingerprint:
                raise WorkspaceImportError('SOURCE_CHANGED: repeat the import with workers stopped')
            if target.exists() and (not target.is_file() or _sha(target) != fingerprint):
                raise WorkspaceImportError('DESTINATION_CONFLICT: ' + relative.as_posix())
        for relative, fingerprint, existed in plan:
            target = _check_path(destination, relative)
            if existed:
                continue
            try:
                _copy_exclusive(_check_path(source, relative), target, fingerprint)
            except FileExistsError:
                raise WorkspaceImportError('DESTINATION_CHANGED: import stopped without replacing files') from None
        receipt = destination / 'data' / 'migration_receipts' / (uuid.uuid4().hex + '.json')
        _check_path(destination, receipt.relative_to(destination))
        receipt.parent.mkdir(parents=True, exist_ok=True)
        with receipt.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, indent=2)
            stream.write('\n')
    return result
