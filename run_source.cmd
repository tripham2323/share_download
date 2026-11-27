@echo off
setlocal
set "ROOT=%~dp0"
set "PYTHON=python"
if exist "%ROOT%.venv\Scripts\python.exe" set "PYTHON=%ROOT%.venv\Scripts\python.exe"
"%PYTHON%" -c "import PySide6, cryptography; from pathlib import Path; import subprocess, sys; exe = Path(sys.executable).with_name('pythonw.exe'); subprocess.Popen([str(exe), str(Path(sys.argv[1]) / 'launcher.py'), *sys.argv[2:]], cwd=sys.argv[1])" "%ROOT%." %*
if errorlevel 1 goto setup
exit /b 0
:setup
echo Chua tim thay Python hoac thu vien. Chay mot lan:
echo   python -m pip install -r "%ROOT%requirements-desktop.txt"
echo Hoac dung goi ShareDownload portable de khong can cai Python.
pause
exit /b 1
