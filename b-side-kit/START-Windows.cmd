@echo off
rem B-side: start 5-level orderbook depth sampler (Windows)
rem Output: <this folder>\data\spread\orderbook-YYYY-MM-DD.csv  (~50 MB/day raw)
rem Stop  : close the minimized window named "orderbook_sampler"
cd /d %~dp0
if not exist data\spread mkdir data\spread
if not exist data\logs   mkdir data\logs

start "orderbook_sampler" /min cmd /c "python orderbook_sampler.py --loop --interval 30 >> data\logs\orderbook.log 2>&1"
echo started orderbook_sampler in a minimized window.
echo.
echo self-check: data\spread\orderbook-*.csv should keep growing.
echo one-shot  : python orderbook_sampler.py --once
pause
