# Astra 最新验证结果：no-pass 任务列表

数据来源：[`astra-c0-latest-verified-report.md`](../../runs/astra-c0-all-jobs/analysis/v2/output/astra-c0-latest-verified-report.md) 与 [`astra-c0-latest-verified-no-pass.csv`](../../runs/astra-c0-all-jobs/analysis/v2/output/astra-c0-latest-verified-no-pass.csv)。统计范围为排除 `tune-mjcf` 后，按 task 选择最后一次运行、且 latest 运行存在数字 verifier reward 的 86 个任务中的 42 个 no-pass 任务。

## 口径

- `no-pass 原因`：优先按报告的 `outcome_bucket` 汇总；`timeout_no_pass` 表示发生 LLM 请求超时，`deadline_suspected_no_pass` 表示达到 controller deadline 的推断性终止，`completed_no_pass` 表示产品完成但 verifier 未通过，`failed_no_pass` 表示产品/适配器失败且 verifier 未通过。
- `交互轮次`：采用 CSV 的 `agentic_steps`，即 Astra 轨迹中的 Agentic Step 数，不等同于产品内部所有模型请求数。
- `token`：`token_input` 为输入 token（含 cache read/cache creation），`token_output` 为输出 token；`token_total` 优先使用完整可观测 total，缺失时使用 `token_known_minimum`。缺失不补零，因此表中 total 可能是可观测下界。
- `verifier 通过率`：`verifier_passed / verifier_tests`。`verifier_tests=0` 或 verifier 缺失时记为 `N/A`，不把它解释为 0%。
- `Astra Agent 执行时间 / 配置 deadline`：前者来自最新运行记录的 `agent_execution_s`，后者来自同一任务的 `configured_product_timeout_s`；表中统一显示为分钟 / 分钟。deadline 是产品执行预算，不是 LLM fallback timeout，也不是 verifier timeout。

## 分类汇总

| no-pass 原因 | 任务数 |
|---|---:|
| LLM 请求超时（`timeout_no_pass`） | 32 |
| Controller deadline 推断终止 | 2 |
| 产品完成但 verifier 未通过（`completed_no_pass`） | 6 |
| 产品/适配器失败（`failed_no_pass`） | 2 |
| 合计 | **42** |

## 逐任务列表

