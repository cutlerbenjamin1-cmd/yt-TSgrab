@echo off
REM Caption repair launcher. No args = full run (small lane then large lane).
REM   repair.bat                -> --mode both  (small lane then large lane, resumeable)
REM   repair.bat --mode small   -> short+medium only
REM   repair.bat --mode large   -> large+huge only
REM   repair.bat --dry-run      -> plan + progress (no GPU), shows already-done count
cd /d "%~dp0"
if "%~1"=="" (
    python repair_captions.py --mode both
) else (
    python repair_captions.py %*
)
