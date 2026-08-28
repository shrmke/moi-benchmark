# MOI RAG 四平台五数据集实验复现报告

> Date: 2026-08-14
> TLDR: 本文档为复现MOI RAG Benchmark指引，指标简介。


## 1. 复现条件

1. 使用相同 dataset package、split 和四类 SHA-256：manifest、documents、questions、Gold；
2. 使用相同平台版本、部署方式、parser、ingest representation、chunk、Embedding 及维度；
3. 使用相同检索范围、Top-K、reranker、generator、prompt、judge 和评分脚本；


## 2. 四个平台的固定实现

### 2.1 版本与 API 合同

| 系统 | 固定版本/镜像 | 本地合同 | 关键限制 |
|---|---|---|---|
| MOI | MatrixOne image `matrixorigin/matrixone:4.1.4`；runtime `8.0.30-MatrixOne-v4.1.4`；image ID `sha256:16ef37311b43c6882ed8242bf38b1e281d853d4f4f6163830fe10773c9fe011d` | MatrixFlow parser/index/retrieval；MatrixOne 全文、向量、标量过滤 | 历史 build dirty；部分轨使用 current corpus / LIKE full-text fallback；不同 embedding 空间必须分表 |
| Dify | `1.16.1`，source commit `6f8ed69ee15f9a2e7189ca066275e973d091d1e9` | Dataset API、`/datasets/{id}/retrieve`、App `/chat-messages` | dataset key 与 app key 分离；Weaviate/worker readiness 必须探针验证 |
| FastGPT | source `v4.15.6` / commit `3db33e93b78e75b37c93f7a6e3d0fafeafbfd256`；checked compose 的 deployment images 为 `v4.15.4` | dataset API、`searchTest`、`/api/v1/chat/completions`，`detail=true` | `searchTest.limit` 是 token budget，不是命中数；Top-10 回放使用 `limit=20000` 后取唯一 source 前10 |
| MaxKB | `v2.10.4-lts` / commit `fd6141e662582e88a41edbb7f6f89f4539e3e5dd` | admin ingest/diagnostic hit-test、published OpenAI-compatible Chat | 没有已验证的稳定 public Direct Retrieval API；主检索指标必须 `N/A: UNSUPPORTED_API`，不能用 admin hit-test 冒充 |

### 2.2 MOI 数据链路

产品兼容路径：

```text
raw document
→ moi:parser.convert.document.rich
→ moi:parser.split.documents.length
→ MultiLevelIndex（doc / section / chunk）
→ moi:embedding.generate
→ moi:data.retrieval.vector.write
→ MatrixOne table + lineage
→ full-text route + dense-vector route
→ deterministic merge / literal priority / evidence expansion
→ generator
```

历史 WikiEval 默认：chunk size 512 字符、overlap 50（生产默认 64）、每 section 5 chunks；Embedding `bge-m3` 1024d；MatrixOne 列 `VECF64(1024)`；最大 embedding 输入 8192 UTF-8 bytes；写入 float32→float64；IVFFLAT `lists=256`。检索过滤 `disabled=0`、`level=chunk`、file/version/volume scope；全文使用 `MATCH ... AGAINST`，向量使用 `l2_distance`，然后稳定去重与 evidence expansion。现有主结果没有 cross-encoder reranker。

历史索引曾用 `vector_cosine_ops` 建索引却用 `l2_distance` 查询；当前 workitems 建表默认已冻结为 `vector_l2_ops`，检索实现也会根据现有 IVFFLAT operator 选择匹配的 `l2_distance`/`cosine_distance`。本 benchmark 的正式配置使用每 run 唯一 database/table，readiness smoke 已实际验证 L2 index；历史 cosine 资源不纳入本轮 formal package，修复后的运行使用新 run ID，不覆盖历史延迟。

### 2.3 Dify 数据链路

