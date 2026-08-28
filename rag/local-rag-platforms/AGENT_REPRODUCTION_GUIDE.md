# Agent 复现指南：本地 RAG 竞品

本指南给 Agent 一个可重复的入口：准备本地服务，校验部署，使用统一 API
总控发请求或压测，并把原始证据写入运行目录。所有命令默认从仓库根目录执行：

```bash
cd /Users/muuushroom/gitrepos/moi-benchmark/rag
```

## 1. 目录和边界

`local-rag-platforms/` 是竞品实现、平台适配、脚本、fixture、共享测试和统一
控制器的规范目录；脚本职责见 [`scripts/README.md`](scripts/README.md)：

```text
local-rag-platforms/
├── api/control.py              # 统一 API 总控
├── dify-rag-eval/              # 共享 HTTP/指标引擎
├── dify_local/                 # Dify 本地部署和运行说明
├── fastgpt_local/              # FastGPT 本地部署、provider 和 smoke
├── maxkb_local/                # MaxKB 本地部署和 API discovery
├── ragflow_local/              # RAGFlow 本地部署、架构门禁和 smoke
├── scripts/                    # deployment/evaluation/benchmark/report 脚本
├── fixtures/                   # 共享 smoke 文档和问题集
├── providers/                  # 外部模型 provider 探针
└── tests/                      # 全部竞品测试，按组件分组
```

历史命令统一使用 `local-rag-platforms/`，新文档、新脚本和新产物也统一使用
该路径。`local-rag-platforms/dify-rag-eval/` 是共享 HTTP/指标引擎，
不是另一份 Dify 竞品 checkout。

统一控制器当前暴露五个目标：`moi`、`dify`、`fastgpt`、`maxkb`、`ragflow`。
它负责目标解析、认证头、请求协议、SSE/JSON 读取、并发/时延参数和报告落盘；
它不负责启动或停止 Docker 服务，服务生命周期仍由各平台部署脚本负责。

本次 500-document/1000-QA text-only campaign 的 provider policy 是严格
MaaS-only：`bge-m3/1024`、`glm-5.2`、Judge 同为 `glm-5.2`，thinking disabled，
`mllm=NOT_APPLICABLE`。不要把旧 TaaS/Qianfan/Qwen 示例当作当前配置；active
campaign 会从子进程环境中移除这些 legacy provider 变量并 fail-closed。

## 2. 复现不变量

- 一次只启动一个竞品服务栈；先检查端口和同名容器，避免相互污染。
- 凭据只通过环境变量或被忽略的 `.local-services/**` 配置传入，不写入 Git、日志、fixture 或报告。
- 每个实验使用新的 `run_id`；不同 embedding provider 或维度不得复用同一知识库索引。
- `skipped` 表示缺少配置，`unsupported` 表示平台没有稳定公开的接口，`error` 才表示已发请求但失败；不要把三者合并成“平台不可用”。
- RAGFlow 先过架构/资源门禁；当前主机若不满足官方资源要求，保留 `BLOCKED_LOCAL_RESOURCES` 证据，不强行启动。
- Agent 默认先执行 `list`/`dry-run`，确认目标路径和凭据变量名称，再执行真实 `request` 或 `benchmark`。

## 3. 安装与静态校验

先做不触网的代码校验：

```bash
python3 -m compileall -q local-rag-platforms/api local-rag-platforms/scripts local-rag-platforms/tests
uv run --with pytest --with-requirements local-rag-platforms/api_console/requirements.txt pytest local-rag-platforms/tests -q
```

确认本机 Docker、Compose、Colima 和资源状态：

```bash
python3 local-rag-platforms/scripts/deployment/prepare_local_services.py preflight
docker version
docker compose version
docker info
```

## 4. 环境变量

统一控制器只读取变量名，不会自动打印变量值。常用变量如下：

