# Hermes：Terminal-Bench 2.1 Linux 最新 89 题结果汇总

更新日期：2026-09-13
范围：Linux 89 题完整 cohort（包含 tune-mjcf）；

| 指标               | 结果    |
| ------------------ | ------- |
| 任务覆盖           | 89 / 89 |
| 有效 verifier      | 89 / 89 |
| verifier pass      | 45      |
| verifier no-pass   | 44      |
| verifier pass rate | 50.56%  |
| completed          | 57      |
| timeout            | 25      |
| max_turn           | 7       |
| max_token          | 0       |

Hermes 当前 verifier 得分为 **45/89（50.56%）**。89 题均有二元 reward 和有效 CTRF verifier 报告。

## 统计口径与配置

- 任务全集来自 work/linux-terminal-bench/hermes/state/resource.queue.tsv，共 89 题并包含 tune-mjcf。
- 产品版本为 v2026.7.20，预构建源 commit 为 3ef6bbd201263d354fd83ec55b3c306ded2eb72a；模型为 zai/glm-5.2；reasoning_effort=high；temperature=0；上限为 90 turns。
- 产品 wall-clock timeout 为数据集 [agent].timeout_sec × 1.0；Harbor agent_timeout_multiplier=1.25 仅为结束处理预留时间，不增加产品预算。
- 单产品任务并行上限为 3，同时受 3 个 memory token / 6 CPU 的统一资源调度约束。
- 数据集 commit：5c8eadf1f393183288fa08b8f73ca9a469cc5e00。
- 当前选中结果时间范围：2026-09-08T07:32:25.013990Z 至 2026-09-11T23:06:17.123116Z。
- 报告记录的可用尝试次数分布：1 次=31 题，2 次=45 题，3 次=10 题，4 次=3 题；仅本次覆盖题目的次数刷新至当前目录。

## 运行结束状态

| 结束状态  | 任务数 | verifier pass | verifier no-pass |
| --------- | ------ | ------------- | ---------------- |
| completed | 57     | 42            | 15               |
| max_turn  | 7      | 0             | 7                |
| timeout   | 25     | 3             | 22               |

本轮结束状态按 product_final_status 记录（timed_out 归为 timeout）；若 gateway 明确记录迭代预算耗尽则归为 max_turn。native_finish_reason 保留原生终止原因或事件。Agent timeout 与 verifier reward 分开统计。

## Verifier 条目进度

| 指标                       | 结果    |
| -------------------------- | ------- |
| 有 passed/total 明细的任务 | 89 / 89 |
| verifier 条目累计通过      | 224     |
| verifier 条目累计执行      | 308     |
| 条目级通过比例（诊断项）   | 72.7%   |

条目级比例不能替代任务级 reward：不同任务的 verifier 条目数量不同，Terminal-Bench 的任务得分仍以每题最终二元 reward 为准。

## 按作者难度

| 难度   | pass / tasks | pass rate |
| ------ | ------------ | --------- |
| Easy   | 2 / 4        | 50.0%     |
| Medium | 29 / 55      | 52.7%     |
| Hard   | 14 / 30      | 46.7%     |

## 时间、模型响应、Tool 与 Token

| 指标                        | 覆盖  | 总计    | 中位数    | P90       |
| --------------------------- | ----- | ------- | --------- | --------- |
| 端到端时间                  | 89/89 | 26.12 h | 16.10 min | 31.79 min |
| Agent 执行时间              | 89/89 | 22.20 h | 13.78 min | 30.32 min |
| Verifier 时间               | 89/89 | 3.12 h  | 0.73 min  | 4.33 min  |
| 模型响应/API calls（trace） | 89/89 | 2743    | 23.0      | 82.2      |
| Tool calls（trace）         | 89/89 | 3008    | 24.0      | 90.0      |

Hermes Tool calls 来自选中 trial 的产品原生 trace；本轮读取 hermes-session.jsonl 的 tool_call_count 与 api_call_count，不使用 result 元数据的缺省值。缺失指标留空，不补零。

