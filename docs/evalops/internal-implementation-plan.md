# MOI EvalOps 定义与实施计划

> **内部执行文档｜2026-08-20**
>
> **Goal：** 用现有 `moi-benchmark` 资产体系与 `agent-eval-tools` 执行引擎，在 30 天内形成可用的 EvalOps MVP，60 天形成可进入项目的产品化能力，90 天形成持续运营闭环。
>
> **Architecture：** 不合并两个仓库，不做大爆炸重构。`moi-benchmark` 继续作为方法论、数据、任务、运行和证据的 Source of Truth；`agent-eval-tools` 作为统一执行引擎；MOI/MatrixFlow 通过 API、Trace 和控制台逐步接入。先统一 Contract，再补 Gate、Bundle 和 AI Copilot，最后做服务化与 UI。
>
> **Tech Stack：** Python CLI/SDK、JSON Schema/YAML、现有 evaluator/adapter、Git/对象存储、CI/CD、MOI/MatrixFlow API、LLM/VLM Agent。

---

## 1. EvalOps 的内部定义

### 1.1 一句话定义

**EvalOps 是把企业 AI 的业务目标持续转化为可执行验收、可追溯证据和发布决策的一套质量工程体系。**

它不是单独的 Benchmark 工具，也不是一个排行榜。它包含：

1. 方法论与治理标准；
2. 可复用评测资产；
3. 自动化工具链与平台；
4. AI 辅助的资产生产和分析能力；
5. 持续运行的组织流程；
6. 可交付给客户的验收包与证据包；
7. 从客户和生产问题回流形成的数据飞轮。

### 1.2 EvalOps 的对象

EvalOps 评测的是完整 AI 系统，而不只是基础模型：

- 数据与文档处理；
- 业务语义层与上下文构造；
- 检索、重排和引用；
- Prompt、模型与生成策略；
- Agent 规划、工具调用与恢复；
- Memory 写入、检索、更新与治理；
- 最终业务结果、成本、性能、稳定性和安全边界。

### 1.3 不是什么

EvalOps 不是：

- 用一个 LLM Judge 给所有任务打总分；
- 只服务竞品横评的一次性脚本；
- 把不同 Track 的分数强行合并成“万能排行榜”；
- 只在交付前做一次的 QA；
- 只看准确率、不保留运行上下文和原始证据的报表工具。

---

## 2. EvalOps 的完整组成

### 2.1 方法论资产

| 资产 | 内容 | 作用 |
|---|---|---|
| 场景建模规范 | 业务目标、任务边界、成功条件、失败分类 | 决定测什么 |
| 指标设计规范 | 平台层/模型层拆分、硬指标/软指标、N/A 规则 | 决定怎么算 |
| Golden 规范 | 来源、标注、复核、歧义处理、版本与变更 | 决定什么算对 |
| 可比性规范 | Dataset、Task、Model、Config、Evaluator、Aggregation 冻结 | 防止伪对比 |
| 发布门禁规范 | Baseline、绝对阈值、最大退化量、失败率、性能与安全策略 | 决定能否上线 |
| 证据与审计规范 | Run、Artifact、Hash、失败保留、脱敏和保留期 | 决定结论能否复核 |
| LLM Judge 规范 | Rubric、校准集、方差、抽检和人工仲裁 | 控制主观评分风险 |

### 2.2 可沉淀的评测资产

EvalOps 的核心复利来自资产，而不是 UI。需要统一管理以下对象：

