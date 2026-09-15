#!/usr/bin/env bash
set -euo pipefail
set +x
cd /home/vagrant/moi-benchmark
if [[ -z ${TOOLATHLON_DEEPSEEK_ASTRA_API_KEY:-} ]]; then
    read -r -s -p 'DeepSeek API key (hidden): ' TOOLATHLON_DEEPSEEK_ASTRA_API_KEY
    printf '\n'
fi
: "${TOOLATHLON_DEEPSEEK_ASTRA_API_KEY:?A nonempty DeepSeek key is required}"
export TOOLATHLON_DEEPSEEK_ASTRA_API_KEY
export NO_PROXY=127.0.0.1,localhost
export no_proxy="$NO_PROXY"
export PYTHONPATH="$PWD/astra/runners${PYTHONPATH:+:$PYTHONPATH}"
run_id="astra-969-find-alita-$(date -u +%Y%m%dT%H%M%SZ)"
out="$PWD/work/toolathlon-astra-969550b/$run_id"
printf 'Single task: find-alita-paper\nOutput: %s\n' "$out"
exec /home/vagrant/dataset/Toolathlon/.venv/bin/python -m toolathlon_astra_969550b \
    --system astra --task-id find-alita-paper --experiment-id toolathlon-astra-969550b \
    --run-id "$run_id" --output-dir "$out" --docker-via-sudo
