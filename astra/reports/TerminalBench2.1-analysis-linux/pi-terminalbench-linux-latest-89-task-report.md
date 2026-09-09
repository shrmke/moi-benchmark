# PI：Terminal-Bench 2.1 Linux 最新 89 题结果汇总

生成日期：2026-09-09
范围：Linux 89 题完整 cohort（包含 tune-mjcf）；每题取匹配 PI 当前固定产品参数的最新 finished_at 记录。

## 结论摘要

| 指标               |    结果 |
| ------------------ | ------: |
| 任务覆盖           | 89 / 89 |
| 有效 verifier      | 89 / 89 |
| verifier pass      |      52 |
| verifier no-pass   |      37 |
| verifier pass rate |  58.43% |
| completed          |      55 |
| timeout            |      32 |
| max_turn           |       0 |
| max_token          |       2 |

PI 当前 verifier 得分为 **52/89（58.43%）**。89 题均有二元 reward 和有效 CTRF verifier 报告。

## 统计口径与配置

- 任务全集来自 work/linux-terminal-bench/pi/state/resource.queue.tsv，共 89 题并包含 tune-mjcf。
- jobs 目录共有 101 个匹配当前产品 profile 且带 result.json 的 trial；6 题有重跑；每题按 finished_at、再按路径排序取最新记录。
- 产品版本为 0.73.1；模型为 zai/glm-5.2；thinking=high。PI 无独立 max-turn 参数。
- 产品 wall-clock timeout 为数据集 [agent].timeout_sec × 1.0；Harbor agent_timeout_multiplier=1.25 仅为结束处理预留时间，不增加产品预算。
- 单产品任务并行上限为 3，同时受 3 个 memory token / 6 CPU 的统一资源调度约束。
- 数据集 commit：5c8eadf1f393183288fa08b8f73ca9a469cc5e00。
- 最新结果时间范围：2026-09-06T19:36:20.360963Z 至 2026-09-07T18:09:27.126454Z。
- 可用尝试次数分布：1 次=83 题，2 次=1 题，3 次=4 题，4 次=1 题。
- PI 旧记录中缺少规范结束事件的 extract-moves-from-video 以 return_code=137 中断；按本报告限定的四类状态归入 timeout，其原始原因保留在 CSV 的 native_finish_reason。

## 运行结束状态

| 结束状态  | 任务数 | verifier pass | verifier no-pass |
| --------- | -----: | ------------: | ---------------: |
| completed |     55 |            43 |               12 |
| timeout   |     32 |             9 |               23 |
| max_turn  |      0 |             0 |                0 |
| max_token |      2 |             0 |                2 |

## Verifier 条目进度

| 指标                       |    结果 |
| -------------------------- | ------: |
| 有 passed/total 明细的任务 | 89 / 89 |
| verifier 条目累计通过      |     228 |
| verifier 条目累计执行      |     308 |
| 条目级通过比例（诊断项）   |   74.0% |

条目级比例不能替代任务级 reward：不同任务的 verifier 条目数量不同，Terminal-Bench 的任务得分仍以每题最终二元 reward 为准。

## 按作者难度

| 难度   | pass / tasks | pass rate |
| ------ | -----------: | --------: |
| Easy   |        3 / 4 |     75.0% |
| Medium |      35 / 55 |     63.6% |
| Hard   |      14 / 30 |     46.7% |

## 时间、模型响应、Tool 与 Token

| 指标                        |  覆盖 |    总计 |    中位数 |       P90 |
| --------------------------- | ----: | ------: | --------: | --------: |
| 端到端时间                  | 89/89 | 28.88 h | 16.19 min | 36.40 min |
| Agent 执行时间              | 89/89 | 24.52 h | 15.12 min | 30.38 min |
| Verifier 时间               | 89/89 |  3.26 h |  0.61 min |  6.37 min |
| 模型响应/API calls（trace） | 89/89 |    1968 |      17.0 |      45.2 |
| Tool calls（trace）         | 89/89 |    2262 |      19.0 |      54.0 |