| Token 分量 / 统计口径                          | 有 usage 的任务 | 已观测总量 |
| ---------------------------------------------- | --------------- | ---------- |
| fresh input                                    | 89/89           | 6,068,674  |
| cache read                                     | 89/89           | 84,273,088 |
| output                                         | 89/89           | 1,684,507  |
| reasoning（单列）                              | 89/89           | 979,353    |
| input + cache read + output                    | 89/89           | 92,026,269 |
| Harbor result.json usage（n_input + n_output） | 64/89           | 60,823,778 |

Hermes Token 从 `agent/hermes-session.jsonl` 累加，会话汇总的覆盖见上表；已观测 token 不代表未完成请求的完整计费量。reasoning token 单列，不再次加入 input + cache read + output，避免其是否已包含在 output 中造成重复计算。Harbor result usage 来自每题选中 `result.json` 的 `agent_result`；总量按 `n_input_tokens + n_output_tokens` 计算。

## 每题结果与 verifier 进度

passed/total 表示该题 CTRF verifier 明细中通过条目数 / 总条目数；Tool calls 均来自产品原生 trace。

| Task                             | 难度   | Reward | Verifier passed/total | 结束状态  | 模型响应 | Tool calls | Agent 时间 |
| -------------------------------- | ------ | -----: | --------------------: | --------- | -------: | ---------: | ---------: |
| adaptive-rejection-sampler       | Medium |      1 |                   9/9 | completed |       25 |         24 |  13.78 min |
| bn-fit-modify                    | Hard   |      1 |                   9/9 | completed |       28 |         27 |  20.06 min |
| break-filter-js-from-html        | Medium |      1 |                   1/1 | completed |       32 |         32 |  11.82 min |
| build-cython-ext                 | Medium |      0 |                 10/11 | max_turn  |       90 |        104 |  14.29 min |
| build-pmars                      | Medium |      1 |                   4/4 | completed |       28 |         33 |   4.99 min |
| build-pov-ray                    | Medium |      0 |                   2/3 | completed |       54 |         81 |  20.29 min |
| caffe-cifar-10                   | Medium |      0 |                   1/6 | max_turn  |       90 |         90 |  37.17 min |
| cancel-async-tasks               | Hard   |      0 |                   5/6 | timeout   |       72 |         72 |  15.14 min |
| chess-best-move                  | Medium |      0 |                   0/1 | timeout   |       22 |         22 |  15.36 min |
| circuit-fibsqrt                  | Hard   |      0 |                   2/3 | completed |        1 |          2 |  27.61 min |
| cobol-modernization              | Easy   |      0 |                   1/3 | timeout   |       13 |         21 |  15.19 min |
| code-from-image                  | Medium |      1 |                   2/2 | completed |        5 |          4 |   1.50 min |
| compile-compcert                 | Medium |      0 |                   0/3 | timeout   |       43 |         45 |  40.42 min |
| configure-git-webserver          | Hard   |      1 |                   1/1 | completed |       14 |         13 |   2.06 min |
| constraints-scheduling           | Medium |      1 |                   3/3 | completed |        6 |          7 |   2.54 min |
| count-dataset-tokens             | Medium |      0 |                   0/1 | timeout   |       23 |         23 |  15.34 min |
| crack-7z-hash                    | Medium |      1 |                   2/2 | completed |       66 |         65 |  24.51 min |
| custom-memory-heap-crash         | Medium |      0 |                   5/6 | max_turn  |       90 |         98 |  18.95 min |
| db-wal-recovery                  | Medium |      0 |                   0/7 | timeout   |       83 |         83 |  15.13 min |
| distribution-search              | Medium |      1 |                   4/4 | completed |        8 |          7 |   8.52 min |
| dna-assembly                     | Hard   |      1 |                   1/1 | completed |       28 |         30 |  22.48 min |
| dna-insert                       | Medium |      0 |                   0/1 | completed |       23 |         22 |   5.95 min |
| extract-elf                      | Medium |      1 |                   2/2 | completed |       11 |         14 |   8.55 min |
| extract-moves-from-video         | Hard   |      0 |                   0/2 | timeout   |       25 |         32 |  30.16 min |
| feal-differential-cryptanalysis  | Hard   |      1 |                   1/1 | completed |        7 |          6 |   5.75 min |
| feal-linear-cryptanalysis        | Hard   |      1 |                   1/1 | completed |       18 |         20 |  19.90 min |
| filter-js-from-html              | Medium |      0 |                   1/2 | completed |        7 |          6 |   1.48 min |
| financial-document-processor     | Medium |      1 |                   7/7 | completed |       21 |         20 |  10.24 min |
| fix-code-vulnerability           | Hard   |      1 |                   6/6 | completed |       30 |         32 |   3.88 min |
| fix-git                          | Easy   |      1 |                   2/2 | completed |        8 |          7 |   1.11 min |
| fix-ocaml-gc                     | Hard   |      1 |                   1/1 | timeout   |       34 |         39 |  60.32 min |
| gcode-to-text                    | Medium |      0 |                   0/2 | timeout   |       31 |         31 |  15.15 min |
| git-leak-recovery                | Medium |      1 |                   5/5 | completed |        8 |         12 |   1.20 min |
| git-multibranch                  | Medium |      1 |                   1/1 | completed |       33 |         32 |   4.95 min |
| gpt2-codegolf                    | Hard   |      0 |                   0/1 | timeout   |       20 |         25 |  15.31 min |
| headless-terminal                | Medium |      0 |                   6/7 | completed |       21 |         21 |   3.71 min |
| hf-model-inference               | Medium |      1 |                   4/4 | completed |       13 |         14 |   7.18 min |
| install-windows-3.11             | Hard   |      0 |                   3/4 | completed |       44 |         43 |   9.17 min |
| kv-store-grpc                    | Medium |      0 |                   5/7 | completed |       10 |         10 |   1.99 min |
| large-scale-text-editing         | Medium |      1 |                   5/5 | completed |       11 |         13 |   6.41 min |
| largest-eigenval                 | Medium |      0 |                   2/3 | completed |       11 |         11 |   2.85 min |
| llm-inference-batching-scheduler | Hard   |      1 |                   6/6 | completed |       16 |         18 |  23.02 min |
| log-summary-date-ranges          | Medium |      1 |                   2/2 | completed |        7 |          6 |   1.18 min |
| mailman                          | Medium |      0 |                   2/3 | completed |       89 |        104 |  15.54 min |
| make-doom-for-mips               | Hard   |      0 |                   0/3 | timeout   |       82 |         93 |  15.16 min |
| make-mips-interpreter            | Hard   |      0 |                   0/3 | timeout   |       68 |        110 |  30.15 min |
| mcmc-sampling-stan               | Hard   |      0 |                   2/6 | timeout   |       26 |         29 |  30.31 min |
| merge-diff-arc-agi-task          | Medium |      1 |                   5/5 | completed |       21 |         22 |   5.96 min |
| model-extraction-relu-logits     | Hard   |      0 |                   0/1 | timeout   |       15 |         15 |  15.14 min |
| modernize-scientific-stack       | Medium |      1 |                   2/2 | completed |       20 |         21 |   3.66 min |
| mteb-leaderboard                 | Medium |      0 |                   0/2 | max_turn  |       90 |         90 |  21.90 min |
| mteb-retrieve                    | Medium |      0 |                   0/2 | max_turn  |       90 |         91 |  22.73 min |
| multi-source-data-merger         | Medium |      1 |                   3/3 | completed |       13 |         16 |   2.58 min |
| nginx-request-logging            | Medium |      1 |                   8/8 | completed |       11 |         16 |   2.02 min |
| openssl-selfsigned-cert          | Medium |      0 |                   5/6 | completed |       18 |         17 |   2.51 min |
| overfull-hbox                    | Easy   |      0 |                   3/4 | timeout   |       32 |         35 |  12.62 min |
| password-recovery                | Hard   |      1 |                   2/2 | completed |       25 |         30 |   6.49 min |
| path-tracing                     | Hard   |      0 |                   0/5 | timeout   |       30 |         32 |  30.38 min |
| path-tracing-reverse             | Hard   |      1 |                   3/3 | completed |       53 |         64 |  28.70 min |
| polyglot-c-py                    | Medium |      1 |                   1/1 | completed |        9 |         11 |   3.91 min |
| polyglot-rust-c                  | Hard   |      1 |                   1/1 | completed |       13 |         16 |   9.21 min |
| portfolio-optimization           | Medium |      0 |                   1/4 | completed |       16 |         19 |   9.67 min |
| protein-assembly                 | Hard   |      0 |                   0/1 | timeout   |       32 |         34 |  30.14 min |
| prove-plus-comm                  | Easy   |      1 |                   4/4 | completed |        5 |          5 |   0.96 min |
| pypi-server                      | Medium |      1 |                   1/1 | completed |       22 |         21 |   3.38 min |
| pytorch-model-cli                | Medium |      1 |                   6/6 | completed |       32 |         35 |  11.63 min |
| pytorch-model-recovery           | Medium |      1 |                   5/5 | completed |       22 |         21 |  12.80 min |
| qemu-alpine-ssh                  | Medium |      0 |                   0/1 | timeout   |       54 |         55 |  15.35 min |
| qemu-startup                     | Medium |      1 |                   1/1 | completed |        8 |          7 |   2.83 min |
| query-optimize                   | Medium |      0 |                   5/6 | completed |       25 |         26 |   7.38 min |
| raman-fitting                    | Medium |      0 |                   1/3 | timeout   |       27 |         27 |  15.15 min |
| regex-chess                      | Hard   |      0 |                   0/4 | completed |        1 |          2 |  37.70 min |
| regex-log                        | Medium |      1 |                   1/1 | timeout   |        3 |          3 |  15.14 min |
| reshard-c4-data                  | Medium |      0 |                   0/1 | max_turn  |       90 |         96 |  25.29 min |
| rstan-to-pystan                  | Medium |      1 |                   6/6 | completed |       39 |         40 |  13.80 min |
| sam-cell-seg                     | Hard   |      1 |                   9/9 | completed |       36 |         39 |  20.38 min |
| sanitize-git-repo                | Medium |      1 |                   3/3 | completed |       47 |         76 |  10.38 min |
| schemelike-metacircular-eval     | Medium |      1 |                   1/1 | timeout   |       42 |         45 |  40.33 min |
| sparql-university                | Hard   |      1 |                   3/3 | completed |       15 |         14 |   7.59 min |
| sqlite-db-truncate               | Medium |      1 |                   1/1 | completed |        7 |          9 |   1.98 min |
| sqlite-with-gcov                 | Medium |      0 |                   2/3 | completed |       26 |         29 |  10.02 min |
| torch-pipeline-parallelism       | Hard   |      0 |                   0/3 | timeout   |        6 |          7 |  15.33 min |
| torch-tensor-parallelism         | Hard   |      1 |                   3/3 | completed |       13 |         12 |   7.99 min |
| train-fasttext                   | Hard   |      0 |                   1/2 | timeout   |       42 |         43 |  60.40 min |
| tune-mjcf                        | Medium |      0 |                   3/4 | timeout   |       24 |         26 |  15.30 min |
| video-processing                 | Hard   |      0 |                   3/5 | completed |       72 |         82 |  37.04 min |
| vulnerable-secret                | Medium |      1 |                   3/3 | completed |       13 |         12 |   2.05 min |
| winning-avg-corewars             | Medium |      0 |                   2/3 | max_turn  |       88 |         92 |  41.13 min |
| write-compressor                 | Hard   |      0 |                   0/3 | timeout   |        1 |          2 |  15.12 min |

## 数据来源

- work/linux-terminal-bench/hermes/jobs/
- work/linux-terminal-bench/hermes/state/resource.queue.tsv
- work/terminal-bench-2-1/tasks/*/task.toml
- 每个选中 trial 的 result.json、verifier/ctrf.json 与产品原生 trace
