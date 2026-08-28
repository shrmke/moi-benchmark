# 文档解析评测工具统一升级方案

> 状态：Draft
> 版本：v0.1
> 日期：2026-08-20
> 上位需求：[MOI 统一评测工具沉淀需求文档](../../../docs/requirements/unified-evaluation-tool-requirements.md)
> 既有实现：`/Users/muuushroom/gitrepos/algorithm/algorithm-yields/agent-eval-tools`

## 1. 方案结论

本次升级不重写文档解析评分算法，而是在既有 `agent-eval-tools` 基础上完成统一化改造：保留已经验证的 Golden Generator、解析 Evaluator、聚合逻辑、API Client 和回归测试，在其上建立 `moi-eval` 的统一合同、Golden 生命周期、Prediction 标准化、Run 产物和报告能力。

推荐继续在 `agent-eval-tools` 所在实现仓库中演进代码，首版同时保留 `parsing-benchmark` 和 `benchmark` 旧命令。`moi-benchmark` 的 `document-parsing/` 只保存本 Track 的方案、数据集、Frozen Golden、运行记录和报告，并在 Run manifest 中固定 `moi-eval` 版本与代码 commit，避免复制两份工具源码。

首个可验收纵向切片为：

```text
现有私有解析 Golden
  -> 迁移为 reviewed/frozen Golden Envelope
  -> 导入一批已有 parsed JSON
  -> 标准化为 Canonical Prediction
  -> 调用既有解析 Scorer 离线评分
  -> 输出 scores.jsonl、summary.json、report.md 和 manifest.json
```

在该切片通过后，再扩展 XLSX、OmniDocBench 官方口径、在线 Target 和 Agent 辅助。

## 2. 背景与现状

既有工具当前已经具备以下可复用资产：

- Python 包、`parsing-benchmark` 和 `benchmark` CLI；
- PDF、DOCX、PPTX、XLSX 解析评测；
- 文本、标题、页眉页脚、表格、TEDS、公式、图片、Caption、版面和阅读顺序等 Evaluator；
- XLSX sheet、cell value/type/format、公式、表结构、视觉元素和导出保真等 Evaluator；
- 从 parsed JSON、原始 XLSX 或 XLSX 导出产物生成 Golden 草稿；
- 在线解析和已有结果离线评分；
- Console、JSON、Markdown 报告与历史报告对比；
- 通用 `evalcore` 的 Sample、Prediction、Metric、Score、Runner、Registry 和 Aggregate 雏形；
- 大量单元、集成和回归测试。

现有实现的主要不足不是评分维度缺失，而是正式评测治理不足：Golden 没有冻结状态和 hash，Prediction 没有统一 Envelope，Run 没有完整 manifest，旧 Scorer 尚未真正收敛到统一 Interface，且缺少 Codex/Claude 辅助工作流。

## 3. 目标与非目标

### 3.1 升级目标

1. 提供 `moi-eval` distribution、`moi_eval` namespace 和 `moi-eval` CLI。
2. 让解析 Golden 遵循 `draft -> reviewed -> frozen -> deprecated` 生命周期。
3. 将不同 Parser 的原始结果转换为统一 Parsing Prediction。
4. 将既有解析 Evaluator 隐藏在一个稳定的解析 Scorer Interface 后。
5. 支持只读取 Frozen Golden 和已有 Prediction 的确定性离线重算。
6. 生成逐 Case、可聚合、可追溯且可复跑的标准 Run 产物。
7. 保持旧 CLI、已有 Golden 和既有核心评分用例在迁移期间可用。
8. 为 Codex、Claude Code 和纯人工 Golden 流程提供统一 Assistant Interface。

### 3.2 非目标

1. 本方案不重新设计每个解析维度的算法。
2. 首个纵向切片不要求在线调用 MOI、MinerU 或 Paddle。
3. 不在首版构建 Web 标注平台、公共排行榜或多人协同审核系统。
4. 不把 OmniDocBench 官方分与私有解析分合成为一个总分。
5. 不在本方案中实现 RAG Scorer；公共内核必须为 RAG 预留稳定接入点，但 RAG 领域实现另行规划。
6. 不允许用任一 Parser 输出直接生成未经复核的 Frozen Golden。

## 4. 设计原则