1. **Scenario Card**：客户业务目标、用户路径、关键风险和验收重点。
2. **Dataset Manifest**：数据来源、版本、License、访问级别、脱敏、切分、Hash。
3. **Task Set**：标准输入、预期行为、前置条件和任务类型。
4. **Golden Set**：参考答案、证据、标注人/审核角色、置信度、歧义说明。
5. **Metric Contract**：指标定义、方向、单位、计算方式、N/A 和聚合规则。
6. **Evaluator / Verifier**：可执行评分器及其契约测试。
7. **Target Adapter**：MOI、第三方系统、在线 API、离线结果的接入方式。
8. **System Card**：产品、模型、Provider、Prompt、配置、环境和能力边界。
9. **Baseline**：批准的质量基线及适用上下文。
10. **Gate Policy**：发布阈值、最大允许退化、豁免和审批规则。
11. **Run Record**：不可变运行记录、逐题结果、错误、性能、Token 与诊断。
12. **Evidence Bundle**：运行摘要、版本、工件索引、Hash、脱敏证据和报告。
13. **Badcase Library**：失败样本、根因、责任域、修复状态和回归覆盖。
14. **Industry Starter Kit**：按行业预置的场景、指标、数据模板和 POC 流程。
15. **Reference Run**：可复现的对外或内部标准运行。

### 2.3 工具能力

最低完整工具链应包含：

- `benchmark run`：统一执行；
- `benchmark compare`：同上下文版本对比；
- `benchmark gate`：按策略判定并返回标准退出码；
- `benchmark bundle`：生成 Evidence Bundle；
- `benchmark assets`：校验和索引 Dataset、Golden、Task、Evaluator；
- `benchmark doctor`：检查依赖、版本、数据与 Contract 健康度；
- `benchmark report`：生成管理、技术和客户三种报告；
- SDK/API：供 CI、MOI/MatrixFlow 和第三方系统调用；
- Scheduler：定时回归与生产影子评测；
- Dashboard：趋势、失败钻取、门禁和资产状态。

### 2.4 运营机制

必须建立一条常态化链路：

**需求进入** → 场景卡 → 数据与 Golden → Benchmark Contract → Baseline → 自动回归 → Gate → 失败归因 → 产品修复 → Badcase 回流 → 新 Baseline

建议固定四个 Human Gate：

1. **Contract Gate**：业务负责人批准“测什么、什么算对”。
2. **Golden Gate**：领域专家批准 Golden 和歧义规则。
3. **Release Gate**：产品/质量负责人批准阈值与豁免。
4. **Evidence Gate**：对外发布前批准数字、范围和脱敏内容。

其余数据处理、代码生成、运行、聚类和报告尽量由 AI 与自动化完成。

### 2.5 客户可交付物

EvalOps 最终应形成可销售、可复用的交付包：

| 交付包 | 内容 | 适用阶段 |
|---|---|---|
| EvalOps QuickStart | 场景工作坊、数据模板、首个 Baseline、问题清单 | 售前/POC |
| AI Capability Acceptance Pack | 数据集、Golden、指标、运行、逐题结果、验收报告 | 项目交付 |
| Continuous Regression Pack | Baseline、CI、定时任务、门禁、趋势和告警 | 生产运行 |
| Evidence Bundle | 版本、配置、原始工件、Hash、失败分类、脱敏报告 | 审计/争议复核 |
| Industry Benchmark Starter Kit | 行业场景、指标模板、样本分类和 POC Playbook | 行业复制 |
| Quarterly AI Quality Report | 质量趋势、Top badcase、优化收益与下季计划 | 运营汇报 |

### 2.6 当前能力边界

客户材料中的“当前”指现有评测资产与可按项目组织的交付能力，不等同于统一平台能力已经全部 GA。内部按以下边界管理：

| 能力 | 当前状态 | 证据/验收 | 目标窗口 |
|---|---|---|---|
| NL2SQL、文档解析、Memory 阶段性评测结果 | Validated | 已冻结报告、逐题结果与源工件 | 当前 |
| 五域方法、数据集、任务与报告资产 | Available | 仓库资产与项目级评测流程 | 当前 |
| 统一 Run / Compare / Gate / Bundle Contract | Preview | 两域共用契约、Schema、退出码与可复核 Bundle | 30 天 |
| 五 Track 统一接入 | Planned | 五域 smoke profile 与统一 Run Manifest | 60 天 |
| REST API、控制台、CI/定时回归 | Planned | 无代码发起 Run、权限隔离、CLI/控制台一致 | 60 天 |
| 客户 Pilot 与持续运营闭环 | Pilot | 真实升级经 Gate 放行/阻断，badcase 回流 | 90 天 |

