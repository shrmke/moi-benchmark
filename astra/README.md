# Astra Agent 产品评测

状态：v0.5 总体方案草稿待审阅；Terminal-Bench 2.1 与 Toolathlon 已形成四产品常规任务结果，其中 Linux Terminal-Bench 2.1 的四产品均已完成 89 题评测。本目录同时保存运行配置、进度、分析材料和可审计的轨迹记录。

当前材料：

- `research`：当前技术证据；
- `datasets/manifest.yaml`：候选数据集及冻结门槛；
- [`datasets/linux-terminal-bench-trajectory`](datasets/linux-terminal-bench-trajectory/README.md)：标准化轨迹数据、Hugging Face Dataset Card、质量报告和可复现清洗脚本；
- `systems/manifest.yaml`：本地参评系统快照；
- `runners/`：Astra、Hermes、PI、DSH 的运行器、配置和辅助脚本；
- [`runners/linux_terminal_bench/`](runners/linux_terminal_bench/README.md)：
  Linux amd64 上四产品统一的 Terminal-Bench 2.1 89 题、1.0× 产品预算、
  单产品内并行入口；
- `runs/README.md`：运行批次、证据边界和最小运行记录字段；

## Terminal-Bench 2.1 轨迹数据

当前轨迹数据包含 DSH、Hermes 和 PI 在 Terminal-Bench 2.1 上的 300 条有效 trial，统一为 `user`、`assistant`、`tool` 消息结构。数据仅保留 `complete` 和 `partial` 两个 split；只有运行元数据、没有有效用户与助手对话的记录不会发布。

| 产品 | complete | partial | 合计 |
| ---- | -------: | ------: | ---: |
| DSH | 68 | 27 | 95 |
| Hermes | 98 | 10 | 108 |
| PI | 57 | 40 | 97 |
| 合计 | 223 | 77 | 300 |

`complete` 要求轨迹保存成功、存在终止事件证据、工具调用与结果完整配对，并具有有效 verifier；reward 为 `0` 或 `1` 均可，因此该分层表示轨迹完整性，不等同于任务成功。`partial` 表示 trial 已结束且包含有效对话，但尚未满足全部完整性条件。

清洗过程省略隐藏 reasoning/thinking 内容，移除图片 base64，并对常见私钥、访问令牌及本机绝对路径进行脱敏。详细 schema、分类规则和复现命令见[轨迹数据说明](datasets/linux-terminal-bench-trajectory/README.md)，逐项统计见[质量报告](datasets/linux-terminal-bench-trajectory/quality_report.json)。清洗器已支持 Astra 当前轨迹格式；本批数据尚不包含 Astra；Astra 的 89 题评测已完成，结果及补充 trace 已用于分析报告，后续可通过同一清洗流程接入轨迹数据集。

## 当前公开基准结果

当前结果统一按常规任务完成层面比较，不将不同数据集或不同产品的指标合并为单一总分。通过率以各数据集 verifier/evaluator 为准；时间、工具调用和 token 作为独立资源指标。Terminal-Bench 不同产品的内部请求边界和 token 计量方式不完全同构，token 结果仅用于描述观测资源量级。

### Terminal-Bench 2.1：macOS 历史结果

Mac 结果固定为排除 `tune-mjcf` 的 88 题历史最新记录。Astra 有 2 题缺少有效 verifier reward，因此同时列出有效 verifier 分母和固定 88 题分母；其他三个产品两种口径相同。该批 Astra 使用 commit `844473c68649d8ea43e10b616dc4fbf98e2321e8`。

| 产品 | verifier pass | 有效 verifier | 有效 verifier 通过率 | 固定 88 题通过率 |
| --- | ---: | ---: | ---: | ---: |
| Astra | 44 | 86/88 | 51.16% | 50.00% |
| Hermes | 50 | 88/88 | 56.82% | 56.82% |
| PI | 53 | 88/88 | 60.23% | 60.23% |
| DSH | 47 | 88/88 | 53.41% | 53.41% |

### Terminal-Bench 2.1：Linux 结果

Linux 使用包含 `tune-mjcf` 的 89 题 cohort、1.0× 产品预算和单产品任务并行。四产品均已完成，89/89 题各有有效 verifier。Astra 使用 commit `969550b611ceba11653c4b2651079bcb85d1c24e`，按 latest-results 选中的每题最新结果汇总为 **48/89（53.93%）**。

| 产品 | verifier pass | 有效 verifier | 有效 verifier 通过率 | 状态 |
| --- | ---: | ---: | ---: | --- |
| Astra | 48 | 89/89 | 53.93% | 已完成 |
| Hermes | 34 | 89/89 | 38.20% | 已完成 |
| PI | 52 | 89/89 | 58.43% | 已完成 |
| DSH | 49 | 89/89 | 55.06% | 已完成 |

Astra 选中结果结束时间截至 2026-09-11T07:49:26.415917Z，全部标记 formal_score_eligible=false，属于探索性结果汇总。不同产品运行日期、重试与环境修复状态不同，不视为完全同步、同环境的受控实验。Astra 的 Token 与工具统计优先采用 63 题完整原生汇总，其余 26 题使用同 session 的数据库 trace 补充；已观测 Token 总量按下界解释。

Mac 与 Linux 的 Astra 使用不同 commit，当前结果不应解释为同版本跨操作系统对照。

### Toolathlon：108 题结果

现有 Astra 结果使用 commit `844473c68649d8ea43e10b616dc4fbf98e2321e8`。最新 Astra commit `969550b611ceba11653c4b2651079bcb85d1c24e` 尚未运行 Toolathlon，因此表中的 Astra 分数不代表最新版本。

| 产品 | pass | 明确 evaluator | 按 108 题通过率 | 已测评题通过率 |
| --- | ---: | ---: | ---: | ---: |
| Astra | 61 | 108/108 | 56.48% | 56.48% |
| Hermes | 72 | 108/108 | 66.67% | 66.67% |
| PI | 77 | 104/108 | 71.30% | 74.04% |
| DSH | 79 | 108/108 | 73.15% | 73.15% |

详细结果：

- [Terminal-Bench 2.1 macOS 四产品对比](reports/TerminalBench2.1-analysis-mac/TerminalBench-comparison-astra_hermes_pi_dsh.md)
- [Terminal-Bench 2.1 Linux 四产品对比](reports/TerminalBench2.1-analysis-linux/ALL-four-agents-terminalbench-linux-latest-89-task-comparison.md)
- [Terminal-Bench 2.1 Linux Astra 报告](reports/TerminalBench2.1-analysis-linux/astra-terminalbench-linux-latest-89-task-report.md)
- [Terminal-Bench 2.1 Linux DSH 报告](reports/TerminalBench2.1-analysis-linux/dsh-terminalbench-linux-latest-89-task-report.md)
- [Terminal-Bench 2.1 Linux Hermes 报告](reports/TerminalBench2.1-analysis-linux/hermes-terminalbench-linux-latest-89-task-report.md)
- [Terminal-Bench 2.1 Linux PI 报告](reports/TerminalBench2.1-analysis-linux/pi-terminalbench-linux-latest-89-task-report.md)
- [Toolathlon 四产品对比](reports/Toolathlon-analysis/astra-hermes-pi-dsh-toolathlon-108-task-comparison.md)