| task_name | no-pass 原因 | 交互轮次 | Astra Agent 执行时间 / 配置 deadline (min) | 输入 token | 输出 token | token total/下界 | verifier 通过率 |
|---|---|---:|---:|---:|---:|---:|---:|
| adaptive-rejection-sampler | LLM 请求超时 | 35 | 33.36 min / 33.75 min | 476215 | 12784 | 488999 | 0.0% (0/9) |
| build-pmars | 产品完成但 verifier 未通过 | 52 | 24.73 min / 30.00 min | 1261503 | 75234 | 1336737 | 75.0% (3/4) |
| build-pov-ray | 产品完成但 verifier 未通过 | 56 | 25.24 min / 450.00 min | 1765187 | 45774 | 1810961 | 66.7% (2/3) |
| cancel-async-tasks | 产品完成但 verifier 未通过 | 5 | 3.20 min / 33.75 min | 52754 | 13129 | 65883 | 83.3% (5/6) |
| chess-best-move | LLM 请求超时 | 8 | 14.02 min / 30.00 min | 73893 | 6143 | 80036 | 0.0% (0/1) |
| circuit-fibsqrt | LLM 请求超时 | 28 | 27.14 min / 135.00 min | 353367 | 13982 | 367349 | 66.7% (2/3) |
| code-from-image | LLM 请求超时 | 26 | 75.00 min / 40.00 min | 371711 | 111965 | 483676 | 0.0% (0/2) |
| crack-7z-hash | LLM 请求超时 | 34 | 61.00 min / 60.00 min | 493561 | 30863 | 524424 | 0.0% (0/2) |
| distribution-search | LLM 请求超时 | 2 | 31.05 min / 120.00 min | 9480 | 7903 | 17383 | 0.0% (0/4) |
| dna-assembly | LLM 请求超时 | 30 | 18.92 min / 67.50 min | 579637 | 54240 | 633877 | 0.0% (0/1) |
| dna-insert | 产品完成但 verifier 未通过 | 13 | 10.66 min / 67.50 min | 234975 | 51365 | 286340 | 0.0% (0/1) |
| extract-elf | LLM 请求超时 | 7 | 16.19 min / 30.00 min | 79089 | 37071 | 116160 | 0.0% (0/2) |
| extract-moves-from-video | 产品/适配器失败 | 50 | 47.66 min / 60.00 min | 780975 | 49962 | 830937 | N/A (0/0) |
| filter-js-from-html | LLM 请求超时 | 2 | 8.63 min / 60.00 min | 9329 | 4897 | 14226 | 0.0% (0/2) |
| gcode-to-text | LLM 请求超时 | 26 | 29.07 min / 30.00 min | 513144 | 95855 | 608999 | 0.0% (0/2) |
| gpt2-codegolf | LLM 请求超时 | 25 | 23.24 min / 33.75 min | 327449 | 25396 | 352845 | 0.0% (0/1) |
| hf-model-inference | 产品完成但 verifier 未通过 | 24 | 5.84 min / 33.75 min | 375217 | 23803 | 399020 | 25.0% (1/4) |
| install-windows-3.11 | LLM 请求超时 | 43 | 78.12 min / 120.00 min | 1000681 | 86050 | 1086731 | 75.0% (3/4) |
| kv-store-grpc | 产品/适配器失败 | 14 | 3.69 min / 33.75 min | 158890 | 9813 | 168703 | 71.4% (5/7) |
| llm-inference-batching-scheduler | LLM 请求超时 | 3 | 13.06 min / 60.00 min | 25123 | 12264 | 37387 | 16.7% (1/6) |
| mailman | LLM 请求超时 | 15 | 18.67 min / 60.00 min | 266769 | 69391 | 336160 | 0.0% (0/3) |
| make-doom-for-mips | Controller deadline 推断终止 | 23 | 33.84 min / 33.75 min | 1123373 | 162757 | 1286130 | 0.0% (0/3) |
| make-mips-interpreter | LLM 请求超时 | 34 | 41.37 min / 67.50 min | 737928 | 115323 | 853251 | 0.0% (0/3) |
| mcmc-sampling-stan | 产品完成但 verifier 未通过 | 32 | 51.38 min / 60.00 min | 792441 | 69395 | 861836 | 83.3% (5/6) |
| model-extraction-relu-logits | LLM 请求超时 | 19 | 18.09 min / 33.75 min | 186642 | 14934 | 201576 | 0.0% (0/1) |
| overfull-hbox | LLM 请求超时 | 6 | 9.82 min / 25.00 min | 57122 | 3920 | 61042 | 75.0% (3/4) |
| path-tracing | LLM 请求超时 | 16 | 69.19 min / 60.00 min | 197478 | 23320 | 220798 | 0.0% (0/5) |
| path-tracing-reverse | LLM 请求超时 | 15 | 29.52 min / 60.00 min | 502519 | 54637 | 557156 | 0.0% (0/3) |
| polyglot-rust-c | LLM 请求超时 | 20 | 24.89 min / 33.75 min | 223617 | 12467 | 236084 | 0.0% (0/1) |
| portfolio-optimization | LLM 请求超时 | 3 | 6.00 min / 120.00 min | 18856 | 258 | 19114 | 25.0% (1/4) |
| protein-assembly | LLM 请求超时 | 18 | 16.55 min / 60.00 min | 306679 | 43169 | 349848 | 0.0% (0/1) |
| qemu-startup | Controller deadline 推断终止 | 25 | 33.82 min / 33.75 min | 414885 | 118617 | 533502 | 0.0% (0/1) |
| raman-fitting | LLM 请求超时 | 12 | 24.01 min / 30.00 min | 155724 | 56755 | 212479 | 33.3% (1/3) |
| regex-chess | LLM 请求超时 | 3 | 19.12 min / 120.00 min | 19561 | 4242 | 23803 | 0.0% (0/4) |
| regex-log | LLM 请求超时 | 22 | 25.85 min / 33.75 min | 245320 | 9304 | 254624 | 0.0% (0/1) |
| reshard-c4-data | LLM 请求超时 | 3 | 9.28 min / 120.00 min | 20202 | 9682 | 29884 | 0.0% (0/1) |
| rstan-to-pystan | LLM 请求超时 | 13 | 67.39 min / 60.00 min | 160170 | 25257 | 185427 | 16.7% (1/6) |
| schemelike-metacircular-eval | LLM 请求超时 | 3 | 7.42 min / 80.00 min | 21937 | 209 | 22146 | 0.0% (0/1) |
| torch-pipeline-parallelism | LLM 请求超时 | 3 | 9.67 min / 30.00 min | 19718 | 9322 | 29040 | 0.0% (0/3) |
| video-processing | LLM 请求超时 | 40 | 44.47 min / 135.00 min | 757878 | 92907 | 850785 | 40.0% (2/5) |
| winning-avg-corewars | LLM 请求超时 | 4 | 8.04 min / 120.00 min | 29851 | 361 | 30212 | 33.3% (1/3) |
| write-compressor | LLM 请求超时 | 17 | 21.58 min / 33.75 min | 165352 | 29642 | 194994 | 0.0% (0/3) |

