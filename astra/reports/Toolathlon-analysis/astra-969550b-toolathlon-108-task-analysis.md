# Astra 969550b：Toolathlon 108 题结果分析

生成日期：2026-09-15

范围：Toolathlon 固定 108 题；Astra commit `969550b611ceba11653c4b2651079bcb85d1c24e`。

## 口径

- Snowflake 账号不可用的四题按用户指定引用上一轮 Astra 结果：`payable-invoice-checker`、`sla-timeout-monitor`、`landing-task-reminder` 为历史 pass；`travel-expense-reimbursement` 为历史 no_pass。这些是跨版本替代，不能视为本 commit 的新执行。
- 分母固定为 108；通过与否以选定 Evaluator 或明确标注的复核／历史结果为准，不以 Agent 自称完成代替。
- 耗时、工具、模型请求与 Token 主口径覆盖 108 题，包含四题上一轮历史数据；补充列展示去除四题的 104 题口径。离线复制只计一次，仅重评不重复计入 Agent 工作量；未选用尝试及人工维护成本不计入。该混合版本统计不代表本 commit 独立运行的总成本。
- 缺失 usage 保持未知；可见 token 合计为下界。

## 实验产品与配置

| 项目           | Astra                                                                               |
| -------------- | ----------------------------------------------------------------------------------- |
| 产品版本       | Astra`969550b` 源码快照，`astra-969550b:local` 服务镜像                         |
| Commit         | `969550b611ceba11653c4b2651079bcb85d1c24e`                                        |
| API 模型 ID    | `deepseek-v4-flash`                                                               |
| 模型提供方     | DeepSeek 官方 API；经 runner 本地模型代理                                           |
| 推理配置       | Thinking enabled；`reasoning_effort=max`                                          |
| Temperature    | 发送`temperature=0`；Thinking 模式下不据此推断实际采样行为                        |
| 请求预算       | 每题最多 100 次 product model request；受控续接仍共享总预算                         |
| Agent deadline | 以每题 run.json 为准：{'3600': 10, '5400': 71, '2700': 15, '1800': 8}（秒：运行数） |
| Prompt 与工具  | Astra 原生 Agent 循环及内置工具；通过 Gateway 暴露当前任务 MCP                      |
| 隔离           | 每 attempt 独立用户身份、session 和任务工作区；保留文件系统隔离                     |
| 数据库生命周期 | 实验中由逐题建库调整为共享库；任务间取消、归档和清理执行状态                        |
| 恢复策略       | runner 受控续接；后期增加每完成 5 题重启 MatrixOne 的部署策略，非整个批次统一配置   |
| 仅重评         | 保留原工作区与启动时间；本次 Notion 目标纠正重评每题限 600 秒                       |

## 基础运行环境

| 环境项     | 配置                                                                                                         |
| ---------- | ------------------------------------------------------------------------------------------------------------ |
| 宿主机     | matrixorigin-vm；Linux Vagrant 虚拟机                                                                        |
| 容器运行时 | rootful Docker；Toolathlon 固定任务镜像                                                                      |
| 任务镜像   | `lockon0927/toolathlon-task-image@sha256:4d04fe4e0a6fdb4946f51bb05120cb44a0eef980231c11252f93b62897afcb9f` |
| 网络       | Astra 使用 host 网络访问本地代理及 Gateway；外部连接使用 ShellCrash 透明／显式代理                           |
| 应用状态   | 由 Toolathlon 每题 preprocess 恢复，本地应用及 K8s 等服务按部署共享                                          |
| 持久化     | MatrixOne 与 Memoria；实验期间发生服务重启和维护                                                             |
| Evaluator  | Agent 结束后调用 Toolathlon 原生每题 Evaluator；离线复核单独标注                                             |

硬件容量、连接池状态与代理选择曾随维护发生变化；本报告不把当前服务器快照倒推为所有历史运行的统一资源配置。

## 任务完成结果

| 指标 | 结果 |
| --- | --- |
| 基准题目 | 108 |
| pass | 80 |
| no_pass | 28 |
| 按 108 题通过率 | 74.07% |
| 跨版本历史替代 | 4 题（3 pass、1 no_pass） |
| 去除跨版本历史，104分母 | 74.04%（77 pass、27 no_pass） |

## No-pass 原因

| 直接终态分类                       | 题数 |
| ---------------------------------- | ---- |
| Agent 完成，但交付未满足 Evaluator | 26   |
| 历史替代：达到模型请求预算         | 1    |
| 达到模型请求预算                   | 1    |

