# MOI 统一评测工具沉淀需求文档

> 状态：Draft
> 版本：v0.1
> 日期：2026-08-19
> 需求来源：郭博提出，基于既有解析评测工具继续沉淀
> 暂定产品名：moi-eval

## 1. 文档目的

本文定义一套可安装、可复用、可通过命令行运行的统一评测工具，覆盖以下核心能力：

1. 将原始材料或已有结果转换为 Golden 候选草稿；
2. 使用本地 Codex 或 Claude Code 辅助生成和检查 Golden；
3. 对 Golden 进行校验、人工复核和冻结；
4. 将不同系统的运行结果转换为统一格式；
5. 根据冻结 Golden 和运行结果计算指标；
6. 生成可追溯的逐样本结果、汇总数据和评测报告；
7. 支持解析、RAG、NL2SQL、信息提取等不同评测领域扩展。

本文用于需求评审、方案设计、任务拆分和验收，不包含具体代码实现细节。

## 2. 背景

目前 MOI Benchmark 各评测方向已经积累了较多可复用资产，但实现分散在不同仓库或目录中：

- 解析评测已有可安装 Python 包、CLI、Golden 生成器、多个 Scorer 和报告模块；
- NL2SQL 已有通用评测内核和基于数据库执行结果的评分实现；
- RAG 已有数据准备、系统运行、检索与回答评分脚本，以及较完整的 Golden 规范；
- 信息提取已有字段归一化、字段级 Precision/Recall/F1、错误分类和报告脚本；
- 各方向的输入格式、运行目录、错误语义、报告结构和命令入口尚未统一；
- Golden 制作仍依赖较多人工处理，尚未形成可复用的 Codex/Claude Code 辅助工作流。

本需求希望将上述能力沉淀为一个统一工具，降低后续新增数据集、接入新系统和重复评测的成本。

## 3. 需求目标

### 3.1 核心目标

构建一个 Python 包和统一命令行工具，使评测人员能够通过少量稳定命令完成：

~~~text
准备输入
  → 生成 Golden 草稿
  → 校验与冻结 Golden
  → 导入或运行系统结果
  → 标准化结果
  → 计算指标
  → 生成报告
~~~

### 3.2 具体目标

1. 统一安装方式、命令入口、配置格式和运行目录；
2. 统一 Sample、Golden、Prediction、Metric、Report 的外层数据合同；
3. 保留各领域独立的 Golden 内容、归一化规则和评分指标；
4. Golden 制作支持确定性生成、人工编辑和本地 Agent 辅助三种方式；
5. 保证评测输入、Golden、系统输出、配置和代码版本可追溯；
6. 支持只对已有结果离线重算分数，不强制重新调用被测系统；
7. 保持已有解析和 NL2SQL 能力可迁移、可兼容。

## 4. 非目标

首版不以以下内容为目标：

1. 不建设 Web 标注平台或多人协同标注系统；
2. 不接入所有 MOI、竞品和第三方系统的在线运行接口；
3. 不允许使用被测系统输出直接作为未经复核的最终 Golden；
4. 不使用单一 LLM Judge 代替冻结 Golden；
5. 不默认把解析、RAG、NL2SQL、信息提取合并为一个跨领域总分；
6. 不在首版建设公共排行榜；
7. 不要求首版发布到公共 PyPI；
8. 不负责自动下载和分发所有 Benchmark 数据集。

## 5. 需求优先级

本文使用以下优先级：

- P0：首版必须具备，否则无法形成完整可用流程；
- P1：重要能力，首版可预留 Interface，后续迭代实现；
- P2：增强能力，不阻塞核心流程。

## 6. 核心原则

### 6.1 Golden 与运行结果必须分离

Golden 是评测前冻结的真值，运行结果是被评对象的输出，两者不得相互污染。

如果使用已有运行结果辅助生成 Golden，该结果只能作为候选线索，并必须：

- 标记来源；
- 回到原始文档、数据库或业务 Schema 进行独立验证；
- 经过人工复核；
- 在正式评分前生成新的冻结版本。

### 6.2 Agent 只能辅助，不能直接成为真值

Codex 或 Claude Code 可以：

- 提取候选字段；
- 起草 SQL；
- 拆分 RAG claims；
- 定位证据；
- 检查 Golden 内部一致性；
- 生成待人工确认事项。

Codex 或 Claude Code 不得：

- 直接写入或覆盖 Frozen Golden；
- 根据评分结果修改 Golden 或阈值；
- 在无来源证据的情况下补造事实；
- 静默忽略无法确定的字段或样本。

### 6.3 离线评分优先

评分模块必须可以只读取 Golden 和已有 Prediction 运行，不依赖在线系统。

在线调用系统属于 Target Adapter 的能力，可单独扩展，不应与核心 Scorer 强耦合。

