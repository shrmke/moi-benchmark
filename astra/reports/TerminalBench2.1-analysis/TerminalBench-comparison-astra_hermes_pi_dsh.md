# Astra、Hermes、PI 与 DSH：Terminal-Bench 2.1 最新结果对比

生成日期：2026-08-20
比较范围：固定 88 个任务（排除 `tune-mjcf`），每个产品按其当前结果集选取最新记录。

## 结论摘要

- Pi 补齐原先 4 个 verifier unavailable 任务后，当前覆盖为 88/88，结果为 **53 pass、35 no-pass（60.23%）**。
- 88 题全量视图中，Hermes 为 50/88，DSH 为 47/88；Astra 为 44 pass、42 no-pass、2 unavailable。
- 因 Astra 仍有 2 题没有有效 verifier reward，公平的四方严格配对样本为 86 题；该样本中 Pi 53/86、Hermes 49/86、DSH 46/86、Astra 44/86。
- 仅比较 Agent 执行时间时，DSH 中位数为 7.84 分钟，Hermes 为 12.12 分钟，PI 为 12.78 分钟，Astra 为 15.70 分钟。
- Token 统计已补充覆盖率、input/cache/output/total 与逐题明细；由于历史采集边界不一致，只作为已观测使用足迹，不作 token 效率排名。
- 这些结果来自不同日期的历史最新快照，且 DSH/Pi 部分记录为 exploratory 配置；下表用于描述性对比，不是同步正式排行榜。

## 统计口径

- 任务全集固定为 88 题，即 Terminal-Bench 2.1 源任务中排除 `tune-mjcf`。
- 每个产品、每个任务只保留当前汇总口径选定的最新记录；Pi 的汇总已在 2026-08-19 重新生成，包含 4 个补跑任务。
- `verify pass/no-pass` 以每题 numeric reward 为准；`unavailable` 不合并到 no-pass，也不进入有效 reward 通过率的分母。
- “正常端到端成功”定义为 `reward=1` 且没有 wall-clock timeout；产品 lifecycle 诊断字段不作额外得分门槛。
- 逐题 `passed/total` 来自 CTRF verifier 明细；它表示验证进度，但不以条目数量加权任务 reward。
- Token 统一按 `total_tokens = input_tokens + output_tokens` 统计；`input_tokens` 已包含 cache read，cache 只是 input 的子集，不再重复相加。缺失或不可靠记录保留为空值，不按 0 处理。

## 数据选择与覆盖

| 产品 | 最新时间范围终点 | 88 题中有效 reward | pass | no-pass | unavailable |
|---|---|---:|---:|---:|---:|
| Astra | `2026-08-03T10:31:10.405671Z` | 86/88 | 44 | 42 | 2 |
| Hermes | `2026-08-04T02:46:49.416550Z` | 88/88 | 50 | 38 | 0 |
| PI | `2026-08-19T03:46:46.668553Z` | 88/88 | 53 | 35 | 0 |
| DSH | `2026-08-18T08:15:31.029533Z` | 88/88 | 47 | 41 | 0 |

Astra 的 unavailable 为 `train-fasttext`（agent 结果读取阶段异常，未进入完整 verifier）和 `torch-tensor-parallelism`（verifier 1,800 秒超时）。PI 本次补齐的 4 题如下：

| Pi 补跑任务 | verifier passed/total | reward |
|---|---:|---:|
| `path-tracing-reverse` | 0/3 | 0 |
| `sam-cell-seg` | 9/9 | 1 |
| `torch-pipeline-parallelism` | 0/3 | 0 |
| `torch-tensor-parallelism` | 0/3 | 0 |

## 实验环境与配置边界

| 项目 | Astra | Hermes | PI | DSH |
|---|---|---|---|---|
| 产品版本 | `v0.0.5-4-g844473c68` | `v2026.7.20` | `0.73.1` | `0.1.0rc6` |
| 模型 | `glm-5.2` | `zai/glm-5.2` | `zai/glm-5.2` | `zai/glm-5.2` |
| temperature | 0 | 0 | 0 | 0 |
| max turns / 请求预算 | 50 | 90（当前记录最高 90） | 未显式设置 | 50 个模型请求 step |
| 产品执行时限 | 数据集 agent timeout 的历史倍率配置 | 数据集 agent timeout × 2 | 数据集 agent timeout × 2 | profile 有效 timeout，逐题写入结果元数据 |
| 结果性质 | 历史 formal/exploratory 混合 | 历史快照 | 历史记录 + 4 题 exploratory 补跑 | exploratory，source distribution mismatch |