分类按选定运行的终态与请求预算记录；历史替代单独计数。工具或传输错误可能恢复，事件数不等同于失败题数。Notion 两题已按原页面正式重评，失败证据来自实际内容比对；此前目标错配产生的 404 不用于本报告判定。

## 时间消耗

| 指标 | 主口径：含四题历史数据 | 补充：去除四题历史数据 |
| --- | --- | --- |
| 端到端（含准备与收尾）：样本数 | 108 / 108 | 104 / 104 |
| 端到端（含准备与收尾）：总计 | 25.82 h | 24.46 h |
| 端到端（含准备与收尾）：平均 | 14.34 min | 14.11 min |
| 端到端（含准备与收尾）：中位数 | 11.73 min | 11.50 min |
| 端到端（含准备与收尾）：P90 | 24.37 min | 23.75 min |
| Agent 执行：样本数 | 108 / 108 | 104 / 104 |
| Agent 执行：总计 | 20.71 h | 19.46 h |
| Agent 执行：平均 | 11.51 min | 11.23 min |
| Agent 执行：中位数 | 9.21 min | 8.63 min |
| Agent 执行：P90 | 21.08 min | 21.07 min |
| 原执行 Evaluator：样本数 | 107 / 108 | 103 / 104 |
| 原执行 Evaluator：总计 | 0.81 h | 0.75 h |
| 原执行 Evaluator：平均 | 0.45 min | 0.44 min |
| 原执行 Evaluator：中位数 | 0.25 min | 0.24 min |
| 原执行 Evaluator：P90 | 0.70 min | 0.61 min |
| Orchestration / 收尾：样本数 | 107 / 108 | 103 / 104 |
| Orchestration / 收尾：总计 | 4.25 h | 4.21 h |
| Orchestration / 收尾：平均 | 2.38 min | 2.45 min |
| Orchestration / 收尾：中位数 | 1.59 min | 1.60 min |
| Orchestration / 收尾：P90 | 4.29 min | 4.48 min |

时间取选定执行的生命周期记录或四题上一轮统计；缺少阶段边界不补零。仅重评的额外耗时仍单独计算，不重复计入 Agent 工作量。5 次最终采用的仅重评另耗时 3.44 分钟。

## 工具调用

| 指标 | 主口径：含四题历史数据 | 补充：去除四题历史数据 |
| --- | --- | --- |
| 有计数的执行 | 107 / 108 | 103 / 104 |
| 工具终态事件总数 | 5,789 | 5,616 |
| 单执行平均 | 54.10 | 54.52 |
| 中位数 | 40.00 | 40.00 |
| P90 | 90.40 | 90.80 |
| 失败工具事件 | 206 | 206 |

计数采用各选定执行的工具统计；未配对的 started 不作为已完成调用。失败工具事件不等于失败任务。

## 模型请求

| 指标 | 主口径：含四题历史数据 | 补充：去除四题历史数据 |
| --- | --- | --- |
| 统计执行数 | 108 | 104 |
| 请求 started | 3,544 | 3,295 |
| 请求 completed | 3,544 | 3,295 |
| 成功 completed | 3,430 | 3,181 |
| 失败 completed | 114 | 114 |
| 每执行请求平均 | 32.81 | 31.68 |
| 中位数 | 29.00 | 29.00 |
| P90 | 58.30 | 57.70 |
| 流式请求 | 3,259 | 3,151 |
| 记录预算超限的执行 | 2 | 1 |

请求计数包含产品重试；历史四题沿用上一轮完整请求口径，包含内部非流式请求，不等同于推理轮数。

## Token 数据

### 全部可见 token 下界

| 指标 | 主口径：含四题历史数据 | 补充：去除四题历史数据 |
| --- | --- | --- |
| 选定执行数 | 108 | 104 |
| 有 usage 的 completed | 3,430 | 3,181 |
| 缺 usage 的 completed | 114 | 114 |
| 输入 token 下界 | 255,959,726 | 249,513,816 |
| 输出 token 下界 | 6,973,940 | 6,496,508 |
| total token 下界 | 262,933,666 | 256,010,324 |
| 可见 cache-read token 下界 | 235,176,832 | 235,176,832 |
| 有 cache-read 数值的执行 | 104 / 108 | 104 / 104 |

输入 token 已包含缓存命中部分，cache-read 不重复相加。缺失 usage 不补零；合计仅代表已观测下界。四题历史数据未提供 cache-read，保持未知。

### Pass 任务的可见 Token 下界