任何外部主张必须映射到本表或 §10.2 Claim Matrix；状态不能仅凭页面文案升级。

---

## 3. 目标架构

```text
┌──────────────────────────────────────────────────────────────┐
│                        Experience Layer                       │
│ CLI / SDK / REST API / CI Plugin / Dashboard / Customer PDF │
├──────────────────────────────────────────────────────────────┤
│                         Control Plane                         │
│ Asset Catalog │ Run Orchestrator │ Compare │ Gate │ Bundle   │
├──────────────────────────────────────────────────────────────┤
│                      Intelligence Plane                       │
│ Data Profiler │ Golden Copilot │ Test Generator │ RCA Agent   │
│ Judge Calibrator │ Report Agent │ Privacy & Claim Reviewer    │
├──────────────────────────────────────────────────────────────┤
│                       Evaluation Plane                        │
│ Document │ RAG │ NL2SQL │ Memory │ Agent │ Custom Plugin     │
│ Targets / Adapters / Evaluators / Verifiers / Aggregators     │
├──────────────────────────────────────────────────────────────┤
│                         Asset Plane                           │
│ Scenario │ Dataset │ Golden │ Task │ Metric │ Baseline        │
│ System │ Policy │ Run │ Artifact │ Badcase │ Evidence Bundle │
├──────────────────────────────────────────────────────────────┤
│                       Governance Plane                        │
│ Version │ Hash │ RBAC │ Privacy │ Retention │ Approval │ ADR │
└──────────────────────────────────────────────────────────────┘
```

### 3.1 仓库职责

#### `moi-benchmark`：资产与治理 Source of Truth

继续保存：

- 五个 Track 的方案、数据 manifest、任务、系统卡和运行记录；
- 跨 Track 方法论、Schema、发布和隐私规则；
- Reference Run、报告和 Evidence 索引；
- 行业 Starter Kit。

建议新增：

```text
docs/evalops/
├── methodology/
│   ├── scenario-design.md
│   ├── metric-design.md
│   ├── golden-standard.md
│   ├── llm-judge-governance.md
│   └── release-gate.md
├── contracts/
│   ├── track-manifest.schema.json
│   ├── dataset-manifest.schema.json
│   ├── run-manifest.schema.json
│   ├── gate-policy.schema.json
│   └── evidence-bundle.schema.json
├── templates/
│   ├── scenario-card.yaml
│   ├── system-card.yaml
│   ├── gate-policy.yaml
│   └── customer-acceptance-report.md
└── starter-kits/
```

每个 Track 新增标准入口：

```text
<track>/track.yaml
<track>/benchmark/contracts/
<track>/datasets/*/manifest.yaml
<track>/systems/*/system-card.yaml
<track>/runs/<run_id>/run.yaml
```

#### `agent-eval-tools`：统一执行引擎

短期保留现有包名和兼容路径，避免先花时间重命名。基于现有：

- `src/parsing_benchmark/benchmark_cli.py`
- `src/parsing_benchmark/evalcore/`
- `src/parsing_benchmark/registry.py`
- `src/parsing_benchmark/aggregation.py`
- `src/parsing_benchmark/domains/nl2sql/`

建议新增：

```text
src/parsing_benchmark/evalops/
├── manifests.py
├── assets.py
├── baselines.py
├── gates.py
├── bundles.py
├── comparable.py
├── storage.py
└── doctor.py

src/parsing_benchmark/domains/
├── parsing/
├── nl2sql/
├── rag/
├── memory/
└── agent/
```

先增加功能和兼容层，产品稳定后再把 Python 包正式迁移为 `moi_evalops`。

