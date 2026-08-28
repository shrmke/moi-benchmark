# 五个目标 API 性能基准

规范入口是 `local-rag-platforms/api/control.py`。它把 MOI、Dify、FastGPT、MaxKB、
RAGFlow 的请求适配成同一套压测窗口，并输出 `summary.json`、`samples.jsonl`、
`resolved-targets.json` 和 `report.md`。旧的
`local-rag-platforms/api/api_load_benchmark.py` 仍然转发到同一个总控。

## 先验证配置，不需要 API key

```bash
python3 local-rag-platforms/api/control.py dry-run \
  --platforms all \
  --scenario both \
  --connections 1,4,8
```

旧命令也可以运行：

```bash
python3 local-rag-platforms/api/api_load_benchmark.py \
  --dry-run --platforms all --scenario both --connections 1,4,8
```

## 指标口径

- `Application QPS`：应用成功请求数除以测量窗口秒数。HTTP 2xx 但 SSE 中出现明确 `error`/`*_failed` 事件的请求按失败计；同时保留 transport success/error，便于区分 HTTP 与应用错误。
- `Event Throughput (events/s)`：仅对 SSE 输出，为测量窗口内收到的完整产品原生 SSE 事件数除以窗口秒数。不同产品的 token、状态和结束事件分帧不同，所以该值只能做同产品/同协议内诊断，不能直接横向排名。
- `TTFB / TTFE (ms)`：TTFB 是请求开始到首个响应字节；TTFE 是请求开始到首个完整 SSE 事件。非流式 JSON 只有 TTFB 和完整响应延迟，TTFE 必须为 `N/A`。
- `Connections`：并发请求数，同时输出 `peak_in_flight` 和按时间积分得到的 `average_in_flight`。当前采用 `fresh-per-request`，每次请求新建 HTTP 连接，因此延迟包含 TCP/TLS 建连开销，不是 keep-alive 延迟。
- `Empty Workflow QPS`：成功完成的空工作流请求数除以测量窗口秒数。Dify、FastGPT、MaxKB 默认使用各自无输入/无状态应用的 blocking 调用；必须把 key/app ID 指向真正的 no-op 工作流或应用，结果才可称为 empty workflow。
- RAGFlow 的 `events` 使用 `/api/v1/openai/{chat_id}/chat/completions` SSE，`empty_workflow` 使用同一路径的非流式 JSON；请求体保留 `extra_body.reference=true`，以便同时记录回答和引用。

MOI 的 `/byoa/api/v1/data_asking/analyze` 是官方 SDK 暴露的 SSE Data Asking/RAG 分析接口，因此默认参与事件吞吐和 TTFE；它不是一个跨部署统一的 empty-workflow API，所以默认把 MOI 的 Empty Workflow 标为 `unsupported`。如果当前部署有 no-op workflow，使用 `--config local-rag-platforms/api/api_benchmark.example.json` 或设置 `MOI_BENCHMARK_EMPTY_PATH`。

## 与 Dify 官方报告对齐的实验拆分