### 6.4 原始产物必须保留

任何标准化和评分都不能覆盖原始系统响应。报告中的结论必须能够追溯到：

- 原始输入；
- Frozen Golden；
- 原始系统输出；
- 标准化 Prediction；
- Scorer 版本；
- 配置快照；
- 逐样本判分详情。

### 6.5 不适用与失败必须区分

- 指标不适用：值为 null，并提供 reason code；
- 系统超时、空响应、执行错误：属于运行失败，应保留在适用指标的分母中；
- Golden 或输入合同不合法：正式评分应停止；
- 不得使用 0 同时表达“不适用”和“失败”。

## 7. 用户角色与使用场景

### 7.1 评测数据维护者

负责准备数据、生成 Golden 草稿、复核证据并冻结 Golden。

典型任务：

- 从解析结果生成文档解析 Golden 草稿；
- 从 RAG 语料和问题生成 claims 与 evidence；
- 从数据库 Schema、业务问题生成 NL2SQL Golden SQL；
- 从文档和 JSON Schema 生成信息提取字段 Golden。

### 7.2 评测执行者

负责导入系统结果、运行评分和生成报告。

典型任务：

- 对新版本 MOI 结果离线重算；
- 对竞品输出进行统一标准化；
- 查看失败样本和错误类型；
- 比较同一数据集的多个 Run。

### 7.3 领域开发者

负责新增领域、数据集、Prediction Adapter 或 Scorer。

典型任务：

- 新增一个 RAG 平台输出 Adapter；
- 新增一个信息提取字段匹配器；
- 新增一个 NL2SQL 数据库 Adapter；
- 新增一个报告维度。

### 7.4 CI 或自动化任务

负责在代码或模型变更后运行固定评测集，输出机器可读结果，并根据阈值决定是否通过。

## 8. 总体流程

### 8.1 Golden 制作流程

~~~text
原始材料 / 数据库 / Schema / 已有标注
                    │
                    ├── 确定性 Golden Builder
                    │
                    └── Codex / Claude Code Assistant
                                │
                                ▼
                         Golden Draft
                                │
                      Schema 与来源校验
                                │
                         人工复核与修订
                                │
                                ▼
                         Frozen Golden
~~~

### 8.2 评分流程

~~~text
原始系统输出
      │
Prediction Adapter
      │
Canonical Prediction ── Frozen Golden
             │              │
             └──── Scorer ──┘
                      │
              Per-case Scores
                      │
                 Aggregator
                      │
          JSON / JSONL / Markdown Report
~~~

## 9. 功能需求

### 9.1 Python 包与 CLI

#### FR-CLI-001：统一包与命令入口（P0）

工具应提供：

- Python distribution：moi-eval；
- Python import namespace：moi_eval；
- Console command：moi-eval。

#### FR-CLI-002：统一帮助信息（P0）

执行以下命令必须能够获得可读的帮助：

~~~bash
moi-eval --help
moi-eval golden --help
moi-eval score --help
moi-eval report --help
~~~

#### FR-CLI-003：配置优先级（P0）

配置优先级应为：

1. 命令行显式参数；
2. --config 指定的配置文件；
3. 当前目录默认配置；
4. 工具内置默认值。

最终生效配置必须写入 Run 的 config snapshot。

#### FR-CLI-004：稳定退出码（P0）

至少区分：

| Exit Code | 含义 |
|---:|---|
| 0 | 命令成功 |
| 2 | CLI 参数或配置错误 |
| 3 | 数据合同、Golden 或 Schema 校验失败 |
| 4 | Agent 或外部 Target 不可用 |
| 5 | 评分器或内部运行错误 |

### 9.2 Golden 生命周期

#### FR-GOLD-001：生成 Golden Draft（P0）

工具应支持按 task_type 从原始材料生成 Golden 草稿。

支持的 task_type：

- parsing；
- rag；
- nl2sql；
- extraction。

#### FR-GOLD-002：Golden 状态（P0）

Golden 至少支持以下状态：

| 状态 | 含义 |
|---|---|
| draft | 自动或人工生成，尚未完成复核 |
| reviewed | 已完成人工复核，尚未冻结 |
| frozen | 可用于正式评分，不允许原地修改 |
| deprecated | 已被新版本替代，仅用于历史追溯 |

允许的主要状态流转：

~~~text
draft → reviewed → frozen → deprecated
~~~

#### FR-GOLD-003：Golden 校验（P0）

校验应包括：

- 外层 Schema 校验；
- case_id 唯一性；
- task_type 与领域 Reference 匹配；
- 必填字段；
- 来源文件存在性和 hash；
- 领域特定一致性；
- Agent 输出中的待确认项；
- 冻结所需复核信息。

