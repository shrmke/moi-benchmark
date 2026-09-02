# Astra、Hermes、Pi 与 DSH：Toolathlon 108 题对比分析

生成日期：2026-09-02

## 口径

- Astra、Hermes 和 Pi 沿用既有三产品报告的 effective result；DSH 对所有批次中的每个任务按 `started_at` 选择最新一次 attempt。
- Astra、Hermes、DSH 均有 108 个明确 evaluator 结果；Pi 有 104 个明确结果、3 个 `unavailable` 和 1 个 `incomplete`。涉及 Pi 的组合与配对不会把这 4 题计作失败。
- 通过与否只以 evaluator 为准。时间、工具、模型请求和 token 是各产品整体运行时的观测足迹，不是同构 agent loop。
- Pi 和 DSH 都是多批次 effective projection。DSH 恢复运行还跨越了模型凭据更换和网络稳定性修复，因此本报告不是同一时段、同一凭据下的严格同步实验。
- 所有产品使用相同的 Toolathlon 任务顺序和 `deepseek-v4-flash` 模型请求 ID，但产品主循环、prompt、工具封装、缓存和重试策略不同。

### Effective result 来源

| 产品 | 来源 | 采用题数 |
| --- | --- | ---: |
| Pi | `pi_base_108_v1` | 75 |
| Pi | `pi_isolated_rerun_v1` | 25 |
| Pi | `pi_service_and_audit_8_v3` | 8 |
| DSH | `toolathlon-dsh-108-node-undici-v1` | 53 |
| DSH | `toolathlon-dsh-new-key-recovery-48-v1` | 32 |
| DSH | `toolathlon-dsh-rerun-transport-incomplete-30-v1` | 5 |
| DSH | `toolathlon-dsh-recovery-12-v1` | 12 |
| DSH | `toolathlon-dsh-incomplete-recovery-6-v1` | 2 |
| DSH | `toolathlon-dsh-filter-low-selling-v3` | 1 |
| DSH | `toolathlon-dsh-rerun-transport-incomplete-18-v1` | 2 |
| DSH | `toolathlon-dsh-incomplete-recovery-4-v2` | 1 |

Astra/Hermes 继续采用既有正式投影；上表重点披露存在多层覆盖的 Pi 与 DSH。

## 实验产品与配置

| 项目 | Astra | Hermes | Pi | DSH |
| --- | --- | --- | --- | --- |
| 产品版本 | release build，CLI `astra 0.1.0` | project `0.19.0` | `0.73.1` Linux x64 | `0.1.0-rc.7` headless |
| API 模型 ID | `deepseek-v4-flash` | `deepseek-v4-flash` | `deepseek-v4-flash` | `deepseek-v4-flash` |
| 模型提供方 | DeepSeek 官方 API，经本地代理 | 同左 | 同左 | DeepSeek 官方 API，经 Node/Undici sidecar |
| 推理配置 | Thinking enabled，`reasoning_effort=max` | 同左 | 同左 | 同左 |
| Temperature | 发送 `temperature=0` | 同左 | 同左 | 同左 |
| 产品原生 max turns | 300 | 90 | 未显式设置 | 未显式设置 |
| 外部统一请求预算 | 每题最多 100 次 product model request | 同左 | 同左 | 同左 |
| Agent deadline | R1/R2/R3/R4：1800/2700/3600/5400 秒 | 同左 | 同左 | 同左 |
| Prompt 口径 | 原生 system prompt + Toolathlon 公共指令 | 同左 | append 公共指令 | headless 原生 prompt + 公共指令 |
| 工具范围 | 保留内置工具；提供当前任务 MCP | 同左 | 同左 | 同左 |

四种产品的 turn 定义不同。本报告统一使用代理观测到的 `model_request.started`，不把模型请求数直接解释为用户可见回合。

## 基础运行环境

| 环境项 | 配置 |
| --- | --- |
| 数据集 | Toolathlon，固定 108 题；涉及 Pi 的严格比较仅覆盖其 104 个明确 evaluator 结果 |
| Host | Ubuntu 22.04；同一 VM/硬件环境，Astra/Hermes/Pi 冻结记录为 Linux `5.15.0-186`，DSH 运行期为 `5.15.0-190` |
| CPU | Intel Xeon Platinum 8255C @ 2.50 GHz，x86_64，8 vCPU |
| 内存与 Swap | 约 7.8 GiB RAM、8 GiB swap |
| 虚拟化与容器 | Vagrant/KVM；rootful Docker，Toolathlon 固定任务镜像 |
| 单任务资源上限 | 8 CPU、8 GiB RAM、8 GiB swap |
| 外部应用状态 | 由 preprocess 恢复；部分本地应用和 Kind 服务使用共享部署 |
| 网络边界 | 未统一关闭公开互联网出口；模型请求均通过各自本地代理/sidecar |
| Evaluator | Agent 终止后独立执行 Toolathlon 每题原生 evaluator |