#### MOI / MatrixFlow：被测系统与产品入口

通过稳定 Contract 接入，而不是把 evaluator 复制进产品仓库：

- 暴露标准推理与任务执行 API；
- 提供 Trace、检索证据、工具调用、Token、阶段耗时和错误；
- 在控制台提供 Run、趋势、失败钻取和 Gate 状态；
- 将项目和环境权限映射到 Dataset、Run 与 Evidence Bundle。

---

## 4. AI 大模型如何把人力降到最低

### 4.1 AI 的正确定位

大模型主要承担四类工作：

1. **生成**：草拟 Golden、测试样本、Rubric、Evaluator、报告；
2. **理解**：数据画像、场景提取、Schema/文档结构理解；
3. **分析**：错误聚类、根因假设、版本差异和优化建议；
4. **审查**：敏感信息、证据引用、指标口径和报告一致性检查。

大模型不应无约束地承担最终发布裁决。最终 Gate 优先使用：

- 可执行结果；
- 确定性规则；
- 任务原生 Verifier；
- 经校准的 LLM Judge；
- 对歧义样本的人类仲裁。

### 4.2 六个 AI Agent

| Agent | 输入 | 输出 | 人工只做什么 |
|---|---|---|---|
| Data Profiler | 原始数据/生产样本 | 类型、分布、风险、代表性抽样 | 确认访问和脱敏策略 |
| Golden Copilot | 样本、业务资料、历史答案 | Golden 草案、证据、置信度、歧义点 | 审批低置信和争议样本 |
| Test Designer | 场景卡、失败历史 | 正常、边界、对抗和变形测试 | 批准覆盖范围 |
| Evaluator Builder | Metric Contract、样例 | Scorer/Verifier 代码与契约测试 | Code review 与批准合入 |
| RCA Agent | 逐题结果、Trace、Diff | 错误聚类、根因假设、责任域和修复建议 | 确认高影响问题 |
| Report & Claim Agent | Evidence Bundle、模板 | 管理/技术/客户报告及主张证据矩阵 | 对外发布批准 |

### 4.3 AI 自动化流水线

```text
原始数据
  → Data Profiler 自动分类和抽样
  → Golden Copilot 生成草案、证据和置信度
  → Test Designer 扩展边界/对抗/变形样本
  → Evaluator Builder 生成评分器与测试
  → Runner 执行并产出统一 Run
  → RCA Agent 聚类错误、关联 Trace、提出根因
  → Report Agent 生成带证据引用的报告
  → 人工只审批 Contract / 争议 Golden / Gate / 对外主张
```

### 4.4 控制 AI 风险

- Golden 必须带来源、证据和置信度；低置信样本进入人工队列。
- AI 生成的 Evaluator 必须通过正例、反例、边界和对抗契约测试。
- LLM Judge 必须使用固定 Rubric、校准集和版本记录。
- AI 根因分析标记为 hypothesis，必须关联可观察证据。
- 对外数字由 Evidence Bundle 自动回填，禁止手工复制。
- 敏感信息检测和客户主张检查作为 Bundle 生成的固定步骤。

---

## 5. 最快实施路径

### 5.1 排期口径、缓冲与关键依赖

本排期按**合理预期**而非最佳情况估算。每个两周阶段仅安排 8–9 个实现日，预留 1–2 个工作日用于 Gate、返工和依赖波动，整体缓冲为 10–15%。30/60/90 天为对外里程碑窗口，4/8/12 周为内部目标完成点，剩余日历时间只用于治理和不可预见问题。

**内部计划前提：**

- 两个仓库的代码、测试工件和 CI 权限在 Phase 0 就绪；
- MOI/MatrixFlow 在第 6 周前提供稳定 API/Trace，或可导出等价离线 Artifact；
- Pilot 数据与访问审批在启动后 5 个工作日内完成；
- 每个 Pilot 每周可获得至少 2 小时领域专家评审时间；
- 基础模型、Provider 和关键依赖版本可冻结，不在同一 Reference Run 中途切换。