### 4.1 统一内核是深 Module

调用方只需要学习以下六个外部 Interface：

```text
draft_golden
validate_golden
freeze_golden
normalize_predictions
evaluate
render_report
```

Schema 解析、hash、状态流转、文件布局、失败记录和领域注册等复杂度应隐藏在实现内部。CLI 和 Python 调用使用同一 Interface，测试也通过该 Interface 验证行为。

### 4.2 领域复杂度留在解析 Module 内

解析 Module 对统一内核呈现小而稳定的 Interface。文本、标题、表格、公式、XLSX 等既有 Evaluator 是解析 Module 的内部实现，不作为新的公共 Interface 暴露。

### 4.3 只在真实变化点建立 Seam

本次保留以下真实 Seam：

- `GoldenAssistant`：Manual、Codex、Claude 三种 Adapter；
- `PredictionAdapter`：Canonical Blocks、MOI、MinerU、Paddle 等 Adapter；
- `Reporter`：Console、JSON、Markdown Adapter；
- 领域 Scorer：Parsing、RAG、NL2SQL、Extraction；
- `Target`：在线系统调用，作为 P1 扩展。

Golden hash、状态机、Run writer 等只有一种业务语义，不为它们增加不必要的 Adapter。

### 4.4 临时桥接必须有删除条件

迁移期允许 `moi_eval.domains.parsing` 调用旧 `parsing_benchmark` 实现，但该桥接只是迁移手段。完成解析回归、旧 CLI 委托和包结构迁移后，应删除双向转换与重复模型，最终只保留一套评分实现。

## 5. 目标结构

建议在现有实现仓库内形成以下结构：

```text
src/
├── moi_eval/
│   ├── cli/
│   ├── core/
│   │   ├── models.py
│   │   ├── errors.py
│   │   ├── hashing.py
│   │   ├── registry.py
│   │   └── run_store.py
│   ├── golden/
│   │   ├── lifecycle.py
│   │   ├── validation.py
│   │   └── freeze.py
│   ├── assistants/
│   ├── adapters/
│   ├── domains/
│   │   └── parsing/
│   │       ├── models.py
│   │       ├── builders.py
│   │       ├── validation.py
│   │       ├── adapters.py
│   │       └── scorer.py
│   ├── reporters/
│   └── schemas/
└── parsing_benchmark/        # 迁移期兼容入口，最终只保留必要 shim
```

包应同时注册：

```text
moi-eval          -> moi_eval.cli
parsing-benchmark -> 兼容旧解析命令
benchmark         -> 兼容旧通用命令
```

## 6. 数据合同升级

### 6.1 Parsing Golden Envelope

统一外层保存状态和来源，现有解析标注作为 `reference` 保留：

```json
{
  "schema_version": "moi-eval.golden.v1",
  "task_type": "parsing",
  "case_id": "private-001",
  "status": "draft",
  "inputs": {
    "source_ref": "datasets/private/private-001/source.pdf",
    "doc_type": "pdf"
  },
  "reference": {
    "profile": "doc",
    "annotation": {}
  },
  "provenance": {
    "source_hashes": {},
    "assistant": "none",
    "assistance_mode": "seeded",
    "seed_prediction_hash": "sha256:..."
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
```

`reference.annotation` 首版直接兼容现有 `GoldenAnnotation` 和 `xlsx_workbook` 结构，避免在统一外层建设时同时重写所有领域字段。解析内部 Schema 的不兼容变化必须单独提升版本。

### 6.2 Parsing Prediction Envelope

```json
{
  "schema_version": "moi-eval.prediction.v1",
  "task_type": "parsing",
  "case_id": "private-001",
  "system_id": "moi-idc-4.1.14",
  "run_id": "parsing-20260820-001",
  "output": {
    "profile": "doc",
    "blocks": []
  },
  "latency_s": 1.23,
  "artifacts": {
    "markdown_ref": null,
    "html_refs": []
  },
  "diagnostics": {},
  "raw_output_ref": "raw/results.jsonl#private-001",
  "error": null
}
```

标准化不得覆盖 `raw/`。缺失、空响应、超时和格式错误也必须生成一条带 `error` 的 Prediction。

### 6.3 Metric 和 Score

