# DSH：Terminal-Bench 2.1 最新 88 题结果汇总

生成日期：2026-08-19
范围：排除 `tune-mjcf` 后的 88 个任务；每个任务取匹配 `terminalbench-glm52` profile 的最新 `finished_at` 记录。

## 结论摘要

| 指标 | 结果 |
|---|---:|
| 任务覆盖 | 88 / 88 |
| verifier pass | 47 |
| verifier no-pass | 41 |
| verifier pass rate | 53.4% |
| 正常端到端成功（pass 且无 wall-clock timeout） | 47 |
| 产品 wall-clock timeout | 1 |
| 达到 50 次模型请求限制 | 31 |

DSH 当前为 **47/88（53.41%）**。这是 `deepseek-harness 0.1.0rc6 + zai/glm-5.2` 在当前探索性 profile 下的 verifier 结果，不是正式排行榜分数。

## 统计口径与配置

- 任务全集来自 DSH pending queue：89 个源任务排除 `tune-mjcf`，固定为 88 个。
- 重复任务按 `finished_at` 取最新匹配记录；`torch-tensor-parallelism` 的最新补跑由 no-pass 变为 pass。
- `max_turns=50` 的单位是 **模型请求 step**，不是用户可见对话 turn；一个 step 可以包含一次模型响应及其工具调用。
- 每题产品 wall-clock timeout 为数据集 agent timeout 经当前 profile 倍率计算后的有效值；`write-compressor` 的有效限制为 1,800 秒。
- runtime 为 `0.1.0rc6`，source reference 为 `0.1.0-rc.5` / commit `47f943859bef60e4160492346772ded9b24f765a`；运行元数据标记 `formal_score_eligible=false`、`source_distribution_match=false`。

## 生命周期终态

| DSH 结束原因 | 任务数 | verifier pass | verifier no-pass |
|---|---:|---:|---:|
| `completed` | 56 | 39 | 17 |
| `max_turns` | 31 | 8 | 23 |
| `wall_clock_timeout` | 1 | 0 | 1 |

`max_turns` 是预算终态，不自动等于 verifier 失败：31 个触顶任务中有 8 个通过、23 个未通过。`write-compressor` 属于 wall-clock timeout，但 Harbor 随后完整执行了 verifier，结果为 2/3、reward=0。

## Verifier 条目进度

| 指标 | 结果 |
|---|---:|
| 有 passed/total 明细的任务 | 88 / 88 |
| verifier 条目累计通过 | 221 |
| verifier 条目累计执行 | 304 |
| 条目级通过比例（诊断项） | 72.7% |

条目级比例不能替代任务级 reward：不同任务的 verifier 条目数量不同，Terminal-Bench 的任务得分仍以每题最终 reward 为准。

## 按作者难度

| 难度 | pass / tasks | pass rate |
|---|---:|---:|
| Easy | 4 / 4 | 100.0% |
| Medium | 30 / 54 | 55.6% |
| Hard | 13 / 30 | 43.3% |

## 时间、模型请求、工具与 Token

| 指标 | 覆盖 | 总计 | 中位数 | P90 |
|---|---:|---:|---:|---:|
| 端到端时间 | 88/88 | 20.58 h | 9.66 min | 29.42 min |
| Agent 执行时间 | 88/88 | 18.11 h | 7.84 min | 27.07 min |
| Verifier 时间 | 88/88 | 1.56 h | 0.30 min | 2.31 min |
| 模型请求 step | 88/88 | 2,892 | 32 | 50 |
| 工具调用 | 88/88 | 3,077 | 35 | 54 |

| Token 分量 | 覆盖 | 已观测总量 |
|---|---:|---:|
| fresh input | 88/88 | 3,044,005 |
| cache read | 88/88 | 44,941,760 |
| output | 88/88 | 1,043,593 |
| fresh + cache read + output | 88/88 | 49,029,358 |

`write-compressor` 的正常汇总被 wall-clock timeout 中断，本报告从原始 `dsh-events.jsonl` 恢复了已完成的 25 个模型响应 usage、25 个 step 和 26 个 tool call；这些是可审计的已观测量。

## 每题结果与 verifier 进度

`passed/total` 表示该题 verifier 明细中通过条目数 / 总条目数。

