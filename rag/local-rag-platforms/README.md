# Local RAG Platforms：本地部署与评测指南

本目录保存本地 RAG 竞品的部署约定、平台适配器、统一 API 控制器、数据级评测
脚本和测试。它不保存 vendor checkout、Docker volume、日志或凭据；这些运行时
内容统一放在被忽略的 `.local-services/`，实验结果写入根目录 `runs/`。

当前支持的统一目标是 `moi`、`dify`、`fastgpt`、`maxkb`、`ragflow`。平台服务
必须串行运行：一次只启动一个竞品栈，先检查端口和同名容器，再执行真实请求。

当前 MOI text-only serial benchmark 的 active contract 已冻结为：
`moi_local → dify_local → fastgpt_local → maxkb_local`，所有平台使用 MaaS
`bge-m3/1024` embedding 与 `glm-5.2` text LLM/Judge，`thinking={"type":"disabled"}`，
无 MLLM/Qwen。历史 TaaS/Qianfan 配置仍可能出现在旧 smoke 记录或 legacy helper
中，但 campaign 会过滤并拒绝它们；以
`scripts/evaluation/competitor_eval_campaign.py` 和
`scripts/evaluation/competitor_eval_platform_contracts.json` 为准。

## 目录结构

```text
local-rag-platforms/
├── api/control.py                 # 统一 list / dry-run / request / benchmark
├── api/api_load_benchmark.py      # 兼容旧入口，转发到统一控制器
├── api_console/                   # 本地配置和服务状态控制台
├── dify-rag-eval/                 # 共享 HTTP adapter、ingest、指标和报告
├── dify_local/                    # Dify Community Edition 部署说明
├── fastgpt_local/                 # FastGPT provider、dataset 和 native QA smoke
├── maxkb_local/                   # MaxKB 镜像、API discovery 和 full-chain
├── ragflow_local/                 # RAGFlow compose、资源/架构门禁和 smoke
├── providers/                     # TaaS、MaaS、Qianfan provider 配置与探针
├── scripts/
│   ├── deployment/                # prepare、record、镜像和凭据检查
│   ├── evaluation/                # 可恢复的数据级评测 campaign/runner
│   ├── benchmarks/                # Lenovo、Enterprise 等专项 Benchmark
│   └── reports/                   # 运行结果合并与汇总
└── tests/                         # 统一控制器、平台契约、指标和专项测试
```

先阅读：

- [Agent 复现指南](AGENT_REPRODUCTION_GUIDE.md)：完整运行不变量和部署顺序；
- [API Benchmark](API_BENCHMARK.md)：事件吞吐、TTFE、empty-workflow 口径；
- [Provider 指南](providers/README.md)：外部模型接入和向量空间隔离；
- 各平台 README：平台特有的版本、端口、UI/API 合约和阻塞条件。

## 当前版本和端口

| 目标 | 当前版本/镜像 | 本机入口 | 备注 |
|---|---|---:|---|
| Dify | Community Edition `1.16.1` | `8010` | 原计划 `8000` 被占用 |
| FastGPT | source `v4.15.6` | `3000` | release compose 中部分镜像仍为 `v4.15.4` |
| MaxKB | `v2.10.4-lts` | `8090` | 仅监听 loopback，使用固定 arm64 image digest |
| RAGFlow | `v0.26.4` | `9380` | 必须先通过架构和资源门禁 |
| MOI 保留服务 | MatrixFlow/MOI | `8080`、`6001`、`9876` | 不要让竞品占用 |

版本、source commit、compose 和 image digest 以 `versions.json` 及每次
`.local-services/<system>/` 的 runtime manifest 为准；README 中的版本是当前
工作树的默认目标，不替代运行时记录。

## 1. 准备主机和凭据

从 `rag` 根目录执行：

```bash
cd /Users/muuushroom/gitrepos/moi-benchmark/rag
cp .env.example .env
chmod 600 .env

python3 -m compileall -q local-rag-platforms/api local-rag-platforms/scripts local-rag-platforms/tests
uv run --with pytest --with-requirements local-rag-platforms/api_console/requirements.txt pytest local-rag-platforms/tests -q
python3 local-rag-platforms/scripts/deployment/prepare_local_services.py preflight
docker version
docker compose version
docker info
```

