# Astra Toolathlon runner

This package contains the deployment and runners for Astra commit
`969550b611ceba11653c4b2651079bcb85d1c24e`.

Full prerequisites, credentials, single-task execution, resume semantics and
cleanup instructions are in [Toolathlon reproduction](../TOOLATHLON_REPRODUCTION.md).

## Layout

| Path | Purpose |
| --- | --- |
| `runner.py` | Single-task prepare / Agent / Evaluator / cleanup lifecycle |
| `batch.py` | Serial 108-task scheduling, resume, exclusions and periodic MatrixOne restart |
| `run_108.sh` | Batch shell entry point; retains the existing deployment Python path |
| `restore_services.sh` | Start configured existing services and restore Canvas HTTPS proxy |
| `authorize_notion.sh` | Notion OAuth setup through the configured proxy |
| `notion_oauth_proxy_preload.cjs` | Node proxy preload for Notion OAuth |
| `prepare_policy/sitecustomize.py` | Task-container preparation policy |
| `diagnose_preprocess.py` | Preparation diagnostics; not a mandatory reproduction step |

The shared adapter, model proxy and database lifecycle remain in
`toolathlon_verified/`; this package does not duplicate them. It reuses the same
model configuration, shared database, task image, budgets, lock and output paths.

## Batch

From the repository root, with services and credentials already prepared:

```bash
bash astra/runners/toolathlon_astra/run_108.sh \
  --output-dir work/toolathlon-astra-969550b/batch-108-new \
  --resume \
  --restart-matrixone-every 5
```

This makes real model requests and can restart the shared MatrixOne instance.
Use a new output directory for a new experiment. For saved progress only:

```bash
bash astra/runners/toolathlon_astra/run_108.sh \
  --output-dir work/toolathlon-astra-969550b/batch-108-new \
  --status
```

## Python entry points

With `astra/runners` on `PYTHONPATH`, use `python -m toolathlon_astra.runner`
for a single task, `python -m toolathlon_astra.batch` for the batch, and
`python -m toolathlon_astra.diagnose_preprocess` for preparation diagnostics.
Use the configured Toolathlon virtual environment, not an arbitrary system
Python. The reproduction guide includes the required single-task arguments.

Root-level versioned Python modules and shell launchers are compatibility
wrappers. The old Node preload path is also retained for existing
`NODE_OPTIONS` settings. No live service changes are performed by reorganizing
these files.
