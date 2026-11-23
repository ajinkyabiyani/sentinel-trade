#!/usr/bin/env bash
# Overnight: wait for the 2025 data, then run Claude's free-hand trading (v2 rules) on four random 2025 months.
# A usage-limit stop waits 30 minutes and resumes; each month resumes from its saved look.
cd "$(dirname "$0")/.." || exit 1
export MARKET_DATA_ROOT='C:\Algo\market_data\y2025' BACKTEST_FROM=2025-01-01 BACKTEST_TO=2025-12-31 FREEHAND_RULES=v2
MONTHS=${MONTHS:-2025-01,2025-02,2025-05,2025-07}; ALL=$MONTHS
OPT_LOG="$1"; CANDLE_LOG='/c/Algo/market_data/y2025_candles.log'
log() { echo "$(date '+%m-%d %H:%M') $*"; }

# hold off idle sleep for this script's lifetime only (a per-process request; no power setting is changed)
python -c "import ctypes, time; ctypes.windll.kernel32.SetThreadExecutionState(0x80000001); time.sleep(20 * 3600)" &
AWAKE=$!
trap 'kill $AWAKE 2>/dev/null' EXIT
log "waiting for the 2025 data"
[ -n "$OPT_LOG" ] && until grep -qE "^done|stopped at" "$OPT_LOG" 2>/dev/null && [ "$(grep -c '^done' "$CANDLE_LOG")" -ge 2 ]; do sleep 120; done
log "data ready: $(tail -1 "$OPT_LOG")"

OUT=.orb_cache/claude_freehand_backtest_v2_2025
for round in $(seq 1 40); do
  python -m strategies.claude_freehand_backtest run --months "$MONTHS" --workers ${WORKERS:-2} > /dev/null 2>&1
  left=$(ALL="$ALL" python - <<'EOF'
import json, os, glob
done = {os.path.basename(p)[:7] for p in glob.glob(r".orb_cache/claude_freehand_backtest_v2_2025/months/*.json") if json.load(open(p, encoding="utf-8")).get("done")}
print(",".join(m for m in os.environ["ALL"].split(",") if m not in done))
EOF
)
  if [ -z "$left" ]; then log "all months finished"; break; fi
  MONTHS="$left"
  if grep -q "usage limit" "$OUT"/worker*.log 2>/dev/null; then
    log "usage limit; waiting 30 minutes (left: $left)"
    for f in "$OUT"/worker*.log; do mv "$f" "$f.$round"; done
    sleep 1800
  else
    log "a run ended without finishing (left: $left); retrying in 5 minutes"
    for f in "$OUT"/worker*.log; do [ -f "$f" ] && mv "$f" "$f.$round"; done
    sleep 300
  fi
done
python -m strategies.claude_freehand_backtest report
