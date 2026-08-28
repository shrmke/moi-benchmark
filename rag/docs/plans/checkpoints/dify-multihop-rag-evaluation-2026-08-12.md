# Dify MultiHop-RAG 本地评估断点（2026-08-12）

## 当前状态

- 状态：`PARTIAL`（主批次已完成，保留 182 条失败记录）
- 最近保存：2026-08-13 12:19（Asia/Shanghai）
- Dify runner：QA 已记录 2556/2556 条，其中 2374 成功、182 失败；最后题号为 `multihop-rag-2556`。
- Dify Docker 项目：正在运行，API 与 Weaviate 已 ready。

## 评估目标

- 系统：`dify_local`
- 数据包：`.local-services/competitor-eval-ready/v1/multihop-rag`
- 数据规模：609 个 source documents、2556 个 questions
- 当前 scope：`global`
- run id：`dify-local-multihop-rag-full-v1`
- Dify dataset id：`9e358804-32ad-49d9-b3c8-5981443cfefd`
- 运行目录：`runs/dify-multihop-rag-20260812/dify-local-multihop-rag-full-v1`

## 最近断点快照

最近一次完整分页观测到的索引状态为：609 documents、608 completed、1 error（`article-0077` 受 MaaS content-safety 403 限制）。retrieval 已完成 2556/2556；QA 正在消费同一份 questions 队列。

运行资源映射当前保留 609 条 upload 记录；global 资源标记为 `partial/ready`，并记录 0077 的 `MAAS_CONTENT_SAFETY_403` 限制：

- `resource-map.json` 与 `resource-map.partial.json` 均保留。
- 两个 `.sha256` sidecar 在暂停前已重新校验通过。
- 原始 ingest、retrieval、QA HTTP artifacts 和 start/preflight artifacts 均保留。

### 已完成阶段

- retrieval：`SUCCESS`，2556/2556；Recall@1/3/5/10 = `0.9550311665`，MRR = `0.7312467314`，p50/p95 = `407.684/533.002 ms`；2547 成功、9 超时。
- QA：`PARTIAL`，2556/2556 terminal rows（2374 成功、182 失败）；指标见运行目录中的 `qa-metrics.json` 与 `qa-status.json`。失败项保留在统一分母中。
- 首次恢复时 Weaviate 尚在加载 shard，`2297–2326` 产生 30 条 `Vector database is not ready` 启动窗口记录；这些记录已原样归档到 `terminal-ledger.invalid-vector-startup-20260813.jsonl`，完整修复前备份保存在 `terminal-ledger.before-vector-startup-repair-20260813.jsonl`，主 ledger 已从 `2297` 重新执行。
- 完整 stdout/stderr 日志：`runs/dify-multihop-rag-20260812/dify-local-multihop-rag-full-v1/logs/qa-resume-ready-20260813-1121.log`；逐题结果见 `terminal-ledger.jsonl`。
- QA 使用 `--qa-concurrency 1`；本批次已结束，重新执行同一命令只会校验/复用既有 terminal rows。

## 已处理的单文档恢复

为保持 Dify dataset 总数为 609，只对确认失败的文档做过定向删除并重新上传；其他文档没有重新上传：

1. `article-0001-200-of-the-best-deals-from-amazon-s-cyber-monday-sale.md`：旧 remote id `f2eab553-dc18-4fbe-bb04-b1ce092524c0`，新 remote id `332a2020-e356-4641-9f21-355cd835aa0a`。
2. `article-0077-how-the-conspiracy-fueled-epoch-times-went-mainstream-and-made-millions.md`：旧 remote id `a5054b63-2970-456b-9625-68aeb2ac6284`，新 remote id `aecdd85b-7942-4112-a31b-c04dcdbaf66b`；暂停前新文档状态为 `waiting`。

`resource-map.json`、`resource-map.partial.json` 已同步到上述新 remote id。第 2 个文档的原始 Dify 错误为 `[MatrixOrigin TaaS] Incorrect model credentials provided`；重传后的文档尚未完成索引，恢复后需继续观察。

## 已验证的代码修复

- `local-rag-platforms/scripts/evaluation/competitor_eval_runner.py`：Dify documents API 分页聚合；恢复时复用 `submitted + remote_id`，避免重复上传。
- `local-rag-platforms/tests/test_competitor_eval_runner.py`：对应聚焦测试。
- 验证命令：`uv run --with pytest --with-requirements local-rag-platforms/api_console/requirements.txt pytest local-rag-platforms/tests/test_competitor_eval_runner.py -q -k 'dify_readiness or dify_global_ingest_reuses_submitted_remote_documents'`
- 结果：`3 passed, 39 deselected`。

## 若再次中断时的恢复顺序

先启动 Dify Docker 项目，确认 `http://127.0.0.1:8010/console/api/setup` ready，然后在仓库根目录加载 Qianfan 环境并沿用同一 run id。当前 ingest/retrieval 已完成，优先直接运行 `qa`；若资源状态被外部修改，再运行 `ingest`，它会复用已有 upload 记录。

```bash
cd /Users/muuushroom/gitrepos/moi-benchmark/rag
set -a; . .local-services/providers/qianfan.env; set +a

python3 local-rag-platforms/scripts/evaluation/competitor_eval_runner.py ingest \
  --system dify_local \
  --package .local-services/competitor-eval-ready/v1/multihop-rag \
  --output-root runs/dify-multihop-rag-20260812 \
  --run-id dify-local-multihop-rag-full-v1 \
  --repeats 1 --top-k 10 --poll-seconds 10 \
  --service-timeout 120 --provider-timeout 60 --upload-timeout 300 \
  --index-timeout 14400 --query-timeout 120 --qa-timeout 240 \
  --qa-concurrency 4 --dify-max-indexing-scopes 1
```

本批次不要新建 run id，不要删除 dataset，不要覆盖本 checkpoint 的 artifact 根目录。模型调用会访问外部 MaaS/Qianfan endpoint。