旧 `MetricValue.name/value/weight_count/na_reason` 迁移为统一字段：

```text
metric_id
value
numerator
denominator
aggregation
higher_is_better
unit
applicable
reason_code
diagnostics
```

不得通过 `value * total_count` 反推已经丢失的 numerator。对 Precision、Recall、F1、覆盖率等比例指标，应修改内部 Evaluator 的返回合同，直接保留匹配数、预测数和 Golden 数。对不能自然表示为比值的 NED、TEDS 或复合指标，仍需保留字段，并在指标定义中明确 numerator/denominator 是否为 `null` 以及聚合语义。

## 7. Golden 升级方案

### 7.1 Builder 分类

| 输入路径 | 模式 | 处理规则 |
|---|---|---|
| OmniDocBench 官方标注 | `source_only` | 由 Dataset Adapter 确定性导入 |
| 原始 XLSX | `source_only` | 复用 Excel Golden Generator |
| DOCX/PPTX 原生结构 | `source_only` | 有可信确定性提取器时使用，否则转人工/Assistant |
| PDF 或扫描件原文 | `source_only` | Manual 或 Assistant 生成候选，必须保留证据定位 |
| 任一 Parser 输出 | `seeded` | 复用现有 Golden Generator，只能输出 Draft |
| 已有历史 Golden | `migration` | 默认包装为 Draft；只有保留了可验证 reviewer 记录时才能导入为 Reviewed |

现有 `parsing-benchmark golden generate --parsed` 必须被明确映射为 `seeded`，不能因 `assistant=none` 就被误标为 `source_only`。

### 7.2 校验规则

解析 Golden 的阻断性校验至少包括：

- Envelope Schema 合法；
- `case_id` 在数据集内唯一；
- `task_type=parsing`；
- 原始文件存在且 hash 匹配；
- `doc_type`、profile 和 Reference 结构匹配；
- 评分所需字段完整；
- 标题层级、阅读顺序和页码引用合法；
- 表格、公式、图片和 Caption 引用不存在越界；
- `_todo`、uncertainty 和 unresolved issues 已处理；
- seeded Draft 已完成 source-based validation；
- freeze 前 reviewer 和 reviewed_at 完整。

### 7.3 冻结规则

- 只接受通过全部阻断性校验的 `reviewed` Golden；
- 对 canonical JSON 计算 `gold_hash`；
- 生成稳定 `freeze_id`；
- Frozen 文件默认禁止覆盖；
- 修改 Frozen Golden 必须创建新文件、新 freeze_id 和新 hash；
- 正式评分默认拒绝 Draft，`--allow-draft` 只能生成非正式 Run。

## 8. Prediction 标准化方案

首版先实现以下 Adapter：

1. `CanonicalBlocksAdapter`：读取现有 `*_parse.json`、`*_parsed.json` 及 Markdown/HTML 工件；
2. `MoiParsingAdapter`：兼容 GenAI 和 MOI Core 的原始解析响应；
3. `OmniDocBenchPredictionAdapter`：生成官方 Evaluator 所需格式。

MinerU 和 Paddle Adapter 在私有竞品同环境评测启动前补充，不阻塞第一个纵向切片。

Adapter 只允许：

- 字段映射；
- 类型和编码规范化；
- 明确、可配置的无损归一化；
- Artifact 引用整理；
- 产品错误映射为稳定 reason code。

Adapter 不允许补文本、修表格、重写标题层级或根据 Golden 修改输出。

## 9. Scorer 迁移方案

### 9.1 两类解析评分 Profile

| scorer_id | 用途 | 实现来源 |
|---|---|---|
| `parsing.internal.v1` | 私有 PDF/DOCX/PPTX/XLSX | 既有 `parsing_benchmark` Evaluator |
| `parsing.omnidocbench.v1` | 公开 OmniDocBench | 官方 Evaluator 与官方指标 |

两类分数分别报告，不生成跨 Profile 总分。

### 9.2 迁移步骤

1. 为既有 Evaluator 建立稳定的解析 Scorer Interface；
2. 输入只接受 Frozen Golden Reference 和 Canonical Prediction；
3. 输出统一 Score Envelope，不再让新调用方读取旧内部对象；
4. 为每个 Metric 固定 metric_id、聚合方式和适用性规则；
5. 保存 scorer 版本和配置 hash；
6. 用现有 fixtures 对新旧分数做逐 Case 回归；
7. 回归通过后，让旧 CLI 委托新 Scorer；
8. 删除只做对象搬运的临时 bridge 和重复聚合路径。

