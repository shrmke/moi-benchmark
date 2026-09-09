# Hermes：Terminal-Bench 2.1 Linux 最新 89 题结果汇总

生成日期：2026-09-09
范围：Linux 89 题完整 cohort（包含 tune-mjcf）；每题取匹配 Hermes 当前固定产品参数的最新 finished_at 记录。

## 结论摘要

| 指标               |    结果 |
| ------------------ | ------: |
| 任务覆盖           | 89 / 89 |
| 有效 verifier      | 89 / 89 |
| verifier pass      |      34 |
| verifier no-pass   |      55 |
| verifier pass rate |  38.20% |
| completed          |      52 |
| timeout            |      28 |
| max_turn           |       5 |
| max_token          |       4 |

Hermes 当前 verifier 得分为 **34/89（38.20%）**。89 题均有二元 reward 和有效 CTRF verifier 报告。

## 统计口径与配置

- 任务全集来自 work/linux-terminal-bench/hermes/state/resource.queue.tsv，共 89 题并包含 tune-mjcf。
- jobs 目录共有 143 个匹配当前产品 profile 且带 result.json 的 trial；44 题有重跑；每题按 finished_at、再按路径排序取最新记录。
- 产品版本为 v2026.7.20，预构建源 commit 为 3ef6bbd201263d354fd83ec55b3c306ded2eb72a；模型为 zai/glm-5.2；reasoning_effort=high；temperature=0；上限为 90 turns。
- 产品 wall-clock timeout 为数据集 [agent].timeout_sec × 1.0；Harbor agent_timeout_multiplier=1.25 仅为结束处理预留时间，不增加产品预算。
- 单产品任务并行上限为 3，同时受 3 个 memory token / 6 CPU 的统一资源调度约束。
- 数据集 commit：5c8eadf1f393183288fa08b8f73ca9a469cc5e00。
- 最新结果时间范围：2026-09-08T07:32:25.013990Z 至 2026-09-09T06:07:44.908398Z。
- 可用尝试次数分布：1 次=45 题，2 次=35 题，3 次=8 题，4 次=1 题。

## 运行结束状态

| 结束状态  | 任务数 | verifier pass | verifier no-pass |
| --------- | -----: | ------------: | ---------------: |
| completed |     52 |            32 |               20 |
| timeout   |     28 |             2 |               26 |
| max_turn  |      5 |             0 |                5 |
| max_token |      4 |             0 |                4 |

## Verifier 条目进度

| 指标                       |    结果 |
| -------------------------- | ------: |
| 有 passed/total 明细的任务 | 89 / 89 |
| verifier 条目累计通过      |     194 |
| verifier 条目累计执行      |     308 |
| 条目级通过比例（诊断项）   |   63.0% |

条目级比例不能替代任务级 reward：不同任务的 verifier 条目数量不同，Terminal-Bench 的任务得分仍以每题最终二元 reward 为准。

## 按作者难度

| 难度   | pass / tasks | pass rate |
| ------ | -----------: | --------: |
| Easy   |        2 / 4 |     50.0% |
| Medium |      21 / 55 |     38.2% |
| Hard   |      11 / 30 |     36.7% |

## 时间、模型响应、Tool 与 Token

| 指标                        |  覆盖 |    总计 |    中位数 |       P90 |
| --------------------------- | ----: | ------: | --------: | --------: |
| 端到端时间                  | 89/89 | 26.14 h | 16.07 min | 31.86 min |
| Agent 执行时间              | 89/89 | 22.77 h | 14.29 min | 30.43 min |
| Verifier 时间               | 89/89 |  2.61 h |  0.60 min |  3.87 min |
| 模型响应/API calls（trace） | 89/89 |    2521 |      23.0 |      72.0 |
| Tool calls（trace）         | 89/89 |    2809 |      23.0 |      81.2 |

Hermes Tool calls 直接从每题最新 trial 的产品原生 trace 提取，不使用 result 元数据的缺省值。

