# MaxKB local（v2.10.4-lts）

本方案只在 Dify 停止后运行 MaxKB；MOI 的 `moi-openxml-parser` 与
`matrixone` 必须保持运行。MaxKB 服务留在本机 Colima；当前 MOI text-only
竞品评估链路使用 MaaS `bge-m3`（1024 维）embedding 与 DeepSeek 官方
`deepseek-v4-flash` 文本生成，thinking disabled，不调用 MLLM/Qwen。由于 MaxKB `2.10.4` 无法证明
原生应用能持久化嵌套 thinking 参数，统一 runner 采用 MaxKB admin
`hit_test` 检索 + DeepSeek 外部生成，并在产物中标记为
`diagnostic_admin_retrieval_plus_external_deepseek_generation`。
历史 Qianfan/TaaS smoke 证据仍保留在本目录说明中，但不属于当前评测。

## 已核对的不变量

- 镜像：`1panel/maxkb:v2.10.4-lts`
- 本地架构：`linux/arm64`
- OCI index digest：`sha256:20205df1ba6eef4e4276e48c892038de72cf8618d1e1c1d50eb1f535d45dfedc`
- arm64 manifest digest：`sha256:4d8c2807cf0fdd271d4e06ffa6645faa7ac87c09f6ad2fdb1a1e2035731f2386`
- 本地 image ID：`sha256:20205df1ba6eef4e4276e48c892038de72cf8618d1e1c1d50eb1f535d45dfedc`
- 镜像配置：内部端口 `8080`，`PGDATA=/opt/maxkb/data/postgresql/pgdata`，声明卷 `/opt/maxkb`
- 映射：仅监听 `127.0.0.1:8090`，持久化到 `.local-services/maxkb_local/data:/opt/maxkb`

镜像环境记录的 build commit 是 `69701d4`，而官方 tag checkout
`v2.10.4-lts` 是 `fd6141e...`。所以源码候选路由只能帮助 discovery，运行
实例中智能体概览显示的 API 文档/Base URL 才是最终合约。

当前阶段只能执行只读核对，不能执行 `start`：

```bash
local-rag-platforms/maxkb_local/maxkb-local.sh verify-image
```

## Dify 停止后的启动

脚本会拒绝在任何 `moi_dify_local-*` 容器运行、MOI 两个容器未运行、8090
已占用、镜像/digest/架构不符或同名 MaxKB 容器已存在时启动。

```bash
local-rag-platforms/maxkb_local/maxkb-local.sh start
```

它实际执行的固定命令为：

```bash
mkdir -p "$PWD/.local-services/maxkb_local/data"
docker run -d \
  --name moi-maxkb-local \
  --restart unless-stopped \
  --platform linux/arm64 \
  --pull never \
  -p 127.0.0.1:8090:8080 \
  -v "$PWD/.local-services/maxkb_local/data:/opt/maxkb" \
  1panel/maxkb@sha256:20205df1ba6eef4e4276e48c892038de72cf8618d1e1c1d50eb1f535d45dfedc
```

等待 `http://127.0.0.1:8090/admin/` 可访问。不要停止 MOI，也不要把 8080
映射给 MaxKB。

## 管理员与当前 split provider

1. 打开 `http://127.0.0.1:8090/admin/`，使用官方初始账号 `admin` /
   `MaxKB@123..` 登录，立即在个人信息中修改密码；新密码只保存在本地
   密码管理器或 `.local-services/maxkb_local/runtime.env`，不要写入日志。
2. 在“模型 → 全部模型 → OpenAI → 添加模型”分别创建两个记录：
   - 大语言模型：基础模型填 `deepseek-v4-flash`；API URL 填
     `https://api.deepseek.com`；API Key 填 `DEEPSEEK_API_KEY_NEW`。
   - 向量模型：基础模型填 `bge-m3`；API URL 填
     `https://api.modelarts-maas.com/v1`；API Key 填 `MAAS_API_KEY`；维度固定为 `1024`。
3. 两个记录都要通过 MaxKB 的保存时连通性验证。不要把 TaaS key 放入
   智能体提示词、API discovery artifact 或 shell history。

下面的 TaaS/Qianfan 记录说明仅用于解释历史运行，不得用于当前 benchmark：

- LLM：API URL `https://qianfan.baidubce.com/v2`，模型
  `deepseek-v4-flash`；