### 9.3 失败和 N/A

- Parser 超时、空结果和服务错误：Prediction 失败，适用指标按失败进入分母；
- 文档确实没有某类元素：对应指标 `applicable=false`，`value=null`；
- Golden 不合法：停止正式 Run；
- 单个 Scorer 异常：保留 Case，写入 `scorer_error`；
- 不允许用 `0` 同时表达失败和不适用；
- 不适用样本不参与聚合，但必须统计数量和 reason code。

## 10. Run 与报告

解析 Track 的默认输出根目录为 `document-parsing/runs/`，每次评分创建独立且默认不可覆盖的 Run：

```text
document-parsing/runs/<run_id>/
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
└── state.json
```

`manifest.json` 至少记录：

- run_id、task_type、dataset_id；
- source hashes、freeze_id、gold_hash；
- system_id、系统版本和配置；
- Schema 和 Scorer 版本；
- 代码 commit、依赖版本和配置 hash；
- 开始/结束时间；
- 成功、失败、重试和 N/A 数量。

报告必须包含逐 Case 评分、指标 numerator/denominator、失败分母、reason code 分布、典型失败 Case、时延、资源信息和限制说明。

## 11. Agent 辅助

统一 `GoldenAssistant` Interface 支持：

```text
none
codex
claude
```

同时支持三种 assistance mode：

| mode | 输入 | 约束 |
|---|---|---|
| `source_only` | 原始文档及显式 Schema | 默认正式 Golden 制作路径 |
| `seeded` | 原始文档加已有 Prediction | 只能生成候选，必须独立 source validation |
| `validate_only` | Draft Golden 加来源材料 | 只检查一致性，不静默重写全部 Golden |

解析领域的 Assistant 只负责：

- 检查 deterministic/seeded Draft；
- 提议标题层级和阅读顺序修订；
- 检查表格、公式、图片和 Caption 遗漏；
- 定位来源页码、span 或 bbox；
- 输出 uncertainty 和待人工确认清单。

每个 job 在独立目录运行，只读取显式输入，并保存 Assistant 类型、CLI/模型信息、Prompt hash、输入 hash、assistance mode、时间、Exit Code、脱敏日志、候选 hash 和 unresolved issues。Assistant 失败不得改变 Golden 状态，也不得写入 Frozen Golden。

## 12. CLI 与兼容策略

新命令：

```bash
moi-eval golden draft --task parsing ...
moi-eval golden validate --task parsing ...
moi-eval golden freeze --task parsing ...
moi-eval prediction normalize --task parsing ...
moi-eval score --task parsing ...
moi-eval report --run ...
```

兼容策略：

- `parsing-benchmark eval` 委托到 `normalize_predictions + evaluate + render_report`；
- `parsing-benchmark golden generate` 委托到 parsing Builder，并始终生成 Draft；
- `parsing-benchmark run` 暂保留在线能力，内部改为先保存 raw/Prediction 再评分；
- 旧 JSON Golden 通过显式 migration Adapter 读取，不在 loader 中静默猜测版本；
- 迁移期报告同时显示 legacy metric name 和新 metric_id，稳定后移除 legacy 字段；
- Schema 或评分语义变化必须提升版本，历史 Run 不做静默重算。

统一 Exit Code：

| Exit Code | 含义 |
|---:|---|
| 0 | 成功 |
| 2 | CLI 参数或配置错误 |
| 3 | 数据合同、Golden 或 Schema 校验失败 |
| 4 | Assistant 或外部 Target 不可用 |
| 5 | Scorer 或内部运行错误 |

配置合并顺序固定为：命令行显式参数、`--config` 文件、当前目录默认配置、工具内置默认值。合并后的完整配置必须写入 `config.snapshot.yaml`，敏感字段只能保存脱敏值或 secret reference。

## 13. 分阶段实施

### 阶段 A：基线冻结与公共骨架

交付：