PI Tool calls 直接从每题最新 trial 的产品原生 trace 提取，不使用 result 元数据的缺省值。

| Token 分量 / 统计口径                          | 有 usage 的任务 | 已观测总量 |
| ---------------------------------------------- | --------------: | ---------: |
| fresh input                                    |           88/89 | 16,216,107 |
| cache read                                     |           88/89 | 47,470,464 |
| output                                         |           88/89 |  3,033,741 |
| input + cache read + output                    |           88/89 | 66,720,312 |
| Harbor result.json usage（n_input + n_output） |           89/89 | 66,708,960 |

PI Token 从 `agent/pi-sessions/*.jsonl` 的 assistant message usage 累加；无会话导出的 adaptive-rejection-sampler 使用 `agent/pi.txt`，其中没有完成的 assistant 响应，计数为 0。Harbor result usage 来自每题最新 `result.json` 的 `agent_result`；`n_input_tokens` 已包含 cache read，因此总量按 `n_input_tokens + n_output_tokens` 计算，不重复加入 `n_cache_tokens`。

## 每题结果与 verifier 进度

passed/total 表示该题 CTRF verifier 明细中通过条目数 / 总条目数；Tool calls 均来自产品原生 trace。

| Task                             | 难度   | Reward | Verifier passed/total | 结束状态  | 模型响应 | Tool calls | Agent 时间 |
| -------------------------------- | ------ | -----: | --------------------: | --------- | -------: | ---------: | ---------: |
| adaptive-rejection-sampler       | Medium |      0 |                   0/9 | timeout   |        0 |          0 |  15.12 min |
| bn-fit-modify                    | Hard   |      1 |                   9/9 | completed |       16 |         16 |   5.66 min |
| break-filter-js-from-html        | Medium |      1 |                   1/1 | completed |       17 |         18 |   4.11 min |
| build-cython-ext                 | Medium |      1 |                 11/11 | timeout   |       45 |         59 |  15.13 min |
| build-pmars                      | Medium |      1 |                   4/4 | completed |       25 |         33 |   6.37 min |
| build-pov-ray                    | Medium |      0 |                   2/3 | completed |       91 |         90 |  31.31 min |
| caffe-cifar-10                   | Medium |      1 |                   6/6 | completed |       32 |         49 |  51.69 min |
| cancel-async-tasks               | Hard   |      1 |                   6/6 | completed |       18 |         20 |   8.71 min |
| chess-best-move                  | Medium |      0 |                   0/1 | timeout   |       11 |         12 |  15.13 min |
| circuit-fibsqrt                  | Hard   |      0 |                   2/3 | max_token |        3 |          2 |  19.31 min |
| cobol-modernization              | Easy   |      0 |                   1/3 | timeout   |       14 |         16 |  15.13 min |
| code-from-image                  | Medium |      1 |                   2/2 | completed |       19 |         18 |   5.87 min |
| compile-compcert                 | Medium |      1 |                   3/3 | timeout   |       53 |         59 |  40.09 min |
| configure-git-webserver          | Hard   |      0 |                   0/1 | completed |       15 |         17 |  10.62 min |
| constraints-scheduling           | Medium |      1 |                   3/3 | completed |        6 |          8 |   4.84 min |
| count-dataset-tokens             | Medium |      0 |                   0/1 | timeout   |       16 |         16 |  15.12 min |
| crack-7z-hash                    | Medium |      1 |                   2/2 | completed |       17 |         18 |   8.81 min |
| custom-memory-heap-crash         | Medium |      1 |                   6/6 | completed |       38 |         54 |  21.13 min |
| db-wal-recovery                  | Medium |      1 |                   7/7 | completed |       11 |         13 |   2.46 min |
| distribution-search              | Medium |      1 |                   4/4 | completed |        9 |          8 |   6.78 min |
| dna-assembly                     | Hard   |      0 |                   0/1 | timeout   |        6 |          9 |  30.12 min |
| dna-insert                       | Medium |      1 |                   1/1 | completed |       12 |         14 |  14.37 min |
| extract-elf                      | Medium |      0 |                   0/2 | timeout   |       12 |         23 |  15.14 min |
| extract-moves-from-video         | Hard   |      0 |                   0/2 | timeout   |       30 |         32 |  19.24 min |
| feal-differential-cryptanalysis  | Hard   |      1 |                   1/1 | completed |       15 |         14 |   8.36 min |
| feal-linear-cryptanalysis        | Hard   |      1 |                   1/1 | completed |       19 |         21 |  18.77 min |
| filter-js-from-html              | Medium |      0 |                   1/2 | timeout   |       14 |         14 |  30.13 min |
| financial-document-processor     | Medium |      1 |                   7/7 | completed |       22 |         24 |   9.44 min |
| fix-code-vulnerability           | Hard   |      1 |                   6/6 | completed |       30 |         35 |   3.84 min |
| fix-git                          | Easy   |      1 |                   2/2 | completed |       11 |         20 |   4.60 min |
| fix-ocaml-gc                     | Hard   |      1 |                   1/1 | timeout   |       46 |         54 |  60.14 min |
| gcode-to-text                    | Medium |      0 |                   0/2 | timeout   |       47 |         49 |  15.14 min |
| git-leak-recovery                | Medium |      1 |                   5/5 | completed |       12 |         18 |   3.35 min |
| git-multibranch                  | Medium |      0 |                   0/1 | completed |       21 |         24 |   8.03 min |
| gpt2-codegolf                    | Hard   |      0 |                   0/1 | timeout   |        9 |         10 |  15.12 min |
| headless-terminal                | Medium |      0 |                   0/7 | timeout   |        3 |          5 |  15.14 min |
| hf-model-inference               | Medium |      0 |                   1/4 | completed |       12 |         14 |   6.41 min |
| install-windows-3.11             | Hard   |      1 |                   4/4 | timeout   |       96 |        104 |  60.12 min |
| kv-store-grpc                    | Medium |      0 |                   5/7 | completed |        7 |          8 |   2.09 min |
| large-scale-text-editing         | Medium |      1 |                   5/5 | timeout   |       24 |         24 |  20.12 min |
| largest-eigenval                 | Medium |      1 |                   3/3 | timeout   |       28 |         32 |  15.13 min |
| llm-inference-batching-scheduler | Hard   |      0 |                   1/6 | timeout   |       21 |         22 |  30.13 min |
| log-summary-date-ranges          | Medium |      1 |                   2/2 | completed |        5 |          5 |   1.15 min |
| mailman                          | Medium |      1 |                   3/3 | completed |       61 |         77 |  25.08 min |
| make-doom-for-mips               | Hard   |      0 |                   0/3 | timeout   |       40 |         58 |  15.13 min |
| make-mips-interpreter            | Hard   |      0 |                   0/3 | timeout   |       42 |         50 |  30.12 min |
| mcmc-sampling-stan               | Hard   |      1 |                   6/6 | timeout   |       37 |         44 |  30.10 min |
| merge-diff-arc-agi-task          | Medium |      1 |                   5/5 | completed |       18 |         20 |   5.08 min |
| model-extraction-relu-logits     | Hard   |      0 |                   0/1 | timeout   |        2 |          2 |  15.13 min |
| modernize-scientific-stack       | Medium |      1 |                   2/2 | completed |        8 |         10 |   1.89 min |
| mteb-leaderboard                 | Medium |      1 |                   2/2 | completed |       51 |         57 |  34.96 min |
| mteb-retrieve                    | Medium |      0 |                   1/2 | completed |       34 |         45 |  22.20 min |
| multi-source-data-merger         | Medium |      1 |                   3/3 | completed |        7 |          9 |   5.31 min |
| nginx-request-logging            | Medium |      0 |                   3/8 | completed |       12 |         14 |   2.02 min |
| openssl-selfsigned-cert          | Medium |      1 |                   6/6 | completed |       14 |         13 |   2.58 min |
| overfull-hbox                    | Easy   |      1 |                   4/4 | completed |       16 |         19 |   9.31 min |
| password-recovery                | Hard   |      1 |                   2/2 | completed |       12 |         15 |   5.75 min |
| path-tracing                     | Hard   |      0 |                   0/5 | timeout   |       53 |         53 |  30.14 min |
| path-tracing-reverse             | Hard   |      1 |                   3/3 | timeout   |       22 |         27 |  30.13 min |
| polyglot-c-py                    | Medium |      1 |                   1/1 | completed |        7 |          6 |   3.55 min |
| polyglot-rust-c                  | Hard   |      1 |                   1/1 | completed |       12 |         15 |  12.19 min |
| portfolio-optimization           | Medium |      1 |                   4/4 | completed |       17 |         21 |  17.23 min |
| protein-assembly                 | Hard   |      0 |                   0/1 | timeout   |       17 |         22 |  30.15 min |
| prove-plus-comm                  | Easy   |      1 |                   4/4 | completed |        9 |          8 |   1.41 min |
| pypi-server                      | Medium |      0 |                   0/1 | completed |       14 |         16 |   1.65 min |
| pytorch-model-cli                | Medium |      0 |                   5/6 | completed |       19 |         26 |   7.59 min |
| pytorch-model-recovery           | Medium |      1 |                   5/5 | completed |       15 |         14 |   8.58 min |
| qemu-alpine-ssh                  | Medium |      0 |                   0/1 | timeout   |       14 |         17 |  15.13 min |
| qemu-startup                     | Medium |      1 |                   1/1 | timeout   |       28 |         28 |  15.12 min |
| query-optimize                   | Medium |      0 |                   5/6 | completed |       13 |         19 |   9.57 min |
| raman-fitting                    | Medium |      1 |                   3/3 | completed |       21 |         20 |  10.33 min |
| regex-chess                      | Hard   |      0 |                   0/4 | max_token |       11 |         10 |  25.66 min |
| regex-log                        | Medium |      1 |                   1/1 | completed |        9 |          8 |  10.19 min |
| reshard-c4-data                  | Medium |      0 |                   0/1 | completed |       20 |         19 |  16.85 min |
| rstan-to-pystan                  | Medium |      1 |                   6/6 | completed |       29 |         32 |  20.38 min |
| sam-cell-seg                     | Hard   |      1 |                   9/9 | completed |       68 |         72 |  60.50 min |
| sanitize-git-repo                | Medium |      1 |                   3/3 | completed |       22 |         34 |   9.78 min |
| schemelike-metacircular-eval     | Medium |      0 |                   0/1 | timeout   |       27 |         27 |  40.14 min |
| sparql-university                | Hard   |      1 |                   3/3 | completed |       19 |         18 |   9.25 min |
| sqlite-db-truncate               | Medium |      1 |                   1/1 | completed |       10 |         11 |   1.95 min |
| sqlite-with-gcov                 | Medium |      1 |                   3/3 | completed |       22 |         31 |  10.11 min |
| torch-pipeline-parallelism       | Hard   |      0 |                   0/3 | timeout   |       27 |         33 |  15.12 min |
| torch-tensor-parallelism         | Hard   |      1 |                   3/3 | completed |       12 |         12 |  11.16 min |
| train-fasttext                   | Hard   |      0 |                   0/2 | timeout   |       29 |         32 |  60.19 min |
| tune-mjcf                        | Medium |      0 |                   3/4 | timeout   |       26 |         27 |  15.13 min |
| video-processing                 | Hard   |      0 |                   4/5 | completed |       15 |         17 |  19.34 min |
| vulnerable-secret                | Medium |      1 |                   3/3 | completed |       17 |         16 |   2.53 min |
| winning-avg-corewars             | Medium |      1 |                   3/3 | completed |       25 |         29 |  21.15 min |
| write-compressor                 | Hard   |      0 |                   2/3 | timeout   |        7 |          8 |  15.12 min |

## 数据来源

- work/linux-terminal-bench/pi/jobs/
- work/linux-terminal-bench/pi/state/resource.queue.tsv
- work/terminal-bench-2-1/tasks/*/task.toml
- 每个选中 trial 的 result.json、verifier/ctrf.json 与产品原生 trace
