# Astra 与 Hermes：Terminal-Bench 常规任务对比（88 个最新配对任务）

生成日期：2026-08-04  
数据范围：Terminal-Bench 同一任务集的常规运行结果；每个产品、每个任务只取最新一次记录。

## 口径

- 配对任务数为 **88**：两份最新汇总 CSV 的 86 个共同任务，加上 Astra 从头重跑中较新的 `torch-tensor-parallelism` 与 `train-fasttext` 两条记录。
- 完成状态分为 `pass`、`no-pass` 与 `verify unavailable`。`verify unavailable` 单列展示，**不计入 no-pass，也不进入通过率分母**。
- “正常端到端成功”严格定义为：`verify pass` 且该任务 **无 timeout**。它是任务层面的成功指标，不使用运行器返回码、轨迹或其他过程字段作附加门槛。
- timeout 覆盖产品、运行器和 verifier 阶段的明确超时信号；它在完成状态之外单独统计。
- 不比较美元成本；不纳入过程完整性、轨迹质量、故障注入或 verifier 测试明细。

## 任务完成结果

| 指标 | Astra | Hermes |
|---|---:|---:|
| 最新配对任务 | 88 | 88 |
| verify pass | 44 | 50 |
| no-pass（不含 unavailable） | 42 | 38 |
| verify unavailable | 2 | 0 |
| 有可用 verify 结果的任务 | 86 | 88 |
| verify pass rate（仅有结果任务） | 51.16% (44/86) | 56.82% (50/88) |
| 正常端到端成功（pass 且无 timeout） | 41 | 47 |
| timeout | 39 | 10 |
| 无 timeout | 49 | 78 |

在双方均有 verify 结果的 86 个任务中，30 个均通过、23 个均未通过；Hermes 独自通过 19 个，Astra 独自通过 14 个。按上述“pass 且无 timeout”定义，Hermes 有 47 个正常端到端成功，Astra 有 41 个。

timeout 的配对分布为：双方均无 timeout 46 个、仅 Astra timeout 32 个、双方 timeout 7 个、仅 Hermes timeout 3 个。timeout 在此作为运行结果的一部分，不能据此推断为需要重跑的基础设施故障。

## Astra no-pass 原因小结

以下只对 Astra 的 42 个 `no-pass` 任务归类；两个 `verify unavailable` 任务不计入其中。

| 主要原因 | 任务数 | 占 Astra no-pass | 说明 |
|---|---:|---:|---|
| LLM 请求超时并伴随 stream transport 中断 | 32 | 76.2% | 记录有 `llm_request_timeout`；任务未得到可验证完成结果。 |
| Controller deadline 疑似到期 | 2 | 4.8% | 有 deadline 超时信号，未形成通过结果。 |
| 任务完成但 verifier 未通过 | 6 | 14.3% | 无 timeout；产品运行完成，但最终产物未满足 verifier。 |
| Adapter / runner 基础设施错误 | 2 | 4.8% | 无 timeout；分别表现为预算耗尽或 runner/controller 异常。 |

因此，当前 Astra no-pass 的主导因素是 LLM 请求/回传超时（32/42），其次才是已完成但产物未通过验证的能力性失败（6/42）。这张表描述本批最新运行记录中的终态原因，不将 timeout 自动解释为需要重跑。

## 两条 Astra verify unavailable 记录

| 任务 | 状态 | 原因 | timeout | 对通过率处理 |
|---|---|---|---:|---|
| `torch-tensor-parallelism` | unavailable | verifier 环境在安装 PyTorch/CUDA 依赖期间超过 1,800 秒 | 是 | 从 Astra 通过率分母和 no-pass 中排除 |
| `train-fasttext` | unavailable | agent 阶段结束后，runner 读取产品 stdout 的命令在 5 秒限制内超时；未进入 verifier | 是 | 从 Astra 通过率分母和 no-pass 中排除 |

前者是 verifier 环境构建/依赖下载超时；后者不是 verifier 环境失败。两者均保留在 88 个任务的时间与 timeout 统计中。

## 时间消耗

下表以每个产品的 88 个最新任务为单位；verifier 时间的 Astra 样本量为 87（`train-fasttext` 无 verifier），Hermes 为 88。