Host 为 Apple M3 / 16 GB / macOS 15.6 / Docker Desktop 4.82.0 / Harbor 0.20.0。四套系统都在 Harbor 创建的独立任务容器中执行，并由 Terminal-Bench verifier 检查最终工作区。

## 任务级完成结果

### 88 题全量视图

| 指标 | Astra | Hermes | PI | DSH |
|---|---:|---:|---:|---:|
| 有效 verifier reward | 86 | 88 | 88 | 88 |
| verify pass | 44 | 50 | 53 | 47 |
| verify no-pass | 42 | 38 | 35 | 41 |
| verify unavailable | 2 | 0 | 0 | 0 |
| 正常端到端成功 | 41 | 47 | 48 | 47 |
| pass after timeout | 3 | 3 | 5 | 0 |
| wall-clock timeout | 39 | 10 | 17 | 1 |
| 有效 reward 内 pass rate | 51.2% | 56.8% | 60.2% | 53.4% |
| 以固定 88 题为分母的 pass/source | 50.0% | 56.8% | 60.2% | 53.4% |

Astra 的 `pass/source=50.0%` 不能替代其有效 reward 内的 44/86，因为两个 unavailable 不是 verifier no-pass。DSH 的 31 个 `max_turns` 也不等同于 wall-clock timeout，其中 8 题最终 verifier 通过。

### 86 题严格四方配对

| 产品 | pass | no-pass | pass rate |
|---|---:|---:|---:|
| Astra | 44 | 42 | 51.2% |
| Hermes | 49 | 37 | 57.0% |
| PI | 53 | 33 | 61.6% |
| DSH | 46 | 40 | 53.5% |

严格样本中四方全部通过 22 题，四方全部未通过 14 题。按“通过产品数量”分布：0 方通过 14 题、1 方通过 16 题、2 方通过 14 题、3 方通过 20 题、4 方通过 22 题。
仅单个产品通过的任务数：Astra 7、Hermes 2、PI 6、DSH 1。

如果进一步要求四方都有可审计的 `passed/total` 条目明细，则需再排除 `extract-moves-from-video`、`pytorch-model-recovery`、`train-fasttext` 和 `torch-tensor-parallelism`，得到 **84 题条目明细严格交集**。其中 Astra 43/84、Hermes 49/84、PI 53/84、DSH 45/84。这个口径用于审计 verifier 执行进度，不取代上述以 numeric reward 为准的 86 题主比较。

## Verifier 条目进度

| 产品 | 有 passed/total 明细的任务 | verifier 条目 passed | verifier 条目 total | 条目通过比例（诊断项） |
|---|---:|---:|---:|---:|
| Astra | 85/88 | 204 | 297 | 68.7% |
| Hermes | 87/88 | 233 | 299 | 77.9% |
| PI | 88/88 | 230 | 304 | 75.7% |
| DSH | 88/88 | 221 | 304 | 72.7% |

条目累计比例不用于产品排名，因为不同任务拥有不同数量、不同粒度的 verifier 条目；正式任务结果仍以每题 reward 为准。Astra 的 `extract-moves-from-video` 和 Hermes 的 `pytorch-model-recovery` 都有 numeric reward=0，但没有 CTRF 条目明细，因此逐题表中显示 `NO-PASS —`。

## 按作者难度

| 难度 | Astra | Hermes | PI | DSH |
|---|---:|---:|---:|---:|
| Easy | 3/4 (75.0%) | 4/4 (100.0%) | 4/4 (100.0%) | 4/4 (100.0%) |
| Medium | 32/54 (59.3%) | 31/54 (57.4%) | 35/54 (64.8%) | 30/54 (55.6%) |
| Hard | 9/28 (32.1%) | 15/30 (50.0%) | 14/30 (46.7%) | 13/30 (43.3%) |

## Agent 执行时间

时长对比只统计 `agent_execution_s`，不包含环境准备、Agent 安装、Verifier 和清理时间。样本包含成功、失败和 timeout 任务，因此表示当前快照的 Agent 执行负担，不是仅成功任务的速度。

| 产品 | 覆盖 | Agent 执行总时长 | 平均 | 中位数 | P90 |
|---|---:|---:|---:|---:|---:|
| Astra | 88/88 | 34.70 h | 23.66 min | 15.70 min | 54.27 min |
| Hermes | 88/88 | 30.71 h | 20.94 min | 12.12 min | 38.73 min |
| PI | 88/88 | 34.35 h | 23.42 min | 12.78 min | 60.11 min |
| DSH | 88/88 | 18.11 h | 12.35 min | 7.84 min | 27.07 min |

