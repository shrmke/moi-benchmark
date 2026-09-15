# Toolathlon reproduction

This directory contains the standalone Astra 969550b runner and historical
Toolathlon entry points for Astra, Hermes, and Pi 0.73.1. It also contains an
exploratory single-task DeepSeek Harness adapter; that adapter is documented
separately and is not part of the formal three-product batch. Historical qualification,
freeze-generation, and hotfix scripts remain under
`astra/benchmark/toolathlon-verified/scripts`; users should start runs only
through the entry points documented here.

## What is reproduced

- Astra 969550b runs independently through `run_astra_969550b_108.sh`; see the
  current standalone runner instructions below.
- Historical Astra and Hermes run as a serial, paired 108-task experiment. The launcher
  first creates the qualification pair, then runs the first 14 tasks (M2) and
  the remaining 94 tasks (M3).
- Pi runs the same 108-task schedule through its isolated container adapter.
- DSH can run one task through a separate exploratory headless slot; it is not
  included in the formal 108-task artifact contract or batch scheduler.
- Every task uses the full prepare, agent, evaluate, cleanup, and artifact
  finalization lifecycle. Interrupted batches resume when invoked with the same
  output directory.

The checked-in runtime manifests under
`astra/benchmark/toolathlon-verified/freeze` pin the task set, model settings,
budgets, permission policy, Toolathlon source commit, and task image. Re-running
the freeze generators is not required. Credential values are never committed;
application credential fingerprints are refreshed at the start of a new batch.

## Prerequisites

1. Linux with Docker, Python 3.10, `uv`, `jq`, and `sudo`.
2. A Toolathlon checkout at commit
   `2aed2468858f15818acafa178518390cc4b0f5cb`.
3. Toolathlon shared services and application credentials prepared by the
   upstream Toolathlon setup scripts.
4. The pinned task image available locally.
5. Astra server/runtime configuration prepared as described in
   `astra/runners/toolathlon_verified/README.md`.
6. Hermes installed according to the runtime manifest.
7. Pi 0.73.1 installed as a complete release directory, including its adjacent
   `package.json`.

## Astra 969550b: current standalone runner

Use this entry point for Astra commit
`969550b611ceba11653c4b2651079bcb85d1c24e`. Do not use the historical
Astra/Hermes paired launcher below to reproduce this version.

### Runtime and deployment prerequisites

These commands target the existing `matrixorigin-vm` deployment:

| Item | Configuration |
| --- | --- |
| Repository | `/home/vagrant/moi-benchmark` |
| Toolathlon source and Python | `/home/vagrant/dataset/Toolathlon`, `.venv/bin/python` |
| Astra source version | `969550b611ceba11653c4b2651079bcb85d1c24e` |
| Prebuilt service image | `astra-969550b:local`, built from this commit |
| Linux CLI | `work/astra-optimize-0731-05-linux-amd64/target/release/astra` |
| Model | `deepseek-v4-flash`, Thinking enabled, reasoning effort `max`, temperature sent as `0` |
| Request budget | 100 product model requests per task; continuation shares the budget |
| Task manifest and deadlines | `astra/benchmark/toolathlon-verified/freeze/task-runtime-tiers.json`; task-specific limits |
| MatrixOne | `all-in-one-matrixone-1`, host port `6001` |
| Memoria | `all-in-one-memoria-1`, `http://127.0.0.1:18100` |
| Shared Astra database | `astra_toolathlon_shared_969550b` |
| Networking | Task-scoped Astra service uses host networking for the local proxy and Gateway |

The launcher currently uses the absolute Toolathlon Python path above.
`--toolathlon-source` selects the dataset and child Python environment; it does
not change the shell launcher's own interpreter. A different machine must
provide compatible paths and deployed services before using these examples.
The image tag alone is not proof of the source commit; provision the matching
image and CLI rather than building an arbitrary latest branch.

No permanent benchmark `astra-server` needs to be started manually. The runner
creates a task-scoped Astra container, selects its local API port, registers the
task identity and model, runs the Agent, and stops/removes the container during
cleanup. MatrixOne and Memoria remain shared services. Filesystem isolation is
retained: Astra receives the task workspace, not the dataset source, evaluator,
or Docker socket. Do not modify Astra or dataset source to run this deployment.

