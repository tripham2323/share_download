# PyInstaller shares one Python/Qt runtime across all three executables.
from pathlib import Path

root = Path(SPECPATH)
analysis = Analysis([str(root / 'launcher.py')], pathex=[str(root)])
archive = PYZ(analysis.pure)
executables = [EXE(archive, analysis.scripts, [], name=f'ShareDownload-{mode}',
                   exclude_binaries=True, console=False, upx=False)
               for mode in ('Client', 'Server', 'Demo')]
collect = COLLECT(*executables, analysis.binaries, analysis.datas, upx=False,
                  name='ShareDownload')
