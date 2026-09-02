# DeepSeek Harness（DSH）：Toolathlon 108 题结果分析

生成日期：2026-09-02
范围：Toolathlon 第 1–108 题，DeepSeek Harness（DSH）0.1.0-rc.7。

## 口径

- 从所有 DSH 批次的 attempt 中，对每个 `task_id` 按 `started_at` 选择时间最新的一次；时间相同再以批次名和 `run_id` 确定顺序。
- 本投影覆盖 108 个任务，最新 attempt 全部具有 Agent 执行和 evaluator 结果。`pass`/`no_pass` 只以 evaluator 为准。
- 结果是多个基础批次与恢复批次组成的 effective projection，不代表 108 题在同一批次连续执行。采用 attempt 的开始时间范围为 `2026-08-20T15:25:59.739485+00:00` 至 `2026-08-27T16:05:05.649752+00:00`。
- 时间、工具调用、模型请求和 token 均为 DSH 产品整体运行时的观测口径；模型请求包含 transport 重试，不等同于用户可见对话轮数。
- Token 仅汇总 provider 已上报的 usage；缺失 usage 不补零。

### 最新结果来源

| 批次 | 采用题数 |
| --- | ---: |
| `toolathlon-dsh-108-node-undici-v1` | 53 |
| `toolathlon-dsh-new-key-recovery-48-v1` | 32 |
| `toolathlon-dsh-rerun-transport-incomplete-30-v1` | 5 |
| `toolathlon-dsh-recovery-12-v1` | 12 |
| `toolathlon-dsh-incomplete-recovery-6-v1` | 2 |
| `toolathlon-dsh-filter-low-selling-v3` | 1 |
| `toolathlon-dsh-rerun-transport-incomplete-18-v1` | 2 |
| `toolathlon-dsh-incomplete-recovery-4-v2` | 1 |

## 实验产品与配置

| 项目 | DSH |
| --- | --- |
| 产品版本 | DeepSeek Harness `0.1.0-rc.7`，headless profile |
| Node.js | `22.19.0` |
| API 模型 ID | `deepseek-v4-flash` |
| 模型提供方 | DeepSeek 官方 API，经每次运行独立的本地 Node/Undici sidecar |
| 推理配置 | Thinking enabled；`reasoning_effort=max` |
| Temperature | 发送 `temperature=0`；Thinking 模式下不据此推断采样行为 |
| 外部统一请求预算 | 每题最多 100 次 product model request；允许第 100 次，拒绝第 101 次 |
| Agent deadline | 按任务采用 R1/R2/R3/R4：1800/2700/3600/5400 秒 |
| Prompt 口径 | 保留 DSH 原生 headless prompt，并输入 Toolathlon 公共 system/task 指令 |
| 工具范围 | 保留产品内置工具；仅向当前任务暴露相应 MCP 工具 |
| 状态隔离 | 每次 attempt 使用 fresh ephemeral DSH home，不从旧 attempt resume |

## 基础运行环境

| 环境项 | 配置 |
| --- | --- |
| 数据集 | Toolathlon，固定 108 题 |
| Host OS | Ubuntu 22.04，Linux `5.15.0-190-generic`，UTC |
| CPU | Intel Xeon Platinum 8255C @ 2.50 GHz，x86_64，8 vCPU |
| 内存与 Swap | 约 7.8 GiB RAM、8 GiB swap |
| 虚拟化 | Vagrant 虚拟机（KVM） |
| 容器运行时 | rootful Docker；任务容器使用 Toolathlon 固定镜像 |
| 单任务资源上限 | 8 CPU、8 GiB RAM、8 GiB swap |
| 外部应用状态 | 由任务 preprocess 恢复；部分本地应用和 Kind 服务由任务间共享部署提供 |
| 网络边界 | 未统一关闭公开互联网出口；任务 MCP 限于当前任务，模型请求经本地 sidecar 转发 |
| Evaluator | Agent 终止后独立运行 Toolathlon 每题原生 evaluator |

这里记录的是本次 DSH 实验环境口径；当前宿主机状态的后续变化不追溯修改历史运行记录。

## 任务完成结果

| 指标 | DSH |
| --- | ---: |
| 基准题目 | 108 |
| 有 Agent + Evaluator 完整结果 | 108 |
| pass | 79 |
| no-pass | 29 |
| 未完成 | 0 |
| 按 108 题通过率 | 73.15% |
| 已测评题通过率 | 73.15% |