| 关键依赖 | 是否阻塞关键路径 | Contingency path |
|---|---|---|
| MOI/MatrixFlow API 或 Trace 延迟 | 不阻塞 CLI/Gate/Bundle；阻塞在线集成 | 先接离线 Artifact Adapter，API 到位后只替换 Target Adapter |
| 客户数据/审批/专家延迟 | 不阻塞产品 MVP；阻塞客户 Pilot | 使用冻结 Reference Dataset 完成验收，Pilot 计时从数据就绪开始 |
| 外部模型或 Provider 波动 | 阻塞可比 Run | 使用冻结模型与缓存；切换 Provider 时创建新 System Card/Baseline，不做伪对比 |
| Phase 0 基线清理超过 3 天 | 阻塞后续 Contract | 优先削减 Dashboard、Copilot 次要功能，不削减 Gate、Bundle 与证据完整性 |

### Phase 0｜第 0 周：清理基线与冻结定义

**目标：** 先保证现有工具可稳定运行，再开始扩展。

任务：

1. 固化本文定义和五层架构；
2. 记录现有 parsing / NL2SQL CLI、数据、报告与测试基线；
3. 修复当前测试中的缺失 fixture、跨仓工件依赖和配置语义不一致；
4. 统一 Python 版本说明与安装命令；
5. 建立“外部主张—证据—适用范围—兑现版本”矩阵；
6. 将客户 Deck 中归为“当前能力”的近期项列入 30 天 P0。

**验收：**

- 标准环境一条命令安装；
- 核心测试全绿；无法本地运行的外部集成测试明确隔离并有原因；
- parsing 和 NL2SQL 两个 smoke run 可重复；
- 外部每个能力主张都有 Owner 和兑现日期。

### Phase 1｜第 1–2 周：EvalOps CLI MVP

**目标：** 在已有两域上打通 Run → Compare → Gate → Bundle。

实现：

1. 定义并实现五个 JSON Schema：Track、Dataset、Run、Gate、Bundle；
2. 在 `benchmark_cli.py` 增加全局命令：
   - `benchmark run`
   - `benchmark compare`
   - `benchmark gate`
   - `benchmark bundle`
   - `benchmark doctor`
3. 复用 `UniversalReport`、AggregationContext、Target/Scorer Registry；
4. 支持绝对阈值、相对 Baseline 最大退化量、失败率和 P95 时延门禁；
5. Gate 通过返回 0，不通过返回 1，配置/运行错误返回 2；
6. 生成最小 Evidence Bundle：manifest、summary、per-sample、artifact index、hash。

**验收：**

- parsing 和 NL2SQL 共用同一 Run/Gate/Bundle 契约；
- 上下文不可比时 Compare/Gate 默认拒绝；
- 一个示例 CI 能因质量退化被阻断；
- 所有 Bundle 可通过 Schema 校验和 Hash 复核。

### Phase 2｜第 3–4 周：AssetOps + AI Copilot MVP

**目标：** 把最耗人力的数据、Golden、测试和报告制作半自动化。

实现：

1. `benchmark assets validate/index`；
2. Dataset Profiler：格式识别、统计、分层抽样、PII 提示；
3. Golden Copilot：草案、证据、置信度和人工审核队列；
4. Badcase Cluster：按症状、阶段、根因和责任域聚类；
5. Test Designer：根据历史错误生成边界和变形用例；
6. Report Agent：从 Bundle 生成管理、技术、客户三类报告；
7. 建立 Claim Matrix：报告中的每个数字链接到 Run 与 Metric。

**验收：**

- 对支持域，在数据、权限和环境就绪后，从导入到首个 Benchmark 草案不超过 1 个工作日；
- AI 生成 Golden 100% 带证据和置信度；
- 低置信度和冲突样本自动进入人工队列；
- 客户报告中的数字可从 Bundle 自动重建。

