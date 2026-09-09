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

该数据集是 DSH、Hermes 和 Pi 在 Terminal-Bench 2.1 上的标准化运行轨迹。当前版本只发布 `complete` 和 `partial` 两个 split，不发布仅有运行元数据、没有有效用户/助手对话的轨迹。

## 数据规模

| 产品 | complete | partial | 保留总数 | 未发布 metadata-only |
| --- | ---: | ---: | ---: | ---: |
| DSH | 68 | 27 | 95 | 40 |
| Hermes | 98 | 10 | 108 | 36 |
| Pi | 57 | 40 | 97 | 6 |
| 合计 | 223 | 77 | 300 | 82 |

数据包含 16,519 条标准化消息和 8,511 次工具调用。详细的非互斥分类原因见 `quality_report.json`。

## 分层规则

所有发布记录均满足：trial 已结束、轨迹可解析，并且至少包含一条 user 消息和一条 assistant 消息。

- `complete`：同时具有成功的产品侧轨迹保存标记、终止事件证据、完整配对的工具调用/结果，以及有效 verifier。
- `partial`：满足基本发布条件，但缺少上述一个或多个 complete 条件。
- 有效 verifier：`verifier_result.rewards.reward` 是结构化的 `0` 或 `1`，verifier 有完成时间，且不存在 `VerifierTimeoutError` 或 `VerifierInfrastructureError`。

reward 为 `0` 的轨迹只要 verifier 有效，也可以属于 `complete`。`complete` 表示轨迹数据完整，不表示任务执行成功。

## 记录结构

每行 JSON 是一条 trial，主要字段为：

- `record_id`：`产品/trial_id` 形式的唯一标识。
- `benchmark`：基准版本、任务标识和校验信息。
- `trial`：trial 标识、同任务尝试序号和起止时间。
- `agent`：产品、模型、运行条件和工具列表。
- `instruction`：任务指令。
- `messages`：统一为 `user`、`assistant`、`tool` 三种角色；工具调用位于 assistant 消息的 `tool_calls`，工具结果通过 `tool_call_id` 配对。
- `outcome`：reward、verifier 有效性及异常类型。
- `usage`、`timing`：token、费用、工具调用和耗时信息；源数据没有提供的值为 `null`。
- `quality`：分层、失败原因和完整性指标。
- `source`：相对于原始数据根目录的 trial/trace 路径及原始格式名称。

## 清洗与隐私处理

- DSH、Hermes、Pi 分别由独立适配器解析，再映射到同一消息 schema。
- 不保留模型的隐藏 reasoning/thinking 内容；`internal_content_omitted` 标记该消息是否发生省略。
- 图片的 base64 内容替换为包含 MIME 类型和编码长度的占位符。
- 清洗器会替换常见私钥、Hugging Face token、AWS access key、API key、Slack token，以及本机 workspace 绝对路径。
- 原始轨迹目录保持只读，清洗结果写入本数据集目录。

## Astra 接入

清洗器已包含 Astra 适配器，可读取 `agent/astra-trajectory/**/conversation_log.jsonl`，并兼容 `agent/session.jsonl` 的 turn wrapper。Astra 仍在运行时，未结束或没有有效对话的 trial 会被排除；完成后可与现有三款产品一起重新生成：

```bash
python3 scripts/clean_trajectories.py --products dsh hermes pi astra
```

当前发布文件只包含 DSH、Hermes 和 Pi，尚未把进行中的 Astra 结果写入数据集。

## 复现

在本目录运行：

```bash
python3 scripts/clean_trajectories.py
```

在当前 MOI Benchmark workspace 中，默认从 `work/linux-terminal-bench` 读取原始数据，并覆盖生成六个 JSONL 文件与质量报告。在其他目录使用脚本时，可通过 `--source` 指定原始数据目录。只做统计、不写文件时使用：

```bash
python3 scripts/clean_trajectories.py --dry-run
```

本次清洗对应的 Terminal-Bench 2.1 数据仓库 revision 为 `5c8eadf1f393183288fa08b8f73ca9a469cc5e00`。
