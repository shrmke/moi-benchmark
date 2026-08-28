# DeepSeek Harness Terminal-Bench adapter

This directory connects DeepSeek Harness (DSH) to the repository's Harbor
0.20 / Terminal-Bench evaluation path. It adds an S0 adapter and a C0 adapter;
it does not change the existing Astra, Hermes, or Pi runners.

## Evaluation profiles

All profiles use `deepseek-harness-runtime-bin==0.1.0rc6`, persistent bash,
`str-replace-editor`, DSH's local `danger-full-access` policy bounded by the
task container, and one fresh non-resumable DSH session per trial.

| Profile | Route | Request settings | Purpose |
| --- | --- | --- | --- |
| `deepseek-native` | `deepseek-official/deepseek-v4-flash` | provider-default thinking; 1,000,000 context; 49,152 output cap | Preserve the original native-model exploratory run |
| `terminalbench-glm52` | `zai/glm-5.2` through `dsh-llm-pi-ai` | `temperature=0`; 50 model-request steps; provider output default | Same-model Terminal-Bench candidate matching the follow-up configuration in the 83-task report |
| `terminalbench-deepseek-v4-flash-max` | `deepseek-official/deepseek-v4-flash` | `thinking=enabled`; `reasoningEffort=max`; `temperature=0`; 49,152 output cap; 50 model-request steps | Native DeepSeek V4 Flash Max Terminal-Bench run |

The Terminal-Bench report's `max_turns` means an agent/model iteration. DSH
calls one model request plus its tools a `step`, while one DSH `turn` can hold
many such steps. The profile therefore blocks entry to step 51 and records the
terminal reason as `max_turns`; counting DSH `turn/start` would not enforce the
same budget.

Model route, credential environment name, Cordis file, context/output limits,
temperature, and max turns are owned by the selected profile in `agent.py`.
Harbor's `model_name` must match that profile, so changing only the display
model cannot silently run another route.

The local source checkout at `/Users/chenyuwei/Documents/deepseek-harness` is
`0.1.0-rc.5` at commit
`47f943859bef60e4160492346772ded9b24f765a`. The evaluated binary is the newer
official `0.1.0rc6` distribution, so the source checkout is a reference, not an
exact source build of the measured runtime. Trial metadata records this
mismatch and sets `formal_score_eligible=false`.

The installer selects the official x86-64 or AArch64 manylinux wheel and
verifies its published SHA-256 before extracting only the JSON-RPC runtime.
This boundary check is intentional: a corrupt or substituted external binary
would silently change the evaluated product, while the package version string
alone would not detect it.

## Run

Prerequisites are Docker, Harbor 0.20, the local Terminal-Bench task checkout,
outbound access to `files.pythonhosted.org` during container setup, and the
credential required by the selected profile.

```bash
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export DEEPSEEK_API_KEY='...'
harbor run --config astra/runners/dsh_terminal_bench/s0-four-cases.yaml --yes
harbor run --config astra/runners/dsh_terminal_bench/c0-four-cases.yaml --yes

export ZAI_API_KEY='...'
harbor run \
  --config astra/runners/dsh_terminal_bench/c0-four-cases-glm52.yaml --yes
harbor run \
  --config astra/runners/dsh_terminal_bench/c0-terminalbench-83-glm52.yaml --yes

# Full source snapshot queue; excludes tune-mjcf and resumes pending tasks.
./astra/runners/scripts/dsh-terminal-bench-all-c0.sh --check
./astra/runners/scripts/dsh-terminal-bench-all-c0.sh

export DEEPSEEK_API_KEY='...'
./astra/runners/scripts/dsh-terminal-bench-all-c0-deepseek-v4-flash-max.sh --check
./astra/runners/scripts/dsh-terminal-bench-all-c0-deepseek-v4-flash-max.sh
```

For an install-only compatibility check that makes no model request:

```bash
DEEPSEEK_API_KEY=install-only-no-model-call harbor run \
  --config astra/runners/dsh_terminal_bench/install-smoke.yaml \
  --install-only --yes

ZAI_API_KEY=install-only-no-model-call harbor run \
  --config astra/runners/dsh_terminal_bench/install-smoke-glm52.yaml \
  --install-only --yes
```

Use a full-cohort config only after its four-case smoke succeeds. The GLM-5.2
83-task file contains exactly the strict paired task IDs from
`TerminalBench-comparison-astra_hermes_pi.md`. The product execution budget is
still computed per task as twice its dataset `[agent].timeout_sec`, capped only
by the runner's 24,000-second safety ceiling.

The full wrapper validates the 89-task source snapshot and, like Pi, excludes
`tune-mjcf`, leaving an 88-task evaluation cohort. It reuses Pi's
resource-aware queue and scheduler without its Pi image-building path. It
schedules three 2GB memory tokens: an 8GB task runs alone, a 4GB task can
overlap one 2GB task, and three 2GB tasks can overlap.
Before each invocation the selected wrapper scans its model-specific jobs
directory and skips trials from that exact model/profile cohort that have a
binary verifier reward plus non-empty CTRF evidence. GLM-5.2 and DeepSeek V4
Flash Max therefore never satisfy each other's pending queue. Both verifier
passes and verifier failures are terminal results; verifier infrastructure
failures remain pending. `--max-tasks N` limits one invocation, while
`--retry-queue FILE` intentionally reruns the listed canonical queue rows.
After `Ctrl+C`, rerun the same wrapper to recompute and continue the pending
queue.

## Evidence and metric semantics

Each successful trial keeps these files under `agent/`:

- `dsh-run.json`: normalized terminal status, counts, finish reason, and native
  usage buckets;
- `dsh-events.jsonl`: raw inbound JSON-RPC responses and notifications;
- `dsh-runtime.stderr.txt`: runtime diagnostics;
- `dsh-sessions/`: DSH's native JSONL session persistence;
- `dsh-install.json`, `dsh-<profile>.cordis.yml`, and any adjacent profile
  plugin: evaluated runtime and composition evidence;
- C0 additionally keeps `controller.jsonl` and `product.cleanup.json`.

DSH reports fresh input, cache reads, cache writes, output, and reasoning
tokens separately. Harbor's `n_input_tokens` is populated as fresh input plus
cache reads, `n_cache_tokens` as cache reads, and `n_output_tokens` as output.
All five native buckets remain in metadata. If DSH provides no usage object,
the values remain null rather than being imputed as zero.

The resulting verifier outcome is an end-to-end DSH plus selected-model result,
not a pure Harness score. The native profile must remain outside the GLM-5.2
table. The GLM-5.2 profile is still exploratory until a scored run verifies the
runtime/configuration match; report verifier pass, normal E2E pass, timeout,
infrastructure/API failures, latency, and token coverage with their denominators
and failure attribution.