### Phase 3｜第 5–6 周：五 Track 统一接入

**目标：** 统一“运行和证据”，不强行统一各领域的评分逻辑。

实现顺序：

1. RAG：优先接入离线 retrieval/generation artifacts 与现有 Evidence Chain；
2. Memory：接入状态断言、检索结果、版本关系和治理测试；
3. Agent：接入 task、trajectory、budget、timeout 和原生 verifier；
4. 为五域建立统一 Track Plugin Contract；
5. 每域至少一个 Reference Dataset、一个 Reference Run、一个 Gate Policy；
6. 建立跨 Track 综合索引，但不计算跨域单一总分。

**验收：**

- `benchmark run <track>` 可运行五个 Track 的 smoke profile；
- 五域均产出统一 Run Manifest 和 Evidence Bundle；
- 每域保留自己的 Metric Contract 与 Verifier；
- 任一失败可定位到 sample、stage、artifact 和 error taxonomy。

### Phase 4｜第 7–8 周：服务化与 MOI 集成

**目标：** 从工程 CLI 变成项目团队可用的服务。

实现：

1. EvalOps REST API：Asset、Run、Compare、Gate、Bundle；
2. Worker 队列运行长任务并持久化状态；
3. 对接 MOI/MatrixFlow Trace 与项目权限；
4. 最小控制台：项目、资产、运行、趋势、失败钻取、Gate；
5. GitHub/GitLab CI 模板与定时回归；
6. Bundle 脱敏导出和访问控制。

**验收：**

- 产品、交付和算法人员无需写代码即可发起标准 Run；
- CI 和定时任务均可调用同一 API；
- 控制台看到的数据与 CLI Bundle 一致；
- 私有 Dataset 与 Evidence 按项目隔离。

### Phase 5｜第 9–12 周：客户 Pilot 与运营闭环

**目标：** 形成可复制交付能力，而不是只完成平台功能。

实现：

1. 选择 1 个主客户 Pilot，并用文档/RAG、NL2SQL、Agent 中另外 2 域完成内部 Reference Pilot；
2. 每个 Pilot 建立 Scenario、Golden、Baseline、Gate 与周期报告；
3. 建立生产 badcase 回流 API 与审核队列；
4. 发布第一个 Industry Starter Kit；
5. 完成客户见证运行流程；
6. 建立月度质量评审和季度 Benchmark 报告机制。

**验收：**

- 三个 Pilot 均能从客户问题追溯到回归资产；
- 至少一次真实版本升级被 Gate 正确放行或阻断；
- 相同 Starter Kit 可在第二个项目复用；
- 对外报告可由独立人员按 Bundle 复核。

---

## 6. P0 产品 Backlog

| ID | 能力 | 主要文件 | Done 标准 |
|---|---|---|---|
| EVAL-001 | Manifest Schema | `moi-benchmark/docs/evalops/contracts/` | 五类 Schema + 正反例 + CI 校验 |
| EVAL-002 | 统一 Run Loader | `agent-eval-tools/src/parsing_benchmark/evalops/manifests.py` | parsing/NL2SQL 都能加载与验证 |
| EVAL-003 | Baseline/Compare | `.../evalops/baselines.py`、`comparable.py` | 不可比上下文默认拒绝 |
| EVAL-004 | Gate Engine | `.../evalops/gates.py` | 阈值/退化/失败率/P95 + 退出码 |
| EVAL-005 | Evidence Bundle | `.../evalops/bundles.py` | Manifest、逐题、Artifacts、Hash 完整 |
| EVAL-006 | CLI | `.../benchmark_cli.py` | run/compare/gate/bundle/doctor 可用 |
| EVAL-007 | Contract Tests | `agent-eval-tools/tests/evalops/` | 正例、反例、边界、迁移测试全绿 |
| EVAL-008 | CI Template | `.github/workflows/evalops-gate.yml` | 人为注入退化能阻断 PR |
| EVAL-009 | AI Golden Draft | `.../evalops/copilot/golden.py` | 输出证据、置信度和 review queue |
| EVAL-010 | RCA/Report Agent | `.../evalops/copilot/analysis.py` | 报告数字全部引用 Bundle |

