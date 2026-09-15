# Astra Toolathlon 轨迹导入 Langfuse

脚本位于 `astra/datasets/toolathlon-trajectory/scripts/langfuse_import.py`。它只读取 `clean_astra.py` 生成的清洗数据，不修改原始 attempt、Evaluator 结果或正式报告。

## 数据口径

- 一条已选择且 Evaluator 有效的 Toolathlon attempt 对应一个 trace 和一个 Agent 根 observation。
- Input 是任务指令，Output 是完整的标准化消息序列，包含可用的 `reasoning_content`、工具调用和工具结果。
- 同一 attempt 的受控续接已由 cleaner 合并，分段信息保存在 metadata 的 `trial.segments`。
- `complete` / `partial` 表示轨迹捕获完整性，不表示任务是否通过。
- Token 缺失保持 `null`，同时写入 `missing_fields` 和 `token_usage_coverage`，不影响质量分层。
- 每条有效 Evaluator 结果产生一个 `runner_reward`，`pass=1`、`no_pass=0`。
- 当前数据集每题只含选择清单指定的一条记录，不做历史最佳 attempt 选择。
- 不伪造逐模型调用 observation 或逐工具调用 span；相关缺项明确写入 metadata。

## 1. 清洗

```bash
cd /home/vagrant/moi-benchmark
python3 astra/datasets/toolathlon-trajectory/scripts/clean_astra.py
```

## 2. Prepare

```bash
python3 astra/datasets/toolathlon-trajectory/scripts/langfuse_import.py prepare \
  --dataset astra/datasets/toolathlon-trajectory \
  --output work/toolathlon-astra-969550b/langfuse/import.jsonl
```

第一行是批次报告，其余每行包含一条 OTLP trace 请求和对应 score 请求。每次 prepare 创建新的 `import_batch`；同一 bundle 中的 trace、observation 和 score ID 是确定的。

导入失败时必须复用原 bundle，不要重新 prepare，否则会创建新的分析批次和 ID。

## 3. Import

使用现有 Langfuse 实例时加载其受保护的环境文件：

```bash
set -a
. work/linux-terminal-bench/langfuse/server.env
set +a

python3 astra/datasets/toolathlon-trajectory/scripts/langfuse_import.py import \
  --bundle work/toolathlon-astra-969550b/langfuse/import.jsonl
```

脚本要求 `LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY` 和 `LANGFUSE_SECRET_KEY`。它通过 `/api/public/otel/v1/traces` 上传 observation，通过 `/api/public/scores` 上传评分，并复用现有客户端的限流、5xx 和网络重试策略。认证失败、重定向或 OTLP partial success 不会被当成成功。

## 4. Verify

```bash
python3 astra/datasets/toolathlon-trajectory/scripts/langfuse_import.py verify \
  --bundle work/toolathlon-astra-969550b/langfuse/import.jsonl \
  --api-version v4 \
  --report work/toolathlon-astra-969550b/langfuse/verification.json
```

Verify 逐条回读并核对：

- observation ID 与完整 Output；
- 起止时间，按 Langfuse API 的毫秒精度比较；
- record、Evaluator、selection、usage、timing、quality、source 和 trial metadata；
- score ID、名称和值。

Import 显示 API accepted 只说明服务接收了请求。只有 verify 返回 0 且报告中 `failures` 为空，才能认定该 bundle 已完整持久化。

## 测试

```bash
python3 -m unittest discover \
  -s astra/datasets/toolathlon-trajectory/scripts/tests \
  -p 'test_langfuse_import.py'
```

测试不连接真实 Langfuse。实现阶段不会自动运行测试、prepare、import 或 verify。