通过（79）：`find-alita-paper`（1）、`set-conf-cr-ddl`（2）、`course-schedule`（3）、`canvas-homework-grader-python`（4）、`notion-movies`（6）、`price-comparison`（7）、`quantitative-financial-analysis`（8）、`excel-data-transformation`（9）、`notion-hr`（10）、`woocommerce-stock-alert`（12）、`git-bug-hunt`（13）、`k8s-safety-audit`（14）、`ab-testing`（15）、`academic-pdf-report`（16）、`academic-warning`（17）、`apply-phd-email`（19）、`canvas-arrange-exam`（20）、`canvas-art-quiz`（22）、`canvas-do-quiz`（23）、`canvas-list-test`（24）、`canvas-new-students-notification`（25）、`canvas-submit-late-work`（26）、`cooking-guidance`（27）、`courses-ta-hws`（29）、`dataset-license-issue`（31）、`dietary-health`（33）、`excel-market-research`（35）、`fillout-online-forms`（37）、`filter-low-selling-products`（38）、`flagged-transactions`（39）、`game-statistics`（40）、`gdp-cr5-analysis`（41）、`git-milestone`（42）、`git-repo`（43）、`huggingface-upload`（45）、`identify-all-songs`（46）、`imagenet`（47）、`inter-final-performance-analysis`（48）、`interview-report`（49）、`inventory-sync`（50）、`invoice-org`（52）、`ipad-edu-price`（53）、`k8s-deployment-cleanup`（54）、`k8s-mysql`（55）、`k8s-redis-helm-upgrade`（57）、`landing-task-reminder`（58）、`live-transactions`（61）、`llm-training-dataset`（62）、`machine-operating`（64）、`meeting-assign`（65）、`nhl-b2b-analysis`（69）、`notion-find-job`（70）、`notion-personal-website`（71）、`nvidia-stock-analysis`（73）、`oil-price`（74）、`payable-invoice-checker`（76）、`personal-website-construct`（77）、`ppt-analysis`（78）、`reimbursement-form-filler`（81）、`sales-accounting`（82）、`search-ca-school`（83）、`sla-timeout-monitor`（84）、`stock-build-position`（85）、`student-interview`（86）、`sync-todo-to-readme`（88）、`train-ticket-plan`（90）、`travel-exchange`（91）、`travel-expense-reimbursement`（92）、`trip-adviser`（93）、`trip-itinerary-generator`（94）、`update-material-inventory`（96）、`upenn-campus-route`（97）、`verl-dataset`（98）、`wandb-best-score`（100）、`wandb-shortest-length`（101）、`woocommerce-customer-survey`（102）、`woocommerce-new-welcome`（104）、`woocommerce-product-recall`（105）、`woocommerce-update-cover`（106）。

### No-pass 逐题结果

| 序号 | 任务 | 产品终态 | 直接分类 | 模型请求 | Transport 错误 | 最新来源 |
| ---: | --- | --- | --- | ---: | ---: | --- |
| 5 | `arrange-workspace` | completed | Agent 完成，但 evaluator 未通过 | 13 | 0 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 11 | `shopping-helper` | completed | Agent 完成，但 evaluator 未通过 | 50 | 1 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 18 | `add-bibtex` | completed | Agent 完成，但 evaluator 未通过 | 30 | 0 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 21 | `canvas-art-manager` | crashed | 产品执行错误 | 6 | 6 | `toolathlon-dsh-recovery-12-v1` |
| 28 | `course-assistant` | crashed | 产品执行错误 | 6 | 6 | `toolathlon-dsh-recovery-12-v1` |
| 30 | `cvpr-research` | completed | Agent 完成，但 evaluator 未通过 | 30 | 1 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 32 | `detect-revised-terms` | completed | Agent 完成，但 evaluator 未通过 | 15 | 0 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 34 | `email-paper-homepage` | completed | Agent 完成，但 evaluator 未通过 | 20 | 0 | `toolathlon-dsh-incomplete-recovery-6-v1` |
| 36 | `experiments-recordings` | completed | Agent 完成，但 evaluator 未通过 | 35 | 0 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 44 | `hk-top-conf` | crashed | 产品执行错误 | 24 | 11 | `toolathlon-dsh-recovery-12-v1` |
| 51 | `investment-decision-analysis` | completed | Agent 完成，但 evaluator 未通过 | 13 | 0 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 56 | `k8s-pr-preview-testing` | crashed | 产品执行错误 | 6 | 6 | `toolathlon-dsh-recovery-12-v1` |
| 59 | `language-school` | completed | Agent 完成，但 evaluator 未通过 | 59 | 1 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 60 | `latex-prompt-box` | completed | Agent 完成，但 evaluator 未通过 | 32 | 1 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 63 | `logical-datasets-collection` | completed | Agent 完成，但 evaluator 未通过 | 15 | 0 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 66 | `merge-hf-datasets` | completed | Agent 完成，但 evaluator 未通过 | 42 | 0 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 67 | `mrbeast-analysis` | completed | Agent 完成，但 evaluator 未通过 | 20 | 0 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 68 | `music-analysis` | completed | Agent 完成，但 evaluator 未通过 | 72 | 0 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 72 | `nvidia-market` | crashed | 产品执行错误 | 6 | 6 | `toolathlon-dsh-recovery-12-v1` |
| 75 | `paper-checker` | completed | Agent 完成，但 evaluator 未通过 | 14 | 0 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 79 | `privacy-desensitization` | completed | Agent 完成，但 evaluator 未通过 | 11 | 0 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 80 | `profile-update-online` | completed | Agent 完成，但 evaluator 未通过 | 32 | 1 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 87 | `subway-planning` | completed | Agent 完成，但 evaluator 未通过 | 9 | 0 | `toolathlon-dsh-new-key-recovery-48-v1` |
| 89 | `task-tracker` | timeout | Agent deadline | 62 | 6 | `toolathlon-dsh-recovery-12-v1` |
| 95 | `university-course-selection` | completed | Agent 完成，但 evaluator 未通过 | 17 | 3 | `toolathlon-dsh-recovery-12-v1` |
| 99 | `vlm-history-completer` | completed | Agent 完成，但 evaluator 未通过 | 55 | 12 | `toolathlon-dsh-recovery-12-v1` |
| 103 | `woocommerce-new-product` | crashed | 产品执行错误 | 37 | 16 | `toolathlon-dsh-recovery-12-v1` |
| 107 | `yahoo-analysis` | crashed | 产品执行错误 | 6 | 6 | `toolathlon-dsh-recovery-12-v1` |
| 108 | `youtube-repo` | crashed | 产品执行错误 | 6 | 6 | `toolathlon-dsh-recovery-12-v1` |

