@echo off
cd /d "%~dp0"

REM ==========================================================================
REM  dump_channel - list all videos on a channel into channel_<handle>.txt
REM  (paste-ready for links.txt). Routes through the phone-hotspot egress like
REM  the grabber. Channel listing hits the browse endpoint, not captions.
REM
REM  Usage: drag a channel URL onto this file, or run:
REM         dump_channel.bat https://www.youtube.com/@somechannel/videos
REM  No URL -> the DEFAULT_URL at the top of dump_channel.py (@johnnyharris).
REM ==========================================================================

set "YOUTUBE_BIND_IP="
for /f "tokens=2 delims=:" %%a in ('netsh interface ip show address "Wi-Fi" ^| findstr /c:"IP Address"') do set "YOUTUBE_BIND_IP=%%a"
set "YOUTUBE_BIND_IP=%YOUTUBE_BIND_IP: =%"

if defined YOUTUBE_BIND_IP (
    echo [dump] egress bound to Wi-Fi/hotspot IP: %YOUTUBE_BIND_IP%
) else (
    echo [dump] no hotspot IP detected - using default route.
)

python dump_channel.py %*

echo.
pause