建议每个 ID 拆成 0.5–2 日、可独立验收的任务；任务内部再用 5–15 分钟的 TDD 步骤推进，并在每个功能完成后独立提交。

---

## 7. 最小团队与工作方式

### 7.1 最小配置

| 角色 | 投入 | 主要责任 |
|---|---:|---|
| EvalOps Owner / 架构与口径 | 0.5 FTE | Contract、优先级、Gate 与最终验收 |
| 平台工程 | 1 FTE | CLI、Gate、Bundle、API 与 CI |
| 领域评测 | 1 FTE | 五域 Adapter、Evaluator、Golden 与 Reference Run |
| 产品/设计 | 0.25–0.5 FTE（Phase 4 起） | 最小控制台与客户交付模板 |
| 领域专家 | 每 Pilot 每周 ≥2 小时 | Golden、歧义和业务验收，不进入日常编码 |
| AI Coding Agents | 按独立任务持续并行 | 代码草拟、测试、文档、迁移和 review |

核心执行基线为 2 名全职工程人员 + 0.5 FTE Owner；产品/设计在服务化阶段介入。若 Owner 同时承担领域评测，则必须减少同期 Pilot 数量，不允许同一人同时卡住 Contract Gate 和 Golden Gate。

### 7.2 AI 原生研发方式

每个 Epic 采用：

1. 人定义 Contract、失败示例和 Done 标准；
2. AI Agent 生成测试和最小实现；
3. 独立 AI Agent 做规范符合性 review；
4. 独立 AI Agent 做代码质量、安全和边界 review；
5. 人只处理设计冲突和最终合入；
6. EvalOps 自己评测自己的报告、Agent 和版本，形成 dogfood。

### 7.3 避免的人力浪费

- 不先做大而全 Dashboard，先让 CLI/Gate/Bundle 工作；
- 不先重命名整个 Python 包，先加兼容层；
- 不为五域重写一套统一评分器，只统一 Contract 和运行证据；
- 不手工制作报告，使用 Bundle 模板自动生成；
- 不要求专家从零标注全部 Golden，让 AI 草拟、专家只审低置信样本；
- 不追求一次覆盖所有指标，先覆盖能影响发布决策的核心指标。

---

## 8. 内部目标指标

以下为建议内部目标，需在首个 Pilot 后校准，不直接写入客户承诺：

| 指标 | 30 天目标 | 90 天目标 |
|---|---:|---:|
| 已统一接入 Track | 2 | 5 |
| 支持域首个 Baseline 周期 | ≤1 个工作日 | ≤4 小时（数据就绪后） |
| Run 证据完整率 | 100% | 100% |
| 报告数字 Bundle 可追溯率 | 100% | 100% |
| 历史 badcase 回归覆盖率 | 建立统计 | ≥90% 高优先级样本 |
| 失败自动聚类覆盖率 | ≥70% | ≥90% |
| Gate 判定人工介入 | 核心歧义项 | 仅豁免/争议项 |
| 客户报告生成时间 | ≤2 小时 | ≤30 分钟 |

North Star：**从“发现客户问题”到“形成可自动回归的质量资产”的时间。**

---

## 9. 主要风险与应对

