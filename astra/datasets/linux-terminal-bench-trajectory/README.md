---
pretty_name: Linux Terminal Bench 2.1 Agent Trajectories
language:
  - en
task_categories:
  - text-generation
tags:
  - agents
  - agent-trajectories
  - terminal-bench
configs:
  - config_name: default
    data_files:
      - split: complete
        path: data/complete/*.jsonl
      - split: partial
        path: data/partial/*.jsonl
---

# Linux Terminal Bench 2.1 Agent Trajectories

该数据集是 Astra、DSH、Hermes 和 Pi 在 Terminal-Bench 2.1 上的标准化运行轨迹。当前版本只发布 `complete` 和 `partial` 两个 split，不发布仅有运行元数据、没有有效用户/助手对话的轨迹。

## 数据规模

| 产品 | complete | partial | 保留总数 | 未发布 metadata-only |
| --- | ---: | ---: | ---: | ---: |
| Astra | 106 | 27 | 133 | 13 |
| DSH | 68 | 27 | 95 | 40 |
| Hermes | 98 | 10 | 108 | 36 |
| Pi | 57 | 40 | 97 | 6 |
| 合计 | 329 | 104 | 433 | 95 |

数据包含 22,951 条标准化消息和 11,595 次工具调用。详细的非互斥分类原因见 `quality_report.json`。

## 分层规则

所有发布记录均满足：trial 已结束、轨迹可解析，并且至少包含一条 user 消息和一条 assistant 消息。

- `complete`：同时具有成功的产品侧轨迹保存标记、终止事件证据、完整配对的工具调用/结果，以及有效 verifier。
- `partial`：满足基本发布条件，但缺少上述一个或多个 complete 条件。
- 有效 verifier：`verifier_result.rewards.reward` 是结构化的 `0` 或 `1`，verifier 有完成时间，且不存在 `VerifierTimeoutError` 或 `VerifierInfrastructureError`。

reward 为 `0` 的轨迹只要 verifier 有效，也可以属于 `complete`。Astra 补充轨迹的 split 仅表示轨迹完整性，verifier 有效性独立保存在 `outcome` 和 `quality.reasons`，不降级已证明完整的采集。`complete` 表示轨迹数据完整，不表示任务执行成功。

## 记录结构

每行 JSON 是一条 trial，主要字段为：

- `record_id`：`产品/trial_id` 形式的唯一标识。
- `benchmark`：基准版本、任务标识和校验信息。
- `trial`：trial 标识、同任务尝试序号和起止时间。
- `agent`：产品、模型、运行条件和工具列表。
- `instruction`：任务指令。
- `messages`：统一为 `user`、`assistant`、`tool` 三种角色；工具调用位于 assistant 消息的 `tool_calls`，工具结果通过 `tool_call_id` 配对。
- `outcome`：reward、verifier 有效性及异常类型。
- `outcome.verifier_passed`、`outcome.verifier_failed`：原始 `verifier/ctrf.json` 的 `results.summary.passed` / `failed` 小项计数；缺失、不可读或不是非负整数时为 `null`，真实的零保留为 `0`。计数独立于 verifier 有效性，不改变 reward 或完整性分类。
- `usage`、`timing`：token、费用、工具调用和耗时信息；源数据没有提供的值为 `null`。
- `quality`：分层、失败原因和完整性指标。
- `source`：相对于原始数据根目录的 trial/trace 路径及原始格式名称。

## 清洗与隐私处理

- Astra、DSH、Hermes、Pi 分别由独立适配器解析，再映射到同一消息 schema。
- 不保留模型的隐藏 reasoning/thinking 内容；`internal_content_omitted` 标记该消息是否发生省略。
- 图片的 base64 内容替换为包含 MIME 类型和编码长度的占位符。
- 清洗器会替换常见私钥、Hugging Face token、AWS access key、API key、Slack token，以及本机 workspace 绝对路径。
- 原始轨迹目录保持只读，清洗结果写入本数据集目录。

## Astra 补充轨迹清洗

当前 Astra 数据来自 `work/linux-terminal-bench/astra/trace-supplements/all-verifier-attempts`。
新增脚本 `scripts/clean_astra_supplements.py` 按该目录 `inventory.json` 的 146 次 attempt 处理，覆盖原始批次的 89 个 task，不额外引入批次外的历史 attempt。

- 优先读取 current/old 数据库的 `session_transcript_items.jsonl`，按 `item_seq` 排序；通过 session ID 核对归属。
- 数据库缺少有效 user/assistant 对话时，使用 native-artifact 中的对话快照或 turn 记录回退；不拼接重复快照，不将不同来源当成额外轮次。
- 保留 133 次 attempt、85 个 task：128 条数据库对话（current 55、old 73），5 条原生文件回退；13 条缺少有效对话的记录排除。
- 共 6,432 条消息、3,084 次工具调用；工具参数保留 JSON 结构，调用与结果保持配对。
- 结果、模型和 verifier passed/failed 计数从 inventory 指定的原 trial 读取，包括独立重跑目录中的 trial。
- 对数据库 transcript 按 `run_id` 与终止事件中的工具调用计数逐段核对；所有 run 均有可比较计数且完全一致时，视为采集覆盖已证明。本批 106 条进入 `complete`，其余 27 条因计数不一致、事件覆盖不足或 native 回退无法交叉核对而保留为 `partial`。
- 采用允许字段列表提取对话，不复制数据库的 user 身份、owner/admission token、授权表等内容，也不复制 reasoning 事件。
- 在已有脱敏规则上补充 Bearer/JWT、带产品前缀的 API key/密码赋值、带密码的 URL、本机用户目录及图片 data URL；删除结构化 reasoning/thinking 和图片载荷。嵌套工具参数的对象、数组和数字类型保留。

运行命令（在本数据集目录内）：

```bash
python3 scripts/clean_astra_supplements.py
```

可通过 `--source` 指定补充导出目录、`--raw-root` 指定原始 Linux 数据根目录、`--output` 指定输出目录。inventory 的 `result_path` 必须能读取；迁移数据到其他服务器时需同步这些路径。

输出覆盖 `data/complete/astra.jsonl`、`data/partial/astra.jsonl`，更新 `quality_report.json` 中 Astra 的统计并重算合计；不改写其他产品 JSONL。详细来源、统计和排除记录见 [astra_supplement_report.json](astra_supplement_report.json)。本批计数缺失的 3 条轨迹仍保留 `null`。

测试：

```bash
python3 -m unittest discover -s scripts/tests -v
```

## 复现

在本目录运行：

```bash
python3 scripts/clean_trajectories.py --products dsh hermes pi
python3 scripts/clean_astra_supplements.py
```

在当前 MOI Benchmark workspace 中，默认从 `work/linux-terminal-bench` 读取原始数据，并按上述两步命令生成八个 JSONL 文件与质量报告。在其他目录使用脚本时，可通过 `--source` 指定原始数据目录。前三产品只做统计、不写文件时使用（Astra 补充脚本可用 `--output` 指向临时目录验证）：

```bash
python3 scripts/clean_trajectories.py --products dsh hermes pi --dry-run
```

本次清洗对应的 Terminal-Bench 2.1 数据仓库 revision 为 `5c8eadf1f393183288fa08b8f73ca9a469cc5e00`。