#### FR-GOLD-004：Golden 冻结（P0）

冻结时必须：

- 已通过全部阻断性校验；
- 保存 reviewer；
- 保存 reviewed_at；
- 生成 freeze_id；
- 计算 canonical gold_hash；
- 保存 Schema 版本；
- 禁止覆盖已有 Frozen Golden。

修改 Frozen Golden 时必须创建新版本和新 hash。

#### FR-GOLD-005：Golden Diff（P1）

工具应支持比较两个 Golden 版本，至少显示：

- 新增、删除和修改的 Case；
- Reference 字段变化；
- 证据变化；
- Schema 版本变化；
- reviewer 与 freeze 信息变化。

#### FR-GOLD-006：允许基于结果生成候选（P0）

可以通过显式参数提供 seed prediction，但必须：

- 默认关闭；
- 标记 assistance_mode=seeded；
- 保存 seed prediction hash；
- 输出仍为 draft；
- 在 freeze 前完成 source-based validation；
- 不得由同一 seed prediction 自动完成生成、验证和冻结。

### 9.3 Codex 与 Claude Code 辅助 Golden

#### FR-AI-001：统一 Assistant Interface（P0）

应定义统一 GoldenAssistant Interface，并至少提供：

- Codex CLI Adapter；
- Claude Code CLI Adapter；
- None/Manual Adapter。

#### FR-AI-002：Assistant 选择（P0）

CLI 应支持：

~~~text
--assistant none
--assistant codex
--assistant claude
~~~

#### FR-AI-003：辅助模式（P0）

至少支持：

| 模式 | 输入范围 | 用途 |
|---|---|---|
| source_only | 原始材料、问题、Schema | 默认正式 Golden 制作 |
| seeded | source_only 输入加已有 Prediction | 快速生成候选，必须独立复核 |
| validate_only | Golden Draft 和来源材料 | 检查一致性，不重新生成全部内容 |

#### FR-AI-004：结构化输出（P0）

Agent 输出必须满足指定 JSON Schema。以下行为应视为失败：

- 只输出自然语言而无结构化结果；
- 输出无法解析的 JSON；
- 缺少必填字段；
- 引用不存在的文件、页码、字段或数据库对象；
- 在没有证据时填入确定值且未标记 uncertainty。

#### FR-AI-005：运行隔离（P0）

Agent 应在独立 job 目录中运行，只接收显式提供的输入。

Agent 不应默认获得：

- Frozen Golden 写权限；
- 整个 Benchmark 仓库写权限；
- 无关 Run 的结果；
- 未显式授权的凭据；
- 被测系统的内部 Prompt 或秘密配置。

#### FR-AI-006：可追溯性（P0）

每次 Agent 任务至少保存：

- assistant 类型；
- CLI 版本；
- 模型信息（如果可获取）；
- Prompt 或 Prompt hash；
- 输入文件与 hash；
- assistance_mode；
- 开始与结束时间；
- Exit Code；
- stdout/stderr 脱敏日志；
- 生成的 candidate hash；
- 未解决问题列表。

#### FR-AI-007：失败处理（P0）

Agent 超时、不可执行或输出不合法时：

- 不得生成 Frozen Golden；
- 必须明确失败 Case；
- 不得静默跳过；
- 支持只重试失败 Case；
- 不影响纯人工或确定性 Builder 路径。

#### FR-AI-008：敏感数据提示（P0）

工具必须明确说明：

> 本地 Codex/Claude Code CLI 不等于模型在本机离线运行，输入内容可能被发送到远端模型服务。

涉及私有或敏感材料时，应由使用者确认允许发送范围。

### 9.4 Prediction 标准化

#### FR-PRED-001：保留原始输出（P0）

系统原始响应必须独立保存，Prediction Adapter 只能生成新的标准化文件，不得覆盖原始响应。

#### FR-PRED-002：统一 Prediction Envelope（P0）

标准化 Prediction 至少包含：

- schema_version；
- task_type；
- case_id；
- system_id；
- run_id；
- output；
- latency；
- artifacts；
- error；
- diagnostics；
- raw_output_ref。

#### FR-PRED-003：Adapter 可扩展（P0）

不同系统和数据集的输出差异应通过 Prediction Adapter 处理，Scorer 不应直接解析各产品原始响应。

#### FR-PRED-004：失败样本不得丢失（P0）

超时、服务错误、空响应、格式错误和缺少结果的样本必须生成 Prediction 错误记录，并保留在 Run 中。

### 9.5 评分

#### FR-SCORE-001：只使用 Frozen Golden 正式评分（P0）

正式评分默认只接受 status=frozen 的 Golden。

允许通过 --allow-draft 进行调试评分，但：

- 报告必须标记为非正式；
- 输出不得与正式 Run 混放；
- 不得用于 CI Gate 或正式对比结论。

