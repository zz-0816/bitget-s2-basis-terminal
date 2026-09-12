#!/bin/bash
# 抓取 rToken 小时线 + 原生股日线
# 用法（必须走 WSL，Windows Git Bash 的 curl 对本机网络栈全失败）：
#     wsl -- bash /mnt/<盘符>/<仓库路径>/tools/b_side_fetch_kline.sh
# 产出：<仓库根>/data/b-side/raw/（74 份原始 JSON 即由本脚本产出）
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
RAW="$HERE/../data/b-side/raw"
mkdir -p "$RAW"

# rToken 现货对 : 原生股代码
PAIRS="RTSLAUSDT:TSLA RNVDAUSDT:NVDA RAAPLUSDT:AAPL RHOODUSDT:HOOD RMETAUSDT:META RQQQUSDT:QQQ"

for p in $PAIRS; do
  RT="${p%%:*}"; YH="${p##*:}"
  echo "== $RT / $YH =="

  # --- 原生股日线（6 个月）---
  if [ ! -s "$RAW/$YH-日线.json" ]; then
    curl -s --max-time 40 -H 'User-Agent: Mozilla/5.0' \
      "https://query1.finance.yahoo.com/v8/finance/chart/$YH?interval=1d&range=6mo" \
      -o "$RAW/$YH-日线.json"
  fi
  echo "   日线 $(stat -c%s "$RAW/$YH-日线.json" 2>/dev/null || echo 0) 字节"

  # --- rToken 小时线：用 endTime 向后翻页 ---
  rm -f "$RAW/$RT-1h-part"*.json
  END=$(( $(date +%s) * 1000 ))
  for i in $(seq 0 24); do
    curl -s --max-time 40 \
      "https://api.bitget.com/api/v2/spot/market/candles?symbol=$RT&granularity=1h&limit=200&endTime=$END" \
      -o "$RAW/$RT-1h-part$i.json"

    NEW=$(python3 - "$RAW/$RT-1h-part$i.json" <<'PY'
import json, sys
try:
    a = (json.load(open(sys.argv[1])).get('data') or [])
    print(a[0][0] if a else 0)
except Exception:
    print(0)
PY
)
    # 空响应重试一次（偶发限流）
    if [ "$NEW" = "0" ]; then
      sleep 1.5
      curl -s --max-time 40 \
        "https://api.bitget.com/api/v2/spot/market/candles?symbol=$RT&granularity=1h&limit=200&endTime=$END" \
        -o "$RAW/$RT-1h-part$i.json"
      NEW=$(python3 - "$RAW/$RT-1h-part$i.json" <<'PY'
import json, sys
try:
    a = (json.load(open(sys.argv[1])).get('data') or [])
    print(a[0][0] if a else 0)
except Exception:
    print(0)
PY
)
    fi

    [ "$NEW" = "0" ] && { echo "   翻页停止于第 $i 页（无更多历史）"; break; }
    [ "$NEW" -ge "$END" ] && { echo "   翻页停止于第 $i 页（未推进）"; break; }
    END=$NEW
    sleep 0.2
  done
  echo "   小时线 $(ls "$RAW/$RT"-1h-part*.json 2>/dev/null | wc -l) 个分页文件"
done

echo
echo "== 抓取完成，原始数据在 $RAW =="