| 指标 | 主口径：含四题历史数据 | 补充：去除四题历史数据 |
| --- | --- | --- |
| Pass 执行数 | 80 | 77 |
| 有可见 total token 的执行 | 80 / 80 | 77 / 77 |
| 输入 token 下界 | 143,705,909 | 140,742,467 |
| 输出 token 下界 | 4,075,609 | 3,871,812 |
| total token 下界 | 147,781,518 | 144,614,279 |
| 单执行平均可见 total | 1,847,268.98 | 1,878,107.52 |
| 单执行中位数可见 total | 1,319,941.00 | 1,389,641.00 |

纳入全部选定 Pass 执行的可见 usage，不再按 usage_reliable 筛选。平均与中位数以有数值的执行为样本，不将完全缺失的执行补零。补充列仅用于当前详细报告，后续汇总报告不携带该列。

## 综合结论

1. 调整后 108 题全部有判定：**80 pass、28 no_pass，通过率 74.07%**。
2. 四题引用上一轮跨版本结果，一题采用离线复核；原生口径为 76 pass、31 no_pass、1 未评测／排除。
3. 18 题重跑最终为 12 pass、6 no_pass；原环境导致的中断经恢复后，仍须以重新执行的 Evaluator 判断交付。
4. 本报告是恢复和重评后每题选定结果的汇总，不代表同一配置下连续完成的一次 108 题批次。资源消耗只覆盖选定执行，不能作为包括所有重试与运维的总成本。

## 附件

- 108 题结果与指标：`astra-969550b-toolathlon-108-task-results.csv`
- 结构化汇总：`astra-969550b-toolathlon-108-task-summary.json`
- 生成脚本：`generate_astra_969550b_report.py`



## 附录：108 题逐题结果

模型响应列为“成功 completed / 全部 completed 事件”，失败完成事件不代表成功模型响应；Tool calls 为工具终态计数，Agent 时间单位为秒。缺失计数记为未知，不补零。历史替代及离线复核单独标注。