### Credentials

The batch loads the DeepSeek key in this order:

1. `TOOLATHLON_DEEPSEEK_ASTRA_API_KEY` in the invoking environment.
2. `--models-file`, defaulting to
   `external/astra-optimize_0731_05/.models.yaml`.

The YAML must be a list containing exactly one `name: deepseek-v4-flash` entry
with an `api_key`. A value such as `${DEEPSEEK_API_KEY}` is resolved from the
environment. Keep the file private; do not print or commit its contents.

The private service configuration defaults to
`work/toolathlon-astra-969550b/server-runtime.private.json`; override it with
`TOOLATHLON_ASTRA_SERVER_CONFIG`. Its file permissions must not grant group or
other access (use mode `600`). It contains deployment credentials, including
`MATRIXONE_USER`, `MATRIXONE_PASSWORD`, `MEMORIA_MASTER_KEY`, `ASTRA_JWT_SECRET`,
`ASTRA_TOKEN_ENCRYPTION_KEY`, and `ASTRA_BRIDGE_SECRET`. Reuse the protected
configuration for the deployed services; do not invent replacement secrets.
The DeepSeek provider key stays with the runner's model proxy, not in the Astra
service container. A global `ASTRA_ADMIN_ACCESS_TOKEN` from the historical
launcher is not required for this task-scoped registration flow.

Run the new launcher as the deployment user (`vagrant`), with working
non-interactive `sudo -n docker` access. It invokes privileged Docker actions
internally; the blanket `sudo -E` instructions later in this document apply to
the historical launchers, not this new batch entry point.

### Dataset services and proxy

For a fresh deployment or an explicitly approved global redeployment, use the
Toolathlon-provided script while all evaluations are stopped:

```bash
cd /home/vagrant/dataset/Toolathlon
bash global_preparation/deploy_containers.sh true
```

This is a potentially destructive global deployment operation, not a routine
resume step. The `true` argument permits the upstream Dovecot plaintext-auth
configuration. Preserve anything needed before deployment, and prepare all
required local applications, Kubernetes services, external account credentials,
and OAuth authorizations, not only services for one example task.

To start an existing deployment without recreating its data:

```bash
cd /home/vagrant/moi-benchmark
bash astra/runners/restore_toolathlon_services.sh
```

The restore script starts its configured MatrixOne, Memoria, WooCommerce,
Canvas, mail and Kubernetes containers, checks application responses, and
restores the Canvas HTTPS proxy on `20001` forwarding to `10001`. It is a fixed
list of existing containers, not a replacement for the upstream full deployment
script. It does not refresh external SaaS credentials or prove that every MCP
service is usable. `--restore-services` on the batch runs this same script.

Keep local services directly reachable. The batch explicitly sets
`NO_PROXY`/`no_proxy` for `127.0.0.1,localhost`, but that alone does not bypass
host transparent routing. External Google/Notion/W&B requests still depend on
the deployed proxy and task-container configuration. Do not switch proxy nodes
or globally redeploy shared applications during a running batch. Gateway startup
uses debug logging; consult the task logs rather than assuming the host and
container have identical routes or credentials.

### Single-task smoke run

Use a new output directory. Unlike the batch, the single-task entry point
requires `TOOLATHLON_DEEPSEEK_ASTRA_API_KEY` in its environment and does not load
`--models-file`. In a Bash shell, if the key is not already exported, enter it
without echoing it or placing it in command arguments:

```bash
cd /home/vagrant/moi-benchmark
read -r -s -p 'DeepSeek API key: ' TOOLATHLON_DEEPSEEK_ASTRA_API_KEY
printf '\n'
export TOOLATHLON_DEEPSEEK_ASTRA_API_KEY
export PYTHONPATH="$PWD/astra/runners${PYTHONPATH:+:$PYTHONPATH}"

run_id="astra969-find-alita-paper-$(date -u +%Y%m%dT%H%M%SZ)"
/home/vagrant/dataset/Toolathlon/.venv/bin/python -u \
  -m toolathlon_astra_969550b \
  --system astra \
  --task-id find-alita-paper \
  --experiment-id toolathlon-astra-969550b-smoke \
  --run-id "$run_id" \
  --output-dir "$PWD/work/toolathlon-astra-969550b/$run_id" \
  --toolathlon-source /home/vagrant/dataset/Toolathlon \
  --docker-via-sudo
```