Community Edition Compose → 本地 admin/workspace → 独立 Dataset → 上传 source document → platform-native parse/chunk/index → searchable-ready probe → public Direct Retrieval → isolated Chat App Native QA。Lenovo formal 明确使用 `dify_custom_separator_newline_max_tokens_512_overlap_64`；其他 start-record 标记 `platform_native`，不能事后臆测精确 separator/chunk 参数。

### 2.4 FastGPT 数据链路

MongoDB + PostgreSQL/PgVector + AIProxy + FastGPT → dataset/collection → source 或 prepared text push → collection training terminal → `searchTest` → isolated app Chat。WikiEval 旧 runner 把每个 Markdown 作为一个 `q` chunk 推入；后续统一 runner 多数使用 platform-native chunking。Native prompt 的共同意图是“只依据知识回答、与问题同语言、证据不足则拒答”，但多个历史 start-record 的 `prompt_hash=UNKNOWN`，正式复现必须导出 app/workflow JSON 并计算 hash。

### 2.5 MaxKB 数据链路

官方 arm64 image → admin 初始化 → knowledge/application → document/split batch → embedding/index terminal → SIMPLE application Chat。自定义 prompt 必须包含 `{data}` 与 `{question}`，否则检索命中不会注入 LLM。Direct Retrieval 只记录 `UNSUPPORTED_API`；admin diagnostic latency/hits 可以用于排障，不进入 public retrieval 排名。

### 2.6 模型、检索与 rerank 配置矩阵

开始复现前，需要准备的模型如下：

- **Model（Generator / Judge）**：文本生成与主要 Judge 使用  `deepseek-v4-flash`；涉及视觉输入时使用 Qianfan `qwen3.5-35b-a3b` （对MMDocIR, DocBench, Lenovo-bench调用视觉模型）。
- **Embedding**： 纯文本任务使用  `bge-m3`(1024d)；涉及视觉输入时使用 `Qwen3-VL-Embedding` (1024d)。
- **Reranker**：均未启用（Dify `reranking_enable=false`、FastGPT `usingReRank=false`，其余系统记为 `disabled` 或“无 cross-encoder”）。

数据集、系统配置矩阵：