| Token 分量 / 统计口径                          | 有 usage 的任务 | 已观测总量 |
| ---------------------------------------------- | --------------: | ---------: |
| fresh input                                    |           89/89 |  5,651,935 |
| cache read                                     |           89/89 | 71,739,840 |
| output                                         |           89/89 |  1,393,760 |
| reasoning（单列）                              |           89/89 |    842,813 |
| input + cache read + output                    |           89/89 | 78,785,535 |
| Harbor result.json usage（n_input + n_output） |           61/89 | 50,093,267 |

Hermes Token 从 `agent/hermes-session.jsonl` 累加，89/89 题均有会话汇总。reasoning token 单列，不再次加入 input + cache read + output，避免其是否已包含在 output 中造成重复计算。Harbor result usage 来自每题最新 `result.json` 的 `agent_result`；总量按 `n_input_tokens + n_output_tokens` 计算。

## 每题结果与 verifier 进度

passed/total 表示该题 CTRF verifier 明细中通过条目数 / 总条目数；Tool calls 均来自产品原生 trace。

| Task                             | 难度   | Reward | Verifier passed/total | 结束状态  | 模型响应 | Tool calls | Agent 时间 |
| -------------------------------- | ------ | -----: | --------------------: | --------- | -------: | ---------: | ---------: |
| adaptive-rejection-sampler       | Medium |      0 |                   0/9 | timeout   |        3 |          4 |  15.14 min |
| bn-fit-modify                    | Hard   |      1 |                   9/9 | completed |       28 |         27 |  20.06 min |
| break-filter-js-from-html        | Medium |      1 |                   1/1 | completed |       32 |         32 |  11.82 min |
| build-cython-ext                 | Medium |      0 |                 10/11 | max_turn  |       90 |        104 |  14.29 min |
| build-pmars                      | Medium |      1 |                   4/4 | completed |       28 |         33 |   4.99 min |
| build-pov-ray                    | Medium |      0 |                   2/3 | completed |       54 |         81 |  20.29 min |
| caffe-cifar-10                   | Medium |      0 |                   1/6 | max_turn  |       90 |         90 |  37.17 min |
| cancel-async-tasks               | Hard   |      0 |                   5/6 | timeout   |       72 |         72 |  15.14 min |
| chess-best-move                  | Medium |      0 |                   0/1 | timeout   |       22 |         22 |  15.36 min |
| circuit-fibsqrt                  | Hard   |      0 |                   2/3 | max_token |        1 |          2 |  30.61 min |
| cobol-modernization              | Easy   |      0 |                   1/3 | timeout   |        9 |         14 |  15.13 min |
| code-from-image                  | Medium |      1 |                   2/2 | completed |        5 |          4 |   1.50 min |
| compile-compcert                 | Medium |      0 |                   0/3 | timeout   |       43 |         45 |  40.42 min |
| configure-git-webserver          | Hard   |      0 |                   0/1 | completed |       13 |         12 |   1.88 min |
| constraints-scheduling           | Medium |      1 |                   3/3 | completed |        6 |          7 |   2.54 min |
| count-dataset-tokens             | Medium |      0 |                   0/1 | timeout   |       23 |         23 |  15.34 min |
| crack-7z-hash                    | Medium |      1 |                   2/2 | completed |       66 |         65 |  24.51 min |
| custom-memory-heap-crash         | Medium |      0 |                   5/6 | max_turn  |       90 |         98 |  18.95 min |
| db-wal-recovery                  | Medium |      0 |                   0/7 | timeout   |       83 |         83 |  15.13 min |
| distribution-search              | Medium |      1 |                   4/4 | completed |        8 |          7 |   8.52 min |
| dna-assembly                     | Hard   |      1 |                   1/1 | completed |       28 |         30 |  22.48 min |
| dna-insert                       | Medium |      0 |                   0/1 | completed |       23 |         22 |   5.95 min |
| extract-elf                      | Medium |      0 |                   0/2 | timeout   |       11 |         17 |  15.16 min |
| extract-moves-from-video         | Hard   |      0 |                   0/2 | timeout   |       25 |         32 |  30.16 min |
| feal-differential-cryptanalysis  | Hard   |      1 |                   1/1 | completed |        7 |          6 |   5.75 min |
| feal-linear-cryptanalysis        | Hard   |      0 |                   0/1 | max_token |        1 |          4 |  29.42 min |
| filter-js-from-html              | Medium |      0 |                   1/2 | completed |        7 |          6 |   1.48 min |
| financial-document-processor     | Medium |      1 |                   7/7 | completed |       21 |         20 |  10.24 min |
| fix-code-vulnerability           | Hard   |      1 |                   6/6 | completed |       30 |         32 |   3.88 min |
| fix-git                          | Easy   |      1 |                   2/2 | completed |        8 |          7 |   1.11 min |
| fix-ocaml-gc                     | Hard   |      1 |                   1/1 | timeout   |       34 |         39 |  60.32 min |
| gcode-to-text                    | Medium |      0 |                   0/2 | timeout   |       31 |         31 |  15.15 min |
| git-leak-recovery                | Medium |      1 |                   5/5 | completed |        8 |         12 |   1.20 min |
| git-multibranch                  | Medium |      0 |                   0/1 | completed |       32 |         31 |   5.21 min |
| gpt2-codegolf                    | Hard   |      0 |                   0/1 | timeout   |        0 |          0 |  15.13 min |
| headless-terminal                | Medium |      0 |                   6/7 | completed |       21 |         21 |   3.71 min |
| hf-model-inference               | Medium |      0 |                   2/4 | completed |       16 |         18 |   6.98 min |
| install-windows-3.11             | Hard   |      0 |                   1/4 | completed |       64 |         69 |  22.14 min |
| kv-store-grpc                    | Medium |      0 |                   5/7 | completed |       10 |         10 |   1.99 min |
| large-scale-text-editing         | Medium |      1 |                   5/5 | completed |       11 |         13 |   6.41 min |
| largest-eigenval                 | Medium |      0 |                   2/3 | completed |       11 |         11 |   2.85 min |
| llm-inference-batching-scheduler | Hard   |      1 |                   6/6 | completed |       16 |         18 |  23.02 min |
| log-summary-date-ranges          | Medium |      1 |                   2/2 | completed |        7 |          6 |   1.18 min |
| mailman                          | Medium |      0 |                   2/3 | completed |       89 |        104 |  15.54 min |
| make-doom-for-mips               | Hard   |      0 |                   0/3 | timeout   |       82 |         93 |  15.16 min |
| make-mips-interpreter            | Hard   |      0 |                   0/3 | timeout   |       36 |         52 |  30.19 min |
| mcmc-sampling-stan               | Hard   |      0 |                   2/6 | timeout   |       26 |         29 |  30.31 min |
| merge-diff-arc-agi-task          | Medium |      1 |                   5/5 | completed |       21 |         22 |   5.96 min |
| model-extraction-relu-logits     | Hard   |      0 |                   0/1 | timeout   |       15 |         15 |  15.14 min |
| modernize-scientific-stack       | Medium |      1 |                   2/2 | completed |       20 |         21 |   3.66 min |
| mteb-leaderboard                 | Medium |      0 |                   0/2 | max_turn  |       90 |         90 |  21.90 min |
| mteb-retrieve                    | Medium |      0 |                   0/2 | max_turn  |       90 |         91 |  22.73 min |
| multi-source-data-merger         | Medium |      1 |                   3/3 | completed |       13 |         16 |   2.58 min |
| nginx-request-logging            | Medium |      0 |                   3/8 | completed |       16 |         19 |   2.51 min |
| openssl-selfsigned-cert          | Medium |      0 |                   5/6 | completed |       18 |         17 |   2.51 min |
| overfull-hbox                    | Easy   |      0 |                   3/4 | timeout   |       32 |         35 |  12.62 min |
| password-recovery                | Hard   |      1 |                   2/2 | completed |       25 |         30 |   6.49 min |
| path-tracing                     | Hard   |      0 |                   0/5 | timeout   |       30 |         32 |  30.38 min |
| path-tracing-reverse             | Hard   |      0 |                   0/3 | timeout   |       33 |         42 |  30.17 min |
| polyglot-c-py                    | Medium |      1 |                   1/1 | completed |        9 |         11 |   3.91 min |
| polyglot-rust-c                  | Hard   |      1 |                   1/1 | completed |       13 |         16 |   9.21 min |
| portfolio-optimization           | Medium |      0 |                   1/4 | completed |       16 |         19 |   9.67 min |
| protein-assembly                 | Hard   |      0 |                   0/1 | timeout   |       32 |         34 |  30.14 min |
| prove-plus-comm                  | Easy   |      1 |                   4/4 | completed |        5 |          5 |   0.96 min |
| pypi-server                      | Medium |      0 |                   0/1 | completed |       38 |         37 |   4.64 min |
| pytorch-model-cli                | Medium |      1 |                   6/6 | completed |       32 |         35 |  11.63 min |
| pytorch-model-recovery           | Medium |      1 |                   5/5 | completed |       22 |         21 |  12.80 min |
| qemu-alpine-ssh                  | Medium |      0 |                   0/1 | timeout   |       54 |         55 |  15.35 min |
| qemu-startup                     | Medium |      0 |                   0/1 | completed |       29 |         28 |   6.20 min |
| query-optimize                   | Medium |      0 |                   5/6 | completed |       25 |         26 |   7.38 min |
| raman-fitting                    | Medium |      0 |                   1/3 | timeout   |       27 |         27 |  15.15 min |
| regex-chess                      | Hard   |      0 |                   0/4 | max_token |        8 |          8 |  42.01 min |
| regex-log                        | Medium |      1 |                   1/1 | timeout   |        3 |          3 |  15.14 min |
| reshard-c4-data                  | Medium |      0 |                   0/1 | max_token |        6 |          8 |  43.88 min |
| rstan-to-pystan                  | Medium |      1 |                   6/6 | completed |       39 |         40 |  13.80 min |
| sam-cell-seg                     | Hard   |      1 |                   9/9 | completed |       36 |         39 |  20.38 min |
| sanitize-git-repo                | Medium |      1 |                   3/3 | completed |       47 |         76 |  10.38 min |
| schemelike-metacircular-eval     | Medium |      0 |                   0/1 | timeout   |        9 |         36 |  40.15 min |
| sparql-university                | Hard   |      1 |                   3/3 | completed |       15 |         14 |   7.59 min |
| sqlite-db-truncate               | Medium |      1 |                   1/1 | completed |        7 |          9 |   1.98 min |
| sqlite-with-gcov                 | Medium |      0 |                   2/3 | completed |       26 |         29 |  10.02 min |
| torch-pipeline-parallelism       | Hard   |      0 |                   0/3 | timeout   |        6 |          7 |  15.33 min |
| torch-tensor-parallelism         | Hard   |      1 |                   3/3 | completed |       13 |         12 |   7.99 min |
| train-fasttext                   | Hard   |      0 |                   1/2 | timeout   |       42 |         43 |  60.40 min |
| tune-mjcf                        | Medium |      0 |                   3/4 | timeout   |       24 |         26 |  15.30 min |
| video-processing                 | Hard   |      0 |                   3/5 | completed |       72 |         82 |  37.04 min |
| vulnerable-secret                | Medium |      1 |                   3/3 | completed |       13 |         12 |   2.05 min |
| winning-avg-corewars             | Medium |      0 |                   1/3 | completed |       28 |         61 |  12.79 min |
| write-compressor                 | Hard   |      0 |                   0/3 | timeout   |        1 |          2 |  15.16 min |

## 数据来源

- work/linux-terminal-bench/hermes/jobs/
- work/linux-terminal-bench/hermes/state/resource.queue.tsv
- work/terminal-bench-2-1/tasks/*/task.toml
- 每个选中 trial 的 result.json、verifier/ctrf.json 与产品原生 trace