API key、app ID、dataset ID、chat ID 统一写入 `rag/.env`。平台的
`runtime.env`、Compose 文件和 `.local-services/**` 只保存非敏感运行参数或
被严格保护的本机 secret，不要提交或把 key 打进日志。

外部 provider 与本地服务是两层：Dify/FastGPT/MaxKB/RAGFlow 的数据库、解析、
索引和 HTTP 服务留在本机；TaaS、Huawei MaaS、Qianfan 或 DeepSeek 官方 API
只作为模型请求出口。更换 Embedding provider 或维度时，必须创建新的知识库
或索引，禁止复用旧向量空间。

## 2. 准备和记录 vendor 运行时

`prepare` 只把 pinned source/compose 准备到 `.local-services/`，不会把 vendor
源码复制进仓库，也不会创建或输出凭据：

```bash
python3 local-rag-platforms/scripts/deployment/prepare_local_services.py prepare dify_local
python3 local-rag-platforms/scripts/deployment/prepare_local_services.py record dify_local
```

对 `fastgpt_local`、`maxkb_local`、`ragflow_local` 重复执行。记录完成后才可以
把 source commit、image manifest、架构和 compose digest 作为一次部署证据。

## 3. 按平台启动

### Dify

```bash
python3 local-rag-platforms/scripts/deployment/prepare_local_services.py prepare dify_local
cd .local-services/dify_local/source/dify/docker
cp .env.example .env
docker compose -p moi_dify_local up -d
curl -fsS http://127.0.0.1:8010/console/api/setup
```

首次启动需要在本地 UI 完成 admin、Dataset API key、App API key、Chatflow/Workflow
和模型 provider 配置。请阅读 [Dify local](dify_local/README.md)，不要复用
Dify Cloud 的 app key 或 dataset ID。

### FastGPT

```bash
python3 local-rag-platforms/scripts/deployment/prepare_local_services.py prepare fastgpt_local
python3 local-rag-platforms/fastgpt_local/fastgpt_local.py preflight
docker compose -p moi_fastgpt_local \
  -f .local-services/fastgpt_local/compose/docker-compose.pg.yml up -d
curl -fsS http://127.0.0.1:3000
```

在 UI 中先注册并测试 chat/embedding/rerank，再创建本地 API key 和 app。当前
Qianfan Embedding 返回 4096 维，而 FastGPT 本地索引有效维度为 1536；adapter
会把 source/effective dimension 写入 smoke manifest，不能把它描述为无损
4096 维索引。详见 [FastGPT local](fastgpt_local/README.md)。

### MaxKB

```bash
python3 local-rag-platforms/scripts/deployment/prepare_local_services.py prepare maxkb_local
python3 local-rag-platforms/maxkb_local/maxkb-local.sh verify-image
python3 local-rag-platforms/maxkb_local/maxkb-local.sh start
curl -fsS http://127.0.0.1:8090/admin/ >/dev/null
```

MaxKB 启动前要求 Dify 停止、MOI 相关容器正常、8090 未占用且 image digest/
架构匹配。direct retrieval 若没有稳定公开接口，统一结果记为
`unsupported`，不能用 admin hit-test 冒充正式 retrieval trace。详见
[MaxKB local](maxkb_local/README.md)。

### RAGFlow

RAGFlow 先做只读架构/资源门禁：

```bash
python3 local-rag-platforms/scripts/deployment/prepare_local_services.py prepare ragflow_local
local-rag-platforms/ragflow_local/preflight.sh
docker buildx imagetools inspect infiniflow/ragflow:v0.26.4
```

只有门禁通过后才按 [RAGFlow local](ragflow_local/README.md) 的 pinned compose
启动。当前主机若不满足官方 CPU、内存、磁盘或 ARM64/模拟架构条件，保留
`BLOCKED_LOCAL_RESOURCES` 或 `BLOCKED_LOCAL_ARCH` 证据，不强行启动。解析/OCR
失败应记录为 ingest failure，不要改写成 retrieval miss。

## 4. 统一 API 控制器

先列出目标和解析配置；`list`/`dry-run` 不发送业务请求：