| 目标 | 默认地址 | 必需变量 |
| --- | --- | --- |
| MOI | `http://127.0.0.1:8000` | `MOI_API_KEY` |
| Dify | `http://127.0.0.1:8010/v1` | `DIFY_API_KEY` |
| FastGPT | `http://127.0.0.1:3000` | `FASTGPT_API_KEY`, `FASTGPT_APP_ID` |
| MaxKB | `http://127.0.0.1:8090` | `MAXKB_API_KEY`, `MAXKB_APPLICATION_ID` |
| RAGFlow | `http://127.0.0.1:9380` | `RAGFLOW_API_KEY`, `RAGFLOW_CHAT_ID` |

地址可用 `*_BENCHMARK_BASE_URL` 覆盖；Dify 也支持
`DIFY_API_BASE_URL`，FastGPT/MaxKB 也支持各自的 `*_BASE_URL`。把本地值统一
写入仓库根目录 `.env`，然后在当前 shell 中加载：

```bash
set -a
source .env
set +a
```

文件只保留在本机，例如：

```dotenv
DIFY_API_KEY=<local-dify-key>
FASTGPT_API_KEY=<local-fastgpt-key>
FASTGPT_APP_ID=<local-fastgpt-app-id>
MAXKB_API_KEY=<local-maxkb-application-key>
MAXKB_APPLICATION_ID=<local-maxkb-application-id>
RAGFLOW_API_KEY=<local-ragflow-key>
RAGFLOW_CHAT_ID=<local-ragflow-chat-id>
```

如果历史运行把凭据分散在被忽略的运行文件中，可只在本机执行一次迁移；命令
不会打印 key 值：

```bash
python3 tools/centralize_env.py --sync --strip-legacy
chmod 600 .env
```

迁移后，平台运行文件只保留非敏感的 Compose/UI 参数。新增凭据时直接编辑
根 `.env`，不要再复制到 `local-rag-platforms/dify-rag-eval/.env` 或 `.local-services/**`。

## 5. 部署各竞品

### Dify

准备并记录 pinned checkout：

```bash
python3 local-rag-platforms/scripts/deployment/prepare_local_services.py prepare dify_local
python3 local-rag-platforms/scripts/deployment/prepare_local_services.py record dify_local
```

按 `local-rag-platforms/dify_local/README.md` 配好本地模型 provider 后启动对应的
Compose 栈。默认 HTTP 入口是 `http://127.0.0.1:8010`；首次本地安装要先完成
`/console/api/setup`，再创建工作流 API key。Dify 的事件场景使用
`POST /v1/workflows/run`，真正压测前必须用一个可重复的 workflow 和固定输入。

### FastGPT

```bash
python3 local-rag-platforms/scripts/deployment/prepare_local_services.py prepare fastgpt_local
python3 local-rag-platforms/scripts/deployment/prepare_local_services.py record fastgpt_local
python3 local-rag-platforms/fastgpt_local/fastgpt_local.py preflight
docker compose -p moi_fastgpt_local \
  -f .local-services/fastgpt_local/compose/docker-compose.pg.yml up -d
curl -fsS http://127.0.0.1:3000
```

在本地 UI 创建 API key 和 app，并把 `FASTGPT_APP_ID` 指向这个 app。需要先
完成模型/provider 注册，再做知识库导入；`fastgpt_local/README.md` 记录了
Qianfan 4096 维返回与 FastGPT 本地 1536 维索引之间的已知差异。

### MaxKB

先执行只读镜像校验，再启动：

```bash
python3 local-rag-platforms/scripts/deployment/prepare_local_services.py prepare maxkb_local
python3 local-rag-platforms/maxkb_local/maxkb-local.sh verify-image
python3 local-rag-platforms/maxkb_local/maxkb-local.sh start
curl -fsS http://127.0.0.1:8090/admin/ >/dev/null
```

首次登录后创建本地 application key、智能体和知识库，把 application id 填入
`MAXKB_APPLICATION_ID`。若使用 Qianfan embedding，按
`local-rag-platforms/maxkb_local/README.md` 的注册/verify 流程确认 4096 维模型，不能
把旧知识库直接切换到新向量空间。

### RAGFlow