#### FR-SCORE-002：领域 Scorer（P0）

Scorer 通过 task_type 注册，不同领域保留独立算法和配置。

#### FR-SCORE-003：确定性重算（P0）

给定完全相同的：

- Frozen Golden；
- Canonical Prediction；
- 配置；
- Scorer 版本；

离线评分结果必须可重复。

如果使用随机采样，必须固定并记录随机种子。

#### FR-SCORE-004：指标数据结构（P0）

每个 Metric 至少包含：

- metric_id；
- value；
- numerator；
- denominator；
- aggregation；
- higher_is_better；
- unit；
- applicable；
- reason_code；
- diagnostics。

#### FR-SCORE-005：错误语义（P0）

| 场景 | 处理要求 |
|---|---|
| Golden 无效 | 停止正式评分 |
| Prediction 缺失 | 记录系统失败，适用指标按失败计入 |
| Target 超时或报错 | 保留在分母中 |
| 指标不适用 | value=null，提供 reason_code |
| Scorer 异常 | 保存 scorer_error，不得丢弃 Case |
| Trace 不可用 | 不推断检索指标，标记 TRACE_UNAVAILABLE |

#### FR-SCORE-006：聚合（P0）

聚合必须：

- 保留原始 numerator 和 denominator；
- 区分 macro、micro、weighted、sum、min 等语义；
- 不将 N/A 自动当成 0；
- 记录参与聚合的样本数；
- 记录跳过原因；
- 避免默认跨 task_type 生成一个总分。

#### FR-SCORE-007：LLM Judge 指标（P1）

可以增加 LLM Judge 指标，但必须：

- 与确定性指标分开展示；
- 记录模型、Prompt、版本和温度；
- 保存逐样本 judgement reason；
- 不得作为首版唯一主指标；
- 支持对相同结果离线重放或读取已保存 judgement。

### 9.6 报告

#### FR-REPORT-001：基础报告格式（P0）

至少输出：

- JSONL 逐样本结果；
- JSON 汇总；
- Markdown 报告；
- Console 摘要。

#### FR-REPORT-002：报告内容（P0）

报告至少包含：

- 数据集和 Run 基本信息；
- Frozen Golden 的 freeze_id 和 gold_hash；
- 被测系统、版本和配置；
- 样本总数、成功数、失败数；
- 各指标 numerator、denominator 和 value；
- N/A 数量和原因；
- 逐 Case 评分；
- 错误类型分布；
- 典型失败 Case；
- 时延和资源指标（如果存在）；
- 评测限制。

#### FR-REPORT-003：Run 对比（P1）

工具应支持在配置、Golden 和指标合同兼容时比较两个 Run。

如果上下文不兼容，应拒绝输出误导性的单值差异，并展示不兼容原因。

### 9.7 在线运行

#### FR-RUN-001：Target Interface（P1）

在线运行模块通过 Target Interface 扩展，至少约定：

- health_check；
- predict；
- timeout；
- retry policy；
- raw response capture；
- latency；
- system metadata；
- error mapping。

#### FR-RUN-002：运行与评分解耦（P1）

在线运行完成后必须先落盘 Prediction，再调用 Scorer。不得只在内存中完成运行和评分。

## 10. CLI 需求草案

### 10.1 Golden Draft

~~~bash
moi-eval golden draft \
  --task rag \
  --source benchmark_data/source \
  --dataset benchmark_data/questions.jsonl \
  --assistant codex \
  --assistant-mode source_only \
  --output benchmark_data/golden.draft.jsonl
~~~

### 10.2 Golden Validate

~~~bash
moi-eval golden validate \
  --task rag \
  --golden benchmark_data/golden.draft.jsonl \
  --source benchmark_data/source
~~~

### 10.3 Golden Freeze

~~~bash
moi-eval golden freeze \
  --task rag \
  --golden benchmark_data/golden.draft.jsonl \
  --reviewer guobo \
  --output benchmark_data/golden.jsonl
~~~

### 10.4 Prediction Normalize

~~~bash
moi-eval prediction normalize \
  --task rag \
  --adapter matrixflow \
  --input runs/raw/results.jsonl \
  --output runs/normalized/predictions.jsonl
~~~

### 10.5 Offline Score

~~~bash
moi-eval score \
  --task rag \
  --golden benchmark_data/golden.jsonl \
  --predictions runs/normalized/predictions.jsonl \
  --config benchmark.yaml \
  --output runs/rag-eval-001
~~~

### 10.6 Report

~~~bash
moi-eval report \
  --run runs/rag-eval-001 \
  --format console,json,markdown
~~~

## 11. 统一数据合同

### 11.1 Golden Envelope 示例