<table>
  <thead>
    <tr>
      <th>数据集</th>
      <th>系统</th>
      <th>输入与切分</th>
      <th>检索与 Top-K</th>
      <th>Judge</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td rowspan="4"><strong>WikiEval</strong></td>
      <td>MOI</td>
      <td>50 Markdown；512 chars、overlap 50、section 5、多级索引</td>
      <td>MatrixOne full-text + L2 dense；每 route <code>max_hits=10</code>，再确定性融合/证据展开</td>
      <td rowspan="4">RAGAS v0.2.15 Embedding：<code>bge-m3</code><br>Judge：<code>deepseek-v4-flash</code></td>
    </tr>
    <tr>
      <td>Dify</td>
      <td>source document；platform native</td>
      <td>semantic search；Top-K 1/3/5/10；query 截 250 chars</td>
    </tr>
    <tr>
      <td>FastGPT</td>
      <td>旧专用 runner 每文一整个 <code>q</code> chunk；current runner 为 native</td>
      <td>embedding search；<code>searchTest limit=20000</code> 后取 Top-K；app <code>maxContext=6</code></td>
    </tr>
    <tr>
      <td>MaxKB</td>
      <td>source document；platform native</td>
      <td>application native；public Direct Retrieval unsupported；<code>top_n=Top-K</code></td>
    </tr>
    <tr>
      <td rowspan="4"><strong>MMDocIR</strong></td>
      <td>MOI</td>
      <td>官方预提取 page/layout candidate</td>
      <td>每题所属文档内 L2 Top-K 1/3/5/10</td>
      <td rowspan="4">Embedding：<code>Qwen3-VL-Embedding</code><br>Judge：<code>qwen3.5-35b-a3b</code></td>
    </tr>
    <tr>
      <td>Dify</td>
      <td>exact-input page text；20 docs/681 pages/103 QA</td>
      <td>document-local</td>
    </tr>
    <tr>
      <td>FastGPT</td>
      <td>exact-input page text；20 docs/681 pages/103 QA</td>
      <td>document-local；<code>limit=20000</code></td>
    </tr>
    <tr>
      <td>MaxKB</td>
      <td>exact-input page text；20 docs/681 pages/103 QA</td>
      <td>document-local；admin hit-test 仅诊断</td>
    </tr>
    <tr>
      <td rowspan="4"><strong>DocBench</strong></td>
      <td>MOI</td>
      <td>MatrixOne 当前语料；LIKE full-text fallback + vector exact scan</td>
      <td>source/document-local adapted；Top-K 1/3/5/10</td>
      <td rowspan="4">Embedding：<code>Qwen3-VL-Embedding</code><br>Judge：<code>qwen3.5-35b-a3b</code></td>
    </tr>
    <tr>
      <td>Dify</td>
      <td>229 raw PDF；native parser/chunk</td>
      <td>native semantic；Top-K 1/3/5/10</td>
    </tr>
    <tr>
      <td>FastGPT</td>
      <td>candidate Markdown；platform native；runner 候选分片默认上限约 24,000 chars、批量 ≤200</td>
      <td>native embedding；Top-K 1/3/5/10</td>
    </tr>
    <tr>
      <td>MaxKB</td>
      <td>candidate Markdown；段落上限 90,000 chars</td>
      <td>native application；public retrieval unsupported</td>
    </tr>
    <tr>
      <td rowspan="4"><strong>Enterprise</strong></td>
      <td>MOI</td>
      <td>同一 722-doc adapted slice；MatrixOne 当前语料</td>
      <td>current-corpus adapted；Top-K 10</td>
      <td rowspan="4">Embedding：<code>bge-m3</code><br>Judge：<code>deepseek-v4-flash</code></td>
    </tr>
    <tr>
      <td>Dify</td>
      <td>722 Markdown wrappers；platform native</td>
      <td>native semantic；Top-K 10</td>
    </tr>
    <tr>
      <td>FastGPT</td>
      <td>同一 722-doc slice</td>
      <td><code>limit=20000</code>，去重 source 后 Top-10 replay</td>
    </tr>
    <tr>
      <td>MaxKB</td>
      <td>同一 722-doc slice；段落上限 90,000</td>
      <td>native QA；public retrieval unsupported</td>
    </tr>
    <tr>
      <td rowspan="4"><strong>Lenovo</strong></td>
      <td>MOI</td>
      <td>native/parsed product chain；以 run artifacts 为准</td>
      <td>evidence-chain retrieval，Top-K 10</td>
      <td rowspan="4">Embedding：<code>Qwen3-VL-Embedding</code><br>Judge：<code>qwen3.5-35b-a3b</code></td>
    </tr>
    <tr>
      <td>Dify</td>
      <td>raw PDF；newline separator，512 tokens，overlap 64</td>
      <td>native semantic；Top-K 10</td>
    </tr>
    <tr>
      <td>FastGPT</td>
      <td>pypdf；扫描件 MinerU；page-aware 6,000 chars、overlap 300</td>
      <td>Native Top-K 10</td>
    </tr>
    <tr>
      <td>MaxKB</td>
      <td>native/MinerU plain text；90,000 chars，newline/space boundary</td>
      <td>native QA；public retrieval unsupported</td>
    </tr>
  </tbody>
</table>



## 3. 五个数据集的冻结与处理方式

### 3.1 总览