This runs prepare, Agent, Evaluator, and cleanup and makes real model requests.
Do not run it concurrently with the batch; task preparation mutates shared
application state.

### Start a new 108-task batch

Use a new empty output directory; do not overwrite the evidence of previous
experiments. The following enables the later deployment policy of restarting
MatrixOne at five-task boundaries:

```bash
cd /home/vagrant/moi-benchmark
bash astra/runners/run_astra_969550b_108.sh \
  --output-dir work/toolathlon-astra-969550b/batch-108-new \
  --models-file external/astra-optimize_0731_05/.models.yaml \
  --toolathlon-source /home/vagrant/dataset/Toolathlon \
  --restore-services \
  --restart-matrixone-every 5
```

`--restart-matrixone-every` defaults to `0` (disabled), not `5`. The counter
advances after tasks whose cleanup was confirmed, regardless of pass/no_pass;
skipped tasks do not increment it. When runnable tasks remain, the runner
restarts `all-in-one-matrixone-1` and waits up to 300 seconds for Docker health
to report `healthy`. Restart failure stops the batch. This restarts a shared
database service: do not enable it while unrelated workloads rely on that
MatrixOne instance. It does not restart the dataset applications or Memoria.

### Resume and run in tmux

To resume the same batch, retain the original output directory and supply the
restart policy again; it is taken from the current command, not implicitly
restored from a previous launch:

```bash
cd /home/vagrant/moi-benchmark
bash astra/runners/run_astra_969550b_108.sh \
  --output-dir work/toolathlon-astra-969550b/batch-108-new \
  --resume \
  --restart-matrixone-every 5
```

For a detached run, set the working directory explicitly. The command can also
be used for an empty new batch directory. Attach to see progress and any early
startup error:

```bash
tmux new-session -d -s astra-toolathlon \
  -c /home/vagrant/moi-benchmark \
  'bash astra/runners/run_astra_969550b_108.sh --output-dir work/toolathlon-astra-969550b/batch-108-new --resume --restart-matrixone-every 5; rc=$?; printf "Runner exited: %s\n" "$rc"; exec bash'
tmux attach -t astra-toolathlon
```

The existing historical batch path is
`work/toolathlon-astra-969550b/batch-108-resumable`. Use it only when deliberately
continuing that batch; it is not the default destination for a new experiment.

Resume behavior is based on saved results and cleanup, not only Agent status:

- Both `pass` and `no_pass` are skipped when cleanup passed. A crashed Agent
  with a scored `no_pass` is therefore not automatically rerun.
- Unscored/incomplete tasks can be attempted again when cleanup is confirmed.
- Explicit reruns are represented by the existing `requeue_tasks` task-name
  list in `batch.json`; there is no batch `--task-id` or `--rerun-failed` flag.
  Change rerun selections only with the batch stopped, preserving prior attempts.
- An optional `excluded-tasks.json` maps known task names to exclusion reasons.
  Exclusion is not a pass. A previous unconfirmed cleanup can still block resume
  before exclusion takes effect.
- `cleanup_requires_attention` stops the sequence. Diagnose and complete
  controlled cleanup before resuming; do not edit `cleanup_passed` to bypass it.

### Progress, interruption, and evidence

Read saved progress without starting services, loading model credentials, or
making model requests:

```bash
cd /home/vagrant/moi-benchmark
bash astra/runners/run_astra_969550b_108.sh \
  --output-dir work/toolathlon-astra-969550b/batch-108-new \
  --status
```

The batch executes serially and holds
`work/toolathlon-astra-969550b/.batch.lock`; the active child inherits the lock.
In an attached foreground session, Ctrl+C forwards SIGINT to the active task
and allows up to 180 seconds for shutdown. If the child remains alive, wait and
inspect it rather than starting another batch or deleting the lock file.
Killing only the batch parent can leave a task running and retaining the lock.