### 未完成任务

| 序号 | 任务 | 阶段 | 原因 |
| ---: | --- | --- | --- |
| — | 无 | — | — |

## No-pass 原因

| 原因 | 题数 |
| --- | ---: |
| Agent 完成，但 evaluator 未通过 | 20 |
| 达到模型请求预算 | 0 |
| 产品执行错误 | 8 |
| Agent deadline | 1 |
| 其他 | 0 |

这是直接终态分类，不推断模型内部原因。另有 3 个 evaluator 已通过的任务记录了产品执行错误终态；因此报告坚持以 evaluator 作为任务通过与否的唯一判据，不把产品终态机械等同于 benchmark 结果。

## 时间消耗

| 阶段 | 样本数 | 总计 | 平均 | 中位数 | P90 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 端到端 | 108 | 15.62 h | 8.68 min | 5.32 min | 15.12 min |
| Agent 执行 | 108 | 14.44 h | 8.02 min | 4.76 min | 14.52 min |
| Evaluator | 108 | 32.52 min | 18.07 s | 9.61 s | 34.30 s |
| Orchestration/收尾 | 108 | 38.14 min | 21.19 s | 21.00 s | 22.84 s |

时间包含通过与未通过任务。`orchestration` 是端到端减去 Agent 和 evaluator 后的剩余时间，包含准备、adapter 收尾及 post-terminal drain，不是纯环境准备时间。

## 工具调用

| 指标 | DSH |
| --- | ---: |
| 有工具计数的运行 | 108 |
| 工具调用总数 | 3,892 |
| 单运行平均 | 36.04 |
| 单运行中位数 | 33 |
| 单运行 P90 | 65.50 |
| 失败工具事件总数 | 130 |

常见终态工具：`bash` 805、`local-python-execute` 172、`read` 156、`local-web_search` 126、`fetch-fetch_txt` 114、`google-cloud-bigquery_run_query` 107、`pdf-tools-read_pdf_pages` 103、`emails-send_email` 85、`emails-read_email` 83、`todo_write` 73。

失败工具事件可能在重试后恢复，不等同于失败任务数；不同 MCP 的工具粒度也不一致。

## 模型请求

| 指标 | DSH |
| --- | ---: |
| 统计运行数 | 108 |
| 模型请求 started | 3,089 |
| 模型请求 completed event | 3,089 |
| 成功 completed event | 2,140 |
| 失败 completed event | 949 |
| Transport 错误事件 | 949 |
| HTTP 错误响应 | 0 |
| Provider API 错误响应 | 0 |
| 单运行 started 平均 | 28.60 |
| 单运行 started 中位数 | 24 |
| 单运行 started P90 | 58.30 |
| 触及 100 请求上限 | 0 |
| 因请求预算 no-pass | 0 |
| 流式请求 | 3,089（100.00%） |
| 非流式请求 | 0（0.00%） |