~~~json
{
  "schema_version": "moi-eval.golden.v1",
  "task_type": "rag",
  "case_id": "q-001",
  "status": "draft",
  "inputs": {
    "question": "示例问题"
  },
  "reference": {},
  "provenance": {
    "source_hashes": {},
    "assistant": "codex",
    "assistance_mode": "source_only",
    "prompt_hash": "sha256:..."
  },
  "review": {
    "reviewed_by": null,
    "reviewed_at": null
  },
  "freeze": {
    "freeze_id": null,
    "gold_hash": null
  },
  "metadata": {}
}
~~~

### 11.2 Prediction Envelope 示例

~~~json
{
  "schema_version": "moi-eval.prediction.v1",
  "task_type": "rag",
  "case_id": "q-001",
  "system_id": "matrixflow",
  "run_id": "rag-eval-001",
  "output": {},
  "latency_s": 1.23,
  "artifacts": {},
  "diagnostics": {},
  "raw_output_ref": "raw/results.jsonl#q-001",
  "error": null
}
~~~

### 11.3 Score Envelope 示例

~~~json
{
  "schema_version": "moi-eval.score.v1",
  "task_type": "rag",
  "case_id": "q-001",
  "scorer_id": "rag.retrieval",
  "metrics": [
    {
      "metric_id": "recall_at_5",
      "value": 0.8,
      "numerator": 4,
      "denominator": 5,
      "aggregation": "weighted",
      "higher_is_better": true,
      "applicable": true,
      "reason_code": null
    }
  ],
  "errors": [],
  "diagnostics": {}
}
~~~

## 12. 各领域需求

### 12.1 文档解析

#### Golden 输入

- 原始 PDF、DOCX、PPTX、XLSX；
- 已有解析 JSON/Markdown/HTML；
- 人工标注；
- 开源 Benchmark 标注。

#### Golden 内容

- 文本内容；
- 页眉页脚；
- 标题与层级；
- 表格内容与结构；
- 公式；
- 图片与图注；
- 阅读顺序；
- 页面与版面位置；
- XLSX sheet、cell、formula、format、visual elements。

#### 核心指标

- Precision、Recall、F1；
- NED；
- TEDS；
- 阅读顺序；
- 文本覆盖率；
- 表格结构；
- 公式保真；
- XLSX 单元格值、类型、格式和导出保真。

#### Agent 辅助

- 检查自动生成 Draft；
- 补充标题层级和阅读顺序；
- 检查表格、公式和图片遗漏；
- 输出待人工复核清单。

解析领域优先复用现有 deterministic Golden Generator 和 Scorer。

### 12.2 RAG

#### Golden 输入

- 问题；
- 冻结语料；
- 文档 ID、文件 hash 和页面信息；
- 可选候选答案或已有检索结果；
- 可回答性和引用要求。

#### Golden 内容

每个 Case 至少包含：

- question；
- answerability；
- question_type；
- scored_reference_claims；
- critical_required_claims；
- evidence_sets；
- evidence；
- allowed_document_ids；
- citation_required；
- negative_type 与 negative_reason（不可回答题）；
- source/page/span/bbox/hash；
- freeze 信息。

Evidence Set 语义：

- 不同 Evidence Set 之间为 OR；
- 同一个 Evidence Set 内的 Evidence Items 为 AND。

#### 核心指标

检索层：

- Hit@K；
- Recall@K；
- Precision@K；
- MRR；
- nDCG@K；
- Complete Evidence-set Recall@K。

回答层：

- Claim Correctness；
- Reference-claim Recall；
- Critical Claim Coverage；
- Gold-evidence Support；
- Strict Unanswerable Success；
- False Refusal；
- Critical Contradiction。

引用层：

- Citation Locator Validity；
- Citation Entailment Precision；
- Answer-claim Citation Coverage；
- Fabricated Citation Count；
- Out-of-scope Citation Count。

可靠性：

- Request Success；
- Initial Availability；
- P50/P95 Latency；
- Repeat Consistency。

#### 特殊规则

- 没有真实 retrieval trace 时，不得从答案或 Citation 反推检索指标；
- Trace 指标应返回 null 和 TRACE_UNAVAILABLE；
- RAG 不应只依赖 reference_answer 字符串相似度；
- Answer、Retrieval、Citation 指标必须分层报告。

#### Agent 辅助

- 判断可回答性；
- 将答案拆成原子 claims；
- 为每个 claim 定位一组或多组 Evidence Sets；
- 检查证据是否完整支持 claim；
- 生成 reference_answer 供人阅读；
- 检查不可回答题 negative_reason；
- 标记证据不充分和歧义。

### 12.3 NL2SQL

#### Golden 输入

- 自然语言问题；
- 数据库 Schema；
- 数据库方言；
- 冻结数据库快照；
- 业务口径；
- 可选已有 SQL 或运行结果。