| 数据集 | 评测范围 | 输入 | 核心任务 | 主要协议标签 |
|---|---:|---|---|---|
| WikiEval | 50 sources / 50 QA | Markdown | 文本 RAG 回归、RAGAS | `WIKIEVAL_FROZEN_50_50_V1` |
| MMDocIR | 313 docs / 20,395 pages / 170,338 layouts / 1,658 QA | 官方预提取 page/layout + bbox；QA 扩展另用 Gold | document-local Page/Layout Retrieval；adapted QA | official task scope + `ADAPTED_PROTOCOL` |
| DocBench | 229 PDF / 1,102 QA | raw PDF 或 candidate Markdown | 长 PDF、表格/图片、metadata、拒答 | `NATIVE_PDF` / `CONTROLLED_PARSED_TEXT` |
| EnterpriseRAG-Bench | 本项目适配集 722 docs / 500 QA；公开全库 511,962 docs | 多来源合成企业文档 | 企业检索、冲突、完整性、info-not-found | `QUESTION_LINKED_FULL_500_ADAPTED_SLICE_V1` |
| Lenovo-bench | 46 PDF / 1,104 pages / 100 QA；formal=60 | raw PDF；受控条件可为 parsed text | 自建 PDF evidence-chain、引用、TDAS | `LENOVO_BENCH_FORMAL_*_V1` |

### 3.2 WikiEval