- 向量：同一 API URL，模型 `qwen3-embedding-8b`，固定维度 4096；
- 重排：模型 `qwen3-reranker-8b`，接口 `/v2/rerank`；
- Dimensions 可留空时优先留空，避免发送千帆未支持的 `dimensions` 参数；
- 使用新的知识库重新向量化，禁止切换原 TaaS 知识库的 Embedding。
- 三个模型名先作为候选接入点 ID，最终以千帆 `/v2/models` 返回为准。

### Qianfan embedding 注册与发现

MaxKB v2.10.4 的通用 OpenAI embedding 表单只提供 1536/1024/768/512，
不能用表单默认值注册 Qianfan 的 4096 维模型。注册请求必须满足：

```text
provider         = model_openai_provider
model_type       = EMBEDDING
model_name       = qwen3-embedding-8b
model_params_form = []
credential.api_base = https://qianfan.baidubce.com/v2
```

用 MaxKB admin token 执行一次（会先直连 Qianfan `/v2/embeddings` 验证返回
长度为 4096，再按模型名幂等发现或创建记录）：

```bash
set -a; source .env; \
source .local-services/maxkb_local/runtime.env; set +a
local-rag-platforms/maxkb_local/maxkb-local.sh qianfan-embedding register --execute
local-rag-platforms/maxkb_local/maxkb-local.sh qianfan-embedding verify
```

命令输出和 `--output` 产物只包含 model ID、模型元数据和向量维度，不包含
API key。若设置 `MAXKB_EMBEDDING_MODEL_ID`，verify 会严格验证这个 ID；不设置
时按 `model_type + provider + model_name` 唯一发现。发现到的 ID 必须作为知识库
创建请求的 `embedding_model_id`，不能只填模型名称。

可从脱敏模板准备仅本机的非敏感运行配置；API key 不写入该文件：

```bash
install -m 600 local-rag-platforms/maxkb_local/runtime.env.example \
  .local-services/maxkb_local/runtime.env
# 手工编辑非敏感参数后：set -a; source .env; source .local-services/maxkb_local/runtime.env; set +a
```

### DeepSeek text / Huawei MaaS embedding 注册与验证

DeepSeek text 与 MaaS embedding 使用独立的 OpenAI-compatible 记录，不复用
旧 Qianfan 或 MaaS-only GLM 记录。`maxkb_maas_models.py` 按
`provider + model_type + model_name` 精确
选择；重复记录必须显式传入 model ID，错误 provider/模型不会被静默替换。
请求合同为：

```text
provider                      = model_openai_provider
LLM model_name               = deepseek-v4-flash
LLM credential.api_base      = DEEPSEEK_BASE_URL or https://api.deepseek.com
LLM credential.api_key       = DEEPSEEK_API_KEY_NEW
EMBEDDING model_name         = bge-m3
EMBEDDING dimensions         = 1024
EMBEDDING credential.api_base = MAAS_BASE_URL or https://api.modelarts-maas.com/v1
EMBEDDING credential.api_key  = MAAS_API_KEY
```

先做只读 dry-run，再按需验证/注册。以下命令不会打印 key；`register` 默认
不写 MaxKB，只有 `--execute` 会创建缺失的精确记录：

```bash
set -a; source .env; source .local-services/maxkb_local/runtime.env; set +a
local-rag-platforms/maxkb_local/maxkb_maas_models.py register --skip-provider-probe
local-rag-platforms/maxkb_local/maxkb_maas_models.py verify --skip-provider-probe
local-rag-platforms/maxkb_local/maxkb_maas_models.py register --execute
```

`verify`/`register --execute` 可在不使用 `--skip-provider-probe` 时对 DeepSeek
`/chat/completions` 与 MaaS `/embeddings` 做真实探测；单元测试使用 fake admin/provider，
不会发真实请求。MaxKB `2.10.4` 无法保存嵌套 `thinking` 参数，因此 helper
明确返回 `native_supported: false`、`runner_mode: external`；DeepSeek 生成必须由
外部 OpenAI-compatible 请求完成，并携带 `thinking: {"type":"disabled"}`。
这不是 native MaxKB DeepSeek 支持，runner 仍需由主流程按外部生成处理。