#### Golden 内容

- question；
- database_id；
- database_snapshot_hash；
- dialect；
- gold_sql 或 sql_cases；
- expected result 或可重算引用；
- ordered comparison；
- numeric tolerance；
- column match policy；
- business conventions；
- query selector（多轮或多 SQL 场景）。

#### 核心指标

- Execution Accuracy；
- Valid SQL Rate；
- SQL Execution Success；
- Result-set Equivalence；
- Case Pass Rate；
- End-to-end Latency；
- Repeat Correct Rate；
- 错误分类：无 SQL、只读检查失败、执行错误、结果错误、超时。

#### 安全要求

- 使用只读数据库账号；
- 只允许单条只读 SELECT 或 CTE；
- 拒绝 DDL、DML、事务控制、文件读写和多语句；
- 设置超时和最大返回行数；
- 每条 SQL 使用隔离连接或隔离事务；
- 不将 Golden SQL 和详细业务口径提供给被测系统。

#### 比较要求

- 有 ORDER BY 时按有序结果比较；
- 无 ORDER BY 时按无序集合或多重集合比较；
- 统一 NULL、整数与等值小数；
- 支持字段级数值容差；
- SQL 文本不同但结果等价应判为正确。

#### Agent 辅助

- 根据问题和 Schema 起草 Gold SQL；
- 识别题目歧义；
- 执行并检查结果；
- 生成可解释的业务口径；
- 输出需要人工确认的 join、去重、排序、时间和空值语义。

Agent 生成的 SQL 必须实际在冻结快照上执行并由人工复核后才能冻结。

### 12.4 信息提取

#### Golden 输入

- 原始文档；
- 业务 JSON Schema；
- 字段说明和别名；
- 数据集原始标注；
- 可选已有提取结果。

#### Golden 内容

- case_id；
- source hash；
- schema version；
- typed fields；
- scalar/array/object 类型；
- 缺失值语义；
- 字段级原文 evidence；
- 页码、bbox 或 grounding（可用时）；
- 归一化规则。

#### 核心指标

- 字段级 Precision、Recall、F1；
- Raw Exact Match；
- Normalized Exact Match；
- 文档全字段正确率；
- 缺失字段正确率；
- 空字段误提率；
- 非空字段漏提率；
- Schema 合法率；
- API/任务成功率；
- 数组和多实体集合匹配。

#### 归一化要求

- Unicode NFKC；
- 首尾空白和连续空格；
- 大小写；
- 日期；
- 金额和百分比；
- 单位；
- 前导零；
- scalar 与 singleton list；
- 数组顺序是否敏感；
- 空字符串、null、缺键和空数组的业务语义。

归一化规则必须按字段配置，不能用一个通用模糊匹配器处理所有字段。

#### Agent 辅助

- 根据 JSON Schema 提取候选字段；
- 标记字段来源页码和原文；
- 区分缺失值与无法判断；
- 检查 Schema 类型；
- 检查实体混淆、日期混淆和多值遗漏；
- 输出 uncertainty 和人工复核事项。

## 13. Module 与 Interface 设计要求

工具应采用统一内核和领域扩展的结构，外部 Interface 保持小而稳定，领域复杂度隐藏在内部实现中。

建议 Module：

~~~text
moi_eval/
├── cli/                 # 命令解析与用户输出
├── core/                # Sample、Prediction、Score、Aggregate、Run
├── golden/              # Draft、Validate、Review、Freeze、Diff
├── assistants/          # Codex、Claude、Manual Adapter
├── domains/
│   ├── parsing/
│   ├── rag/
│   ├── nl2sql/
│   └── extraction/
├── adapters/            # 系统输出和数据集 Adapter
├── reporters/           # Console、JSON、Markdown
└── schemas/             # JSON Schema 与版本
~~~

### 13.1 外部 Interface

核心 Python Interface 建议收敛为：

- draft_golden；
- validate_golden；
- freeze_golden；
- normalize_predictions；
- evaluate；
- render_report。

### 13.2 扩展 Seam

只有确实存在多种实现的地方建立 Adapter：

- GoldenAssistant：Codex、Claude、Manual；
- PredictionAdapter：MOI、Dify、FastGPT、其他产品；
- Target：在线系统调用；
- DBProvider：SQLite、MySQL、MatrixOne；
- Reporter：Console、JSON、Markdown；
- 领域 Scorer。

### 13.3 Scorer 测试要求

每个 Scorer 必须通过统一 Interface 测试，不应要求测试绕过 Interface 读取内部状态。

## 14. 运行产物目录

建议每次评测生成独立目录：

