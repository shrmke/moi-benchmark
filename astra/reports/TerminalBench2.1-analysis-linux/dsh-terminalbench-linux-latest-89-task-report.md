# DSH：Terminal-Bench 2.1 Linux 最新 89 题结果汇总

生成日期：2026-09-09
范围：Linux 89 题完整 cohort（包含 tune-mjcf）；每题取匹配 DSH 当前固定产品参数的最新 finished_at 记录。

## 结论摘要

| 指标 | 结果 |
| --- | ---: |
| 任务覆盖 | 89 / 89 |
| 有效 verifier | 89 / 89 |
| verifier pass | 49 |
| verifier no-pass | 40 |
| verifier pass rate | 55.06% |
| completed | 60 |
| timeout | 21 |
| max_turn | 7 |
| max_token | 1 |

DSH 当前 verifier 得分为 **49/89（55.06%）**。89 题均有二元 reward 和有效 CTRF verifier 报告。

## 统计口径与配置

- 任务全集来自 work/linux-terminal-bench/dsh/state/resource.queue.tsv，共 89 题并包含 tune-mjcf。
- jobs 目录共有 133 个匹配当前产品 profile 且带 result.json 的 trial；32 题有重跑；每题按 finished_at、再按路径排序取最新记录。
- 产品版本为 0.1.0rc6；模型为 zai/glm-5.2；temperature=0；上限为 50 个模型请求 step。
- 产品 wall-clock timeout 为数据集 [agent].timeout_sec × 1.0；Harbor agent_timeout_multiplier=1.25 仅为结束处理预留时间，不增加产品预算。
- 单产品任务并行上限为 3，同时受 3 个 memory token / 6 CPU 的统一资源调度约束。
- 数据集 commit：5c8eadf1f393183288fa08b8f73ca9a469cc5e00。
- 最新结果时间范围：2026-09-02T11:12:46.241815Z 至 2026-09-03T18:35:49.277983Z。
- 可用尝试次数分布：1 次=57 题，2 次=23 题，3 次=6 题，4 次=3 题。

## 运行结束状态

| 结束状态 | 任务数 | verifier pass | verifier no-pass |
| --- | ---: | ---: | ---: |
| completed | 60 | 44 | 16 |
| timeout | 21 | 4 | 17 |
| max_turn | 7 | 1 | 6 |
| max_token | 1 | 0 | 1 |

## Verifier 条目进度

| 指标 | 结果 |
| --- | ---: |
| 有 passed/total 明细的任务 | 89 / 89 |
| verifier 条目累计通过 | 222 |
| verifier 条目累计执行 | 308 |
| 条目级通过比例（诊断项） | 72.1% |

条目级比例不能替代任务级 reward：不同任务的 verifier 条目数量不同，Terminal-Bench 的任务得分仍以每题最终二元 reward 为准。

## 按作者难度

| 难度 | pass / tasks | pass rate |
| --- | ---: | ---: |
| Easy | 4 / 4 | 100.0% |
| Medium | 31 / 55 | 56.4% |
| Hard | 14 / 30 | 46.7% |

## 时间、模型响应、Tool 与 Token

| 指标 | 覆盖 | 总计 | 中位数 | P90 |
| --- | ---: | ---: | ---: | ---: |
| 端到端时间 | 89/89 | 23.39 h | 14.66 min | 31.12 min |
| Agent 执行时间 | 89/89 | 19.02 h | 10.75 min | 27.16 min |
| Verifier 时间 | 89/89 | 3.01 h | 0.54 min | 5.61 min |
| 模型响应/API calls（trace） | 89/89 | 1954 | 19.0 | 42.4 |
| Tool calls（trace） | 89/89 | 2050 | 20.0 | 48.4 |

DSH Tool calls 直接从每题最新 trial 的产品原生 trace 提取，不使用 result 元数据的缺省值。

| Token 分量 / 统计口径 | 有 usage 的任务 | 已观测总量 |
| --- | ---: | ---: |
| fresh input | 89/89 | 7,992,685 |
| cache read | 89/89 | 24,351,872 |
| output | 89/89 | 1,533,489 |
| input + cache read + output | 89/89 | 33,878,046 |
| Harbor result.json usage（n_input + n_output） | 68/89 | 21,335,078 |

DSH Token 从 `agent/dsh-events.jsonl` 的去重 `assistant/message.data.usage` 累加。89/89 题均有已完成 usage；timeout 在途且未返回的请求不会出现在事件中，因此这是下界。Harbor result usage 来自每题最新 `result.json` 的 `agent_result`；`n_input_tokens` 已包含 cache read，因此总量按 `n_input_tokens + n_output_tokens` 计算，不重复加入 `n_cache_tokens`。

## 每题结果与 verifier 进度

passed/total 表示该题 CTRF verifier 明细中通过条目数 / 总条目数；Tool calls 均来自产品原生 trace。