| 阶段 | 产品 | 总计 | 中位数 | P90 |
|---|---|---:|---:|---:|
| 端到端 | Astra | 43.69 h | 17.19 min | 64.11 min |
| 端到端 | Hermes | 33.00 h | 13.52 min | 40.40 min |
| Agent 执行 | Astra | 34.70 h | 15.70 min | 54.27 min |
| Agent 执行 | Hermes | 30.71 h | 12.12 min | 38.73 min |
| 环境准备 | Astra | 1.76 h | 5.67 s | 20.33 s |
| 环境准备 | Hermes | 2.45 min | 1.36 s | 2.25 s |
| Agent 准备 | Astra | 33.06 min | 3.98 s | 74.89 s |
| Agent 准备 | Hermes | 8.87 min | 5.23 s | 7.00 s |
| Verifier | Astra | 6.12 h | 28.74 s | 167.55 s |
| Verifier | Hermes | 1.78 h | 28.29 s | 117.72 s |

以同一任务配对计算，Astra 相对 Hermes 的端到端时间中位差为 **+1.99 分钟**，Agent 执行时间中位差为 **+0.69 分钟**。总时间包含超时任务，因此用于描述当前运行负担，不应被解释为仅成功任务的速度比较。

## 工具调用

工具调用以终态工具事件计数：Astra 使用 `tool_calls_terminal`，Hermes 使用 `tool_calls`。两者都反映已完成或已报告终态的调用，但工具命名和封装不同，因此适合比较工作负担，不适合解释为完全等价的工具效率。

| 指标 | Astra | Hermes |
|---|---:|---:|
| 有工具计数的任务 | 86 | 88 |
| 工具调用总数 | 2,253 | 3,101 |
| 单任务工具调用中位数 | 18 | 20 |
| 单任务工具调用 P90 | 59 | 90 |
| 失败工具调用总数 | 155 | 275 |
| 单任务失败工具调用中位数 | 1 | 2 |
| 单任务失败工具调用 P90 | 5 | 8 |

在 86 个双方都有工具数据的配对任务中，Astra 相对 Hermes 的单任务工具调用中位差为 **-5**，失败工具调用中位差为 **-1**。常见工具类型分别为：Astra 的 `bash`（1,546）、`read_file`（258）和 `task_board`（104）；Hermes 的 `terminal`（2,124）、`write_file`（213）和 `read_file`（204）。

## Token 数据口径与覆盖率

已使用完善后的 Astra token 汇总重新计算。Token 仅作为各产品内部的已上报模型用量足迹，不作 Astra 与 Hermes 的跨产品成本或效率胜负比较。

| 指标 | Astra | Hermes |
|---|---:|---:|
| 保守可用 token 记录 | 83（`session_reconciled` 或 `server_reconciled`） | 76（`reported`） |
| 保守可用输入 token 总量 | 29,207,308 | 88,169,676 |
| 保守可用输出 token 总量 | 3,441,787 | 1,945,545 |
| 保守可用 token 总量 | 32,649,095 | 90,115,221 |
| 带数值 token_total 的记录 | 85 | 78 |
| 带数值 token_total 总量 | 34,138,962 | 90,115,221 |
| 保守可用 token_total 中位数 | 236,084 | 378,155 |
| 未可靠上报或缺失 | 5 | 12 |

Astra 的可靠记录由 `session_reconciled` 或 `server_reconciled` 标识；其余 3 条 CSV 记录为无完整可比 token 的辅助状态，另有两条 `verify unavailable` 重跑记录无 token。Hermes 的规范来源是终态 `hermes-run.json.usage`，其中 10 条为模型活动后仍缺失用量、2 条为模型活动后疑似零值。因采集位置、重试覆盖和缓存计量不同，以上 token 总量和中位数**不能**用于直接比较两产品的 token 效率，也不应折算为美元成本。

## verify pass 任务的 token 对比

此处仅选取 `verify pass` 任务，并只在各自可靠 token 记录中统计；因此“有 token 的 pass 任务”是通过任务的子集，而不是将缺失补零。

