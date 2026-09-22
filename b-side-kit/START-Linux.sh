#!/bin/sh
# B-side: start 5-level orderbook depth sampler (Linux / macOS)
# Output: <this folder>/data/spread/orderbook-YYYY-MM-DD.csv  (~50 MB/day raw, ~5 MB gzipped)
# Stop:   kill the pid printed below (or: pkill -f orderbook_sampler.py)
cd "$(dirname "$0")" || exit 1
mkdir -p data/spread data/logs

if [ ! -f data/spread/.orderbook_sampler.lock ] || ! pgrep -f orderbook_sampler.py >/dev/null 2>&1; then
  nohup python3 orderbook_sampler.py --loop --interval 30 \
      > data/logs/orderbook.log 2>&1 &
  echo "started orderbook_sampler pid=$!"
else
  echo "already running (skip)"
fi

sleep 3
echo
echo "--- files in data/spread/ ---"
ls -l data/spread/ | tail -5
echo
echo "self-check in 5 min: data/spread/orderbook-*.csv should keep growing."
echo "one-shot test  : python3 orderbook_sampler.py --once"