- 固定既有实现 commit、Python 版本和依赖；
- 选择一组 doc 与 xlsx 回归 fixtures；
- 建立 `moi_eval` 包、CLI 和公共 Schema；
- 实现统一错误类型、配置优先级、hash 和 Run writer；
- 保留旧 CLI 可运行。

验收：全新虚拟环境可安装，三个 CLI 的 `--help` 可用；同一配置能够生成确定性的 config hash。

### 阶段 B：Golden 生命周期

交付：

- Parsing Golden Envelope；
- 历史 Golden migration；
- draft/validate/freeze；
- source/seed hash；
- Frozen 防覆盖；
- seeded 污染标记和 reviewer 记录。

验收：历史私有 Golden 可迁移、复核和冻结；未解决 `_todo`、来源 hash 错误或 Draft 状态均会阻断正式评分。

### 阶段 C：离线解析纵向切片

交付：

- CanonicalBlocksAdapter；
- Parsing Prediction Envelope；
- `parsing.internal.v1` Scorer；
- scores.jsonl、summary.json、report.md；
- N/A、Prediction 失败和 Scorer 失败语义。

验收：使用同一 Golden、Prediction、配置和 Scorer 版本重复运行，结果字节级或 canonical JSON 级一致；已有核心 fixtures 的新旧指标一致或存在经评审的版本化差异。

### 阶段 D：XLSX 与公开集

交付：

- XLSX Golden/Prediction 迁移；
- Markdown/HTML Artifact 引用；
- OmniDocBench Prediction Adapter；
- `parsing.omnidocbench.v1` 官方 Scorer；
- 公开和私有 Profile 分层报告。

验收：至少各有一个 doc、xlsx 和 OmniDocBench 示例完成端到端运行，且不生成跨 Profile 总分。

### 阶段 E：兼容与 Agent

交付：

- 旧 CLI 全量委托；
- Codex、Claude、Manual Adapter；
- Assistant job、Schema、日志脱敏和失败重试；
- 敏感数据提示。

验收：Assistant 失败不会生成 Frozen Golden；旧解析核心用例无回归；旧命令输出明确指向对应的新 Run。

### 阶段 F：清理与冻结 v1

交付：

- 删除重复模型、聚合逻辑和临时 bridge；
- 固定 parsing Schema、metric catalog 和 scorer version；
- 补充迁移指南和用户手册；
- 形成 v1 发布候选。

验收：新代码只通过统一 Interface 调用解析 Module；不再需要测试越过 Interface 读取旧内部状态。

## 14. 工作项拆分

| ID | 工作项 | 优先级 | 依赖 |
|---|---|---|---|
| PAR-UP-001 | 冻结旧实现基线和回归 fixtures | P0 | 无 |
| PAR-UP-002 | 创建 `moi_eval` 包、CLI 与配置系统 | P0 | 001 |
| PAR-UP-003 | 建立 Golden/Prediction/Score JSON Schema | P0 | 002 |
| PAR-UP-004 | 实现 hash、状态机和 Frozen 写保护 | P0 | 003 |
| PAR-UP-005 | 实现历史解析 Golden migration/validation | P0 | 004 |
| PAR-UP-006 | 实现 CanonicalBlocksAdapter | P0 | 003 |
| PAR-UP-007 | 将既有 Evaluator 收敛到 Parsing Scorer | P0 | 003、006 |
| PAR-UP-008 | 补齐 numerator/denominator 和 N/A 语义 | P0 | 007 |
| PAR-UP-009 | 实现 Run writer 与标准报告 | P0 | 003、007 |
| PAR-UP-010 | 完成 doc 离线端到端回归 | P0 | 005～009 |
| PAR-UP-011 | 迁移 XLSX profile | P0 | 010 |
| PAR-UP-012 | 接入 OmniDocBench 官方 Adapter/Scorer | P0 | 009 |
| PAR-UP-013 | 兼容旧 CLI 和旧 Golden 读取 | P0 | 010、011 |
| PAR-UP-014 | 实现 Codex/Claude/Manual Assistant | P0 | 005 |
| PAR-UP-015 | 在线 Target 改为先落盘再评分 | P1 | 009 |
| PAR-UP-016 | Golden Diff 和 Schema migration 工具 | P1 | 005 |
| PAR-UP-017 | 删除临时 bridge 和重复实现 | P0 | 011～014 |