共有 80 / 108 个最新运行记录过 transport 错误，其中 16 个最终为 no-pass。错误类型累计为：`ECONNRESET` 452、`UND_ERR_SOCKET` 373、`downstream_disconnected` 124。这些计数描述 sidecar 观测到的连接/流事件；没有 HTTP 响应的 transport 失败不能解释为 DeepSeek API 业务错误码。

## Token 数据

### 保守可靠记录

若一个运行的 completed response 存在缺失 usage，该运行不进入本表：

| 指标 | DSH |
| --- | ---: |
| 有完整 provider usage 的运行 | 41 / 108 |
| 输入 token 总量 | 78,999,127 |
| 输出 token 总量 | 1,166,424 |
| total token 总量 | 80,165,551 |
| 单运行 total 中位数 | 1,184,269 |

“保守可靠记录”是运行级完备性口径，不是只挑选成功请求：一个任务必须让其每个 `model_request.completed` 都同时具有 provider 明确上报的 input/output/total token，才进入本表。只要一次 transport 失败没有返回 usage，即使任务最终通过、其他请求都有 usage，整题也会被排除。因此 41 / 108 表示“可以完整求和的任务数”，不表示其余任务没有 token 数据或 token 为 0。

### 全部可见 token 下界

| 指标 | DSH |
| --- | ---: |
| 运行数 | 108 |
| 已上报 / 缺 usage 的 completed 请求 | 2,257 / 832 |
| 输入 token 下界 | 140,602,287 |
| 输出 token 下界 | 2,070,850 |
| total token 下界 | 142,673,137 |
| 可见 cache-read token | 134,808,192 |
| 单运行 total 中位数 | 790,235 |

“全部可见 token 下界”覆盖全部 108 个任务，但只累加 2,257 次明确返回 usage 的请求；832 次缺失 usage 的请求保持未知，不补零。已计入部分包含所有成功响应，以及在断开前已经拿到 usage 的失败响应；流式失败后成功重试所返回的完整 usage 也会作为一笔新的请求计入。未计入部分仍可能已经消耗完整输入和部分输出 token，因此该数值是实际资源消耗的严格下界，而不是账单总量。

由于 DSH 断流后会用相同历史重新发起完整请求，这个可见下界可能接近“每个逻辑步骤一次成功”时的工作量，但不能当作无链路波动的反事实估计：部分失败请求本身已有可见 usage，重新生成还可能改变输出、工具调用和后续 Agent 路径。Token 反映 DSH 主循环、工具 schema、累积上下文及缓存策略的整体足迹，不能直接解释为底层模型的单位推理效率。

### Pass 任务的可靠 Token

| 指标 | DSH |
| --- | ---: |
| 有可靠 token 的 pass 任务 | 23 / 79 |
| total token 总量 | 39,671,821 |
| 单任务平均 | 1,724,861.78 |
| 单任务中位数 | 970,818 |

## 综合结论

1. **最终覆盖完整。** 108 个任务的最新 attempt 都形成了 Agent 与 evaluator 结果；79 题通过、29 题未通过，通过率为 73.15%。
2. **No-pass 以完成后未满足 evaluator 为主。** 20 题正常完成 Agent 流程但未满足验证，8 题以产品执行错误结束，1 题触及 Agent deadline；没有任务因 100 次模型请求预算直接 no-pass。
3. **网络事件仍影响部分 effective result。** 80 题记录过 transport 错误，累计 949 次；它们既可能被重试恢复，也可能成为产品错误的一部分，不能仅按事件数推导任务失败。
4. **请求量必须按产品足迹解释。** 单任务模型请求中位数为 24，P90 为 58.30；请求包含重试，不等同于 Agent 的语义回合。
5. **工具和 token 是观测足迹，不是单独的能力指标。** DSH 单任务工具调用中位数为 33；可靠 token 记录覆盖 41 / 108 题。任务复杂度、工具 schema 和上下文增长都会影响这些数值。
6. **结果来自混合批次。** 本报告严格采用每题开始时间最新的 attempt；恢复批次只替换实际重跑的任务，未重跑任务继续保留其较早结果，因此结论应按 effective projection 理解。

## 附件

- 全批次逐 attempt 数据：[`dsh-toolathlon-all-attempt-results.csv`](dsh-toolathlon-all-attempt-results.csv)
- 全 attempt CSV 生成脚本：[`generate_dsh_all_attempt_results.py`](generate_dsh_all_attempt_results.py)
- 本报告生成脚本：[`generate_dsh_latest_result_report.py`](generate_dsh_latest_result_report.py)