| 序号 | 任务 | 结果 | 结束状态 | 模型响应（成功 / 全部完成事件） | Tool calls | Agent 时间（秒） | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `ab-testing` | pass | completed | 15 / 16 | 23 | 222.38 |  |
| 2 | `academic-pdf-report` | pass | completed | 28 / 30 | 73 | 464.92 |  |
| 3 | `academic-warning` | pass | completed | 24 / 25 | 41 | 419.72 |  |
| 4 | `add-bibtex` | pass | completed | 48 / 50 | 66 | 689.96 |  |
| 5 | `apply-phd-email` | pass | completed | 18 / 19 | 28 | 180.29 |  |
| 6 | `arrange-workspace` | no_pass | completed | 18 / 18 | 19 | 192.73 |  |
| 7 | `canvas-arrange-exam` | pass | completed | 29 / 30 | 79 | 538.54 |  |
| 8 | `canvas-art-manager` | pass | completed | 54 / 55 | 186 | 921.08 |  |
| 9 | `canvas-art-quiz` | pass | completed | 18 / 19 | 19 | 185.94 |  |
| 10 | `canvas-do-quiz` | pass | completed | 35 / 36 | 128 | 746.45 |  |
| 11 | `canvas-homework-grader-python` | pass | completed | 36 / 37 | 70 | 356.43 |  |
| 12 | `canvas-list-test` | no_pass | completed | 55 / 56 | 60 | 810.48 |  |
| 13 | `canvas-new-students-notification` | pass | completed | 30 / 31 | 82 | 372.35 |  |
| 14 | `canvas-submit-late-work` | pass | completed | 20 / 21 | 31 | 312.47 |  |
| 15 | `cooking-guidance` | pass | completed | 18 / 19 | 58 | 440.06 |  |
| 16 | `course-assistant` | no_pass | completed | 15 / 15 | 25 | 128.05 |  |
| 17 | `course-schedule` | pass | completed | 15 / 15 | 22 | 320.64 |  |
| 18 | `courses-ta-hws` | pass | completed | 14 / 15 | 17 | 166.20 |  |
| 19 | `cvpr-research` | pass | completed | 21 / 22 | 30 | 760.20 |  |
| 20 | `dataset-license-issue` | pass | completed | 41 / 43 | 59 | 758.23 |  |
| 21 | `detect-revised-terms` | no_pass | completed | 17 / 18 | 29 | 414.48 |  |
| 22 | `dietary-health` | pass | completed | 12 / 13 | 19 | 289.26 |  |
| 23 | `email-paper-homepage` | no_pass | completed | 29 / 30 | 80 | 576.91 |  |
| 24 | `excel-data-transformation` | pass | completed | 19 / 19 | 20 | 237.69 |  |
| 25 | `excel-market-research` | pass | completed | 12 / 12 | 12 | 166.28 |  |
| 26 | `experiments-recordings` | no_pass | completed | 73 / 76 | 81 | 1,120.27 | 仅重评覆盖 |
| 27 | `fillout-online-forms` | pass | completed | 31 / 31 | 51 | 610.59 |  |
| 28 | `filter-low-selling-products` | pass | crashed | 31 / 31 | 37 | 760.50 |  |
| 29 | `find-alita-paper` | pass | completed | 15 / 16 | 24 | 779.95 |  |
| 30 | `flagged-transactions` | pass | completed | 18 / 19 | 25 | 282.72 |  |
| 31 | `game-statistics` | pass | completed | 14 / 15 | 26 | 277.80 |  |
| 32 | `gdp-cr5-analysis` | pass | completed | 39 / 39 | 40 | 818.84 |  |
| 33 | `git-bug-hunt` | pass | completed | 12 / 13 | 13 | 145.44 |  |
| 34 | `git-milestone` | pass | completed | 7 / 8 | 14 | 142.76 |  |
| 35 | `git-repo` | pass | completed | 16 / 17 | 21 | 246.22 |  |
| 36 | `hk-top-conf` | no_pass | completed | 93 / 97 | 95 | 2,263.10 |  |
| 37 | `huggingface-upload` | pass | completed | 35 / 36 | 45 | 870.11 | 仅重评覆盖 |
| 38 | `identify-all-songs` | pass | completed | 29 / 30 | 39 | 453.65 |  |
| 39 | `imagenet` | pass | completed | 14 / 15 | 24 | 145.68 |  |
| 40 | `inter-final-performance-analysis` | pass | completed | 61 / 63 | 106 | 1,416.65 |  |
| 41 | `interview-report` | pass | completed | 56 / 58 | 420 | 1,249.77 |  |
| 42 | `inventory-sync` | pass | completed | 40 / 41 | 131 | 607.92 |  |
| 43 | `investment-decision-analysis` | no_pass | completed | 18 / 19 | 46 | 785.79 |  |
| 44 | `invoice-org` | pass | completed | 33 / 35 | 71 | 1,019.96 |  |
| 45 | `ipad-edu-price` | no_pass | completed | 20 / 21 | 34 | 380.31 |  |
| 46 | `k8s-deployment-cleanup` | pass | completed | 22 / 23 | 40 | 281.46 |  |
| 47 | `k8s-mysql` | no_pass | completed | 56 / 57 | 61 | 1,235.08 |  |
| 48 | `k8s-pr-preview-testing` | pass | completed | 57 / 59 | 69 | 816.91 |  |
| 49 | `k8s-redis-helm-upgrade` | pass | completed | 79 / 81 | 90 | 1,257.83 |  |
| 50 | `k8s-safety-audit` | pass | completed | 36 / 37 | 49 | 731.49 |  |
| 51 | `landing-task-reminder` | pass | completed | 55 / 55 | 39 | 866.54 | 历史版本替代 |
| 52 | `language-school` | no_pass | completed | 31 / 32 | 81 | 692.28 |  |
| 53 | `latex-prompt-box` | pass | completed | 63 / 65 | 75 | 874.46 |  |
| 54 | `live-transactions` | no_pass | completed | 21 / 22 | 35 | 496.61 |  |
| 55 | `llm-training-dataset` | pass | completed | 48 / 49 | 59 | 978.98 |  |
| 56 | `logical-datasets-collection` | pass | completed | 18 / 19 | 45 | 584.96 |  |
| 57 | `machine-operating` | pass | completed | 28 / 29 | 42 | 456.30 |  |
| 58 | `meeting-assign` | pass | completed | 14 / 15 | 17 | 259.60 |  |
| 59 | `merge-hf-datasets` | no_pass | completed | 29 / 30 | 30 | 434.19 |  |
| 60 | `mrbeast-analysis` | no_pass | completed | 74 / 76 | 131 | 1,264.93 |  |
| 61 | `music-analysis` | pass | completed | 28 / 29 | 30 | 559.78 |  |
| 62 | `nhl-b2b-analysis` | pass | completed | 24 / 25 | 29 | 386.11 |  |
| 63 | `notion-find-job` | pass | completed | 33 / 35 | 39 | 594.55 |  |
| 64 | `notion-hr` | pass | completed | 28 / 29 | 89 | 594.12 |  |
| 65 | `notion-movies` | pass | completed | 23 / 24 | 40 | 376.56 |  |
| 66 | `notion-personal-website` | pass | completed | 30 / 31 | 47 | 772.05 |  |
| 67 | `nvidia-market` | no_pass | completed | 61 / 64 | 85 | 1,707.84 |  |
| 68 | `nvidia-stock-analysis` | pass | completed | 36 / 37 | 73 | 1,104.63 | 仅重评覆盖 |
| 69 | `oil-price` | no_pass | completed | 62 / 63 | 107 | 2,344.99 | 仅重评覆盖 |
| 70 | `paper-checker` | pass | completed | 37 / 38 | 49 | 489.41 |  |
| 71 | `payable-invoice-checker` | pass | completed | 49 / 49 | 45 | 637.72 | 历史版本替代 |
| 72 | `personal-website-construct` | pass | completed | 39 / 40 | 72 | 771.71 |  |
| 73 | `ppt-analysis` | pass | completed | 24 / 25 | 49 | 437.53 |  |
| 74 | `price-comparison` | pass | completed | 14 / 15 | 18 | 218.82 |  |
| 75 | `privacy-desensitization` | no_pass | completed | 21 / 22 | 23 | 424.72 |  |
| 76 | `profile-update-online` | pass | completed | 34 / 36 | 57 | 1,152.77 |  |
| 77 | `quantitative-financial-analysis` | pass | completed | 15 / 16 | 40 | 369.65 |  |
| 78 | `reimbursement-form-filler` | pass | completed | 28 / 29 | 31 | 414.23 |  |
| 79 | `sales-accounting` | pass | completed | 10 / 10 | 11 | 130.68 |  |
| 80 | `search-ca-school` | pass | completed | 19 / 20 | 33 | 692.92 |  |
| 81 | `set-conf-cr-ddl` | pass | completed | 12 / 13 | 32 | 216.44 |  |
| 82 | `shopping-helper` | no_pass | completed | 38 / 38 | 37 | 676.87 |  |
| 83 | `sla-timeout-monitor` | pass | completed | 45 / 45 | 33 | 544.92 | 历史版本替代 |
| 84 | `stock-build-position` | pass | completed | 12 / 13 | 25 | 258.04 |  |
| 85 | `student-interview` | pass | completed | 11 / 12 | 37 | 285.17 |  |
| 86 | `subway-planning` | pass | completed | 20 / 20 | 48 | 480.79 |  |
| 87 | `sync-todo-to-readme` | pass | completed | 14 / 15 | 14 | 313.11 |  |
| 88 | `task-tracker` | no_pass | completed | 54 / 55 | 143 | 1,840.26 |  |
| 89 | `train-ticket-plan` | pass | completed | 20 / 21 | 33 | 295.99 |  |
| 90 | `travel-exchange` | no_pass | completed | 34 / 36 | 87 | 1,806.32 |  |
| 91 | `travel-expense-reimbursement` | no_pass | max_steps | 100 / 100 | 56 | 2,444.64 | 历史版本替代 |
| 92 | `trip-adviser` | pass | completed | 14 / 15 | 34 | 561.51 |  |
| 93 | `trip-itinerary-generator` | pass | completed | 11 / 11 | 26 | 293.11 | 仅重评覆盖 |
| 94 | `university-course-selection` | no_pass | completed | 33 / 34 | 34 | 858.11 |  |
| 95 | `update-material-inventory` | pass | completed | 16 / 17 | 26 | 275.00 |  |
| 96 | `upenn-campus-route` | pass | completed | 21 / 22 | 42 | 581.48 |  |
| 97 | `verl-dataset` | pass | completed | 22 / 22 | 28 | 794.85 |  |
| 98 | `vlm-history-completer` | no_pass | completed | 32 / 33 | 76 | 1,264.84 |  |
| 99 | `wandb-best-score` | pass | completed | 28 / 29 | 37 | 424.93 |  |
| 100 | `wandb-shortest-length` | pass | completed | 16 / 16 | 29 | 322.25 |  |
| 101 | `woocommerce-customer-survey` | pass | completed | 47 / 49 | 91 | 1,263.18 | 离线复核 |
| 102 | `woocommerce-new-product` | no_pass | max_steps | 97 / 100 | 149 | 2,795.75 |  |
| 103 | `woocommerce-new-welcome` | no_pass | completed | 30 / 31 | 未知 | 1,969.62 |  |
| 104 | `woocommerce-product-recall` | no_pass | completed | 53 / 54 | 62 | 1,172.37 |  |
| 105 | `woocommerce-stock-alert` | no_pass | completed | 23 / 24 | 29 | 326.26 |  |
| 106 | `woocommerce-update-cover` | pass | completed | 18 / 19 | 23 | 263.65 |  |
| 107 | `yahoo-analysis` | pass | completed | 20 / 21 | 24 | 451.33 |  |
| 108 | `youtube-repo` | no_pass | completed | 47 / 49 | 85 | 1,312.80 |  |