## 15. 测试与验证

### 15.1 合同测试

- Golden、Prediction、Score 和 manifest 的 Schema 正反例；
- case_id 唯一性和 task_type/profile 一致性；
- canonical hash 稳定性；
- Frozen 防覆盖；
- 配置优先级和 Exit Code。

### 15.2 解析回归

- 每个既有 doc Evaluator 至少保留一个代表性 fixture；
- 每个 XLSX Evaluator 保留正常、N/A 和失败 fixture；
- 新旧路径逐 Case 对比，不只比较 suite 总分；
- 评分语义有意变化时必须提升 scorer version，并保存差异说明。

### 15.3 端到端测试

至少覆盖：

1. 历史 Golden -> migration -> freeze；
2. parsed JSON -> normalize -> score -> report；
3. Prediction 缺失、超时、空输出和坏 JSON；
4. Draft Golden 正式评分被拒绝；
5. `--allow-draft` 生成非正式隔离 Run；
6. doc、xlsx 和 OmniDocBench 三种 Profile；
7. 旧 CLI 委托新实现；
8. Assistant 输出无效和超时。

### 15.4 发布验证

- macOS、Linux 全新虚拟环境安装；
- `moi-eval --help`、`golden --help`、`score --help`、`report --help`；
- 日志和报告不包含 API Key、Cookie、Token 或数据库密码；
- 同输入离线重算可复现；
- 运行目录不覆盖历史产物。

## 16. 风险与控制

| 风险 | 控制措施 |
|---|---|
| 迁移时重写 Evaluator 导致分数漂移 | 先包装、后替换；逐 Case 双跑并版本化差异 |
| parsed output 生成 Golden 形成自证循环 | 强制标记 seeded、保存 seed hash、source-based validation、人工冻结 |
| 新旧模型长期并存形成双重语义 | 临时 bridge 设置删除条件，v1 前只保留一套实现 |
| numerator/denominator 无法从旧报告恢复 | 修改 Evaluator 原始返回合同，不从四舍五入结果反推 |
| 公开和私有口径被错误聚合 | 使用不同 scorer_id/profile，报告层禁止合成总分 |
| Frozen Golden 被旧 `--force` 覆盖 | 所有写入统一经过 lifecycle Module，旧 CLI 不直接写文件 |
| Agent 幻觉或敏感数据外发 | 结构化输出、证据校验、独立 job、显式确认、人工复核 |
| 一次迁移范围过大 | 先完成 doc 离线纵向切片，再扩展 XLSX、公开集、在线和 Agent |

## 17. 待确认决策

1. 是否确认 `agent-eval-tools` 继续作为 `moi-eval` 的实现仓库；本方案建议确认。
2. `moi-eval` 首版最低 Python 版本；应在阶段 A 冻结。
3. Frozen Golden 是单 reviewer 还是双人复核；当前需求允许单 reviewer，但尚未最终确认。
4. 现有私有 Golden 的迁移是否允许原 reviewer 补录，还是必须重新完整审核。
5. 首版是否按上位需求同时交付 Codex 和 Claude 两个 P0 Adapter；如要缩减为一个 Adapter，需要同步修订需求和验收标准。
6. OmniDocBench 官方 Scorer 是作为 parsing v1 必交，还是在私有解析纵向切片之后单独发布。
7. `parsing-benchmark` 旧命令的兼容周期和弃用时间。

## 18. 完成定义

文档解析升级完成需同时满足：

- `moi-eval` 可安装并提供稳定 CLI；
- 解析 Golden 可生成、校验、复核和冻结；
- seeded Golden 不会被误当作 source-only；
- Frozen Golden 默认不可覆盖，正式评分拒绝 Draft；
- 原始解析输出可标准化且不会被覆盖；
- Prediction 失败样本不会丢失；
- 解析 Scorer 通过统一 Interface 运行并保留 numerator/denominator；
- N/A、系统失败和 Scorer 失败语义分离；
- doc、xlsx、OmniDocBench 各至少一个端到端示例；
- 输出标准 Run 目录及四类基础报告；
- 报告可追溯到 source、Golden、Prediction、配置和 Scorer 版本；
- 旧解析核心用例无未说明回归；
- 不生成跨 Profile 或跨领域的默认总分。
