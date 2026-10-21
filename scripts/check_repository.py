"""Reject tracked runtime data and credential-file paths before publishing."""
from pathlib import Path, PurePosixPath
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=root, check=True, capture_output=True)
    files = [p for p in result.stdout.decode('utf-8').split('\0') if p]
    if not files:
        print('No tracked files; stage the reviewed source before checking.')
        return 2
    rejected = []
    for name in files:
        path = PurePosixPath(name)
        if (path.parts[0] in {'runs', 'watchlist', 'exports', 'data'}
                or '__pycache__' in path.parts or '.venv' in path.parts
                or (path.name.startswith('.env') and path.name != '.env.example')
                or path.name in {'config.local.json', 'project.local.json'}
                or path.suffix.lower() in {'.pem', '.key', '.zip', '.pyc', '.db', '.sqlite', '.sqlite3'}):
            rejected.append(name)
    for line in (root/'.env.example').read_text(encoding='utf-8').splitlines():
        value = line.strip()
        if value and not value.startswith('#') and ('=' not in value or value.split('=', 1)[1].strip()):
            rejected.append('.env.example contains a nonempty value')
            break
    if rejected:
        print('Repository check failed:')
        for name in rejected:
            print(' - ' + name)
        return 1
    print(f'Repository paths checked: {len(files)} tracked source/fixture/document files; no runtime paths.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