~~~text
runs/<run_id>/
├── manifest.json
├── config.snapshot.yaml
├── golden.ref.json
├── raw/
│   └── results.jsonl
├── normalized/
│   └── predictions.jsonl
├── scores/
│   └── scores.jsonl
├── summary.json
├── report.md
├── artifacts/
├── assistant/
│   ├── manifest.json
│   ├── prompt.txt
│   └── logs/
└── state.json
~~~

manifest.json 至少记录：

- run_id；
- task_type；
- dataset_id；
- source hashes；
- freeze_id 和 gold_hash；
- system 和版本；
- 开始与结束时间；
- 代码 commit；
- Schema 版本；
- Scorer 版本；
- 配置 hash；
- 成功、失败和重试数量。

## 15. 非功能需求

### 15.1 可复现性（P0）

- 所有输入和配置保存 hash；
- 固定随机种子；
- 记录依赖和代码版本；
- 相同输入离线重算得到相同结果；
- Agent 结果不要求完全确定，但必须保存完整 provenance。

### 15.2 可移植性（P0）

- 核心包不得依赖 MatrixFlow 或 MOI 内部代码；
- 领域和产品差异通过 Adapter 接入；
- 核心离线评分应可在 macOS 和 Linux 运行；
- 密钥只通过环境变量或明确的 secret provider 获取。

### 15.3 安全性（P0）

- 日志和报告不得写入 API Key、Cookie、Token 或数据库密码；
- NL2SQL 使用只读执行；
- Agent 工作目录限制写入范围；
- 输出路径默认不覆盖已有文件；
- Frozen Golden 不允许原地修改；
- 原始文档是否允许发送远端模型必须显式确认。

### 15.4 性能（P1）

- 支持批量 JSONL 流式读取；
- 支持按 Case 并行评分；
- 并发数可配置；
- 大数据集不要求一次加载全部原始 Artifact 到内存；
- 可对失败 Case 断点续跑。

### 15.5 可观测性（P0）

- 每个 Case 有稳定 ID；
- 每一步记录状态、耗时和错误；
- 错误使用稳定 reason code；
- 报告显示失败分母；
- 支持 verbose 日志；
- 日志必须脱敏。

### 15.6 兼容性（P0）

- 迁移期间保留现有 benchmark 和 parsing-benchmark 命令；
- 旧命令可以委托到新实现；
- 已冻结 Golden 不因升级静默改变评分语义；
- Schema 或算法变化必须提升版本并写入报告。

## 16. 建议分期

### 16.1 M0：统一骨架与合同

- 新 Python 包和 moi-eval CLI；
- 通用数据模型；
- Golden 状态、validate、freeze；
- Prediction 标准化和基础聚合；
- JSON/JSONL/Markdown 输出；
- Codex/Claude Assistant Interface；
- 示例领域或 Toy Scorer 打通端到端。

### 16.2 M1：迁移现有能力

- 迁移解析评测；
- 迁移现有 NL2SQL evalcore 和 Execution Accuracy；
- 保持旧 CLI 兼容；
- 补充单元测试和回归测试。

### 16.3 M2：补齐 RAG 与信息提取

- RAG Golden Schema、Builder、Scorer；
- 信息提取 Golden Schema、Normalizer、Scorer；
- Codex/Claude 两个 Assistant Adapter；
- 四领域完整示例。

### 16.4 M3：在线运行与对比

- Target Adapter；
- 多 Run 对比；
- CI Gate；
- 可选 LLM Judge；
- Golden Diff 和迁移工具。

## 17. MVP 验收标准

### 17.1 安装与 CLI

- [ ] 可以在全新虚拟环境安装；
- [ ] moi-eval --help 正常；
- [ ] Golden、Prediction、Score、Report 命令帮助完整；
- [ ] 错误命令返回稳定非零 Exit Code。

### 17.2 Golden

- [ ] 可生成 draft；
- [ ] 可校验外层 Schema 和领域必填字段；
- [ ] 可记录 Codex/Claude provenance；
- [ ] 可由 reviewer 冻结；
- [ ] 冻结后有 freeze_id 和 gold_hash；
- [ ] Frozen Golden 不能被默认覆盖；
- [ ] 正式评分默认拒绝 Draft Golden。

### 17.3 Agent 辅助

- [ ] 支持 none、codex、claude；
- [ ] Agent 输出通过 JSON Schema；
- [ ] Agent 失败不会生成 Frozen Golden；
- [ ] seeded 模式有明确污染标记；
- [ ] 保存 Prompt、输入 hash、CLI 版本和脱敏日志；
- [ ] 敏感数据提示明确。

### 17.4 评分

- [ ] 同一输入离线重复评分结果一致；
- [ ] Prediction 失败样本不被丢弃；
- [ ] N/A 与失败语义分离；
- [ ] 报告保留 numerator 和 denominator；
- [ ] 每个分数能追溯到 Case、Golden 和 Prediction；
- [ ] 不默认产生跨领域总分。