## 说明

42 个 no-pass 中，34 个存在 timeout/deadline 证据（32 个显式 LLM 请求超时、2 个 controller deadline 推断）；6 个是产品完成后 verifier 未通过；2 个是产品或适配器失败。`extract-moves-from-video` 没有 verifier 测试项，因此 verifier 通过率为 `N/A`，不能按 0% 解读。

## “产品/适配器失败”与 Controller deadline 的详细解释

### 1. 产品/适配器失败（2 个）

这里的“产品/适配器失败”不是 verifier 对任务产物给出的普通 no-pass，而是外层运行记录中的 `product_terminal_status=adapter_infra_error`、`product_error_type=LifecycleControllerError` 或对应适配器/生命周期控制器异常。它表示 runner 没有把本次运行记录为一个正常完成的产品执行；即使轨迹中已经产生了一些 Agent 步骤，也不能把它当作完整的正常端到端产品结果。

| task | 具体终态 | 是否超时 | verifier 结果 | 具体含义 |
|---|---|---|---|---|
| `extract-moves-from-video` | `adapter_infra_error`；product return code 4；`LifecycleControllerError`；cleanup report unavailable；Astra state `interrupted`，exit code 5，`budget_exhausted` | 否 | 无 verifier 测试，`N/A` | 运行约 49.6 分钟后，外层生命周期控制器无法取得/完成进程清理报告，最终以适配器基础设施错误结束。该记录不能判断为“任务产物 verifier 失败”，也不能判断为 LLM 超时；verifier 阶段本身还出现 `curl` 访问 `astral.sh` 的 SSL 错误。 |
| `kv-store-grpc` | `adapter_infra_error`；product return code 4；`LifecycleControllerError`；但 Astra state `completed`、exit code 0，轨迹完整 | 否 | 5/7 通过（71.4%）；失败为 gRPC handshake 和 server functionality | Agent 实际完成并生成了可验证产物，但外层适配器仍将最终产品状态标成基础设施错误，因此本统计保守归入产品/适配器失败。与此同时，verifier 明确发现生成的 protobuf 请求没有 `value` 字段，导致两个 gRPC 测试失败；所以该任务同时存在任务实现缺陷和外层终态异常，不能简单归为纯 runner 问题。 |

因此，“产品/适配器失败”回答的是“这次运行是否形成了正常产品终态”，不等于“模型一定没有完成任务”。其中 `kv-store-grpc` 就是一个部分完成、部分 verifier 通过但终态仍被适配器标记异常的例子。

### 2. 两个 Controller deadline 属于超时

属于超时类结果，但证据等级低于显式 LLM timeout

| task | 配置 deadline | Agent 执行时间 | 证据 | verifier 结果 | 结论 |
|---|---:|---:|---|---|---|
| `make-doom-for-mips` | 33.75 min | 33.84 min | retry report `attempt_running/incomplete`；`controller_deadline_suspected=true`；无显式 LLM timeout | 0/3 | **推断为 controller deadline 超时**；不是已观测的 LLM 请求超时。verifier 同时显示未生成 `/tmp/frame.bmp`，任务产物也未完成。 |
| `qemu-startup` | 33.75 min | 33.82 min | retry report `attempt_running/incomplete`；`controller_deadline_suspected=true`；无显式 LLM timeout | 0/1 | **推断为 controller deadline 超时**；不是已观测的 LLM 请求超时。verifier 显示 `/tmp/data.txt` 为空，未验证到目标 Alpine 内核版本。 |

所以在总表中，这两个任务应计入“timeout/deadline suspected”集合；更精确的表述是“达到 controller deadline 的疑似超时”，不能写成“日志已明确记录的超时”。
