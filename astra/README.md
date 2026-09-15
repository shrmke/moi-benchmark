# Astra Agent 产品评测

状态：v0.5 总体方案草稿待审阅；Terminal-Bench 2.1 与 Toolathlon 已形成四产品常规任务结果，其中 Linux Terminal-Bench 2.1 的四产品均已完成 89 题评测。本目录同时保存运行配置、进度、分析材料和可审计的轨迹记录。

当前材料：

- `research`：当前技术证据；
- `datasets/manifest.yaml`：候选数据集及冻结门槛；
- `datasets/linux-terminal-bench-trajectory`：标准化轨迹数据、Hugging Face Dataset Card、质量报告和可复现清洗及 Langfuse 导入脚本；
- `datasets/toolathlon-trajectory`：Astra、Hermes、PI、DSH 的 Toolathlon 选定 attempt 清洗数据、质量报告及 Langfuse 导入脚本；
- `systems/manifest.yaml`：本地参评系统快照；
- `runners/`：Astra、Hermes、PI、DSH 的运行器、配置和辅助脚本；
- `runners/linux_terminal_bench/`：
  Linux amd64 上四产品统一的 Terminal-Bench 2.1 89 题、1.0× 产品预算、
  单产品内并行入口；

## 轨迹数据

### Terminal-Bench 2.1 轨迹数据

当前清洗数据包含 Astra、DSH、Hermes 和 PI 在 Terminal-Bench 2.1 上的 462 条有效 trial，共 24,832 条标准化消息和 12,560 次工具调用。消息统一为 `user`、`assistant`、`tool` 结构；数据仅发布 `complete` 和 `partial` 两个 split，另有 95 条只有运行元数据或缺少有效用户/助手对话的记录未发布。

| 产品   | complete | partial | 合计 |
| ------ | -------: | ------: | ---: |
| Astra  |      106 |      27 |  133 |
| DSH    |       68 |      27 |   95 |
| Hermes |      126 |      11 |  137 |
| PI     |       57 |      40 |   97 |
| 合计   |      357 |     105 |  462 |

`complete` 表示清洗器已有充分的轨迹采集完整性证据，不表示任务通过，reward 为 `0` 的轨迹也可以属于 `complete`。清洗记录同时保存脱敏后的 verifier CTRF、逐测试诊断、reward 文本和 stdout。

清洗过程省略隐藏 reasoning/thinking 内容，移除图片 base64，并对常见私钥、访问令牌及本机绝对路径进行脱敏。详细 schema、分类规则、逐项统计和复现命令见[轨迹数据说明](datasets/linux-terminal-bench-trajectory/README.md)及[质量报告](datasets/linux-terminal-bench-trajectory/quality_report.json)。用于本地 Langfuse 的 462 条轨迹、434 条评分导入包见[import-cleaned](datasets/linux-terminal-bench-trajectory/langfuse/import-cleaned)。

### Toolathlon 轨迹数据

当前清洗数据包含 Astra、Hermes、PI 和 DSH 的 425 条 Evaluator 有效 Toolathlon 轨迹，共 35,926 条标准化消息和 18,347 次工具调用。每条记录对应一个按正式结果投影选定的 product-task attempt；Astra、Hermes 和 PI 沿用既有 effective result，DSH 按每题 `started_at` 选择最新 attempt，不从历史记录中选择最高分。

| 产品   | complete | partial | 合计 |
| ------ | -------: | ------: | ---: |
| Astra  |       78 |      27 |  105 |
| Hermes |      105 |       3 |  108 |
| PI     |      100 |       4 |  104 |
| DSH    |       96 |      12 |  108 |
| 合计   |      379 |      46 |  425 |

只有 Evaluator 产生结构化且与正式投影一致的 `pass` 或 `no_pass` 时才纳入；Astra 有 3 条缺少有效 Evaluator 结果，PI 有 3 条 Evaluator `unavailable`，因此不进入公开轨迹。`complete` 要求事件序列连续、存在正常终止事件、工具调用与包括 `tool.execution_error` 在内的终态事件配对且计数一致；`partial` 保留可用内容和具体缺失原因，不等同于任务失败。

详细 schema、清洗规则和统计见 [Toolathlon 轨迹说明](datasets/toolathlon-trajectory/README.md)、[四产品质量报告](datasets/toolathlon-trajectory/other_agents_quality_report.json)及 [Langfuse 说明](datasets/toolathlon-trajectory/LANGFUSE.md)。用于本地 Langfuse 的压缩导入包见 [import.jsonl.gz](datasets/toolathlon-trajectory/langfuse/import.jsonl.gz)，包含 425 条 trace 和 425 个与选定轨迹一一对应的 `runner_reward`。

## Linux Terminal-Bench 2.1 复现