| Artifact | Meaning |
| --- | --- |
| `batch.json`, `summary.json` | Saved queue, current task, attempts, and progress counts |
| `<task>/attempt-N-<timestamp>/` | One attempt's full evidence; earlier attempts remain separate |
| `<task>/attempt-N-<timestamp>.launch.log` | Single-task runner stdout/stderr, including preparation errors |
| `run.json` | Agent terminal status, Evaluator status, budget and failure classification |
| `trajectory.jsonl`, `tool-calls.jsonl` | Agent events and normalized tool calls |
| `model-usage.jsonl` | Observed requests and provider usage; missing usage remains unknown |
| `preprocess.log`, `container.log`, `astra-server.log` | Task preparation, Gateway/container, and Astra service diagnostics |
| `lifecycle-events.jsonl`, `shared-db-cleanup.json` | Lifecycle boundaries and shared-database cleanup result |
| `shared-db-archive-*` | Cleanup diagnostic archive when created; not a guarantee of complete token recovery |
| `evaluator/` | Evaluation logs and results |
| `matrixone-restarts.log` | Batch-level periodic restart output |

The shared-database lifecycle performs controlled cancellation/session cleanup
before the next task. It does not create a fresh database per task. Runtime
records can be removed by cleanup, so preserve attempt artifacts; do not assume
that a later database query can recover all per-request Token data.

A batch status of `finished` is not synonymous with all tasks passing. Check
pass/no_pass/incomplete/excluded counts and the per-attempt Evaluator evidence.
The published 80/108 Astra report also contains explicitly authorized historical
replacements, evaluator-only reruns and an offline review; those report choices
are not automatically reproduced by launching a fresh batch.

- [Astra 969550b analysis and task appendix](../reports/Toolathlon-analysis/astra-969550b-toolathlon-108-task-analysis.md)
- [Four-product comparison](../reports/Toolathlon-analysis/ALL-astra_new-hermes-pi-dsh-toolathlon-comparison.md)

## Historical three-product environment

Set the Toolathlon checkout and runtime credentials in the invoking shell. Do
not write real keys into the repository.

```bash
export TOOLATHLON_SOURCE_ROOT=/absolute/path/to/Toolathlon
export TOOLATHLON_DEEPSEEK_ASTRA_API_KEY=...
export TOOLATHLON_DEEPSEEK_HERMES_API_KEY=...
export TOOLATHLON_DEEPSEEK_PI_API_KEY=...
export ASTRA_ADMIN_ACCESS_TOKEN=...
export TOOLATHLON_PI_EXECUTABLE=/absolute/path/to/pi-0.73.1/pi
```

Output roots must be beneath this repository or `/tmp`. For the historical launchers, use `sudo -E`
so the allowlisted environment variables reach the trusted lifecycle process.

## Historical: run all three products

```bash
cd /absolute/path/to/moi-benchmark
sudo -E astra/runners/scripts/run_toolathlon_three_products_108.sh \
  "$PWD/astra/results/toolathlon-three-products-108"
```

The output layout is:

```text
toolathlon-three-products-108/
  astra-hermes/
    qualification-pair/
    m2-first-14/
    m3-remaining-94/
  pi/
```

## Historical: run only Astra and Hermes

```bash
sudo -E astra/runners/toolathlon_verified/scripts/run_astra_hermes_108.sh \
  "$PWD/astra/results/toolathlon-astra-hermes-108"
```

## Historical: run only Pi

```bash
sudo -E astra/runners/toolathlon_pi/scripts/run_pi_108.sh \
  "$PWD/astra/results/toolathlon-pi-108"
```

Re-run the identical command to resume. Do not reuse an output root for a
different source checkout, credential cohort, product version, or experiment.

## Repository hygiene

`astra/results/`, runtime work directories, caches, and `.env` files are
ignored. Commit source, tests, documentation, and the redacted runtime
manifests only. Never commit provider keys, Astra tokens, generated application
credentials, task trajectories, or adapter logs.
