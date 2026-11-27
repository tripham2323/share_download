"""Dispatch source and portable launchers to the same desktop entry points."""
from pathlib import Path
import sys


def main() -> int:
    if getattr(sys, 'frozen', False):
        name = Path(sys.executable).stem.casefold()
        mode = name.removeprefix('sharedownload-')
    else:
        mode = sys.argv.pop(1).casefold() if len(sys.argv) > 1 else 'client'
    if mode == 'client':
        from desktop_app import main as run_client
        return run_client()
    if mode in {'server', 'demo'}:
        from server_app import main as run_server
        return run_server(['--demo', *sys.argv[1:]] if mode == 'demo' else sys.argv[1:])
    raise SystemExit('Run mode must be client, server or demo.')


if __name__ == '__main__':
    raise SystemExit(main())
