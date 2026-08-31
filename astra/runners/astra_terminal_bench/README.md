# Astra Terminal-Bench S0 and C0 runners

This package runs the Linux Astra CLI as a Harbor 0.20 installed agent against
unmodified Terminal-Bench tasks. Its S0 adapter has no controller. Its C0
adapter adds lifecycle observation and a `noop` action, but never injects a
fault, kills a process, restarts the task from scratch, or writes the task
workspace.

The four-case S0 job is configured in `s0-four-cases.yaml`. The matching
lifecycle-clean job is `c0-four-cases.yaml`. Both run sequentially to avoid
contention with other local Astra experiments.

Required environment:

```bash
cd "$(git rev-parse --show-toplevel)"

# Builds for Docker Desktop's active architecture (arm64 on this Mac).
./astra/runners/astra_smoke/build-linux.sh

export ASTRA_API_URL=http://host.docker.internal:17001
export ASTRA_ACCESS_TOKEN='short-lived local Astra access token' # only used when memory=true
export ASTRA_TBENCH_LINUX_BINARY="$PWD/work/astra-linux-build/target/release/astra"
export ASTRA_TBENCH_MODEL='c5bde5de-9805-48d4-a016-1db6e6018fc4'
export ASTRA_TBENCH_READ_MEMORY=false
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

harbor run \
  --config "astra/runners/astra_terminal_bench/s0-four-cases.yaml" \
  --yes
```

`ASTRA_TBENCH_READ_MEMORY=false` is the default for these experiment configs.
It gives every trial a newly registered Astra user with an empty user-memory
namespace and removes the explicit `memory` tool from the model's tool set.
This blocks prior-run memory from entering the task without modifying Astra
itself. Set it to `true` before `harbor run` to use `ASTRA_ACCESS_TOKEN` and
the existing user's memory. The selected mode and, for isolated runs, the
effective user ID are saved in the trial metadata.

Use `build-linux-amd64.sh` only when the Harbor task containers are explicitly
running as `linux/amd64`; the adapter rejects an ELF/container architecture
mismatch before model execution.

To run C0, keep the same environment and use:

```bash
harbor run \
  --config "astra/runners/astra_terminal_bench/c0-four-cases.yaml" \
  --yes
```

The C0 adapter launches Astra below a dedicated Linux process supervisor while
a host-side controller independently watches a pre-registered, read-only task
predicate. A stable hit records `fault_action=noop`; the trigger action never
sends a signal, restarts Astra, or writes the task workspace.

| Task | C0 trigger |
|---|---|
| `modernize-scientific-stack` | Exactly one required output class is present and non-empty |
| `overfull-hbox` | `input.tex` changed, before a clean LaTeX log exists |
| `build-pmars` | A Debian pMARS source tree is ready, before the binary is installed |
| `db-wal-recovery` | The WAL has a valid SQLite header, before `recovered.json` exists |

For the other Terminal-Bench 2.1 tasks, C0 automatically uses the deterministic
generic predicate `terminal-bench.generic.product-live`. The controller first
waits for the registered product identity, observes the same read-only
`product_live` evidence twice, and revalidates that exact process before
recording the no-op. Metadata and the ledger distinguish these runs with
`trigger_registration_status=generic` and
`trigger_scope=generic_product_live`; no task-specific trigger is implied.

Each predicate must return the same evidence twice, 500 ms apart. A `no_hit`
does not suppress the upstream verifier or become an infrastructure exception;
it sets `lifecycle_gate_passed=false`.

The product gets 2.25 times the task's upstream `[agent].timeout_sec` (1350,
1687.5, 2025, and 2025 seconds for the four registered tasks). The
container-local supervisor enforces that deadline and, before returning,
proves that the registered root and all descendants—including detached
children adopted by the subreaper—are gone. `agent_timeout_multiplier: 2.25`
is wrapper overhead for setup, controller polling, zero-live cleanup, terminal
ledger persistence, and best-effort log collection; it is not extra product
execution time.

The GLM-5.2 runner accepts repeated `--case` arguments and reuses Pi's
resource-aware multi-process scheduler. It has three 2GB memory tokens and six
CPU slots: an 8GB task runs alone, a 4GB task can overlap one 2GB task, and up
to three 2GB tasks can overlap. The Astra product deadline is exactly the
task's dataset `[agent].timeout_sec`; Harbor's 2.5-times agent-phase allowance
is reserved for setup, cleanup, and trajectory collection.

```bash
cd "$(git rev-parse --show-toplevel)"

ASTRA_SOURCE_ROOT="$PWD/external/astra-optimize_0731_05" \
ASTRA_LINUX_BUILD_ROOT="$PWD/work/astra-optimize-0731-05-linux-amd64" \
  ./astra/runners/astra_terminal_bench/build-linux-portable.sh --arch amd64

export HARBOR_BIN="$HOME/.local/share/uv/tools/harbor/bin/harbor"
export MOI_BENCH_DATA_ROOT="$PWD"
export ASTRA_API_URL="http://host.docker.internal:17101"
export ASTRA_TBENCH_READ_MEMORY="false"

run_name="astra-glm52-c0-$(date '+%Y%m%d-%H%M%S')"

/bin/bash astra/runners/scripts/astra-terminal-bench-all-c0.sh \
  --check \
  --case build-pmars \
  --case db-wal-recovery \
  --concurrency 2 \
  --run-name "$run_name" \
  --jobs-dir "$MOI_BENCH_DATA_ROOT/work/astra-glm52-c0-cases-jobs"

caffeinate -dimsu /bin/bash \
  astra/runners/scripts/astra-terminal-bench-all-c0.sh \
  --yes \
  --case build-pmars \
  --case db-wal-recovery \
  --concurrency 2 \
  --run-name "$run_name" \
  --jobs-dir "$MOI_BENCH_DATA_ROOT/work/astra-glm52-c0-cases-jobs"
```