DSH 在这批历史记录中的 Agent 执行总时长、中位数和 P90 均最低。但这是当前记录的描述性结果；不同产品的轮次限制、超时策略和终止条件不完全相同。

## Token 统计

下表的 input 已包含 cache read，total 始终是 `input + output`。“cache read”列仅展示可单独拆分的 input 子集；Hermes 当前历史结果未单列 cache。

| 产品 / 口径 | Token 覆盖 | Input（含cache） | Cache read（input子集） | Output | Total（input+output） | Total 中位数 / P90 |
|---|---:|---:|---:|---:|---:|---:|
| Astra 可靠记录 | 83/88 | 29,207,308 | 24,859,392 | 3,441,787 | 32,649,095 | 236,084 / 1,056,170 |
| Hermes `reported` | 76/88 | 88,169,676 | — | 1,945,545 | 90,115,221 | 378,155 / 3,403,995 |
| PI provider-boundary canonical | 1/88 | 4,580,303 | 4,454,528 | 93,239 | 4,673,542 | 4,673,542 / 4,673,542 |
| PI canonical + legacy 已观测 | 87/88 | 95,521,894 | 88,780,416 | 3,695,410 | 99,217,304 | 420,369 / 3,004,646 |
| DSH 已观测 | 88/88 | 47,985,765 | 44,941,760 | 1,043,593 | 49,029,358 | 351,042 / 1,350,349 |

覆盖与可靠性边界：

- Astra 有 83 条 `session_reconciled/server_reconciled` 可靠记录；上表只汇总这 83 条，且不包含 Intent Judge 的 token。
- Hermes 有 76 条 `reported`；另有 10 条在观测到模型活动后仍缺失 usage，2 条是可疑零值，均不纳入总量。
- PI 仅 `sam-cell-seg` 具有 provider-boundary canonical usage；86 条历史记录为 legacy Harbor 口径，`torch-tensor-parallelism` 缺失 token。“PI canonical + legacy”只是已观测足迹，不是与其他产品严格同口径的总量。
- DSH 有 87 条正常汇总 usage；`write-compressor` 的 Agent 在 wall-clock timeout 后未写入终态汇总，本报告从原始 `dsh-events.jsonl` 恢复了已完成的 25 次模型响应 usage。

因此，Token 总量只表示当前可观测的模型使用足迹，不用于直接给出跨产品 token 效率排名。

## 执行规模（非时长）

| 产品 | Turn/请求指标 | 覆盖 | 总计 | 中位数 / P90 | 工具调用总计 | 工具调用中位数 / P90 |
|---|---|---:|---:|---:|---:|---:|
| Astra | agentic_steps | 86/88 | 1,674 | 16 / 40 | 2,253 | 18 / 59 |
| Hermes | session API calls | 88/88 | 2,784 | 19 / 81 | 3,101 | 20 / 90 |
| PI | assistant messages | 88/88 | 2,097 | 19 / 49 | 2,512 | 22 / 65 |
| DSH | model request steps | 88/88 | 2,892 | 32 / 50 | 3,077 | 35 / 54 |

不同产品的 turn 和 tool 事件边界不一致，不能把上述总量解释为单位能力效率。

当前记录中，Hermes 有 22 题的 `session_api_calls` 超过 50，其中 7 题达到 90；PI 有 8 题的 `assistant_messages` 超过 50，最高为 122；DSH 有 31 题达到固定的 50 次模型请求限制。这些指标的事件来源不同，只用于说明历史预算差异。

## 每题 verifier 进度

单元格格式为 `任务状态 passed/total`；`—` 表示没有可审计的 verifier 条目明细。