四产品运行时间不同，宿主机内核、外部服务和模型凭据并非完全冻结为同一瞬时状态；这些差异必须作为结果解释边界。

## 任务完成结果

| 产品 | pass | no-pass | 未明确/未完成 | 按 108 题通过率 | 已测评题通过率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Astra | 61 | 47 | 0 | 56.48% | 56.48% |
| Hermes | 72 | 36 | 0 | 66.67% | 66.67% |
| Pi | 77 | 27 | 4 | 71.30% | 74.04% |
| DSH | 79 | 29 | 0 | 73.15% | 73.15% |

### 四方逐题组合

`P/F/U` 顺序固定为 Astra/Hermes/Pi/DSH；`U` 表示该产品没有明确 evaluator 判定。

| 结果组 | 题数 | 任务 |
| --- | ---: | --- |
| `PPPP` | 48 | `find-alita-paper`（1）、`set-conf-cr-ddl`（2）、`canvas-homework-grader-python`（4）、`price-comparison`（7）、`excel-data-transformation`（9）、`notion-hr`（10）、`git-bug-hunt`（13）、`ab-testing`（15）、`academic-pdf-report`（16）、`academic-warning`（17）、`apply-phd-email`（19）、`canvas-arrange-exam`（20）、`canvas-art-quiz`（22）、`canvas-new-students-notification`（25）、`canvas-submit-late-work`（26）、`courses-ta-hws`（29）、`dietary-health`（33）、`excel-market-research`（35）、`flagged-transactions`（39）、`game-statistics`（40）、`gdp-cr5-analysis`（41）、`git-milestone`（42）、`git-repo`（43）、`huggingface-upload`（45）、`inventory-sync`（50）、`invoice-org`（52）、`k8s-redis-helm-upgrade`（57）、`landing-task-reminder`（58）、`live-transactions`（61）、`llm-training-dataset`（62）、`machine-operating`（64）、`meeting-assign`（65）、`notion-find-job`（70）、`notion-personal-website`（71）、`nvidia-stock-analysis`（73）、`payable-invoice-checker`（76）、`ppt-analysis`（78）、`sales-accounting`（82）、`sla-timeout-monitor`（84）、`student-interview`（86）、`sync-todo-to-readme`（88）、`train-ticket-plan`（90）、`trip-adviser`（93）、`trip-itinerary-generator`（94）、`update-material-inventory`（96）、`wandb-shortest-length`（101）、`woocommerce-customer-survey`（102）、`woocommerce-update-cover`（106） |
| `FPPP` | 9 | `notion-movies`（6）、`woocommerce-stock-alert`（12）、`canvas-do-quiz`（23）、`cooking-guidance`（27）、`dataset-license-issue`（31）、`fillout-online-forms`（37）、`inter-final-performance-analysis`（48）、`reimbursement-form-filler`（81）、`wandb-best-score`（100） |
| `PFPP` | 6 | `course-schedule`（3）、`k8s-safety-audit`（14）、`imagenet`（47）、`k8s-deployment-cleanup`（54）、`search-ca-school`（83）、`upenn-campus-route`（97） |
| `PPFP` | 2 | `nhl-b2b-analysis`（69）、`personal-website-construct`（77） |
| `PPPF` | 1 | `latex-prompt-box`（60） |
| `FFPP` | 5 | `quantitative-financial-analysis`（8）、`interview-report`（49）、`oil-price`（74）、`travel-expense-reimbursement`（92）、`woocommerce-product-recall`（105） |
| `FPFP` | 4 | `canvas-list-test`（24）、`identify-all-songs`（46）、`stock-build-position`（85）、`woocommerce-new-welcome`（104） |
| `FPPF` | 2 | `canvas-art-manager`（21）、`profile-update-online`（80） |
| `PFFP` | 1 | `verl-dataset`（98） |
| `PFPF` | 2 | `email-paper-homepage`（34）、`yahoo-analysis`（107） |
| `FFFP` | 1 | `travel-exchange`（91） |
| `FFPF` | 4 | `logical-datasets-collection`（63）、`mrbeast-analysis`（67）、`music-analysis`（68）、`youtube-repo`（108） |
| `FPFF` | 3 | `cvpr-research`（30）、`k8s-pr-preview-testing`（56）、`woocommerce-new-product`（103） |
| `PFFF` | 1 | `add-bibtex`（18） |
| `FFFF` | 15 | `arrange-workspace`（5）、`shopping-helper`（11）、`course-assistant`（28）、`detect-revised-terms`（32）、`experiments-recordings`（36）、`hk-top-conf`（44）、`investment-decision-analysis`（51）、`language-school`（59）、`merge-hf-datasets`（66）、`paper-checker`（75）、`privacy-desensitization`（79）、`subway-planning`（87）、`task-tracker`（89）、`university-course-selection`（95）、`vlm-history-completer`（99） |
| `FPUP` | 3 | `filter-low-selling-products`（38）、`ipad-edu-price`（53）、`k8s-mysql`（55） |
| `FFUF` | 1 | `nvidia-market`（72） |