[Dify v3.9.5 Benchmark Report](https://ee.dify.ai/reports/v3.9.5/benchmark-report/) 的 Empty Workflow 是 `Start → End`，没有 LLM 等外部依赖；TTFE、Connections 和 Event Throughput 使用 `Start → LLM → End`，且 LLM 指向可重复的 mock OpenAI 流式服务。Lenovo 真实 RAG 应用加外部 MaaS 的跑数不能代替这组容量指标。

因此正式汇报应分成两张表：

1. **Lenovo 检索路径延迟**：固定 10 个 query，测 direct-retrieval API 的成功率、TTFB、端到端 p50/p95；它包含 query embedding 与平台编排，不命名为纯数据库 kernel latency。
2. **受控 API 容量**：每个平台建立一个 `Start → End` no-op 和一个 `Start → mock LLM → End` 流式应用，在相同 mock 输出、相同连接阶梯和相同窗口下测 Application QPS、TTFB、TTFE、SSE events/s 与连接峰值。

建议每个平台单独启动，按 `connections=1,4,8`、`warmup=10s`、`duration=60s` 运行 3 次。十个 query 的一次性批次只保留为 smoke/延迟检查，不作为持续 QPS 或容量结论。

当前本地 MOI 只有 MatrixFlow/MatrixOne CLI 检索路径，没有运行中的 HTTP Data Asking 服务或 no-op workflow；在部署真实 MOI SSE/no-op 契约之前，MOI 的 HTTP TTFE、SSE Event Throughput、API Connections 和 Empty Workflow QPS 应继续标为 `N/A`，不能用 CLI 输出伪装成同口径值。

## 启动受控 mock LLM

先执行不占端口的自检：

```bash
python3 local-rag-platforms/scripts/benchmarks/providers/mock_openai_stream.py --self-check
```

macOS 宿主进程可直接这样启动：

```bash
python3 local-rag-platforms/scripts/benchmarks/providers/mock_openai_stream.py \
  --host 0.0.0.0 --port 18080 --chunks 64 --delay-ms 10
```

当前 Colima 的 host proxy 对这个临时端口返回 502，因此容器要访问时应把服务启动在 Colima VM 内（仓库目录已挂载进去）：

```bash
colima ssh -- python3 "$PWD/local-rag-platforms/scripts/benchmarks/providers/mock_openai_stream.py" \
  --host 0.0.0.0 --port 18080 --chunks 64 --delay-ms 10
```

当前已从默认 bridge 和 Dify 自定义 network 验证 `http://192.168.5.1:18080/health` 返回 200；平台模型配置使用 OpenAI-compatible Base URL `http://192.168.5.1:18080/v1`。Colima 重建后先用 `colima ssh -- ip -4 -o addr show eth0` 核对 VM 的 `eth0` 地址。model 填 `mock-stream`，API key 可填任意非空测试值。该服务固定输出 64 个内容 chunk、一个结束 chunk 和 `[DONE]`，不访问外网。

MaxKB v2.10.4 的 OpenAI 接口支持 `stream=true`，但 DRF 会拒绝客户端强制发送的 `Accept: text/event-stream`；压测器 v0.2 保留 `Accept: */*`，再按响应 SSE 解析。不要把该问题绕成 `stream=false` JSON，否则只能得到 TTFB，得不到 TTFE。

## 需要在各平台完成的资源操作

| 平台 | 流式容量资源 | Empty Workflow 资源 | 需要记录 |
|---|---|---|---|
| Dify | 新建 Workflow：`Start → LLM(mock-stream) → End`，发布 | 新建 Workflow：`Start → End`，发布 | 两个独立 App API key；分别跑 `events` 与 `empty_workflow` |
| FastGPT | 新建不挂知识库的工作流/应用，只调用 `mock-stream` 后输出 | 新建不调用模型、检索或工具的直接输出工作流 | 两个 App ID；API key 若可访问二者可复用 |
| MaxKB | 新建不挂知识库、模型指向 `mock-stream` 的应用并发布 API key | 只有当前版本确有不调用模型/检索的原生工作流时才创建；否则记 `N/A` | 两个 Application ID/API key；流式 body 必须 `stream=true` |
| MOI | 部署实现 `/byoa/api/v1/data_asking/analyze` 的本地 HTTP 服务 | 提供真实 no-op workflow endpoint | Base URL、`moi-key`、两个 endpoint；当前均缺失 |

事件应用和 no-op 应用通常有不同 ID/key；内置 target 的两个 scenario 共享一组凭据，因此要分两次运行并写入不同目录，不能在同一进程中途换 key：

```bash
# 第一次：凭据指向 mock LLM 流式应用
python3 local-rag-platforms/api/control.py benchmark \
  --platforms dify,fastgpt,maxkb --scenario events \
  --connections 1,4,8 --warmup 10 --duration 60 --timeout 90 \
  --output runs/api-capacity-events-r1

# 第二次：把对应 key/app ID 改为真正 no-op 资源
python3 local-rag-platforms/api/control.py benchmark \
  --platforms dify,fastgpt,maxkb --scenario empty_workflow \
  --connections 1,4,8 --warmup 10 --duration 60 --timeout 30 \
  --output runs/api-capacity-empty-r1
```

分别再跑 `r2`、`r3`。如果机器内存不足，`--platforms` 每次只填一个平台，并保持 mock 参数、连接阶梯和时长完全相同。

## 真实运行前的环境变量

先设置服务地址和凭据名称对应的值：

```bash
export MOI_API_URL='http://127.0.0.1:8000'
export MOI_API_KEY='...'

export DIFY_BENCHMARK_BASE_URL='http://127.0.0.1:8010/v1'
export DIFY_API_KEY='app-...'
export DIFY_BENCHMARK_INPUTS_JSON='{}'

export FASTGPT_BASE_URL='http://127.0.0.1:3000'
export FASTGPT_API_KEY='fastgpt-...'
export FASTGPT_APP_ID='...'

export MAXKB_BASE_URL='http://127.0.0.1:8090'
export MAXKB_API_KEY='agent-...'
export MAXKB_APPLICATION_ID='...'

export RAGFLOW_BASE_URL='http://127.0.0.1:9380'
export RAGFLOW_API_KEY='ragflow-...'
export RAGFLOW_CHAT_ID='...'
```

然后先做短窗口 smoke：

```bash
python3 local-rag-platforms/api/control.py benchmark \
  --platforms moi,dify,fastgpt,maxkb \
  --scenario both \
  --connections 1,4 \
  --warmup 1 \
  --duration 10 \
  --timeout 60 \
  --max-requests 100 \
  --output runs/api-benchmark-smoke
```

没有配置的目标会在结果里显示为 `skipped`，不会导致其他目标的结果丢失。`--max-requests` 是第一阶段的安全上限；正式比较时应在目标之间使用相同的 duration、warmup、connections、问题文本和应用状态。

## 自定义请求

`local-rag-platforms/api/api_benchmark.example.json` 展示了如何覆盖 base URL、鉴权、事件路径和 MOI 的 empty-workflow 请求。请求体支持以下占位符：

- `{{uuid}}`：每次请求生成唯一 ID；
- `{{timestamp}}`：每次请求生成纳秒时间戳；
- `{{env:NAME}}` 或 `${NAME}`：读取环境变量。

API key 只从环境变量读取，不写入 JSON 或输出 artifact。
