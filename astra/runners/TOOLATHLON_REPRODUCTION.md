# Toolathlon three-product reproduction

This directory contains the supported entry points for reproducing the
Toolathlon runs with Astra, Hermes, and Pi 0.73.1. It also contains an
exploratory single-task DeepSeek Harness adapter; that adapter is documented
separately and is not part of the formal three-product batch. Historical qualification,
freeze-generation, and hotfix scripts remain under
`astra/benchmark/toolathlon-verified/scripts`; users should start runs only
through the entry points documented here.

## What is reproduced

- Astra and Hermes run as a serial, paired 108-task experiment. The launcher
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

## Environment

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

Output roots must be beneath this repository or `/tmp`. Always use `sudo -E`
so the allowlisted environment variables reach the trusted lifecycle process.

## Run all three products

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

## Run only Astra and Hermes

```bash
sudo -E astra/runners/toolathlon_verified/scripts/run_astra_hermes_108.sh \
  "$PWD/astra/results/toolathlon-astra-hermes-108"
```

## Run only Pi

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