- 下载地址：[Hugging Face：vibrantlabsai/WikiEval](https://huggingface.co/datasets/vibrantlabsai/WikiEval)。
- 50 个唯一 Wikipedia Markdown source；50 条英文问题。
- Gold 包含 relevant documents/evidence、retrieval keywords、expected answer keywords。
- 指标：Source Recall@1/3/5/10、MRR、reference-keyword recall、answer non-empty；RAGAS 0.2.15 的 Faithfulness、Answer Relevancy、Context Precision、Context Recall。RAGAS 每项保留独立 valid_n，不把 null 记 0。

### 3.3 MMDocIR

- 下载地址：[Hugging Face：MMDocIR/MMDocIR_Evaluation_Dataset](https://huggingface.co/datasets/MMDocIR/MMDocIR_Evaluation_Dataset)。
- 正式 scorer 以冻结文件实际 1,658 题为准；论文其他位置的 1,685 不作为运行分母。
- Page/Layout 不是 raw-PDF parser 对比：MOI 主结果直接使用官方预提取 page/layout 文本与 bbox；空候选以 U+2060 送 embedding，但数据库 content 保留空值语义。
- Page fraction Recall@K 是找回 Gold page fraction；Page hit@K 是是否命中任一 Gold page；二者不可互换。Layout Recall 使用同页预测 bbox 与 Gold bbox 的覆盖。
- 下游 QA 是项目扩展，不是 MMDocIR 官方任务，QA 使用 text `deepseek-v4-flash`、multimodal `qwen3.5-35b-a3b`。

### 3.4 DocBench

- 下载地址：[GitHub：Anni-Zou/DocBench](https://github.com/Anni-Zou/DocBench)。数据文件下载入口位于该仓库 README 的 Data 小节。
- 229 PDF、1,102 QA；问题类型 text-only、multimodal、metadata、unanswerable。
- Native PDF 与 candidate Markdown 必须分开：FastGPT/MaxKB 当前完整结果主要是 controlled parsed-text，不能冒充 native PDF parser 成绩。
- 统一 current provider：MaaS `bge-m3/1024`、Qianfan `deepseek-v4-flash`、reranker disabled、Top-K 1/3/5/10。


### 3.5 EnterpriseRAG-Bench

- 下载地址：[Hugging Face：onyx-dot-app/EnterpriseRAG-Bench](https://huggingface.co/datasets/onyx-dot-app/EnterpriseRAG-Bench)。
- 公开全库约 511,962 docs / 500 QA；本项目评测采用 question-linked representative slice：722 docs / 500 QA，必须标 `CURRENT_CORPUS_ADAPTED`。
- 来源类型包含 Slack、Gmail、Linear、Drive、HubSpot、Fireflies、GitHub、Jira、Confluence；题型含 basic、semantic、intra-doc、project、constrained、conflicting、completeness、high-level、info-not-found。

### 3.6 Lenovo-bench

- 下载地址：暂无公开 GitHub 或 Hugging Face 数据集页面；该数据集发布前不能声明为可公开下载或由外部独立复现。
- 46 PDF、1,104 pages、约 60.8 MiB；100 QA，formal/dev/pilot=60/20/20；formal answerable/unanswerable=53/7。
- 90 answerable + 10 unanswerable；230 critical atomic claims、90 evidence sets、144 evidence items。evidence-set 为“集合间 OR、集合内 AND”。

## 4. 评分与聚合复现

本节仅定义 `tables.md` 中实际展示的实验指标。若官方协议与本地适配协议不同，必须分别标注，禁止将两者合并成同一列。

### 4.1 通用符号、分母与缺失值规则

对问题 `q`，记：

- `G_q`：该问题冻结后的 Gold 文档、页面、证据项或 Gold claim 集合；
- `R_q@K`：系统返回的前 `K` 个检索单元；除特别说明外，先按稳定 ID 去重，再截取 Top-K；
- `Q_m`：满足指标 `m` 计算条件的问题集合；所有宏平均均除以 `|Q_m|`，而不是默认除以整个数据集大小；
- `I(condition)`：条件成立为 1，否则为 0。

统一执行以下规则：

1. **首次请求决定主结果。** initial attempt 纳入主分母；retry 只用于恢复诊断，不能覆盖首次的 `EMPTY`、`FAILED`、`TIMEOUT` 或 `BLOCKED`。
2. **系统失败不是缺失 Gold。** 对适用的确定性质量指标，超时、空答案和平台错误按 0 计入首次请求分母；只有 Gold 缺失、协议不适用或 judge 没有产生有效结果时才记为 N/A。
3. **不可观察的检索指标不得反推。** 没有真实 Top-K、rank、source ID 或 bbox trace 时，写 `N/A: TRACE_UNAVAILABLE`；平台不提供接口时写 `N/A: UNSUPPORTED_API`。不得从答案文本或引用反推出检索命中。
4. **每个比例保留原始计数。** 至少输出 `numerator`、`denominator`、`value`、`eligible_n`、`missing_n` 和 `na_reason`。LLM judge 指标还要输出 `valid_n` 与 `failed_n`，judge 失败不得静默按 0 或静默缩小分母。
5. **先题内、后题间。** 同一问题有多次 repeat 时，先对该问题求平均，再对问题做宏平均，避免高重试题获得更大权重。
6. **claim 指标同时报告微观与宏观结果。** 表格中的 `109/123` 一类值是 claim 级微观计数；正式比较还应附每题得分的宏平均，不能把同一问题内的 claims 当作相互独立样本。
7. **配对比较必须同条件。** 只有数据集 freeze、问题集、Gold、Top-K、模型和评分器版本完全一致时，才做系统间配对差异。

### 4.2 WikiEval 指标

WikiEval 的确定性检索指标使用冻结的 source ID、reference keywords 和 Top-K trace；生成质量使用 `ragas==0.2.15`。各 RAGAS 指标独立计算 `valid_n`。

| 指标 | 每题计算规则 | 汇总规则 |
|---|---|---|
| Source R@1/3/5 | 若 `R_q@K` 中至少出现一个 Gold source，则为 1，否则为 0 | 对全部 eligible questions 宏平均。它只表示“至少命中一个来源”，不表示完整证据集已找齐。 |
| MRR | 若首个 Gold source 的排名为 `r_q`，则为 `1/r_q`；Top-K 内无 Gold source 为 0 | 对全部 eligible questions 宏平均。 |
| Reference keyword recall | 冻结的 reference/retrieval keywords 中，经同一规范化与匹配规则后出现在实际检索上下文中的关键词数，除以该题 reference keywords 总数 | 对具有 reference keywords 的问题宏平均；同时保存命中关键词和总关键词数。 |
| Faithfulness | 将答案拆为可判定陈述，计算“能由实际运行时上下文支持的陈述数 / 可判定陈述总数” | 直接使用冻结 RAGAS 版本与 judge 配置的结果，对有效 judge rows 宏平均。没有真实上下文 trace 时为 N/A。 |
| Answer Relevance | RAGAS 从答案反向生成问题，并用冻结 embedding 计算这些问题与原问题的平均余弦相似度 | 对有效 judge rows 宏平均。表中沿用 `Answer Relevance` 名称，对应 RAGAS 的 Answer Relevancy。 |
| Context Precision | 使用 RAGAS 0.2.15 判断各检索上下文对 reference answer 的相关性，并按检索位置计算排序敏感的 precision | 直接汇总 RAGAS 输出；不得改写成 Context Relevance。 |
| Context Recall | reference answer 中可由检索上下文归因或支持的 claims 数，除以 reference answer claims 总数 | 对有效 judge rows 宏平均。 |

复现时必须随结果保存 RAGAS 版本、judge LLM、judge embedding、prompt、temperature 和异常处理参数；任一项变化都视为新的 metric version。

### 4.3 MMDocIR 指标

MMDocIR 的候选域限定为问题所属长文档内部的页面和布局区域，不能把跨文档检索结果混入排名。

| 指标 | 每题计算规则 | 汇总规则 |
|---|---|---|
| Page fraction @1/3/5/10 | `Top-K 中命中的唯一 Gold pages 数 / 该题唯一 Gold pages 总数` | 对全部有 page Gold 的问题宏平均；`tables.md` 以百分数值展示。该指标是 page fraction recall，不是“是否命中任一页面”的 Page Hit。 |
| Page MRR | 首个 Gold page 排名的倒数；未命中为 0 | 对全部有 page Gold 的问题宏平均。 |
| Layout @1/5/10 | 对 Top-K 预测 bbox，仅与同页 Gold bbox 求交；`所有交叠面积之和 / Gold bbox 总面积` | 对有 bbox Gold 的问题宏平均，`tables.md` 以百分数值展示。当前本地 scorer 不对重叠预测框做 union 去重，也不显式截断到 1，复现时必须保留这一版本特征。 |
| QA Answer Correctness (/5) | 冻结的语义 judge 按同一 1–5 分 rubric 对预测答案和 Gold/reference answer 评分 | 对有效 judge rows 求算术平均，并报告 `valid_n/failed_n`；不得与 0–1 指标混算。 |
| QA Token F1 | 对预测答案与 reference answer 执行冻结的文本规范化；令 token precision=`共同 token 数/预测 token 数`、token recall=`共同 token 数/Gold token 数`，取二者调和平均；无交集为 0 | 逐题取多个合法 reference 中的约定值后宏平均；多 reference 的选择规则必须随 metric version 固定。 |
| QA Faithfulness | 答案中能被实际提供上下文支持的事实 claims 数，除以答案中的可判定事实 claims 总数 | 对有效 judge rows 宏平均；没有运行时上下文 trace 时为 N/A。 |
| QA Contains Gold | `I(规范化后的预测答案包含规范化后的 Gold answer)` | 对 eligible questions 宏平均，并保留命中数/分母；这是词面诊断指标，不替代语义正确性。 |
| QA Answer non-empty | `I(首次请求获得非空终态答案)` | 分母为全部计划执行的问题；它衡量可用性，不代表答案正确。 |

### 4.4 DocBench 指标

DocBench 主指标使用冻结 judge 的二元正确性评分。Native PDF 与 controlled parsed-text 是不同协议，必须分开呈现；没有执行 judge 的系统只能展示词面诊断指标，不能用 Token F1 或 Contains-gold 替代 Correctness。

| 指标 | 计算规则 | 分母与聚合 |
|---|---|---|
| Overall Correctness | judge 根据问题、reference answer、系统答案和冻结 rubric 输出 0/1 | 在全部 eligible questions 上求均值，同时报告 judge `valid_n/failed_n`。 |
| Text-only | 与 Overall Correctness 相同 | 只在 Gold 类型为 text-only 的问题切片上求均值，并报告切片 `n`。 |
| Multimodal | 与 Overall Correctness 相同 | 只在需要图像、表格或版面信息的问题切片上求均值，并报告切片 `n`。 |
| Metadata | 与 Overall Correctness 相同 | 只在 metadata 问题切片上求均值，并报告切片 `n`。 |
| Unanswerable | 与 Overall Correctness 相同；只有正确拒答且不捏造信息才得 1 | 只在 Gold 标注为 unanswerable 的问题切片上求均值，并报告切片 `n`。 |
| Contains-gold | 与 4.3 的 QA Contains Gold 相同 | 对具有可比较短答案 Gold 的问题宏平均。 |
| Token F1 | 与 4.3 的 QA Token F1 相同 | 对具有可词面比较 reference answer 的问题宏平均。 |

四个子类指标是 Overall Correctness 的切片，不是四套不同评分器。切片之和、各切片分母及总体分母必须能够相互核对。

### 4.5 EnterpriseRAG-Bench 指标

当前本地冻结集包含 500 个问题，其中 470 个问题有 `gold_doc_ids`；检索指标只在这 470 题上计算。对每题检索 trace 先按 source document 去重，保留首次出现位置，再截取前 10 个唯一文档。

| 指标 | 每题计算规则 | 分母与聚合 |
|---|---|---|
| Doc Recall@10 | `Top-10 唯一文档中命中的 Gold documents 数 / Gold documents 总数` | 在有 `gold_doc_ids` 的 470 题上宏平均。 |
| Complete Evidence-set Recall@10 | 若 Top-10 完整覆盖至少一个合法 Gold/alternative evidence document set，则为 1，否则为 0 | 在有可判定 evidence set 的问题上宏平均。 |
| Invalid Extra Docs | Top-10 唯一文档中既非 Gold、也不属于合法 alternative set 的文档数量 | 对 470 题取平均数量；该列是 mean count，不是百分比或 Invalid Extra Rate。 |
| Semantic Recall | 与 Doc Recall@10 相同 | 仅在 `semantic` 类型且具有 doc Gold 的问题切片上宏平均，并报告切片 `n`。 |
| Conflict Recall | 与 Doc Recall@10 相同 | 仅在 `conflicting`/conflict 类型且具有 doc Gold 的问题切片上宏平均，并报告切片 `n`。 |
| Info-not-found Success | judge 判断系统是否给出与该题 Gold 状态一致的“信息不足/找不到”结论，且没有捏造事实 | 仅在 `info_not_found` 切片上宏平均。当前数据包若仍把这些题标为 `answerable=true`，只能称 Info-not-found Success，不能改称 Strict Unanswerable Success。 |
| Correctness | 冻结 judge 对答案是否正确给分；官方/MOI 协议通常为二元 0/1，FastGPT adapted 协议可为 `[0,1]` 连续分 | 对有效 judge rows 宏平均；结果必须附 `judge_protocol` 与 metric version。 |
| Completeness | `系统答案覆盖的 Gold answer_facts 数 / 该题全部 Gold answer_facts 数`，或使用等价的冻结 judge schema 产出该比例 | 对具有 `answer_facts` 且 judge 有效的问题宏平均。 |
| Dataset Aggregate | 官方/MOI 二元门控：`mean(I(Correctness=1) × Completeness)`；FastGPT adapted：`mean(Correctness × Completeness)` | 两种 aggregate 必须分栏并标明 `official/native` 或 `adapted`，不得直接横向混排。 |
| Strict Unanswerable Success | 仅当 Gold 明确标记为 unanswerable 时，正确拒答、理由与 Gold 一致且无虚构事实才为 1 | 如果当前 500 题均为 `answerable=true`，该指标为 N/A；不得把 20 个 `info_not_found` 题静默重标为 unanswerable。 |

因此，`tables.md` 当前“问答 Strict Unanswerable Success”列中的数值必须追溯其 scorer 与 Gold 标记：若实际沿用了 20 道 `info_not_found` 题的成功率，应将该列重命名为 `Info-not-found Success (adapted)`；在没有真实 unanswerable Gold 的版本上不能继续标为 Strict Unanswerable Success。

### 4.6 Lenovo-bench 指标

Lenovo 正式集为 60 题，其中 53 题 answerable、7 题 unanswerable。每个 answerable 问题可以有多个合法 evidence sets；集合之间为 OR，集合内部为 AND。

| 指标 | 每题计算规则 | 分母与聚合 |
|---|---|---|
| Evidence Recall @1/3/5/10 | 对每个合法 evidence set 计算 `Top-K 命中的 evidence items 数 / 该集合 evidence items 总数`，取所有合法集合中的最大值 | 只在 53 个 answerable 问题上宏平均；unanswerable 问题没有 evidence Gold，不按 0 计入。 |
| Complete Evidence-set Recall @1/3/5/10 | 若 Top-K 完整覆盖任一合法 evidence set，则为 1，否则为 0 | 只在 answerable 问题上宏平均。 |
| Response-claim Correctness | 将回答拆成 material factual claims；正确=1、部分正确=0.5、错误或无依据=0，得分为 `claim 权重得分之和 / eligible response claims 数` | 表中同时报告 claim 级微观原始计数（如 `109/123`）和每题宏平均；answerable 问题出现空答、纯拒答或没有可评分事实 claim 时，该题按 0。 |
| Reference-claim Recall | `被回答完整覆盖的 Gold reference claims 数 / 全部需评分 Gold reference claims 数`；部分覆盖不算命中 | 同时报告微观计数和每题宏平均；正式集当前 reference-claim 总分母为 139 时，结果必须能回算到该分母。 |
| Strict Unanswerable Success | 仅当回答正确拒绝作答、理由与 Gold 允许的原因一致、且不含无依据事实或引用时为 1，否则为 0 | 只在 7 个正式 unanswerable 问题上求均值，并报告原始成功数/7。 |

Lenovo 的 retrieval 指标必须来自真实 rank/lineage trace。平台只返回答案、没有检索明细时，Evidence Recall 与 Complete Evidence-set Recall 均记为 N/A，而不是根据回答覆盖的 claims 估算。

### 4.7 Judge 冻结与结果落盘

所有 LLM judge 指标必须随结果保存以下字段：

- `judge_provider`、`judge_model`、模型版本或快照日期；
- prompt 原文与 `prompt_hash`、rubric/schema 版本；
- temperature、max output tokens、timeout、重试策略；
- 输入使用的 question、Gold/reference、actual context 和 system answer 的哈希；
- `valid_n`、`failed_n`、失败原因与 recovery ledger。

代表性冻结配置包括 WikiEval 的 `ragas==0.2.15`，以及 Lenovo 的 Qianfan `deepseek-v4-flash`、temperature 0 和统一 scorer。若 provider、模型、prompt、rubric 或解析 schema 变化，必须生成新的 `metric_version`，不能覆盖旧分数。

每个系统 × 数据集的最终结果至少落盘以下三层数据：

1. **逐请求 ledger**：首次请求状态、retry、耗时边界、原始答案及真实检索 trace；
2. **逐题 metric rows**：各指标的题级 numerator、denominator、score、eligibility 和 N/A 原因；
3. **汇总表**：macro/micro 值、计划题数、完成题数、有效题数、失败题数和 metric version。

未运行 judge 的结果只能报告可复算的确定性指标；不得把未评分的 Correctness、Faithfulness 或 claim 指标填成 0。