### 两两配对

| 配对 | 可比较题 | 两者均通过 | 仅左侧通过 | 仅右侧通过 | 两者均未通过 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Astra vs Hermes | 108 | 51 | 10 | 21 | 26 |
| Astra vs Pi | 104 | 57 | 4 | 20 | 23 |
| Astra vs DSH | 108 | 57 | 4 | 22 | 25 |
| Hermes vs Pi | 104 | 60 | 9 | 17 | 18 |
| Hermes vs DSH | 108 | 66 | 6 | 13 | 23 |
| Pi vs DSH | 104 | 68 | 9 | 8 | 19 |

涉及 Pi 的配对只覆盖 104 个明确结果。在这 104 题上，DSH 相对 Pi 为 8 个 DSH-only 对 9 个 Pi-only；在 108 题上，DSH 相对 Hermes 为 13 个 DSH-only 对 6 个 Hermes-only。这是固定 benchmark 上的逐题描述，不是统计显著性结论。

## No-pass 原因

| 产品 | 完成但 evaluator 未通过 | 模型请求预算 | 产品执行错误 | Agent deadline | 其他 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Astra | 27 | 20 | 0 | 0 | 0 |
| Hermes | 33 | 3 | 0 | 0 | 0 |
| Pi | 23 | 2 | 1 | 1 | 0 |
| DSH | 20 | 0 | 8 | 1 | 0 |

该表只对明确 `no_pass` 做直接终态分类，不推断模型内部原因。DSH 另有 3 个 evaluator 已通过任务记录了产品执行错误终态，说明产品终态和 evaluator 结果不能机械等同。

## 时间消耗

| 阶段 | 产品 | 样本数 | 总计 | 平均 | 中位数 | P90 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 端到端 | Astra | 108 | 34.18 h | 18.99 min | 10.16 min | 50.01 min |
| 端到端 | Hermes | 108 | 17.58 h | 9.77 min | 7.10 min | 19.63 min |
| 端到端 | Pi | 107 | 17.73 h | 9.94 min | 4.83 min | 22.39 min |
| 端到端 | DSH | 108 | 15.62 h | 8.68 min | 5.32 min | 15.12 min |
| Agent 执行 | Astra | 108 | 32.69 h | 18.16 min | 9.10 min | 49.26 min |
| Agent 执行 | Hermes | 108 | 14.21 h | 7.89 min | 5.21 min | 16.93 min |
| Agent 执行 | Pi | 107 | 15.90 h | 8.91 min | 4.07 min | 21.62 min |
| Agent 执行 | DSH | 108 | 14.44 h | 8.02 min | 4.76 min | 14.52 min |
| Evaluator | Astra | 108 | 36.26 min | 20.15 s | 12.68 s | 38.53 s |
| Evaluator | Hermes | 108 | 36.21 min | 20.12 s | 11.40 s | 32.76 s |
| Evaluator | Pi | 107 | 43.34 min | 24.30 s | 10.75 s | 34.52 s |
| Evaluator | DSH | 108 | 32.52 min | 18.07 s | 9.61 s | 34.30 s |
| Orchestration/收尾 | Astra | 108 | 52.94 min | 29.41 s | 27.96 s | 40.95 s |
| Orchestration/收尾 | Hermes | 108 | 2.77 h | 92.36 s | 101.83 s | 2.38 min |
| Orchestration/收尾 | Pi | 107 | 1.12 h | 37.53 s | 21.93 s | 45.92 s |
| Orchestration/收尾 | DSH | 108 | 38.14 min | 21.19 s | 21.00 s | 22.84 s |

