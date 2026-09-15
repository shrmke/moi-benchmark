#!/usr/bin/env bash
set -euo pipefail
set +x

runner_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export TOOLATHLON_SOURCE="${TOOLATHLON_SOURCE:-/home/vagrant/dataset/Toolathlon}"
export TOOLATHLON_NOTION_PROXY="${TOOLATHLON_NOTION_PROXY:-http://127.0.0.1:7890}"
cd "$TOOLATHLON_SOURCE"
export TOOLATHLON_SOURCE="$PWD"
# Node child processes (including npx/mcp-remote) inherit the dispatcher preload.
export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--require=\"$runner_dir/notion_oauth_proxy_preload.cjs\""
export NODE_TLS_REJECT_UNAUTHORIZED=1
export HTTPS_PROXY="$TOOLATHLON_NOTION_PROXY"
export HTTP_PROXY="$TOOLATHLON_NOTION_PROXY"
export https_proxy="$TOOLATHLON_NOTION_PROXY"
export http_proxy="$TOOLATHLON_NOTION_PROXY"
export NO_PROXY="127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"
export PYTHONPATH="$TOOLATHLON_SOURCE${PYTHONPATH:+:$PYTHONPATH}"
exec "$TOOLATHLON_SOURCE/.venv/bin/python" -m global_preparation.special_setup_notion_official "$@"