RAGFlow 是资源门禁最严格的目标：

```bash
python3 local-rag-platforms/scripts/deployment/prepare_local_services.py prepare ragflow_local
local-rag-platforms/ragflow_local/preflight.sh
docker buildx imagetools inspect infiniflow/ragflow:v0.26.4
```

只有 preflight 通过并确认当前没有冲突的竞品容器后，才按
`local-rag-platforms/ragflow_local/README.md` 的 pinned compose 启动，并检查：

```bash
curl -fsS http://127.0.0.1:9380/api/v1/system/healthz
```

在 RAGFlow UI 创建 chat assistant，把 chat id 设置为 `RAGFLOW_CHAT_ID`。
统一 API 使用 `POST /api/v1/openai/{chat_id}/chat/completions`，请求体带
`extra_body.reference=true`，因此 native QA 引用和普通回答会分别保留。

## 6. 使用统一 API 总控

先列出所有目标；这个操作不发业务请求：

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

统一压测示例：

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

每次运行会生成：

```text
runs/api-benchmark-unified/
├── summary.json          # 目标、配置、统计和状态
├── samples.jsonl         # 每次请求的脱敏样本
├── resolved-targets.json # 最终地址、协议、变量名，不含 key
└── report.md             # 可读汇总
```

Python Agent 也可以直接复用同一个接口，不需要了解各平台的 URL、认证头和
SSE 结束标记：

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path("local-rag-platforms").resolve()))
from api import CompetitorController
from api.control import BenchmarkOptions

controller = CompetitorController()
print(controller.describe("all"))
sample = controller.request_once("ragflow", scenario="events")
report, samples = controller.benchmark(
    "moi,dify,fastgpt,maxkb,ragflow",
    options=BenchmarkOptions(
        scenarios=("events",),
        connection_levels=(1, 4),
        duration_s=10,
        warmup_s=2,
        timeout_s=60,
        max_requests=100,
    ),
)
controller.write_artifacts(report, samples, "runs/api-benchmark-unified")
```

旧入口仍可用，内部已经转发到同一个总控：

```bash
python3 local-rag-platforms/api/api_load_benchmark.py --dry-run --platforms all
```

## 7. 数据级评测（不是 API 压测）

需要比较 ingest、retrieval、native QA 时使用已有的可恢复 runner；它和 API
总控共享竞品目录，但职责不同：

```bash
python3 local-rag-platforms/scripts/evaluation/competitor_eval_campaign.py plan
python3 local-rag-platforms/scripts/evaluation/competitor_eval_campaign.py preflight
python3 local-rag-platforms/scripts/evaluation/competitor_eval_campaign.py run --execute
python3 local-rag-platforms/scripts/evaluation/competitor_eval_campaign.py status
```

单个平台或单个阶段可直接调用：

```bash
python3 local-rag-platforms/scripts/evaluation/competitor_eval_runner.py preflight --system fastgpt_local
python3 local-rag-platforms/scripts/evaluation/competitor_eval_runner.py all --system fastgpt_local
```

这条链路会把解析失败、索引未就绪、retrieval 不支持和 native QA 超时分别
记录，不要只看最终答案字符串判断平台能力。

## 8. Agent 运行检查清单

1. 从仓库根目录开始，确认 `local-rag-platforms/` 是唯一的竞品目录。
2. 跑 `compileall`、共享测试和 `prepare_local_services.py preflight`。
3. 只启动一个竞品，记录端口、镜像、版本和 provider。
4. 创建新的知识库/app/chat，并把 key/id 放入本地环境变量。
5. 跑 `list`、`dry-run`，确认 `resolved-targets.json` 中路径符合实例 API。
6. 先用 `request` 验证单次成功，再运行固定参数的 `benchmark`。
7. 将 `summary.json`、`samples.jsonl`、服务日志和阻塞原因一起归档到同一个 run 目录。
8. 汇报时同时给出 `success/skipped/unsupported/error` 数量以及未完成的门禁，不把阻塞结果写成通过。