时间包含通过和未通过任务。`orchestration` 是端到端减去 Agent 与 evaluator 后的剩余时间，不是纯准备时间。不同产品的收尾、重试和内部请求边界不同。

## 工具调用

| 指标 | Astra | Hermes | Pi | DSH |
| --- | ---: | ---: | ---: | ---: |
| 有工具计数的运行 | 108 | 108 | 107 | 108 |
| 工具调用总数 | 4,345 | 4,848 | 4,265 | 3,892 |
| 单运行平均 | 40.23 | 44.89 | 39.86 | 36.04 |
| 单运行中位数 | 31.50 | 32.50 | 29 | 33 |
| 单运行 P90 | 75.60 | 78.60 | 76 | 65.50 |
| 失败工具事件总数 | 56 | 0 | 247 | 130 |

常见终态工具：

- Astra：`local-python-execute` 464、`terminal-run_command` 234、`woocommerce-woo_products_list` 172、`wandb-query_wandb_tool` 153、`word-add_paragraph` 151。
- Hermes：`terminal` 754、`word-add_paragraph` 392、`local-python-execute` 288、`terminal-run_command` 232、`emails-send_email` 129。
- Pi：`bash` 751、`local-python-execute` 371、`terminal-run_command` 142、`canvas-canvas_list_folders` 132、`google-cloud-bigquery_run_query` 104。
- DSH：`bash` 805、`local-python-execute` 172、`read` 156、`local-web_search` 126、`fetch-fetch_txt` 114。

工具名称、封装粒度和失败事件采集方式不同。失败事件可能在重试后恢复，不能直接作为产品可靠性排名。

## 模型请求

| 指标 | Astra | Hermes | Pi | DSH |
| --- | ---: | ---: | ---: | ---: |
| 统计运行数 | 108 | 108 | 107 | 108 |
| 模型请求 started | 5,229 | 3,287 | 2,595 | 3,089 |
| 模型请求 completed event | 5,229 | 3,284 | 2,595 | 3,089 |
| 失败 completed event | 19 | 26 | 15 | 949 |
| 单运行 started 平均 | 48.42 | 30.44 | 24.25 | 28.60 |
| 单运行 started 中位数 | 33.50 | 20 | 16 | 24 |
| 单运行 started P90 | 100 | 66.50 | 48.80 | 58.30 |
| 触及 100 请求上限 | 23 | 3 | 4 | 0 |
| 因请求预算 no-pass | 20 | 3 | 2 | 0 |
| 流式请求 | 2,226（42.57%） | 3,081（93.73%） | 2,595（100.00%） | 3,089（100.00%） |
| 非流式请求 | 3,003（57.43%） | 206（6.27%） | 0（0.00%） | 0（0.00%） |

DSH 的最新投影中，80 / 108 题记录过 transport 错误，共 949 次，类型为 `ECONNRESET` 452、`UND_ERR_SOCKET` 373、`downstream_disconnected` 124。这些 DSH 失败 completed event 主要表示 sidecar 未获得完整上游响应，并不是 DeepSeek HTTP/API 业务错误响应。四产品的请求结构和重试策略不同，因此请求总量不能直接解释为模型效率。

## Token 数据

### 保守可靠记录

若一个 effective run 的 completed response 存在缺失 usage，该运行不进入本表：

| 产品 | 完整 usage 运行 | 输入 token 总量 | 输出 token 总量 | total token 总量 | 单运行 total 中位数 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Astra | 95 / 108 | 114,783,243 | 8,791,196 | 123,574,439 | 649,739 |
| Hermes | 82 / 108 | 142,142,325 | 1,964,613 | 144,106,938 | 1,174,439 |
| Pi | 103 / 107 | 230,864,137 | 2,268,979 | 233,133,116 | 653,387 |
| DSH | 41 / 108 | 78,999,127 | 1,166,424 | 80,165,551 | 1,184,269 |

“保守可靠记录”采用运行级完备性口径：只有一个 effective run 的每个 `model_request.completed` 都具有 provider 明确上报的 input/output/total token，该运行才进入本表。对 DSH 而言，41 / 108 表示只有 41 题可以完整求和；其余 67 题至少有一次请求因 transport 中断缺失 usage，并不表示这些任务没有 token 数据或 token 为 0。

### 全部可见 token 下界

| 产品 | 运行数 | 已上报 / 缺 usage 的 completed 请求 | 输入 token 下界 | 输出 token 下界 | total token 下界 | 单运行 total 中位数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Astra | 108 | 5,210 / 19 | 149,741,746 | 11,654,317 | 161,396,063 | 756,014 |
| Hermes | 108 | 3,256 / 28 | 244,960,861 | 3,124,778 | 248,085,639 | 1,364,392 |
| Pi | 107 | 2,580 / 15 | 237,628,246 | 2,360,566 | 239,988,812 | 653,387 |
| DSH | 108 | 2,257 / 832 | 140,602,287 | 2,070,850 | 142,673,137 | 790,235 |

