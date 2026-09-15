#!/usr/bin/env bash
# Compatibility entry point; keep existing deployment commands working.
set -euo pipefail
runner_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec bash "$runner_dir/toolathlon_astra/restore_services.sh" "$@"
