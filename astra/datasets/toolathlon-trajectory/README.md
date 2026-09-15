# Astra Toolathlon Trajectories

该目录用于把 Astra Toolathlon 108 题的选定 attempt 清洗为统一 JSONL 轨迹。清洗器读取正式结果 CSV 中每题唯一的选择来源，不自行选择“最好”的历史 attempt。

## 纳入规则

- `record_id` 固定为 `astra/<task>/<attempt-run-id>`。
- 只有 Evaluator 产生结构化且与选择结果一致的 `pass` 或 `no_pass` 时才纳入。
- `pass` 与 `no_pass` 都是有效轨迹；Evaluator 网络、超时、解析或基础设施失败不作为有效判定。
- 同一 attempt 内的受控续接合并为一条轨迹，并在 `trial.segments` 中保留分段信息。
- `reasoning_delta` 在脱敏后保存到 assistant 消息的 `reasoning_content`；不存在的隐藏推理不会被推断或补写。
- Token usage 缺失保存为 `null`，coverage 单独记录，不影响 `complete` / `partial`。

## 完整性规则

`complete` 要求轨迹 JSONL 可解析、事件序号连续、最终分段存在终止事件、续接链闭合、工具调用与结果配对且 Runner 工具计数一致。否则保留为 `partial` 并记录具体原因。Evaluator 是否通过不参与完整性分层。

## 运行

从仓库根目录执行：

```bash
python3 astra/datasets/toolathlon-trajectory/scripts/clean_astra.py
```

默认输入：

```text
astra/reports/Toolathlon-analysis/astra-969550b-toolathlon-108-task-results.csv
```

输出：

```text
astra/datasets/toolathlon-trajectory/data/complete/astra.jsonl
astra/datasets/toolathlon-trajectory/data/partial/astra.jsonl
astra/datasets/toolathlon-trajectory/quality_report.json
astra/datasets/toolathlon-trajectory/selection_manifest.json
```

可通过 `--results-csv`、`--repo-root` 和 `--output` 覆盖路径；`--dry-run` 只打印统计，不写文件。写出采用临时文件替换，避免 JSONL 与统计报告只更新一部分。

测试文件位于 `scripts/tests/test_clean_astra.py`。按照仓库协作约定，除非明确要求，不会在实现阶段自动运行测试或生成数据。
