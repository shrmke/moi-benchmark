# Toolathlon DSH adapter

This directory adds an exploratory, single-task DeepSeek Harness (`dsh
headless`) product slot. It reuses the existing Toolathlon lifecycle for task
reset, Gateway startup, public bundles, model freeze and usage artifacts,
evaluator, resource sampling, and trajectory normalization.

The DSH process runs in a read-only Docker sidecar. Its MCP client uses stdio;
`classic_sse_stdio_bridge.mjs` translates that transport to the Toolathlon
Gateway's legacy Classic SSE endpoint. The bridge forwards raw tool names and
schemas without rewriting them.

The model boundary is a separate host-side Node process implemented by
`node_model_proxy.mjs`. It uses Node 22's built-in Undici `fetch` pool for the
DeepSeek connection and streams the response directly to DSH. Astra and Hermes
continue to use the shared Python model proxy.

The slot is intentionally recorded as `exploratory_dsh_only`. The existing
formal Astra/Hermes artifact validator and M2/M3 batch scheduler remain
unchanged; this runner must not be mixed into those formal batches until a DSH
branch is added to their frozen contracts.

The DSH preflight permits only the deployment-generated state files listed in
`lifecycle.py` to drift from the formal credential manifest. They must still
exist and not be symlinks; all other credential files retain SHA-256 checking.
The permitted paths are recorded in
`dsh_runtime.runtime_generated_state_paths` in the resolved run configuration.

## Runtime setup

Use the already built DSH checkout and the exact versions used for this slot:

```bash
export TOOLATHLON_DSH_NODE=/home/vagrant/.nvm/versions/node/v22.19.0/bin/node
export TOOLATHLON_DSH_ROOT=/home/vagrant/deepseek-harness
export TOOLATHLON_DEEPSEEK_DSH_API_KEY='your-dsh-slot-key'
```

The DSH key is passed once to the host-side Node Model Proxy over stdin. It is
not placed in the child command line or environment. The DSH container receives
the non-secret placeholder `DEEPSEEK_API_KEY=toolathlon-run-proxy`.

If Toolathlon application credentials were refreshed after the formal freeze,
snapshot their current fingerprints before starting the batch. The manifest
contains paths and hashes, not credential values:

```bash
cd /home/vagrant/moi-benchmark
export TOOLATHLON_SOURCE_ROOT=/home/vagrant/dataset/Toolathlon
export TOOLATHLON_DSH_OUTPUT="$PWD/astra/results/toolathlon-dsh-108"

mkdir -p "$TOOLATHLON_DSH_OUTPUT"
python3 astra/runners/toolathlon_pi/scripts/snapshot_application_credentials.py \
  --base astra/benchmark/toolathlon-verified/freeze/credential-manifest.json \
  --source "$TOOLATHLON_SOURCE_ROOT" \
  --output "$TOOLATHLON_DSH_OUTPUT/credential-manifest.runtime.json"
export TOOLATHLON_DSH_CREDENTIAL_MANIFEST="$TOOLATHLON_DSH_OUTPUT/credential-manifest.runtime.json"
```

Every file named by the base manifest must be readable while taking the
snapshot. If credentials have not drifted, the lifecycle can use the frozen
manifest without setting `TOOLATHLON_DSH_CREDENTIAL_MANIFEST`.

## Run one task

```bash
cd /home/vagrant/moi-benchmark
python3 -m astra.runners.toolathlon_dsh.lifecycle \
  --task-id find-alita-paper \
  --run-id dsh-find-alita-paper-a1 \
  --output-dir /tmp/toolathlon-dsh-find-alita-paper \
  --toolathlon-source /home/vagrant/dataset/Toolathlon
```

The output contains the same shared trajectory/evaluator streams as the other
product slots, plus `dsh-session.jsonl`, the raw uncompressed DSH session log.

For the diagnosis and fix of proxied SSE disconnects, see
[`MODEL_PROXY_STREAMING.md`](MODEL_PROXY_STREAMING.md).

## Run the 108-task schedule

The batch script runs tasks serially in the frozen order. It defaults to all
108 tasks; use `--max-tasks` to select the first N tasks. Reusing the same
output root skips valid completed runs, so the limit can be increased later.

```bash
cd /home/vagrant/moi-benchmark
astra/runners/toolathlon_dsh/scripts/run_dsh_108.sh \
  --max-tasks 5 \
  /tmp/toolathlon-dsh-108
```

Omit `--max-tasks 5` to run all 108 tasks. An evaluator `no_pass` remains a
completed benchmark observation. If a1 does not produce a valid complete
artifact set, the script preserves it, tries a2, records any second failure,
and continues with the remaining schedule. Reusing the output root resumes by
skipping complete tasks.

With the runtime manifest setup above, the full command is:

```bash
astra/runners/toolathlon_dsh/scripts/run_dsh_108.sh \
  --toolathlon-source "$TOOLATHLON_SOURCE_ROOT" \
  "$TOOLATHLON_DSH_OUTPUT"
```

## Generate reports

The report generators scan all DSH result batches, retain every attempt in the
all-attempt CSV, and select the latest `started_at` per task for the DSH and
four-product Markdown reports:

```bash
cd /home/vagrant/moi-benchmark
python3 astra/reports/Toolathlon-analysis/generate_dsh_all_attempt_results.py
python3 astra/reports/Toolathlon-analysis/generate_dsh_latest_result_report.py
python3 astra/reports/Toolathlon-analysis/generate_astra_hermes_pi_dsh_comparison.py
```

The current effective DSH projection contains 108 evaluated tasks: 79 `pass`
and 29 `no_pass` (73.15%). See
[`dsh-toolathlon-108-task-analysis.md`](../../reports/Toolathlon-analysis/dsh-toolathlon-108-task-analysis.md)
for the DSH-only analysis and
[`astra-hermes-pi-dsh-toolathlon-108-task-comparison.md`](../../reports/Toolathlon-analysis/astra-hermes-pi-dsh-toolathlon-108-task-comparison.md)
for the four-product comparison.
