# Benchmark runners

Runners are grouped by dataset and framework. Keep framework-specific
implementation in its own directory; root-level compatibility entry points
contain no independent execution logic.

## Start here

| Use case | Entry point |
| --- | --- |
| Linux Terminal-Bench, all supported frameworks | [linux_terminal_bench/README.md](linux_terminal_bench/README.md) |
| Toolathlon reproduction and deployment | [TOOLATHLON_REPRODUCTION.md](TOOLATHLON_REPRODUCTION.md) |
| Astra 969550b on Toolathlon | [toolathlon_astra/README.md](toolathlon_astra/README.md) |

## Directory responsibilities

| Directory or file | Responsibility |
| --- | --- |
| `linux_terminal_bench/` | Shared Linux queue, scheduling, result collection and configuration |
| `astra_terminal_bench/` | Astra Terminal-Bench adapter and service lifetime support |
| `hermes_terminal_bench/` | Hermes Terminal-Bench adapter and runtime support |
| `pi_terminal_bench/` | Pi Terminal-Bench adapter and verifier integration |
| `dsh_terminal_bench/` | DeepSeek Harness Terminal-Bench adapter and runtime support |
| `toolathlon_astra/` | Current Astra single-task runner, batch runner, deployment helpers and prepare policy |
| `toolathlon_pi/` | Pi Toolathlon adapter and entry points |
| `toolathlon_dsh/` | DeepSeek Harness Toolathlon adapter and entry points |
| `toolathlon_verified/` | Shared Toolathlon lifecycle, model proxy, contracts and adapters; historical paired launchers |
| `scripts/` | Existing cross-product and deployment launchers |
| `astra_smoke/`, `lifecycle_c0/` | Existing smoke and lifecycle support |
| `llm_observability.py` | Shared LLM observability support |
| `toolathlon.env.example` | Toolathlon environment template; not a credentials file |

## Compatibility entry points

The following root-level paths delegate to `toolathlon_astra/` so existing
commands and deployment settings keep resolving:

| Existing path | Implementation |
| --- | --- |
| `run_astra_969550b_108.sh` | `toolathlon_astra/run_108.sh` |
| `toolathlon_astra_969550b.py` | `toolathlon_astra/runner.py` |
| `toolathlon_astra_969550b_batch.py` | `toolathlon_astra/batch.py` |
| `restore_toolathlon_services.sh` | `toolathlon_astra/restore_services.sh` |
| `authorize_notion_with_proxy.sh` | `toolathlon_astra/authorize_notion.sh` |
| `notion_oauth_proxy_preload.cjs` | `toolathlon_astra/notion_oauth_proxy_preload.cjs` |

Use the grouped paths in new documentation. Do not place generated attempt
artifacts, keys, `.env` files, temporary backups or campaign-specific task lists
in this directory. Runtime output and private configuration remain under their
existing work directories. This directory reorganization does not redeploy
services or migrate previously collected evidence.
