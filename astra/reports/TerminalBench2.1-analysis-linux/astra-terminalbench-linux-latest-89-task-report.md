# Astra：Terminal-Bench 2.1 Linux 最新 89 题结果汇总

生成日期：2026-09-11
范围：Linux 89 题完整 cohort（包含 tune-mjcf）；严格采用 latest-results/results.jsonl 已选定的每题结果。

## 结论摘要

| 指标                  | 结果    |
| --------------------- | ------- |
| 任务覆盖              | 89 / 89 |
| 有效 verifier         | 89 / 89 |
| verifier pass         | 48      |
| verifier no-pass      | 41      |
| verifier pass rate    | 53.93%  |
| completed             | 45      |
| timeout               | 25      |
| failed                | 19      |
| max_turn（明确记录）  | 0       |
| max_token（明确记录） | 0       |

Astra 当前 verifier 得分为 **48/89（53.93%）**。89 题均有二元 reward 和 CTRF verifier 报告。全部选中记录均标记 formal_score_eligible=false，因此这里是最新探索性运行结果汇总，不声称为正式冻结评测成绩。

## 统计口径与配置

- 任务全集与选中尝试来自 work/linux-terminal-bench/astra/jobs/latest-results/results.jsonl，共 89 题；不重新按历史最好成绩选取。nginx 与各轮重测沿用该清单指定的结果。
- 每题通过与否仅按选中 trial 的 verifier reward；Agent completed/failed/timeout 与 verifier pass/no-pass 分开统计。
- 选中产品配置（版本 / 模型 / 产品 timeout multiplier）：astra 0.1.0 / glm-5.2(thinking:high) / 1.0：89 题。
- C0 条件、memory read 关闭；以各 trial 的 result.json 为配置证据。所选记录跨多个批次，服务存活适配与 verifier 缓存准备有过调整，不能将当前 v2 配置追溯套用到所有旧尝试。
- 产品 wall-clock timeout 以 trial 记录的数据集预算与 multiplier 为准；Harbor 结束处理与 verifier 时间单独统计。此次报告不推断所有批次使用同一并发数。
- 数据集任务元数据来自 work/terminal-bench-2-1/tasks/*/task.toml。
- 最新结果结束时间范围：2026-09-09T07:49:05.827541Z 至 2026-09-11T07:49:26.415917Z。
- latest-results 保存的 observed_runs 分布：1 次=61 题，2 次=18 题，3 次=5 题，4 次=1 题，5 次=1 题，6 次=2 题，8 次=1 题；该字段沿用汇总清单，不代表重新扫描得到的全部历史运行数。
- failed 保留原始分类，其中 budget_exhausted 不自动等同 max_turn 或 max_token；明确上限类型缺失时不猜测。

## 运行结束状态

| 结束状态  | 任务数 | verifier pass | verifier no-pass |
| --------- | ------ | ------------- | ---------------- |
| completed | 45     | 39            | 6                |
| timeout   | 25     | 2             | 23               |
| failed    | 19     | 7             | 12               |
| max_turn  | 0      | 0             | 0                |
| max_token | 0      | 0             | 0                |

max_turn/max_token 的 0 表示没有明确记录为该类的任务；failed 中未进一步证实的预算耗尽仍保留在 failed。

## Verifier 条目进度

| 指标                       | 结果    |
| -------------------------- | ------- |
| 有 passed/total 明细的任务 | 89 / 89 |
| verifier 条目累计通过      | 229     |
| verifier 条目累计执行      | 308     |
| 条目级通过比例（诊断项）   | 74.4%   |

条目级比例不能替代任务级 reward：不同任务的 verifier 条目数量不同，Terminal-Bench 的任务得分仍以每题最终二元 reward 为准。

## 按作者难度

| 难度   | pass / tasks | pass rate |
| ------ | ------------ | --------- |
| Easy   | 3 / 4        | 75.0%     |
| Medium | 34 / 55      | 61.8%     |
| Hard   | 11 / 30      | 36.7%     |

## 时间、模型响应、Tool 与 Token

| 指标                        | 覆盖  | 总计    | 中位数    | P90       |
| --------------------------- | ----- | ------- | --------- | --------- |
| 端到端时间                  | 89/89 | 25.79 h | 14.12 min | 32.58 min |
| Agent 执行时间              | 89/89 | 21.16 h | 11.18 min | 30.13 min |
| Verifier 时间               | 89/89 | 3.61 h  | 0.74 min  | 6.44 min  |
| 模型响应/API calls（trace） | 89/89 | 1571    | 15.0      | 33.2      |
| Tool calls（trace）         | 89/89 | 1899    | 19.0      | 43.0      |

63 题优先读取任务目录 agent/session.jsonl 的原生 turn 汇总：tokens_in 为 fresh input，cache_read_tokens 为缓存读取，tokens_out 为输出，tool_count 为请求工具次数，llm_rounds 为模型轮数。字段与同 run 的数据库 run_finished 汇总一致；trajectory manifest 的 complete/partial 只表示导出状态，不能单独判断这些统计是否完整。cache write 分量由对应 run_finished 补齐。

其余 26 题从 all-verifier-attempts 中精确匹配 job、trial、session_id，使用 current/old 导出的 inference_invocations 和 agent_run_events。Token 累加 purpose=primary_agent 且 usage_status=provider_exact 的 invocation，按 invocation_id 去重；工具调用从 tool_call 事件按 (run_id, tool_call.id) 去重，兼容直接与嵌套 payload，不重复计入开始/结束事件。模型响应统计 succeeded invocation。

数据库补充统计只包含 Agent execution 时间窗口内已结束的 invocation 和已发出的工具调用；26 题的 Token 作为已观测下界，不能假定包含未结束请求、usage 缺失、辅助模型或 provider 内部重试的全部账单消耗。原生 llm_rounds 与数据库 succeeded invocation 的粒度可能不同，因此模型响应列不是严格一致口径的 HTTP 请求次数。Tool calls 表示请求调用，不保证成功执行。

| Token 分量 / 统计口径                           | 有 usage 的任务 | 已观测总量 |
| ----------------------------------------------- | --------------- | ---------- |
| fresh input                                     | 89/89           | 10,186,869 |
| cache read                                      | 89/89           | 56,577,024 |
| output                                          | 89/89           | 2,625,096  |
| fresh input + cache read + cache write + output | 89/89           | 69,388,989 |
| reasoning（独立分量）                           | 0/89            | 缺失       |

input 与缓存分开相加，output 不再叠加 reasoning。缺失值在 CSV 留空、Markdown 标为缺失，不用 0 替代。Harbor result.json 的 provider usage 在 89 题中均缺失，不采用其空值作为计数。表内 Token 总量是混合完整运行汇总与部分运行观测值的总和，应解释为已观测下界。

有 23 个 session 在 Agent execution 结束后仍有成功模型 invocation，另观测到 16,277,640 Token。这些数据单列在 CSV 的 post_agent_* 字段，不计入上述任务执行阶段统计，也不改变 verifier 结果；该现象提示部分旧批次存在服务端运行晚于本地 Agent 结束的情况。

## 每题结果与 verifier 进度

passed/total 表示该题 CTRF verifier 明细中通过条目数 / 总条目数；Tool calls 优先采用完整原生汇总，否则采用同 session 数据库 trace。逐题来源、统计覆盖及异常结束原因见配套 CSV。

| Task                             | 难度   | Reward | Verifier passed/total | 结束状态  | 模型响应 | Tool calls | Agent 时间 |
| -------------------------------- | ------ | ------ | --------------------- | --------- | -------- | ---------- | ---------- |
| adaptive-rejection-sampler       | Medium | 1      | 9/9                   | timeout   | 14       | 14         | 15.12 min  |
| bn-fit-modify                    | Hard   | 1      | 9/9                   | completed | 23       | 22         | 6.04 min   |
| break-filter-js-from-html        | Medium | 1      | 1/1                   | completed | 12       | 11         | 6.30 min   |
| build-cython-ext                 | Medium | 0      | 10/11                 | completed | 45       | 58         | 12.48 min  |
| build-pmars                      | Medium | 1      | 4/4                   | failed    | 35       | 39         | 8.66 min   |
| build-pov-ray                    | Medium | 0      | 1/3                   | completed | 33       | 43         | 8.25 min   |
| caffe-cifar-10                   | Medium | 0      | 2/6                   | failed    | 47       | 62         | 18.10 min  |
| cancel-async-tasks               | Hard   | 0      | 5/6                   | completed | 7        | 6          | 1.86 min   |
| chess-best-move                  | Medium | 0      | 0/1                   | timeout   | 16       | 18         | 15.12 min  |
| circuit-fibsqrt                  | Hard   | 0      | 2/3                   | timeout   | 8        | 6          | 60.10 min  |
| cobol-modernization              | Easy   | 1      | 3/3                   | completed | 24       | 24         | 14.97 min  |
| code-from-image                  | Medium | 0      | 0/2                   | timeout   | 34       | 37         | 20.13 min  |
| compile-compcert                 | Medium | 0      | 0/3                   | timeout   | 30       | 43         | 40.18 min  |
| configure-git-webserver          | Hard   | 1      | 1/1                   | completed | 20       | 19         | 3.97 min   |
| constraints-scheduling           | Medium | 1      | 3/3                   | completed | 7        | 7          | 2.37 min   |
| count-dataset-tokens             | Medium | 1      | 1/1                   | completed | 27       | 33         | 11.18 min  |
| crack-7z-hash                    | Medium | 0      | 0/2                   | failed    | 26       | 32         | 25.90 min  |
| custom-memory-heap-crash         | Medium | 1      | 6/6                   | completed | 31       | 44         | 17.28 min  |
| db-wal-recovery                  | Medium | 1      | 7/7                   | completed | 11       | 10         | 2.02 min   |
| distribution-search              | Medium | 1      | 4/4                   | completed | 5        | 4          | 3.30 min   |
| dna-assembly                     | Hard   | 0      | 0/1                   | timeout   | 14       | 21         | 30.10 min  |
| dna-insert                       | Medium | 0      | 0/1                   | completed | 15       | 20         | 7.71 min   |
| extract-elf                      | Medium | 0      | 0/2                   | timeout   | 7        | 10         | 15.11 min  |
| extract-moves-from-video         | Hard   | 0      | 0/2                   | failed    | 35       | 37         | 27.08 min  |
| feal-differential-cryptanalysis  | Hard   | 1      | 1/1                   | failed    | 7        | 5          | 5.56 min   |
| feal-linear-cryptanalysis        | Hard   | 1      | 1/1                   | completed | 6        | 8          | 13.23 min  |
| filter-js-from-html              | Medium | 0      | 1/2                   | failed    | 9        | 8          | 7.27 min   |
| financial-document-processor     | Medium | 1      | 7/7                   | completed | 23       | 56         | 13.31 min  |
| fix-code-vulnerability           | Hard   | 1      | 6/6                   | completed | 9        | 9          | 1.58 min   |
| fix-git                          | Easy   | 1      | 2/2                   | completed | 10       | 15         | 1.63 min   |
| fix-ocaml-gc                     | Hard   | 1      | 1/1                   | completed | 33       | 43         | 31.86 min  |
| gcode-to-text                    | Medium | 0      | 0/2                   | timeout   | 34       | 35         | 15.13 min  |
| git-leak-recovery                | Medium | 1      | 5/5                   | completed | 7        | 6          | 1.43 min   |
| git-multibranch                  | Medium | 1      | 1/1                   | failed    | 25       | 32         | 8.14 min   |
| gpt2-codegolf                    | Hard   | 0      | 0/1                   | timeout   | 6        | 6          | 15.13 min  |
| headless-terminal                | Medium | 1      | 7/7                   | completed | 34       | 34         | 12.05 min  |
| hf-model-inference               | Medium | 1      | 4/4                   | completed | 15       | 16         | 7.64 min   |
| install-windows-3.11             | Hard   | 0      | 2/4                   | failed    | 25       | 33         | 19.68 min  |
| kv-store-grpc                    | Medium | 1      | 7/7                   | completed | 11       | 9          | 2.23 min   |
| large-scale-text-editing         | Medium | 1      | 5/5                   | completed | 13       | 12         | 8.37 min   |
| largest-eigenval                 | Medium | 1      | 3/3                   | timeout   | 20       | 22         | 15.13 min  |
| llm-inference-batching-scheduler | Hard   | 0      | 5/6                   | timeout   | 16       | 19         | 30.13 min  |
| log-summary-date-ranges          | Medium | 1      | 2/2                   | completed | 6        | 5          | 1.64 min   |
| mailman                          | Medium | 0      | 1/3                   | failed    | 25       | 45         | 16.63 min  |
| make-doom-for-mips               | Hard   | 0      | 0/3                   | timeout   | 16       | 23         | 15.13 min  |
| make-mips-interpreter            | Hard   | 0      | 0/3                   | timeout   | 20       | 45         | 30.13 min  |
| mcmc-sampling-stan               | Hard   | 0      | 2/6                   | timeout   | 18       | 21         | 30.16 min  |
| merge-diff-arc-agi-task          | Medium | 1      | 5/5                   | completed | 16       | 17         | 4.08 min   |
| model-extraction-relu-logits     | Hard   | 0      | 0/1                   | failed    | 8        | 6          | 7.48 min   |
| modernize-scientific-stack       | Medium | 1      | 2/2                   | completed | 6        | 7          | 1.42 min   |
| mteb-leaderboard                 | Medium | 0      | 0/2                   | failed    | 35       | 54         | 10.08 min  |
| mteb-retrieve                    | Medium | 0      | 1/2                   | completed | 12       | 14         | 4.36 min   |
| multi-source-data-merger         | Medium | 1      | 3/3                   | completed | 8        | 7          | 1.98 min   |
| nginx-request-logging            | Medium | 1      | 8/8                   | completed | 22       | 26         | 6.04 min   |
| openssl-selfsigned-cert          | Medium | 1      | 6/6                   | completed | 12       | 11         | 2.69 min   |
| overfull-hbox                    | Easy   | 0      | 3/4                   | timeout   | 21       | 24         | 12.61 min  |
| password-recovery                | Hard   | 1      | 2/2                   | completed | 25       | 29         | 7.63 min   |
| path-tracing                     | Hard   | 0      | 0/5                   | timeout   | 20       | 21         | 30.14 min  |
| path-tracing-reverse             | Hard   | 0      | 0/3                   | timeout   | 22       | 32         | 30.13 min  |
| polyglot-c-py                    | Medium | 1      | 1/1                   | completed | 9        | 7          | 4.68 min   |
| polyglot-rust-c                  | Hard   | 1      | 1/1                   | failed    | 11       | 10         | 8.01 min   |
| portfolio-optimization           | Medium | 1      | 4/4                   | completed | 23       | 25         | 27.27 min  |
| protein-assembly                 | Hard   | 0      | 0/1                   | timeout   | 16       | 48         | 30.13 min  |
| prove-plus-comm                  | Easy   | 1      | 4/4                   | completed | 4        | 3          | 0.84 min   |
| pypi-server                      | Medium | 1      | 1/1                   | completed | 11       | 13         | 2.24 min   |
| pytorch-model-cli                | Medium | 1      | 6/6                   | completed | 21       | 21         | 7.13 min   |
| pytorch-model-recovery           | Medium | 1      | 5/5                   | completed | 10       | 10         | 6.34 min   |
| qemu-alpine-ssh                  | Medium | 0      | 0/1                   | failed    | 26       | 28         | 14.49 min  |
| qemu-startup                     | Medium | 1      | 1/1                   | completed | 10       | 10         | 7.86 min   |
| query-optimize                   | Medium | 0      | 5/6                   | failed    | 14       | 16         | 9.51 min   |
| raman-fitting                    | Medium | 0      | 1/3                   | timeout   | 14       | 14         | 15.11 min  |
| regex-chess                      | Hard   | 0      | 0/4                   | failed    | 2        | 2          | 38.56 min  |
| regex-log                        | Medium | 1      | 1/1                   | failed    | 9        | 9          | 6.45 min   |
| reshard-c4-data                  | Medium | 1      | 1/1                   | failed    | 21       | 19         | 11.30 min  |
| rstan-to-pystan                  | Medium | 0      | 1/6                   | failed    | 15       | 21         | 13.23 min  |
| sam-cell-seg                     | Hard   | 1      | 9/9                   | completed | 23       | 27         | 11.84 min  |
| sanitize-git-repo                | Medium | 1      | 3/3                   | completed | 24       | 40         | 7.85 min   |
| schemelike-metacircular-eval     | Medium | 0      | 0/1                   | timeout   | 14       | 27         | 40.13 min  |
| sparql-university                | Hard   | 1      | 3/3                   | completed | 15       | 13         | 6.16 min   |
| sqlite-db-truncate               | Medium | 1      | 1/1                   | failed    | 12       | 10         | 3.36 min   |
| sqlite-with-gcov                 | Medium | 1      | 3/3                   | completed | 19       | 18         | 6.93 min   |
| torch-pipeline-parallelism       | Hard   | 0      | 2/3                   | timeout   | 8        | 9          | 15.13 min  |
| torch-tensor-parallelism         | Hard   | 1      | 3/3                   | completed | 11       | 9          | 5.38 min   |
| train-fasttext                   | Hard   | 0      | 1/2                   | timeout   | 26       | 25         | 60.14 min  |
| tune-mjcf                        | Medium | 0      | 3/4                   | timeout   | 11       | 12         | 15.12 min  |
| video-processing                 | Hard   | 0      | 3/5                   | completed | 24       | 25         | 22.04 min  |
| vulnerable-secret                | Medium | 1      | 3/3                   | completed | 10       | 11         | 1.95 min   |
| winning-avg-corewars             | Medium | 0      | 2/3                   | timeout   | 35       | 39         | 60.13 min  |
| write-compressor                 | Hard   | 0      | 0/3                   | timeout   | 2        | 3          | 15.10 min  |

## 数据来源

- work/linux-terminal-bench/astra/jobs/latest-results/results.jsonl
- 每个选中 trial 的 result.json、verifier/ctrf.json、agent/session.jsonl 与 agent/astra-trajectory/manifest.json
- work/linux-terminal-bench/astra/trace-supplements/all-verifier-attempts/attempts/<job></job>/<trial></trial>/current|old/astra_runtime/ 下的 inference_invocations.jsonl、agent_run_events.jsonl、agent_events.jsonl
- work/terminal-bench-2-1/tasks/*/task.toml
- 配套明细：[astra-terminalbench-linux-latest-89-task-details.csv](astra-terminalbench-linux-latest-89-task-details.csv)
