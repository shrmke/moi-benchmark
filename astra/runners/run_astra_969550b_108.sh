#!/usr/bin/env bash
set -euo pipefail
set +x
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
export PYTHONPATH="$root/astra/runners${PYTHONPATH:+:$PYTHONPATH}"
exec /home/vagrant/dataset/Toolathlon/.venv/bin/python -u -m toolathlon_astra_969550b_batch "$@"