| 风险 | 表现 | 应对 |
|---|---|---|
| 把平台层和模型层混为一谈 | 分数变化无法归因 | Metric Contract 强制标记控制变量和能力层 |
| LLM Judge 取代真实验证 | 分数不稳定、难解释 | 优先可执行 Verifier；Judge 需校准和抽检 |
| Golden 本身错误 | 高分但业务错误 | 来源、双审、置信度、争议队列和版本治理 |
| 五域过度统一 | 失去领域指标语义 | 统一 Contract，不统一具体评分逻辑 |
| 客户数据泄露 | 私有样本进入报告/模型 | 本地运行、权限、脱敏、保留期和禁止训练回流 |
| 外部宣讲超前于实现 | 客户现场无法演示 | Claim Matrix + 30 天兑现清单 + 演示前 Gate |
| 先做 UI 导致延期 | 看起来完整但不能阻断发布 | CLI/Gate/Bundle 先行，UI 后置 |
| AI 生成错误代码/指标 | Silent failure | Contract tests、对抗样本和独立 review Agent |
| 历史测试与依赖不稳定 | MVP 基线不可复现 | Phase 0 清理环境、fixture 和隔离策略 |
| MOI/MatrixFlow API 或 Trace 延迟 | 在线集成阻塞 | 离线 Artifact Adapter 先行，接口就绪后替换 Target Adapter |
| 客户数据、审批或领域专家延迟 | Pilot 无法按日历启动 | 用 Reference Dataset 保持产品主线；Pilot 从数据就绪日单独计时 |
| 关键人员单点 | Gate、领域口径或合入停滞 | ADR、Contract、双人 review 与代理 Owner；关键决策不只留在口头 |
| 模型/Provider/GPU 波动 | 结果不可比或任务中断 | 冻结 System Card、缓存工件、限次重试；变更环境后建立新 Baseline |
| 90 天范围超载 | 质量、进度和功能三者同时受损 | 优先保 Gate/Bundle/证据链，依次后移 Dashboard 美化、次要 Copilot 和非主 Pilot |

---

## 10. 决策与治理

### 10.1 核心决策

1. 对外品牌使用 **MOI EvalOps**；AutoBench 可作为内部 Runner/历史名称逐步迁移。
2. 不合并两个仓库；通过 Schema 和引用关系连接。
3. `moi-benchmark` 是资产与治理真相源，`agent-eval-tools` 是执行真相源。
4. 五域统一 Run/Gate/Evidence，不强制统一指标和总分。
5. AI 用于生成与分析，发布门禁由可验证证据和批准策略决定。
6. 客户材料可以将 30 天内可兑现能力纳入当前项目方案，但必须在 Claim Matrix 标记为 Preview、限定适用场景，并在演示前完成验证。
7. 对外术语统一使用 **NL2SQL**；仓库历史目录 `nlp2sql/` 与既有标题 `NLP2SQL` 仅作为兼容名称保留，新文档不再扩散该写法。

### 10.2 Claim Matrix

内部必须维护：

| 外部主张 | 当前状态 | 证据 | 兑现版本 | Owner | 演示检查 |
|---|---|---|---|---|---|
| 已验证 | GA | Run/Report/测试 | 当前 | 明确 | 必须通过 |
| 近期可兑现 | Preview | 原型/计划/验收标准 | 30 天内 | 明确 | 场景受控 |
| 规划能力 | Planned | Roadmap | 后续 | 明确 | 不做现场承诺 |

外部 Deck 不展示此状态表，但销售、售前和演示人员必须使用同一 Claim Matrix。

---

## 11. 立即开始的 10 个动作

1. 批准本文对 EvalOps 的定义和仓库职责；
2. 建立 `docs/evalops/contracts/` 与五个核心 Schema；
3. 清理现有测试基线和安装文档；
4. 为 parsing/NL2SQL 各冻结一个 Reference Run；
5. 实现 `benchmark gate` 和标准退出码；
6. 实现最小 Evidence Bundle；
7. 建立客户 Deck 的 Claim Matrix；
8. 用 AI 完成第一个 Golden Copilot 和 RCA Agent 原型；
9. 打通一个 CI 质量门禁；
10. 选择一个真实项目做 30 天 Pilot。

完成前六项后，EvalOps 就不再只是概念，而是具备最小闭环：

**有资产 → 能运行 → 能比较 → 能门禁 → 有证据。**