当前 MOI text-only serial smoke 使用 `deepseek-v4-flash` / `bge-m3`，embedding 宽度由
MaaS probe 固定为 `1024`。MaxKB 存在重复的 bge 记录时，必须显式设置
`MAXKB_LLM_MODEL_ID` 与 `MAXKB_EMBEDDING_MODEL_ID`；helper 会对重复发现
fail-closed，不会按列表首项静默选择。外部生成路径的上下文还固定为最多
`64` 个 chunk、`16000` 字符（可用 `MAXKB_EXTERNAL_MAX_CHUNKS` /
`MAXKB_EXTERNAL_CONTEXT_CHARS` 显式覆盖），以避免把整段 admin hit_test
原文直接送入 DeepSeek。当前成功证据为
`runs/readiness/live-smoke-20260820/005-maxkb_local-smoke-context-bounded`；
首轮 004 的 403 作为失败诊断保留。

## 三文档 smoke 初始化

1. “知识库 → 创建知识库”：名称 `moi-maxkb-smoke-3docs`，类型“通用型”，
   向量模型选上面的 MaaS Embedding。该小节是旧的手工三文档 smoke；当前
   benchmark readiness 使用 `text_only_smoke_ready_for_eval` 的 6 文档包。
2. 上传且仅上传：
   - `local-rag-platforms/fixtures/smoke/001-project-boundary.md`
   - `local-rag-platforms/fixtures/smoke/002-service-ports.md`
   - `local-rag-platforms/fixtures/smoke/003-run-policy.md`
3. 等三个文档完成处理并记录原始状态码；本轮落盘证据中的三份文档状态为 `nnn2`，
   不把它推断为 ready。可以在 UI 做命中测试，但在发现稳定、认证的公开
   retrieval API 前，统一结果仍记 `direct retrieval = unsupported`。
4. 创建“简易智能体” `moi-maxkb-smoke`，选择 TaaS LLM，关联上述知识库，
   发布；在概览 → API Key 创建本地 application key，并打开“API 文档”。

## API discovery

先抓取实例级 Swagger 候选（不发送凭据）：

```bash
local-rag-platforms/maxkb_local/maxkb-local.sh discover
```

结果写入 `.local-services/maxkb_local/logs/api-discovery-<UTC>/`，仅包含 HTTP
状态、所有可访问的 schema 和 path 列表。源码 tag 的候选为：

- `/admin/api-doc/schema/`、`/admin/api-doc/`
- `/chat/api-doc/schema/`、`/chat/api-doc/`
- OpenAI 候选总路径：`/chat/api/<application_id>/chat/completions`

若智能体概览给出的 URL 不同，以 UI/API 文档为准。把完整
`.../chat/completions` 拆成 `MAXKB_OPENAI_BASE_URL`（末尾不含该段）与
`MAXKB_OPENAI_PATH=/chat/completions`；不要把 API key 写进 URL。

## Native QA smoke

配置 `runtime.env` 后执行：

```bash
set -a; source .local-services/maxkb_local/runtime.env; set +a
local-rag-platforms/maxkb_local/maxkb-local.sh smoke
```

脚本先要求三个 fixture 文件存在，再调用现有统一 adapter。用
当前 smoke adapter 每次只提交一个显式 `question`；三类问题的批量循环留到
Stage 1 adapter，不宣称本轮已执行。raw artifact 由 adapter 脱敏。当前能力基线为 `ingest = partial`、
`direct retrieval = unsupported`、`native QA = partial`；只有取得稳定公开
retrieval API 或三题均返回有效回答后才能提升结论。

## 2026-08-06 实例验证结果

- `/admin/` 健康检查 200；管理员登录、OpenAI provider、TaaS LLM/Embedding
  均由本地管理 API 自动完成。
- TaaS LLM 与 Embedding 模型保存/连通性成功。三份 fixture 已创建，但现存
  证据为 3/3 文档状态 `nnn2`，因此 ingest 记为 `partial`，不宣称 ready。
- 简易应用已创建、发布并生成 application key。原生
  `/chat/api/<application_id>/chat/completions` 返回 OpenAI 格式 HTTP 200，但
  本轮回答未正确消费问题，故 Native QA 记为 `partial`。
- Swagger schema 候选受浏览器 cookie 会话保护，未取得稳定的机器可读
  OpenAPI；状态、bundle 路由和脱敏结果保存在
  `.local-services/maxkb_local/discovery/`。
