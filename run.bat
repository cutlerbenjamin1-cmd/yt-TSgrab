@echo off
cd /d "%~dp0"

REM ==========================================================================
REM  yt-grab launcher  (multi-source)
REM ==========================================================================
REM  EGRESS is the real defense against YouTube caption IP-bans, and it's now
REM  configured entirely in grab_transcripts.py -> EGRESS_SOURCES. Each source
REM  binds YouTube sockets to its own IP (tether / resi / VPN) with its own rate
REM  budget; they never share a rate limit. A "auto:Wi-Fi" bind auto-detects the
REM  phone-hotspot adapter's live IP each launch (no hardcoded lease).
REM
REM  To add/park a source, edit EGRESS_SOURCES at the top of grab_transcripts.py.
REM  All timing knobs live there too.
REM ==========================================================================

REM Optional cookies - only if you hit "Sign in to confirm you're not a bot" on a
REM SAFE egress. Cookieless is proven safer on a clean IP; leave blank.
set "YOUTUBE_COOKIES_BROWSER="
set "YOUTUBE_COOKIES_PROFILE="

python grab_transcripts.py %*

echo.
echo yt-grab finished. Log: _harvest.log
pause