```bash
python3 local-rag-platforms/api/control.py list --platforms all
python3 local-rag-platforms/api/control.py dry-run --platforms all
```

单次受控请求：

```bash
python3 local-rag-platforms/api/control.py request \
  --platform fastgpt \
  --scenario events \
  --timeout 60
```

统一 API Benchmark：

```bash
python3 local-rag-platforms/api/control.py benchmark \
  --platforms moi,dify,fastgpt,maxkb,ragflow \
  --scenario both \
  --connections 1,4 \
  --warmup 2 \
  --duration 10 \
  --timeout 60 \
  --max-requests 100 \
  --output runs/api-benchmark-unified
```

输出包含 `summary.json`、`samples.jsonl`、`resolved-targets.json` 和
`report.md`。目标缺少配置时记为 `skipped`；平台没有稳定公开接口时记为
`unsupported`；已经发出请求且失败才记为 `error`。

## 5. 数据级评测

API 压测和数据级 RAG 评测是两条不同链路。数据级评测使用
`competitor-eval-ready-v1` 输入包，并由 campaign 保证平台串行、可恢复和
初始分母固定：

```bash
python3 local-rag-platforms/scripts/evaluation/competitor_eval_campaign.py plan
python3 local-rag-platforms/scripts/evaluation/competitor_eval_campaign.py preflight
python3 local-rag-platforms/scripts/evaluation/competitor_eval_campaign.py run --execute
python3 local-rag-platforms/scripts/evaluation/competitor_eval_campaign.py status
```

单平台分阶段运行：

```bash
python3 local-rag-platforms/scripts/evaluation/competitor_eval_runner.py preflight --system fastgpt_local
python3 local-rag-platforms/scripts/evaluation/competitor_eval_runner.py all --system fastgpt_local
```

每个 question 的 ingest、retrieval、QA、judge 和恢复状态都应写入 run ledger。
不要删除失败题、把 unsupported 改成 success，或用 retry 覆盖 initial attempt。

本工作树中的共享三文档 `local-rag-platforms/fixtures/smoke/` 不作为默认提交
内容；依赖 fixture 的 smoke 必须通过 `--source` 指向当前确实存在的本地目录。
MaxKB 的 sentinel 位于 `maxkb_local/fixtures/`，只用于其 full-chain runner，
不能假定它是跨平台通用 fixture。

## 6. 共享评测器 smoke

安装共享 evaluator：

```bash
python3 -m pip install -e local-rag-platforms/dify-rag-eval
```

使用已经准备好的本地 fixture 或数据目录运行单个平台；不要把不存在的旧
`fixtures/smoke` 路径直接复制到新运行：

```bash
python3 -m dify_rag_eval local-smoke \
  --system fastgpt_local \
  --base-url http://127.0.0.1:3000 \
  --api-key-env FASTGPT_API_KEY \
  --output .local-services/fastgpt_local/logs/smoke \
  --source /absolute/path/to/prepared/source
```

结果写入被忽略的 `.local-services/<system>/logs/`，保存统一的 smoke result、
脱敏请求/响应和 hash sidecar。API key、原始私有文档和模型私有响应不要复制
到 Git 可见目录。

## 7. 停止和恢复

平台必须按各自 README 停止，保留 volume，避免误删索引和部署证据：

```bash
docker compose -p moi_fastgpt_local \
  -f .local-services/fastgpt_local/compose/docker-compose.pg.yml down
python3 local-rag-platforms/maxkb_local/maxkb-local.sh stop
```

RAGFlow、Dify 的停止/恢复使用其本地部署目录中的 compose 和脚本。除非明确
要求清理本地状态，不要使用 `docker compose down -v`、递归删除 volume 或删除
`.local-services/`。

## 8. 验收规则

- 先记录版本、镜像、架构、端口、provider 和 embedding dimension；
- 先执行 `list`/`dry-run`，再执行单次 `request`，最后做 benchmark；
- 同一对比使用相同问题、duration、warmup、connections 和应用状态；
- direct retrieval、native QA、ingest readiness 分开记录，不能由一个成功推断另一个成功；
- `runs/` 保留原始 evidence、失败分母和恢复 checkpoint；
- 结果汇总只引用已经验证的 artifact，不手工修改逐题结果。