| Task | 难度 | Astra | Hermes | PI | DSH |
|---|---|---|---|---|---|
| `adaptive-rejection-sampler` | medium | NO-PASS 0/9 | PASS 9/9 | NO-PASS 0/9 | PASS 9/9 |
| `bn-fit-modify` | hard | PASS 9/9 | PASS 9/9 | PASS 9/9 | PASS 9/9 |
| `break-filter-js-from-html` | medium | PASS 1/1 | PASS 1/1 | PASS 1/1 | PASS 1/1 |
| `build-cython-ext` | medium | PASS 11/11 | PASS 11/11 | PASS 11/11 | PASS 11/11 |
| `build-pmars` | medium | NO-PASS 3/4 | PASS 4/4 | PASS 4/4 | PASS 4/4 |
| `build-pov-ray` | medium | NO-PASS 2/3 | NO-PASS 2/3 | PASS 3/3 | NO-PASS 0/3 |
| `caffe-cifar-10` | medium | PASS 6/6 | NO-PASS 3/6 | PASS 6/6 | NO-PASS 3/6 |
| `cancel-async-tasks` | hard | NO-PASS 5/6 | NO-PASS 5/6 | PASS 6/6 | PASS 6/6 |
| `chess-best-move` | medium | NO-PASS 0/1 | PASS 1/1 | PASS 1/1 | NO-PASS 0/1 |
| `circuit-fibsqrt` | hard | NO-PASS 2/3 | NO-PASS 2/3 | NO-PASS 2/3 | NO-PASS 2/3 |
| `cobol-modernization` | easy | PASS 3/3 | PASS 3/3 | PASS 3/3 | PASS 3/3 |
| `code-from-image` | medium | NO-PASS 0/2 | PASS 2/2 | PASS 2/2 | PASS 2/2 |
| `compile-compcert` | medium | PASS 3/3 | NO-PASS 0/3 | NO-PASS 0/3 | NO-PASS 0/3 |
| `configure-git-webserver` | hard | PASS 1/1 | NO-PASS 0/1 | NO-PASS 0/1 | NO-PASS 0/1 |
| `constraints-scheduling` | medium | PASS 3/3 | PASS 3/3 | PASS 3/3 | PASS 3/3 |
| `count-dataset-tokens` | medium | PASS 1/1 | PASS 1/1 | PASS 1/1 | PASS 1/1 |
| `crack-7z-hash` | medium | NO-PASS 0/2 | PASS 2/2 | PASS 2/2 | PASS 2/2 |
| `custom-memory-heap-crash` | medium | PASS 6/6 | PASS 6/6 | PASS 6/6 | PASS 6/6 |
| `db-wal-recovery` | medium | PASS 7/7 | PASS 7/7 | PASS 7/7 | NO-PASS 0/7 |
| `distribution-search` | medium | NO-PASS 0/4 | PASS 4/4 | PASS 4/4 | PASS 4/4 |
| `dna-assembly` | hard | NO-PASS 0/1 | NO-PASS 0/1 | PASS 1/1 | NO-PASS 0/1 |
| `dna-insert` | medium | NO-PASS 0/1 | NO-PASS 0/1 | NO-PASS 0/1 | NO-PASS 0/1 |
| `extract-elf` | medium | NO-PASS 0/2 | PASS 2/2 | PASS 2/2 | NO-PASS 0/2 |
| `extract-moves-from-video` | hard | NO-PASS — | NO-PASS 1/2 | NO-PASS 0/2 | NO-PASS 0/2 |
| `feal-differential-cryptanalysis` | hard | PASS 1/1 | PASS 1/1 | NO-PASS 0/1 | PASS 1/1 |
| `feal-linear-cryptanalysis` | hard | PASS 1/1 | NO-PASS 0/1 | NO-PASS 0/1 | PASS 1/1 |
| `filter-js-from-html` | medium | NO-PASS 0/2 | PASS 2/2 | NO-PASS 0/2 | NO-PASS 0/2 |
| `financial-document-processor` | medium | PASS 7/7 | PASS 7/7 | PASS 7/7 | NO-PASS 6/7 |
| `fix-code-vulnerability` | hard | PASS 6/6 | PASS 6/6 | PASS 6/6 | PASS 6/6 |
| `fix-git` | easy | PASS 2/2 | PASS 2/2 | PASS 2/2 | PASS 2/2 |
| `fix-ocaml-gc` | hard | PASS 1/1 | PASS 1/1 | PASS 1/1 | NO-PASS 0/1 |
| `gcode-to-text` | medium | NO-PASS 0/2 | NO-PASS 0/2 | NO-PASS 1/2 | NO-PASS 0/2 |
| `git-leak-recovery` | medium | PASS 5/5 | PASS 5/5 | PASS 5/5 | PASS 5/5 |
| `git-multibranch` | medium | PASS 1/1 | NO-PASS 0/1 | NO-PASS 0/1 | NO-PASS 0/1 |
| `gpt2-codegolf` | hard | NO-PASS 0/1 | NO-PASS 0/1 | NO-PASS 0/1 | NO-PASS 0/1 |
| `headless-terminal` | medium | PASS 7/7 | PASS 7/7 | NO-PASS 6/7 | PASS 7/7 |
| `hf-model-inference` | medium | NO-PASS 1/4 | NO-PASS 1/4 | NO-PASS 1/4 | NO-PASS 1/4 |
| `install-windows-3.11` | hard | NO-PASS 3/4 | NO-PASS 1/4 | NO-PASS 1/4 | NO-PASS 1/4 |
| `kv-store-grpc` | medium | NO-PASS 5/7 | NO-PASS 5/7 | NO-PASS 5/7 | NO-PASS 5/7 |
| `large-scale-text-editing` | medium | PASS 5/5 | PASS 5/5 | PASS 5/5 | PASS 5/5 |
| `largest-eigenval` | medium | PASS 3/3 | PASS 3/3 | PASS 3/3 | NO-PASS 2/3 |
| `llm-inference-batching-scheduler` | hard | NO-PASS 1/6 | PASS 6/6 | PASS 6/6 | PASS 6/6 |
| `log-summary-date-ranges` | medium | PASS 2/2 | PASS 2/2 | PASS 2/2 | PASS 2/2 |
| `mailman` | medium | NO-PASS 0/3 | NO-PASS 2/3 | PASS 3/3 | NO-PASS 1/3 |
| `make-doom-for-mips` | hard | NO-PASS 0/3 | NO-PASS 0/3 | NO-PASS 0/3 | NO-PASS 0/3 |
| `make-mips-interpreter` | hard | NO-PASS 0/3 | NO-PASS 0/3 | NO-PASS 2/3 | NO-PASS 2/3 |
| `mcmc-sampling-stan` | hard | NO-PASS 5/6 | PASS 6/6 | PASS 6/6 | PASS 6/6 |
| `merge-diff-arc-agi-task` | medium | PASS 5/5 | PASS 5/5 | PASS 5/5 | PASS 5/5 |
| `model-extraction-relu-logits` | hard | NO-PASS 0/1 | PASS 1/1 | NO-PASS 0/1 | PASS 1/1 |
| `modernize-scientific-stack` | medium | PASS 2/2 | PASS 2/2 | PASS 2/2 | PASS 2/2 |
| `mteb-leaderboard` | medium | PASS 2/2 | NO-PASS 1/2 | PASS 2/2 | NO-PASS 0/2 |
| `mteb-retrieve` | medium | PASS 2/2 | NO-PASS 1/2 | PASS 2/2 | PASS 2/2 |
| `multi-source-data-merger` | medium | PASS 3/3 | PASS 3/3 | PASS 3/3 | PASS 3/3 |
| `nginx-request-logging` | medium | PASS 8/8 | NO-PASS 3/8 | NO-PASS 3/8 | NO-PASS 3/8 |
| `openssl-selfsigned-cert` | medium | PASS 6/6 | NO-PASS 5/6 | PASS 6/6 | NO-PASS 5/6 |
| `overfull-hbox` | easy | NO-PASS 3/4 | PASS 4/4 | PASS 4/4 | PASS 4/4 |
| `password-recovery` | hard | PASS 2/2 | PASS 2/2 | PASS 2/2 | PASS 2/2 |
| `path-tracing` | hard | NO-PASS 0/5 | PASS 5/5 | NO-PASS 0/5 | PASS 5/5 |
| `path-tracing-reverse` | hard | NO-PASS 0/3 | PASS 3/3 | NO-PASS 0/3 | NO-PASS 1/3 |
| `polyglot-c-py` | medium | PASS 1/1 | PASS 1/1 | PASS 1/1 | PASS 1/1 |
| `polyglot-rust-c` | hard | NO-PASS 0/1 | PASS 1/1 | PASS 1/1 | PASS 1/1 |
| `portfolio-optimization` | medium | NO-PASS 1/4 | PASS 4/4 | PASS 4/4 | PASS 4/4 |
| `protein-assembly` | hard | NO-PASS 0/1 | NO-PASS 0/1 | NO-PASS 0/1 | NO-PASS 0/1 |
| `prove-plus-comm` | easy | PASS 4/4 | PASS 4/4 | PASS 4/4 | PASS 4/4 |
| `pypi-server` | medium | PASS 1/1 | NO-PASS 0/1 | NO-PASS 0/1 | NO-PASS 0/1 |
| `pytorch-model-cli` | medium | PASS 6/6 | PASS 6/6 | NO-PASS 5/6 | PASS 6/6 |
| `pytorch-model-recovery` | medium | PASS 5/5 | NO-PASS — | NO-PASS 4/5 | PASS 5/5 |
| `qemu-alpine-ssh` | medium | PASS 1/1 | NO-PASS 0/1 | NO-PASS 0/1 | NO-PASS 0/1 |
| `qemu-startup` | medium | NO-PASS 0/1 | NO-PASS 0/1 | NO-PASS 0/1 | NO-PASS 0/1 |
| `query-optimize` | medium | PASS 6/6 | NO-PASS 5/6 | NO-PASS 5/6 | NO-PASS 5/6 |
| `raman-fitting` | medium | NO-PASS 1/3 | NO-PASS 1/3 | NO-PASS 1/3 | NO-PASS 1/3 |
| `regex-chess` | hard | NO-PASS 0/4 | NO-PASS 0/4 | PASS 4/4 | NO-PASS 1/4 |
| `regex-log` | medium | NO-PASS 0/1 | PASS 1/1 | PASS 1/1 | PASS 1/1 |
| `reshard-c4-data` | medium | NO-PASS 0/1 | PASS 1/1 | PASS 1/1 | PASS 1/1 |
| `rstan-to-pystan` | medium | NO-PASS 1/6 | NO-PASS 4/6 | NO-PASS 1/6 | PASS 6/6 |
| `sam-cell-seg` | hard | PASS 9/9 | PASS 9/9 | PASS 9/9 | NO-PASS 1/9 |
| `sanitize-git-repo` | medium | PASS 3/3 | NO-PASS 1/3 | NO-PASS 2/3 | PASS 3/3 |
| `schemelike-metacircular-eval` | medium | NO-PASS 0/1 | NO-PASS 0/1 | PASS 1/1 | PASS 1/1 |
| `sparql-university` | hard | PASS 3/3 | PASS 3/3 | PASS 3/3 | PASS 3/3 |
| `sqlite-db-truncate` | medium | PASS 1/1 | PASS 1/1 | PASS 1/1 | PASS 1/1 |
| `sqlite-with-gcov` | medium | PASS 3/3 | PASS 3/3 | PASS 3/3 | PASS 3/3 |
| `torch-pipeline-parallelism` | hard | NO-PASS 0/3 | NO-PASS 0/3 | NO-PASS 0/3 | NO-PASS 2/3 |
| `torch-tensor-parallelism` | hard | UNAVAILABLE — | PASS 3/3 | NO-PASS 0/3 | PASS 3/3 |
| `train-fasttext` | hard | UNAVAILABLE — | NO-PASS 1/2 | NO-PASS 0/2 | NO-PASS 0/2 |
| `video-processing` | hard | NO-PASS 2/5 | PASS 5/5 | PASS 5/5 | NO-PASS 3/5 |
| `vulnerable-secret` | medium | PASS 3/3 | PASS 3/3 | PASS 3/3 | PASS 3/3 |
| `winning-avg-corewars` | medium | NO-PASS 1/3 | NO-PASS 1/3 | PASS 3/3 | NO-PASS 2/3 |
| `write-compressor` | hard | NO-PASS 0/3 | NO-PASS 0/3 | PASS 3/3 | NO-PASS 2/3 |

## 解读限制

- 这是跨日期“当前最新记录”的描述性快照，不是四个产品在同一时间重新同步运行的实验。
- verifier reward 是任务成败依据；timeout、max-turn、轨迹状态和工具错误用于归因，不应替代 reward。
- failed/incomplete token 不按零处理；本报告没有用 token 总量决定产品优劣。
- DSH 的 runtime `0.1.0rc6` 与源码参考 `0.1.0-rc.5` 不完全一致，结果只能标记为探索性。

## 数据来源

- `work/astra-c0-all-jobs/analysis/v2/output/astra-c0-latest-verified-trials.csv`
- `work/hermes-c0-all-jobs/analysis/v2/output/hermes-c0-latest-verified-trials.csv`
- `work/pi-c0-all-jobs/analysis/v2/output/pi-c0-latest-verified-trials.csv`（已于 2026-08-19 刷新）
- `work/dsh-c0-terminalbench-89-glm52-jobs/`
- [四方逐任务 verifier、Agent 时长与 Token 附录](TerminalBench-comparison-astra_hermes_pi_dsh-appendix.csv)
- [DSH 单独汇总报告](dsh-terminalbench-latest-88-task-report.md)