统一入口为 [Linux runner](runners/linux_terminal_bench/README.md)，执行模块为 [run.py](runners/linux_terminal_bench/run.py)。该入口复用四产品 adapter，统一管理队列、资源调度、断点续跑和结果汇总。

### 环境与产品准备

从仓库根目录执行；需要原生 Linux amd64、可用的 Docker daemon、Harbor 0.20.0、Python 3、Git、curl 和 file。数据集使用包含 `tune-mjcf` 的完整 89 题，固定提交为 `5c8eadf1f393183288fa08b8f73ca9a469cc5e00`。

按 [runner 前置准备](runners/linux_terminal_bench/README.md#前置准备) 获取数据集并设置 `MOI_BENCH_DATA_ROOT`、`HARBOR_BIN`。默认数据根目录为仓库根目录；如使用其他目录，应按 runner 说明准备对应资源。模型使用官方 GLM-5.2；Hermes、PI、DSH 需要 `ZAI_API_KEY`，密钥通过环境或本机配置注入，不写入仓库。

Astra 另需准备源码、Linux CLI 和 API/MatrixOne 服务：

- 源码目录为 `external/astra-optimize_0731_05`；复现本文版本时使用 commit `969550b611ceba11653c4b2651079bcb85d1c24e`，并保持分支名 `optimize_0731_05`，避免直接构建可能已变化的分支最新代码。
- [build-linux-portable.sh](runners/astra_terminal_bench/build-linux-portable.sh)：构建 Linux amd64 CLI，设置 `ASTRA_SOURCE_ROOT` 和 `ASTRA_LINUX_BUILD_ROOT`，传入 `--arch amd64`。
- [start-astra-matrixone.sh](runners/scripts/start-astra-matrixone.sh)：通过 `ASTRA_SOURCE_DIR` 指定源码目录，启动 API/MatrixOne。
- 设置 `ASTRA_TBENCH_LINUX_BINARY` 指向构建出的 ELF、`ASTRA_API_URL=http://host.docker.internal:17101`；在服务端模型配置中注册 `glm-5.2` 和 Z.AI 密钥。

具体构建和服务启动命令见 [Astra 前置准备](runners/linux_terminal_bench/README.md#astra-optimize_0731_05)。使用 ShellCrash 时，镜像拉取代理与任务容器 DNS 需分别配置，详见 runner 文档；Docker daemon 重启应在没有评测任务运行时进行。大型 verifier 依赖应提前准备缓存。Astra 跨工具调用及 Agent/verifier 移交的服务保留机制见 [service_lifetime](runners/astra_terminal_bench/service_lifetime/README.md)。

### 预检、单题与全量运行

以下命令从仓库根目录执行。静态预检不会调用模型，也不替代真实 API 连通性检查；先确认模型服务可用，再进行单题试跑。

```bash
# 静态预检；其他产品将 astra 替换为 hermes、pi 或 dsh
python3 -m astra.runners.linux_terminal_bench.run --product astra --check

# 单题试跑
python3 -m astra.runners.linux_terminal_bench.run --product astra --case modernize-scientific-stack --max-workers 1

# 全量入口：一次选择一个产品运行，勿同时启动不同产品
python3 -m astra.runners.linux_terminal_bench.run --product astra --max-workers 1
python3 -m astra.runners.linux_terminal_bench.run --product hermes --max-workers 1
python3 -m astra.runners.linux_terminal_bench.run --product pi --max-workers 1
python3 -m astra.runners.linux_terminal_bench.run --product dsh --max-workers 1
```

`--max-workers` 可按资源调整至最多 3，实际并行度还受 CPU 和 memory token 调度限制；`--max-tasks N` 限制本次启动任务数。产品执行预算为数据集原始时限的 1.0 倍，verifier 独立采用 2.0 倍时限。

### 续跑、重测与结果

中断后重复同一产品命令即可继续 pending 任务，已有有效结果默认不会再次运行。全量入口在已有数据目录中表示补齐该 cohort，并不强制重测全部 89 题。明确需要重测已完成任务时，使用 `--rerun-completed`；也可传入符合 runner 格式的 TSV 队列：

```bash
python3 -m astra.runners.linux_terminal_bench.run --product astra --retry-queue work/linux-terminal-bench/astra/state/pending.queue.tsv --max-workers 1
```

结果位于数据根目录下的 `work/linux-terminal-bench/<product>/`：

| 路径                                             | 内容                                          |
| ------------------------------------------------ | --------------------------------------------- |
| `jobs/`                                        | Harbor 原始 trial、Agent 日志和 verifier 证据 |
| `state/run-manifest.json`                      | 产品、模型、源码版本和本轮运行配置            |
| `state/resource.queue.tsv`                     | 资源调度队列                                  |
| `state/pending.queue.tsv`                      | 待运行队列                                    |
| `state/analysis/summary.json`、`summary.csv` | runner 结果汇总                               |

完成判定除二元 reward 外还检查 verifier 实际执行证据；已识别的基础设施故障保持 pending，不补记为 0 分。这里的 runner 汇总与人工整理的 `latest-results`、分析报告分别管理，不会自动同步更新。离线轨迹查看与导入见 [Langfuse 说明](datasets/linux-terminal-bench-trajectory/LANGFUSE.md)。

## 当前公开基准结果

当前结果统一按常规任务完成层面比较，不将不同数据集或不同产品的指标合并为单一总分。通过率以各数据集 verifier/evaluator 为准；时间、工具调用和 token 作为独立资源指标。Terminal-Bench 不同产品的内部请求边界和 token 计量方式不完全同构，token 结果仅用于描述观测资源量级。

### Terminal-Bench 2.1：macOS 历史结果

Mac 结果固定为排除 `tune-mjcf` 的 88 题历史最新记录。Astra 有 2 题缺少有效 verifier reward，因此同时列出有效 verifier 分母和固定 88 题分母；其他三个产品两种口径相同。该批 Astra 使用 commit `844473c68649d8ea43e10b616dc4fbf98e2321e8`。

| 产品   | verifier pass | 有效 verifier | 有效 verifier 通过率 | 固定 88 题通过率 |
| ------ | ------------: | ------------: | -------------------: | ---------------: |
| Astra  |            44 |         86/88 |               51.16% |           50.00% |
| Hermes |            50 |         88/88 |               56.82% |           56.82% |
| PI     |            53 |         88/88 |               60.23% |           60.23% |
| DSH    |            47 |         88/88 |               53.41% |           53.41% |

### Terminal-Bench 2.1：Linux 结果

Linux 使用包含 `tune-mjcf` 的 89 题 cohort、1.0× 产品预算和单产品任务并行。四产品均已完成，89/89 题各有有效 verifier。Astra 使用 commit `969550b611ceba11653c4b2651079bcb85d1c24e`，按 latest-results 选中的每题最新结果汇总为 **48/89（53.93%）**。

| 产品   | verifier pass | 有效 verifier | 有效 verifier 通过率 | 状态   |
| ------ | ------------: | ------------: | -------------------: | ------ |
| Astra  |            48 |         89/89 |               53.93% | 已完成 |
| Hermes |            45 |         89/89 |               50.56% | 已完成 |
| PI     |            52 |         89/89 |               58.43% | 已完成 |
| DSH    |            49 |         89/89 |               55.06% | 已完成 |

Astra 的 Token 与工具统计优先采用 63 题完整原生汇总，其余 26 题使用同 session 的数据库 trace 补充；已观测 Token 总量按下界解释。

Mac 与 Linux 的 Astra 使用不同 commit，当前结果不应解释为同版本跨操作系统对照。

### Toolathlon：108 题结果

Astra 使用 commit `969550b611ceba11653c4b2651079bcb85d1c24e`，选定结果汇总为 **80 pass、28 no_pass，通过率 74.07%**。该口径包含四题跨版本历史替代（3 pass、1 no_pass）。

| 产品   | pass | 明确结果 | 按 108 题通过率 | 已测评题通过率 |
| ------ | ---: | -------: | --------------: | -------------: |
| Astra  |   80 |  108/108 |          74.07% |         74.07% |
| Hermes |   72 |  108/108 |          66.67% |         66.67% |
| PI     |   77 |  104/108 |          71.30% |         74.04% |
| DSH    |   79 |  108/108 |          73.15% |         73.15% |

详细结果：

- [Terminal-Bench 2.1 macOS 四产品对比](reports/TerminalBench2.1-analysis-mac/TerminalBench-comparison-astra_hermes_pi_dsh.md)
- [Terminal-Bench 2.1 Linux 四产品对比](reports/TerminalBench2.1-analysis-linux/ALL-four-agents-terminalbench-linux-latest-89-task-comparison.md)
- [Terminal-Bench 2.1 Linux Astra 报告](reports/TerminalBench2.1-analysis-linux/astra-terminalbench-linux-latest-89-task-report.md)
- [Terminal-Bench 2.1 Linux DSH 报告](reports/TerminalBench2.1-analysis-linux/dsh-terminalbench-linux-latest-89-task-report.md)
- [Terminal-Bench 2.1 Linux Hermes 报告](reports/TerminalBench2.1-analysis-linux/hermes-terminalbench-linux-latest-89-task-report.md)
- [Terminal-Bench 2.1 Linux PI 报告](reports/TerminalBench2.1-analysis-linux/pi-terminalbench-linux-latest-89-task-report.md)
- [Toolathlon 四产品对比](reports/Toolathlon-analysis/ALL-astra_new-hermes-pi-dsh-toolathlon-comparison.md)
- [Toolathlon Astra 报告及 108 题明细附录](reports/Toolathlon-analysis/astra-969550b-toolathlon-108-task-analysis.md)