The runner is fixed to `glm-5.2(thinking:high)` with requested
`temperature=0`. Astra's high-thinking protocol omits temperature from the
effective provider request, which is recorded explicitly rather than reported
as an applied sampling value. Omit `--case` to queue all 89 tasks. Each case is
run by a separate Harbor process, and a non-zero result does not prevent the
remaining queue from running.

Every started batch writes a secret-free reproduction record and selected-case
queue to `work/astra-glm52-c0-cases-jobs/.reproduction/`. It records the workspace
and dataset commits, Harbor version, model, binary description, concurrency,
timeouts, memory-read mode, and final Harbor exit code. It explicitly records
that lifecycle audit/no-op status is not a score gate; verifier reward remains
the task-result authority, with infrastructure failures reported separately.

During a run, follow the newest host-side ledger with:

```bash
ledger=$(find \
  "$PWD/work/astra-c0-lifecycle-jobs" \
  -name controller.jsonl -print | sort | tail -1)
tail -f "$ledger"
```

Before Astra starts, the controller pre-registers a session through
`POST /sessions`, records the server-assigned UUID in
`agent/astra-session.json`, `agent/astra-session-created.json`, and
`controller.jsonl`, then passes that exact UUID with `--session-id`. The ID
therefore remains available even if Astra is timed out, cancelled, or exits
before writing its final JSON response.

The runner sets `ASTRA_LLM_FALLBACK_TIMEOUT_S=600` in Astra's isolated runtime
environment; the Astra binary and source remain unchanged. If the CLI exits
with return code 3 and stderr contains `[stream_transport]`, the supervised
retry wrapper sends a continuation message to that same pre-registered session
up to two times. It never resubmits the original instruction or registers a new
session, and all attempts share the original product deadline. The result is
saved in `agent/stream-transport-retry.json` and summarized in Harbor metadata
and `controller.jsonl`.

After the supervisor proves zero live processes—and before Harbor destroys the
task container—the adapter saves the run trajectory under
`agent/astra-trajectory/`:

- `server-session.json` from `GET /sessions/{session_id}`;
- full `server-events.jsonl` from the paginated session-events API;
- the matching local session journal, `step_events.jsonl`, checkpoints, and
  `tool-results/`, preserving their path below the isolated Astra sessions
  directory;
- `manifest.json` with per-file hashes, local-file/server-event counts, and any
  partial-capture errors.

`complete` is an evidence-integrity label, not a prerequisite for running the
Terminal-Bench verifier. The owner-scoped main journal must contain the
registered session ID and real agent activity; completed runs must end with
`session_end` to receive that label. The full server event list must match the
API-reported total and reach two identical snapshots. The adapter then
re-reads every downloaded file and verifies the manifest paths, hashes,
counts, and session IDs. A partial, missing, mismatched, or tampered bundle is
recorded explicitly and makes the optional lifecycle audit incomplete, but it
does not turn a completed agent run into an infrastructure exception or block
the task verifier. Astra's one-shot `chat --stdin --session-id ...` path may
leave the session resumable and therefore omit `session_end`; that is a
trajectory-health distinction, not an agent or Terminal-Bench failure.

Every started run also keeps `agent/trajectory-status.json`, and
`agent/astra-session.json` is updated with `completed`, `failed`, `timeout`, or
`cancelled`. A non-completed status is explicitly marked with `failed=true`;
capture failures are visible as `partial` or `missing` rather than silently
ignored. The fixed process artifacts remain `controller.jsonl`,
`product.identity.json`, `product.cleanup.json`, `astra.stdout.json`, and
`astra.stderr.txt`, plus `stream-transport-retry.json`. Harbor metadata and the
terminal controller ledger record the manifest SHA-256 plus trajectory
file/event counts so later audit can detect a missing or replaced manifest.

The terminal ledger records `product_process_cleanup` with
`fault_action=false`, a cleanup-report hash, and a zero-live proof before
`product_turn_exited`/`controller_completed`. It also preserves
`product_terminal_status` (`completed`, `failed`, `timeout`, or `cancelled`).
Timeout/cancellation cleanup is lifecycle hygiene, not a C0 fault.

The access token is copied into a mode-0600 credentials file inside each task
container and is not exposed to task commands through the Harbor agent
environment. It is registered again only after Harbor 0.20 snapshots that
environment, so Harbor's final text-artifact scrub can redact the literal token
without passing it to task commands.

The historical jobs under `work/astra-c0-jobs` retain their original Harbor
files and labels. The sidecar `work/astra-c0-jobs/classification.json`
reclassifies them as `S0-like / exploratory`; they are not eligible for formal
S0 scoring or C0/F1 pairing.

New C0 jobs without a validated input-freeze hash are marked
`exploratory_unfrozen` and `formal_score_eligible=false`. The exact-33 driver
validates and injects `ASTRA_TBENCH_FREEZE_MANIFEST_SHA256`; those runs are
marked `formal_frozen_inputs` and `formal_score_eligible=true`, binding their
task/image, permission, model, budget, runner, and Astra artifact inputs.