| 指标 | Astra | Hermes |
|---|---:|---:|
| verify pass 任务 | 44 | 50 |
| 有可靠 token 的 pass 任务 | 42 | 47 |
| pass 任务 token 覆盖率 | 95.45% | 94.00% |
| pass 任务输入 token 总量 | 14,622,081 | 41,884,413 |
| pass 任务输出 token 总量 | 1,797,387 | 1,305,362 |
| pass 任务 token 总量 | 16,419,468 | 43,189,775 |
| pass 任务平均输入 / 输出 / total | 348,145 / 42,795 / 390,940 | 891,158 / 27,774 / 918,931 |
| no-pass 任务 token 总量（参照） | 16,229,627（41 条） | 46,925,446（29 条） |
| no-pass 任务 token 中位数（参照） | 254,624 | 654,168 |

两产品在 pass 任务上的 token 记录覆盖率接近。Hermes 的 pass 任务 token 总量和中位数较高，但这不是跨产品效率结论：两侧 token 的计量来源、缓存拆分和缺失机制不同；同时，两侧通过的任务集合也不相同。

双方均 verify pass 且两侧都有可靠 token 的重叠任务有 **29** 个。在这 29 个相同任务上，Astra 的平均输入 / 输出 / total token 为 **274,132 / 38,973 / 313,106**；Hermes 为 **812,971 / 16,751 / 829,721**。该配对均值消除了“通过任务集合不同”的影响，但仍不能消除 token 采集和缓存计量口径差异，故只作为用量描述。

## 按作者难度标签的小结

难度取自每个 Terminal-Bench `task.toml` 的作者标签（Easy / Medium / Hard），而非依据本次运行结果事后划分。Astra 的 Hard 组有 2 个 `verify unavailable`，故其通过率分母为 28；其余分组无 unavailable。

| 难度 | Astra：pass / 有结果任务 | Astra：pass 平均输入 / 输出 / total | Hermes：pass / 有结果任务 | Hermes：pass 平均输入 / 输出 / total | 重叠 pass 任务数 | Astra 重叠平均输入 / 输出 / total | Hermes 重叠平均输入 / 输出 / total |
|---|---:|---:|---:|---:|---:|---:|---:|
| Easy | 3 / 4 (75.0%) | 134,614 / 20,023 / 154,636 | 4 / 4 (100.0%) | 571,280 / 21,546 / 592,826 | 3 | 134,614 / 20,023 / 154,636 | 620,816 / 10,904 / 631,720 |
| Medium | 32 / 54 (59.3%) | 345,717 / 36,852 / 382,569 | 31 / 54 (57.4%) | 567,774 / 23,637 / 591,411 | 19 | 223,903 / 29,870 / 253,773 | 679,840 / 17,094 / 696,934 |
| Hard | 9 / 28 (32.1%) | 427,414 / 70,196 / 497,610 | 15 / 30 (50.0%) | 1,735,851 / 39,237 / 1,775,088 | 7 | 470,264 / 71,803 / 542,067 | 1,256,676 / 18,326 / 1,275,002 |

结论：两产品的通过率均随作者难度上升而下降，Astra 从 Medium 的 59.3% 降至 Hard 的 32.1%，Hermes 从 57.4% 降至 50.0%。在各自通过且有可靠 token 的任务中，Hard 的平均 total token 高于 Medium；这一方向性与难度标签一致。Hermes 的 Hard 通过任务平均 token 明显更高，但仍不能解读为产品间的单位任务 token 效率差异。

## 单任务 token 附录

88 个配对任务的逐任务输入 token、输出 token、total token、verify 状态、难度和 token 可靠性标志见：

- [Astra/Hermes Token 附录](TerminalBench-comparison-astra_hermes-appendix.csv)

附录中的空值表示未可靠取得 token，绝不表示 token 为零。`astra_token_reliable` 仅在 `session_reconciled` 或 `server_reconciled` 时为 `true`；`hermes_token_reliable` 仅在 `reported` 时为 `true`。

## 数据来源

- Astra 主汇总：`work/astra-c0-all-jobs/analysis/v2/output/astra-c0-latest-verified-trials.csv`
- Hermes 汇总：`work/hermes-c0-all-jobs/analysis/v2/output/hermes-c0-latest-verified-trials.csv`
- Astra 补入记录：
  - `work/astra-c0-rerun-from-scratch-33/jobs/2026-08-03__16-52-58/torch-tensor-parallelism__dptG8QM/result.json`
  - `work/astra-c0-rerun-from-scratch-33/jobs/2026-08-03__13-15-00/train-fasttext__DmjqdTq/result.json`