- 未完成稳定公开 direct retrieval 验证，记为 `unsupported`；Native QA
  虽获 HTTP 200，但回答未正确消费问题，记为 `partial`。
- 统一汇总位于
  `.local-services/maxkb_local/logs/smoke-partial-2026-08-06/smoke-result.json`；
  其中 ingest/native 按统一 schema 记为 `error`，并在 `details` 保留上述
  partial 原因，retrieval 记为 `unsupported`。

停止 MaxKB（保留持久化数据）：

```bash
local-rag-platforms/maxkb_local/maxkb-local.sh stop
```

当前容器已保留。Dify 停止且 MOI 正常运行后，准确恢复命令为：

```bash
local-rag-platforms/maxkb_local/maxkb-local.sh resume
```

不要删除 `.local-services/maxkb_local/data`。

## 单文档完整链路 runner

`full-chain` 使用唯一 sentinel Markdown，依次执行 split、document batch create、
等待 embedding 状态 `2 = SUCCESS`、admin `hit_test`、创建并发布普通 SIMPLE
应用、创建应用 key，以及公开 OpenAI-compatible QA。默认要求回答包含 sentinel
且 `usage.total_tokens > 0`，否则 generative RAG 验收失败。公开 direct retrieval
没有稳定接口，仍明确记为 `unsupported`；诊断检索只使用 admin `hit_test`。

当前 Qianfan 链路可让 runner 自动发现模型 ID；也可以显式提供已验证的 ID。
知识库只绑定一个 embedding model，严禁中途切换向量空间：

```bash
export MAXKB_EMBEDDING_PROVIDER=qianfan
export MAXKB_EMBEDDING_MODEL_NAME=qwen3-embedding-8b
export MAXKB_EMBEDDING_DIMENSION=4096
export MAXKB_EMBEDDING_MODEL_ID=<optional-validated-qianfan-model-id>
export MAXKB_CHAT_MODEL_ID=<configured-chat-model-id>
export MAXKB_CHAT_PROVIDER='Baidu Qianfan deepseek-v4-flash'
local-rag-platforms/maxkb_local/maxkb-local.sh full-chain
```

`MAXKB_CHAT_MODEL_ID` 仍应设置为通过保存验证的 `deepseek-v4-flash` 记录。
full-chain 会先写入 `qianfan-embedding-verification.json`，再用同一个
`embedding_model_id` 创建知识库，并在 application request 中绑定该知识库；
manifest 会记录 provider、model ID、4096 维和 dataset/app binding。若只需保留检索链路的 partial 证据，可显式设置
`MAXKB_DIRECT_RETURN=1`；此模式会把文档设为 direct-return，并在 manifest 中
标为 `partial`，绝不宣称 generative RAG 成功。

每次运行的脱敏 raw request/response、HTTP 状态、manifest、错误状态和逐文件
SHA-256 会写入 `.local-services/maxkb_local/logs/full-chain-<UTC>/`；application
key 只写入 mode 0600 的 `secrets/`。聚焦测试：

```bash
local-rag-platforms/tests/maxkb/test-full-chain.sh
```

### 2026-08-10 历史 provider 结果

- MaaS `bge-m3` 以空 `model_params_form` 保存验证成功；新知识库只使用这一种
  向量空间，sentinel 文档 embedding 状态为 `nnn2`，admin `hit_test` 命中成功。
- 有效 Qianfan 配置来自仓库根 `.env` 的 `QIANFAN_API_KEY`。密钥只在进程和
  mode 0600 的 secrets 请求中使用；直连 `/v2/chat/completions` HTTP 200，MaxKB
  `deepseek-v4-flash` 保存验证为 `SUCCESS`。
- 普通 SIMPLE app 必须在 `model_setting.prompt` 中包含 `{data}` 与
  `{question}`。缺少占位符时 search step 虽命中 sentinel，段落却不会进入模型
  message；这是此前 usage 非零但回答称无知识的根因。
- 修复 prompt、重新发布后，公开 OpenAI-compatible QA HTTP 200，
  `usage.total_tokens=267`，回答精确包含 `MAXKB-SENTINEL-ORCHID-7419` 与
  `ORCHID-7419`。完整脱敏证据位于
  `.local-services/maxkb_local/logs/maxkb-full-chain-live/`。