| Task | 难度 | Reward | Verifier passed/total | 结束状态 | 模型响应 | Tool calls | Agent 时间 |
| --- | --- | ---: | ---: | --- | ---: | ---: | ---: |
| adaptive-rejection-sampler | Medium | 1 | 9/9 | timeout | 6 | 6 | 15.12 min |
| bn-fit-modify | Hard | 1 | 9/9 | completed | 17 | 16 | 14.15 min |
| break-filter-js-from-html | Medium | 1 | 1/1 | completed | 18 | 19 | 6.13 min |
| build-cython-ext | Medium | 0 | 9/11 | max_turn | 50 | 50 | 11.59 min |
| build-pmars | Medium | 1 | 4/4 | completed | 26 | 26 | 4.06 min |
| build-pov-ray | Medium | 1 | 3/3 | max_turn | 50 | 54 | 10.57 min |
| caffe-cifar-10 | Medium | 0 | 2/6 | max_turn | 50 | 50 | 25.88 min |
| cancel-async-tasks | Hard | 1 | 6/6 | completed | 12 | 11 | 3.89 min |
| chess-best-move | Medium | 0 | 0/1 | timeout | 27 | 27 | 15.12 min |
| circuit-fibsqrt | Hard | 1 | 3/3 | completed | 9 | 10 | 18.56 min |
| cobol-modernization | Easy | 1 | 3/3 | completed | 21 | 21 | 7.26 min |
| code-from-image | Medium | 1 | 2/2 | timeout | 40 | 40 | 20.11 min |
| compile-compcert | Medium | 0 | 0/3 | timeout | 27 | 29 | 40.13 min |
| configure-git-webserver | Hard | 0 | 0/1 | completed | 16 | 15 | 2.26 min |
| constraints-scheduling | Medium | 1 | 3/3 | completed | 6 | 7 | 2.39 min |
| count-dataset-tokens | Medium | 0 | 0/1 | timeout | 33 | 33 | 15.12 min |
| crack-7z-hash | Medium | 1 | 2/2 | completed | 28 | 28 | 22.72 min |
| custom-memory-heap-crash | Medium | 1 | 6/6 | completed | 32 | 32 | 10.51 min |
| db-wal-recovery | Medium | 1 | 7/7 | completed | 8 | 9 | 1.45 min |
| distribution-search | Medium | 1 | 4/4 | completed | 6 | 5 | 3.17 min |
| dna-assembly | Hard | 1 | 1/1 | completed | 20 | 24 | 14.35 min |
| dna-insert | Medium | 1 | 1/1 | completed | 21 | 20 | 6.33 min |
| extract-elf | Medium | 0 | 0/2 | completed | 10 | 14 | 4.35 min |
| extract-moves-from-video | Hard | 0 | 0/2 | timeout | 24 | 27 | 30.11 min |
| feal-differential-cryptanalysis | Hard | 1 | 1/1 | completed | 8 | 7 | 10.17 min |
| feal-linear-cryptanalysis | Hard | 1 | 1/1 | completed | 18 | 19 | 18.72 min |
| filter-js-from-html | Medium | 0 | 1/2 | completed | 48 | 48 | 12.09 min |
| financial-document-processor | Medium | 1 | 7/7 | completed | 19 | 19 | 11.90 min |
| fix-code-vulnerability | Hard | 1 | 6/6 | completed | 23 | 27 | 2.94 min |
| fix-git | Easy | 1 | 2/2 | completed | 9 | 8 | 1.35 min |
| fix-ocaml-gc | Hard | 0 | 0/1 | completed | 42 | 51 | 35.22 min |
| gcode-to-text | Medium | 0 | 0/2 | timeout | 40 | 40 | 15.11 min |
| git-leak-recovery | Medium | 1 | 5/5 | completed | 12 | 15 | 1.96 min |
| git-multibranch | Medium | 0 | 0/1 | completed | 22 | 21 | 8.92 min |
| gpt2-codegolf | Hard | 0 | 0/1 | timeout | 17 | 20 | 15.11 min |
| headless-terminal | Medium | 1 | 7/7 | completed | 12 | 11 | 3.58 min |
| hf-model-inference | Medium | 0 | 1/4 | completed | 14 | 13 | 6.06 min |
| install-windows-3.11 | Hard | 0 | 1/4 | completed | 40 | 41 | 14.01 min |
| kv-store-grpc | Medium | 0 | 5/7 | completed | 10 | 10 | 1.49 min |
| large-scale-text-editing | Medium | 1 | 5/5 | completed | 13 | 12 | 9.31 min |
| largest-eigenval | Medium | 0 | 2/3 | timeout | 33 | 33 | 15.12 min |
| llm-inference-batching-scheduler | Hard | 1 | 6/6 | completed | 18 | 20 | 23.92 min |
| log-summary-date-ranges | Medium | 1 | 2/2 | completed | 4 | 4 | 0.90 min |
| mailman | Medium | 0 | 0/3 | max_turn | 50 | 53 | 11.34 min |
| make-doom-for-mips | Hard | 0 | 0/3 | timeout | 44 | 55 | 15.11 min |
| make-mips-interpreter | Hard | 0 | 0/3 | max_turn | 50 | 50 | 7.40 min |
| mcmc-sampling-stan | Hard | 1 | 6/6 | completed | 23 | 25 | 26.42 min |
| merge-diff-arc-agi-task | Medium | 1 | 5/5 | completed | 16 | 15 | 3.74 min |
| model-extraction-relu-logits | Hard | 0 | 0/1 | completed | 22 | 22 | 13.48 min |
| modernize-scientific-stack | Medium | 1 | 2/2 | completed | 9 | 12 | 1.43 min |
| mteb-leaderboard | Medium | 0 | 0/2 | max_turn | 50 | 51 | 21.36 min |
| mteb-retrieve | Medium | 0 | 1/2 | completed | 23 | 22 | 10.75 min |
| multi-source-data-merger | Medium | 1 | 3/3 | completed | 6 | 8 | 1.85 min |
| nginx-request-logging | Medium | 0 | 3/8 | completed | 8 | 9 | 1.32 min |
| openssl-selfsigned-cert | Medium | 1 | 6/6 | completed | 9 | 8 | 1.22 min |
| overfull-hbox | Easy | 1 | 4/4 | completed | 17 | 19 | 10.90 min |
| password-recovery | Hard | 0 | 0/2 | timeout | 24 | 35 | 15.12 min |
| path-tracing | Hard | 0 | 0/5 | timeout | 31 | 31 | 30.12 min |
| path-tracing-reverse | Hard | 0 | 0/3 | timeout | 35 | 35 | 30.12 min |
| polyglot-c-py | Medium | 1 | 1/1 | completed | 8 | 7 | 5.42 min |
| polyglot-rust-c | Hard | 1 | 1/1 | completed | 5 | 5 | 8.31 min |
| portfolio-optimization | Medium | 1 | 4/4 | completed | 19 | 18 | 8.33 min |
| protein-assembly | Hard | 0 | 0/1 | timeout | 34 | 38 | 30.11 min |
| prove-plus-comm | Easy | 1 | 4/4 | completed | 8 | 8 | 1.09 min |
| pypi-server | Medium | 0 | 0/1 | completed | 15 | 14 | 1.67 min |
| pytorch-model-cli | Medium | 0 | 5/6 | completed | 36 | 35 | 10.67 min |
| pytorch-model-recovery | Medium | 1 | 5/5 | completed | 9 | 9 | 3.80 min |
| qemu-alpine-ssh | Medium | 0 | 0/1 | timeout | 30 | 30 | 15.11 min |
| qemu-startup | Medium | 0 | 0/1 | completed | 13 | 12 | 7.07 min |
| query-optimize | Medium | 1 | 6/6 | completed | 12 | 11 | 6.72 min |
| raman-fitting | Medium | 0 | 0/3 | timeout | 17 | 18 | 15.12 min |
| regex-chess | Hard | 0 | 0/4 | max_token | 2 | 2 | 17.16 min |
| regex-log | Medium | 1 | 1/1 | completed | 7 | 6 | 9.61 min |
| reshard-c4-data | Medium | 0 | 0/1 | completed | 24 | 23 | 6.86 min |
| rstan-to-pystan | Medium | 1 | 6/6 | completed | 27 | 31 | 17.01 min |
| sam-cell-seg | Hard | 0 | 1/9 | max_turn | 50 | 58 | 49.84 min |
| sanitize-git-repo | Medium | 0 | 2/3 | completed | 24 | 32 | 3.98 min |
| schemelike-metacircular-eval | Medium | 1 | 1/1 | timeout | 31 | 37 | 40.12 min |
| sparql-university | Hard | 1 | 3/3 | completed | 20 | 19 | 4.87 min |
| sqlite-db-truncate | Medium | 1 | 1/1 | completed | 6 | 7 | 1.58 min |
| sqlite-with-gcov | Medium | 0 | 0/3 | timeout | 19 | 20 | 15.11 min |
| torch-pipeline-parallelism | Hard | 0 | 2/3 | timeout | 10 | 11 | 15.11 min |
| torch-tensor-parallelism | Hard | 1 | 3/3 | completed | 19 | 18 | 8.37 min |
| train-fasttext | Hard | 0 | 1/2 | timeout | 30 | 33 | 60.11 min |
| tune-mjcf | Medium | 1 | 4/4 | timeout | 25 | 26 | 15.11 min |
| video-processing | Hard | 1 | 5/5 | completed | 33 | 32 | 22.65 min |
| vulnerable-secret | Medium | 1 | 3/3 | completed | 9 | 8 | 1.57 min |
| winning-avg-corewars | Medium | 1 | 3/3 | completed | 28 | 32 | 12.93 min |
| write-compressor | Hard | 1 | 3/3 | completed | 8 | 8 | 10.79 min |

## 数据来源

- work/linux-terminal-bench/dsh/jobs/
- work/linux-terminal-bench/dsh/state/resource.queue.tsv
- work/terminal-bench-2-1/tasks/*/task.toml
- 每个选中 trial 的 result.json、verifier/ctrf.json 与产品原生 trace
