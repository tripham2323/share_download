"""Produce dist/ShareDownload on the Windows build machine."""
from pathlib import Path
import shutil
import subprocess
import sys


def main() -> None:
    if sys.platform != 'win32' or sys.maxsize <= 2**32:
        raise SystemExit('Build the portable bundle with 64-bit Python on Windows.')
    root = Path(__file__).resolve().parent
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
                    '--distpath', str(root / 'dist'), '--workpath', str(root / 'build'),
                    str(root / 'ShareDownload.spec')], cwd=root, check=True)
    bundle = root / 'dist' / 'ShareDownload'
    for name in ('client_config.example.json', 'Portable.txt'):
        shutil.copy2(root / name, bundle / name)
    print(f'Copy the entire folder: {bundle}')


if __name__ == '__main__':
    main()
