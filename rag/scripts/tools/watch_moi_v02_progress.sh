#!/usr/bin/env bash
set -u

run_root="${1:?usage: watch_moi_v02_progress.sh RUN_ROOT [TOTAL] [INTERVAL] [OFFSET]}"
total="${2:-1000}"
interval="${3:-5}"
offset="${4:-0}"

if [[ -d "$run_root/qa-full" ]]; then
  qa_parent="$run_root/qa-full"
else
  # Also accept a continuation root whose timestamped QA directory is direct.
  qa_parent="$run_root"
fi

while true; do
  clear
  printf 'MOI v0.2 DeepSeek benchmark\n'
  printf 'Run: %s\n' "$run_root"
  printf 'Updated: %s\n\n' "$(date '+%Y-%m-%d %H:%M:%S')"

  qa_dir="$(find "$qa_parent" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | tail -1)"
  rows=0
  if [[ -n "$qa_dir" && -f "$qa_dir/results.jsonl" ]]; then
    rows="$(wc -l < "$qa_dir/results.jsonl" | tr -d ' ')"
  fi
  # The benchmark may be launched with either an absolute or a relative
  # executable path, so match the stable command basename instead of the
  # path spelling.
  pid="$(pgrep -f 'local-matrixflow-rag run' | head -1 || true)"

  if [[ -n "$pid" ]]; then
    process_state="RUNNING (pid $pid)"
  elif [[ "$rows" -ge "$total" ]]; then
    process_state="COMPLETED"
  else
    process_state="STOPPED OR BLOCKED"
  fi

  printf 'Continuation progress: %s/%s (%.1f%%)\n' "$rows" "$total" "$(awk -v n="$rows" -v t="$total" 'BEGIN { if (t > 0) printf 100*n/t; else print 0 }')"
  overall_rows=$((offset + rows))
  overall_total=$((offset + total))
  printf 'Overall progress: %s/%s (%.1f%%)\n' "$overall_rows" "$overall_total" "$(awk -v n="$overall_rows" -v t="$overall_total" 'BEGIN { if (t > 0) printf 100*n/t; else print 0 }')"
  printf 'Process: %s\n' "$process_state"
  printf 'QA directory: %s\n\n' "${qa_dir:-not created}"

  if [[ -f "$qa_dir/results.jsonl" ]]; then
    python3 - "$qa_dir/results.jsonl" <<'PY'
import sys

ok = 0
failed = 0
with open(sys.argv[1], encoding="utf-8") as handle:
    for line in handle:
        # The top-level result status is emitted before the large chunks
        # payload; restricting the scan avoids parsing hundreds of MB every
        # refresh while still reading the durable ledger directly.
        prefix = line[:4096]
        if '"status":"ok"' in prefix:
            ok += 1
        elif '"status":"failed"' in prefix:
            failed += 1
print(f"Live successful: {ok}")
print(f"Live failed: {failed}")
PY
  fi

  if [[ -f "$qa_dir/summary.json" ]]; then
    printf 'Persisted metric snapshot (may lag live rows):\n'
    python3 - "$qa_dir/summary.json" <<'PY'
import json
import sys

data = json.loads(open(sys.argv[1], encoding="utf-8").read())
for label, key in (
    ("Successful", "successful_attempts"),
    ("Mean source recall", "mean_source_recall"),
    ("Mean evidence recall", "mean_evidence_recall"),
    ("Mean reciprocal rank", "mean_reciprocal_rank"),
    ("Generation latency mean ms", "generation_latency_mean_ms"),
):
    value = data.get(key)
    if isinstance(value, float):
        print(f"{label}: {value:.4f}")
    elif value is not None:
        print(f"{label}: {value}")
PY
  fi

  if [[ -f "$qa_dir/results.jsonl" ]]; then
    python3 - "$qa_dir/results.jsonl" <<'PY'
import json
import sys

rows = [line for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
if rows:
    item = json.loads(rows[-1])
    case = item.get("case") or {}
    print(f"Last: {case.get('id', '?')} status={item.get('status', '?')}")
PY
  fi

  if [[ -z "$pid" ]]; then
    printf '\nWatcher stopped because the benchmark process is no longer running.\n'
    read -r -p 'Press Enter to close this terminal... ' _
    exit 0
  fi
  sleep "$interval"
done