“全部可见 token 下界”覆盖所有有完整 run artifact 的任务，只累加每个请求已经明确上报的 usage，缺失 usage 保持未知，不补零。DSH 的 142,673,137 个可见 total token 来自 2,257 次有 usage 的请求，其中包含成功响应、断开前已经拿到 usage 的失败响应，以及流失败后成功重试的新请求；另外 832 次失败请求没有 usage，可能仍已消耗完整输入和部分输出。因此该数值是 DSH 实际资源消耗的严格下界，而不是账单总量。

DSH 断流后会重放完整请求，所以可见下界可能接近“每个逻辑步骤一次成功”时的工作量，但不能当作无链路波动的反事实估计：部分失败请求已有可见 usage，重试生成也可能改变后续 Agent 路径。Pi 和 DSH 的 input 包含 provider 单独报告的 cache-read token；工具 schema、上下文组织、缓存和内部请求边界也不同，不能据此直接计算产品成本或底层模型效率。

### Pass 任务的可靠 Token

| 产品 | 有可靠 token 的 pass 任务 | total token 总量 | 单任务平均 | 单任务中位数 |
| --- | ---: | ---: | ---: | ---: |
| Astra | 56 / 61 | 44,004,486 | 785,794 | 518,071 |
| Hermes | 54 / 72 | 88,693,746 | 1,642,477 | 1,045,431 |
| Pi | 77 / 77 | 98,717,289 | 1,282,043 | 586,542 |
| DSH | 23 / 79 | 39,671,821 | 1,724,862 | 970,818 |

四者通过的任务集合不同；该表只描述各自产生的可靠 token 足迹，不是单位成功成本的严格对比。

## 综合结论

1. **DSH 的已确认通过数最多，但 Pi 覆盖不完整。** DSH 为 79 / 108，Pi 为 77 / 104 个明确结果，Hermes 为 72 / 108，Astra 为 61 / 108。DSH 按全部 108 题通过率最高（73.15%），Pi 按已测评题通过率为 74.04%；Pi 的 4 个未明确 slot 使二者不能形成无条件总排名。
2. **逐题优势不是包含关系。** 在 Pi 与 DSH 可比较的 104 题中，DSH-only 为 8 题，Pi-only 为 9 题；产品主循环、工具策略与自检行为都会改变结果。
3. **No-pass 结构明显不同。** Astra 有 20 题因请求预算终止；Hermes 和 Pi 分别为 3、2 题；DSH 没有预算型 no-pass，但有 8 个产品执行错误和 1 个 Agent deadline。
4. **DSH 的典型运行时间接近 Pi，但网络重试污染请求足迹。** DSH 端到端中位数为 5.32 min，Pi 为 4.83 min；DSH 的 949 次 transport 错误使请求量和 token 完整率不能按正常稳定链路解释。
5. **模型请求不是同构 turn。** 四产品 started 中位数分别为 33.50、20、16、24；内部请求、流式策略和重试边界不同。
6. **Token 对比受完整率与缓存影响。** DSH 只有 41 / 108 个 latest run 具备完整 usage，明显受到 transport 失败影响；总 token 不能作为四产品能力或成本排名。
7. **Effective projection 是必要但有限的汇总。** Pi 混合 3 个批次，DSH 混合 8 个批次；最新或优先覆盖解决了 slot 选择问题，但没有消除运行时段、服务状态、凭据和网络条件差异。

## 附件

- 原三产品报告：[`astra-hermes-pi-toolathlon-108-task-comparison.md`](astra-hermes-pi-toolathlon-108-task-comparison.md)
- DSH 单产品报告：[`dsh-toolathlon-108-task-analysis.md`](dsh-toolathlon-108-task-analysis.md)
- Astra/Hermes/Pi 逐 slot 数据：[`astra-hermes-pi-toolathlon-108-task-results.csv`](astra-hermes-pi-toolathlon-108-task-results.csv)
- DSH 全批次逐 attempt 数据：[`dsh-toolathlon-all-attempt-results.csv`](dsh-toolathlon-all-attempt-results.csv)
- 本报告生成脚本：[`generate_astra_hermes_pi_dsh_comparison.py`](generate_astra_hermes_pi_dsh_comparison.py)