### 17.5 领域覆盖

- [ ] parsing 至少一个可运行示例；
- [ ] rag 至少一个可运行示例；
- [ ] nl2sql 至少一个可运行示例；
- [ ] extraction 至少一个可运行示例；
- [ ] 现有解析和 NL2SQL 核心用例无回归。

### 17.6 报告

- [ ] 输出 scores.jsonl；
- [ ] 输出 summary.json；
- [ ] 输出 report.md；
- [ ] 显示失败数量和 reason code；
- [ ] 显示 Golden hash、配置 hash 和 Scorer 版本。

## 18. 风险

### 18.1 Golden 自证循环

风险：直接用被测结果生成 Golden，导致系统用自己的结果给自己打分。

控制：

- 默认 source_only；
- seeded 模式显式标记；
- 强制 source-based validation；
- 人工复核；
- 评分前冻结。

### 18.2 Agent 幻觉

风险：Agent 生成不存在的证据、字段或 SQL 语义。

控制：

- JSON Schema；
- 来源定位；
- hash 校验；
- NL2SQL 实际执行；
- 信息提取 evidence；
- RAG claim-evidence 验证；
- 人工复核。

### 18.3 指标口径混乱

风险：不同数据集使用相同名字但不同定义，或跨领域错误聚合。

控制：

- metric_id 命名空间；
- Schema 和 Scorer 版本；
- 保存 numerator/denominator；
- 报告 config snapshot；
- 禁止默认跨领域总分。

### 18.4 敏感数据泄露

风险：本地 CLI 将私有文档发送到远端模型。

控制：

- 显式提示；
- 输入白名单；
- source_only job directory；
- 环境与日志脱敏；
- 支持 assistant=none；
- 后续可增加本地模型 Adapter。

### 18.5 迁移范围过大

风险：一次性重构四个领域导致首版周期失控。

控制：

- 先统一合同和 CLI；
- 优先复用现有 evalcore；
- 逐领域通过 Adapter 迁移；
- 旧命令保持可用；
- 按 M0/M1/M2 分期验收。

## 19. 待确认事项

1. 新工具最终名称是否确定为 moi-eval？
2. 代码继续放在 agent-eval-tools，还是迁移到 moi-benchmark 下的新目录？
3. 首版是否必须包含在线调用系统，还是先完成离线 Golden 与评分？
4. “根据结果生成 Golden”是否接受“仅生成 Draft，人工复核后冻结”的约束？
5. Frozen Golden 是否要求双人复核，还是一名 reviewer 即可？
6. 首批需要支持哪些产品的 Prediction Adapter？
7. Codex/Claude Code 是否允许读取公司内部和客户文档？
8. 首版 Python 最低版本要求是什么？
9. 是否需要在首版提供 CI 阈值 Gate？
10. 是否需要保留或改名现有 benchmark、parsing-benchmark 命令？
11. RAG 首版是否需要 LLM Judge，还是只实现确定性与人工 judgement 导入？
12. 是否需要从首版开始提供 Golden Schema migration？

## 20. 现有资产参考

### 20.1 解析与通用内核

本机参考实现：

~~~text
/Users/muuushroom/gitrepos/algorithm/algorithm-yields/agent-eval-tools
~~~

可复用内容：

- Python 包与 CLI；
- evalcore；
- Sample、Prediction、Metric、Score、Report；
- Golden Generator；
- parsing Scorers；
- NL2SQL Execution Accuracy；
- Reporter 与测试。

### 20.2 RAG

- [RAG Golden 与指标规范](../../rag/plans/todo/golden-and-metrics-spec-v0.4.md)
- [Dify RAG 评测包](../../rag/dify-rag-eval/README.md)
- [RAG Benchmark Track](../../rag/README.md)

### 20.3 NL2SQL

- [NL2SQL 评测设计](../../nlp2sql/enron_eval/docs/evaluation-design.md)
- [Enron 统一评测口径](../../nlp2sql/enron_eval/benchmark/questions/spec/evaluation_conventions.md)
- [NL2SQL Track](../../nlp2sql/README.md)

### 20.4 信息提取

- [信息提取评测计划](../../document-extracting/plans/drafts/v0.1.md)
- [信息提取统一评分脚本](../../document-extracting/scripts/evaluate_extraction_benchmark.py)

## 21. 建议评审结论

需求评审时建议优先确认以下四点：

1. Golden 只能由 Draft 经复核后冻结；
2. 首版以离线评分为核心，在线 Target 作为扩展；
3. 采用统一内核和领域 Adapter，不把四个领域揉成一个 Scorer；
4. 首版开发边界按 M0/M1/M2 分期，避免一次性迁移全部历史脚本。