| Task | 难度 | Reward | Verifier passed/total | DSH 终态 | 模型请求 step | Tool calls | Agent 时间 |
|---|---|---:|---:|---|---:|---:|---:|
| `adaptive-rejection-sampler` | medium | 1 | 9/9 | `completed` | 24 | 23 | 9.51 min |
| `bn-fit-modify` | hard | 1 | 9/9 | `completed` | 15 | 14 | 3.86 min |
| `break-filter-js-from-html` | medium | 1 | 1/1 | `completed` | 21 | 24 | 3.94 min |
| `build-cython-ext` | medium | 1 | 11/11 | `max_turns` | 50 | 57 | 8.99 min |
| `build-pmars` | medium | 1 | 4/4 | `completed` | 40 | 44 | 7.82 min |
| `build-pov-ray` | medium | 0 | 0/3 | `max_turns` | 50 | 54 | 12.34 min |
| `caffe-cifar-10` | medium | 0 | 3/6 | `max_turns` | 50 | 53 | 41.35 min |
| `cancel-async-tasks` | hard | 1 | 6/6 | `completed` | 6 | 5 | 1.72 min |
| `chess-best-move` | medium | 0 | 0/1 | `completed` | 31 | 30 | 8.27 min |
| `circuit-fibsqrt` | hard | 0 | 2/3 | `max_turns` | 50 | 51 | 30.30 min |
| `cobol-modernization` | easy | 1 | 3/3 | `max_turns` | 50 | 51 | 9.90 min |
| `code-from-image` | medium | 1 | 2/2 | `completed` | 29 | 28 | 4.40 min |
| `compile-compcert` | medium | 0 | 0/3 | `max_turns` | 50 | 51 | 41.17 min |
| `configure-git-webserver` | hard | 0 | 0/1 | `completed` | 14 | 13 | 1.89 min |
| `constraints-scheduling` | medium | 1 | 3/3 | `completed` | 8 | 9 | 1.87 min |
| `count-dataset-tokens` | medium | 1 | 1/1 | `completed` | 46 | 45 | 16.09 min |
| `crack-7z-hash` | medium | 1 | 2/2 | `completed` | 42 | 41 | 35.96 min |
| `custom-memory-heap-crash` | medium | 1 | 6/6 | `max_turns` | 50 | 59 | 7.82 min |
| `db-wal-recovery` | medium | 0 | 0/7 | `max_turns` | 50 | 50 | 15.08 min |
| `distribution-search` | medium | 1 | 4/4 | `completed` | 8 | 7 | 2.84 min |
| `dna-assembly` | hard | 0 | 0/1 | `completed` | 44 | 44 | 28.53 min |
| `dna-insert` | medium | 0 | 0/1 | `completed` | 22 | 21 | 7.69 min |
| `extract-elf` | medium | 0 | 0/2 | `completed` | 24 | 30 | 4.58 min |
| `extract-moves-from-video` | hard | 0 | 0/2 | `max_turns` | 50 | 53 | 25.49 min |
| `feal-differential-cryptanalysis` | hard | 1 | 1/1 | `completed` | 23 | 22 | 5.25 min |
| `feal-linear-cryptanalysis` | hard | 1 | 1/1 | `completed` | 21 | 25 | 10.98 min |
| `filter-js-from-html` | medium | 0 | 0/2 | `completed` | 31 | 30 | 6.43 min |
| `financial-document-processor` | medium | 0 | 6/7 | `completed` | 48 | 48 | 12.97 min |
| `fix-code-vulnerability` | hard | 1 | 6/6 | `completed` | 30 | 37 | 3.83 min |
| `fix-git` | easy | 1 | 2/2 | `completed` | 9 | 8 | 1.25 min |
| `fix-ocaml-gc` | hard | 0 | 0/1 | `max_turns` | 50 | 50 | 9.38 min |
| `gcode-to-text` | medium | 0 | 0/2 | `max_turns` | 50 | 50 | 16.56 min |
| `git-leak-recovery` | medium | 1 | 5/5 | `completed` | 14 | 24 | 2.46 min |
| `git-multibranch` | medium | 0 | 0/1 | `completed` | 19 | 31 | 3.62 min |
| `gpt2-codegolf` | hard | 0 | 0/1 | `max_turns` | 50 | 50 | 22.92 min |
| `headless-terminal` | medium | 1 | 7/7 | `completed` | 31 | 33 | 5.46 min |
| `hf-model-inference` | medium | 0 | 1/4 | `completed` | 9 | 8 | 2.10 min |
| `install-windows-3.11` | hard | 0 | 1/4 | `max_turns` | 50 | 54 | 39.29 min |
| `kv-store-grpc` | medium | 0 | 5/7 | `completed` | 10 | 10 | 1.24 min |
| `large-scale-text-editing` | medium | 1 | 5/5 | `completed` | 9 | 10 | 4.97 min |
| `largest-eigenval` | medium | 0 | 2/3 | `max_turns` | 50 | 50 | 7.28 min |
| `llm-inference-batching-scheduler` | hard | 1 | 6/6 | `max_turns` | 50 | 50 | 12.31 min |
| `log-summary-date-ranges` | medium | 1 | 2/2 | `completed` | 11 | 16 | 2.30 min |
| `mailman` | medium | 0 | 1/3 | `completed` | 47 | 51 | 13.56 min |
| `make-doom-for-mips` | hard | 0 | 0/3 | `max_turns` | 50 | 63 | 10.77 min |
| `make-mips-interpreter` | hard | 0 | 2/3 | `max_turns` | 50 | 67 | 15.05 min |
| `mcmc-sampling-stan` | hard | 1 | 6/6 | `completed` | 38 | 39 | 22.72 min |
| `merge-diff-arc-agi-task` | medium | 1 | 5/5 | `completed` | 22 | 21 | 3.09 min |
| `model-extraction-relu-logits` | hard | 1 | 1/1 | `max_turns` | 50 | 50 | 26.45 min |
| `modernize-scientific-stack` | medium | 1 | 2/2 | `completed` | 9 | 13 | 2.71 min |
| `mteb-leaderboard` | medium | 0 | 0/2 | `max_turns` | 50 | 54 | 28.82 min |
| `mteb-retrieve` | medium | 1 | 2/2 | `completed` | 24 | 26 | 3.44 min |
| `multi-source-data-merger` | medium | 1 | 3/3 | `completed` | 6 | 7 | 2.30 min |
| `nginx-request-logging` | medium | 0 | 3/8 | `completed` | 12 | 14 | 1.91 min |
| `openssl-selfsigned-cert` | medium | 0 | 5/6 | `completed` | 15 | 14 | 2.52 min |
| `overfull-hbox` | easy | 1 | 4/4 | `completed` | 25 | 28 | 6.54 min |
| `password-recovery` | hard | 1 | 2/2 | `completed` | 33 | 39 | 5.84 min |
| `path-tracing` | hard | 1 | 5/5 | `max_turns` | 50 | 51 | 7.67 min |
| `path-tracing-reverse` | hard | 0 | 1/3 | `max_turns` | 50 | 56 | 11.94 min |
| `polyglot-c-py` | medium | 1 | 1/1 | `completed` | 34 | 33 | 7.86 min |
| `polyglot-rust-c` | hard | 1 | 1/1 | `completed` | 21 | 20 | 5.91 min |
| `portfolio-optimization` | medium | 1 | 4/4 | `completed` | 21 | 20 | 4.07 min |
| `protein-assembly` | hard | 0 | 0/1 | `max_turns` | 50 | 50 | 15.10 min |
| `prove-plus-comm` | easy | 1 | 4/4 | `completed` | 10 | 9 | 1.34 min |
| `pypi-server` | medium | 0 | 0/1 | `completed` | 12 | 12 | 2.63 min |
| `pytorch-model-cli` | medium | 1 | 6/6 | `completed` | 31 | 30 | 7.39 min |
| `pytorch-model-recovery` | medium | 1 | 5/5 | `completed` | 13 | 13 | 4.71 min |
| `qemu-alpine-ssh` | medium | 0 | 0/1 | `max_turns` | 50 | 64 | 11.78 min |
| `qemu-startup` | medium | 0 | 0/1 | `max_turns` | 50 | 50 | 14.67 min |
| `query-optimize` | medium | 0 | 5/6 | `completed` | 27 | 30 | 18.60 min |
| `raman-fitting` | medium | 0 | 1/3 | `completed` | 40 | 40 | 18.37 min |
| `regex-chess` | hard | 0 | 1/4 | `max_turns` | 50 | 51 | 24.12 min |
| `regex-log` | medium | 1 | 1/1 | `completed` | 16 | 15 | 4.22 min |
| `reshard-c4-data` | medium | 1 | 1/1 | `max_turns` | 50 | 63 | 20.73 min |
| `rstan-to-pystan` | medium | 1 | 6/6 | `completed` | 49 | 55 | 22.63 min |
| `sam-cell-seg` | hard | 0 | 1/9 | `completed` | 25 | 28 | 12.60 min |
| `sanitize-git-repo` | medium | 1 | 3/3 | `completed` | 42 | 41 | 6.08 min |
| `schemelike-metacircular-eval` | medium | 1 | 1/1 | `max_turns` | 50 | 57 | 16.78 min |
| `sparql-university` | hard | 1 | 3/3 | `completed` | 32 | 31 | 7.02 min |
| `sqlite-db-truncate` | medium | 1 | 1/1 | `completed` | 10 | 11 | 3.02 min |
| `sqlite-with-gcov` | medium | 1 | 3/3 | `completed` | 14 | 13 | 3.58 min |
| `torch-pipeline-parallelism` | hard | 0 | 2/3 | `max_turns` | 50 | 53 | 24.96 min |
| `torch-tensor-parallelism` | hard | 1 | 3/3 | `completed` | 41 | 40 | 14.65 min |
| `train-fasttext` | hard | 0 | 0/2 | `max_turns` | 50 | 50 | 77.94 min |
| `video-processing` | hard | 0 | 3/5 | `max_turns` | 50 | 51 | 19.15 min |
| `vulnerable-secret` | medium | 1 | 3/3 | `completed` | 9 | 14 | 1.96 min |
| `winning-avg-corewars` | medium | 0 | 2/3 | `max_turns` | 50 | 51 | 17.52 min |
| `write-compressor` | hard | 0 | 2/3 | `wall_clock_timeout` | 25 | 26 | 30.08 min |

## 数据来源

- `work/dsh-c0-terminalbench-89-glm52-jobs/`
- `work/dsh-c0-terminalbench-89-glm52-state/resource.queue.tsv`
- [DSH 逐任务明细 CSV](dsh-terminalbench-latest-88-task-details.csv)
