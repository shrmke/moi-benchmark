# MOI RAG Benchmark：本地实现与评测工作区

本目录是 MatrixOne Intelligence（MOI）/ MatrixFlow 的本地 RAG 实现、竞品部署、Benchmark 运行和结果审计工作区。当前工作树把可复用实现、平台适配、评测编排、原始运行产物和可阅读结果分开保存；本地凭据、语料、容器卷和运行日志不进入版本库。

## 从哪里开始

所有命令默认从本目录执行：

```bash
cd rag
```

准备本机环境变量：

```bash
cp .env.example .env
chmod 600 .env
# 只在本机 .env 中填写 API key、应用 ID、数据集 ID 和 chat ID
```

按目标选择入口：

| 目标 | 入口 |
|---|---|
| 运行 MOI/MatrixFlow 的本地解析、Embedding、RAG 或端到端链路 | [`moi-prototypes/README.md`](moi-prototypes/README.md) |
| 部署和评测 Dify、FastGPT、MaxKB、RAGFlow 等本地平台 | [`local-rag-platforms/README.md`](local-rag-platforms/README.md) |
| 运行 MOI Benchmark Stage 1 | [`benchmarks/README-moi-rag-benchmark.md`](benchmarks/README-moi-rag-benchmark.md) |
| 查看已整理的 canonical 结果和上传规则 | [`results/README.md`](results/README.md) |
| 查看 MOI RAG v2.0 报告 | [`results/reports/MOI_rag_benchmark_v2.0.md`](results/reports/MOI_rag_benchmark_v2.0.md) |
| 核对 v2.0 可审计快照 | [`results/reports/MOI_rag_benchmark_v2.0.provenance.json`](results/reports/MOI_rag_benchmark_v2.0.provenance.json) |
| 查看 MOI RAG v1.0 报告 | [`results/reports/MOI_rag_benchmark_v1.0.md`](results/reports/MOI_rag_benchmark_v1.0.md) |
| 查看复现报告 | [`results/reports/MOI_rag_reproduction_guide.md`](results/reports/MOI_rag_reproduction_guide.md) |

## 当前目录结构

```text
rag/
├── benchmarks/              # 数据集评测、重算、合并和恢复脚本
├── scripts/                 # benchmark 启动、恢复、监控及辅助工具
├── moi-prototypes/          # MOI/MatrixFlow 的本地解析、RAG、Pipeline、Embedding
├── local-rag-platforms/     # 竞品部署、平台适配、统一 API、评测和 Judge
├── datasets/                # 本地数据集、语料和 Gold；通常不提交
├── runs/                    # 每次运行的 ledger、checkpoint、原始响应和指标
├── outputs/                 # 解析文档和中间产物
├── results/                 # 可阅读汇总、canonical metrics、报告和复现说明
├── docs/                    # 指标、研究、计划、参考资料和操作记录
└── tests/                   # Benchmark、TaaS 和仓库级测试
```

## 最小校验

先做不启动服务、不发送请求的静态检查：

```bash
python3 -m compileall -q benchmarks scripts local-rag-platforms tests
uv run --with pytest --with-requirements local-rag-platforms/api_console/requirements.txt pytest local-rag-platforms/tests -q
```

需要访问外部模型或本地服务的测试必须显式配置对应环境变量。不要把真实 key 写入配置 JSON、README、运行产物或提交历史。

## 运行产物和提交边界

- `runs/`、`outputs/`、`.local-services/`：本机运行状态和原始产物；默认保留在本地。
- `datasets/`：语料和 Gold 可能含授权或体积限制；按数据集许可和 `.gitignore` 处理。
- `results/`：汇总结果、指标、报告和 README 可用于归档；大体量生成载荷按[`results/README.md`](results/README.md) 的当前规则处理。
- `.env`：唯一的本机凭据入口；`.env.example` 只保存变量名和非敏感默认值。


## 重要边界

`moi-prototypes/` 中的实现是可独立运行的产品链路切片，不等于完整部署MatrixFlow Web 应用。特别是：

- 本地 RAG 直接调用 MatrixFlow 的 Split、Index、SearchRAGChunks 等产品模块，不需要启动完整前端、后端、Catalog 或 Worker。
- Parser 的 `v3-native` 是明确标记的本地兼容路线；它不能自动宣称等价于 Web 知识库的 `standard_rag` V2 解析工作流。
- `local-rag-platforms/` 的服务按串行窗口运行；一次只启动一个竞品栈，避免端口、容器、模型配置和知识库相互污染。

## 结果与审计

Benchmark 的原始响应、失败题、恢复轮次和 Judge 输入都应保留在对应 run 中。最终汇总只引用已冻结的 canonical 文件，并同时报告成功数、失败数、有效 Judge 分母和协议边界。需要调整指标或结果时，优先修改生成脚本和 manifest，再重新生成汇总。

v1.0 与 v2.0 是并行的两条研究线，不是前后替代关系：

| 版本 | 数据基座 | 研究重点 | 入口 |
|---|---|---|---|
| **v1.0** | WikiEval、MMDocIR、DocBench、EnterpriseRAG-Bench、Lenovo-bench | 公开数据集上的任务与指标表现 | [报告](results/reports/MOI_rag_benchmark_v1.0.md) |
| **v2.0** | 从 DocBench、EnterpriseRAG-Bench、MultiHop-RAG 收集、筛选并合并的混合数据集 | RAG 证据链质量 + 系统接口与服务交付能力 | [报告](results/reports/MOI_rag_benchmark_v2.0.md) · [Provenance](results/reports/MOI_rag_benchmark_v2.0.provenance.json) |

## v2.0 当前核心结果

v2.0 使用 **297 份文档、275 条纯文本 QA 和 275 条 Gold**，其中 245 条可回答、30 条拒答或不可回答。四平台统一使用 MaaS `bge-m3/1024` Embedding、DeepSeek V4 Flash 生成与 Judge，关闭 thinking，不调用 MLLM。

| 平台 | C8 Request QPS | Lenovo 检索 P50 | Recall@1 | Recall@10 | Token F1 | Answer Relevance | Unsupported Claim↓ |
|---|---:|---:|---:|---:|---:|---:|---:|
| **MOI** | 0.364 | 981.858 ms | **59.50%** | 61.95% | **30.05%** | 68.47% | 64.97% |
| **Dify** | 0.450 | 1,369.693 ms | 54.34% | 83.33% | 24.12% | 55.49% | **60.64%** |
| **FastGPT** | **9.926** | **443.851 ms** | 56.07% | **85.66%**¹ | 19.49% | 57.09% | 61.09% |
| **MaxKB*** | 8.616 | 2,098.449 ms | 55.06% | 86.23%* | 29.00% | **69.31%** | 72.47% |

¹ FastGPT 是严格原生 Recall@10 最高的平台。`MaxKB*` 使用管理诊断入口，并按 `comprehensive_score` 离线恢复排序，因此只作为带条件的诊断结果。

这些指标分轨解释：C8 QPS 来自固定 `64 chunks × 10 ms` 的受控流式负载，不代表真实 RAG 或模型速度；Lenovo P50 是 10 条查询的完整检索合同耗时，包含 Query Embedding 与平台编排；Retrieval 分母为 265，词面和通用 Judge 分母为 275，回答型 Judge 分母为 245，严格拒答分母为 30。

### v2.0 核心结论

1. **MOI 的优势**：Recall@1 和 Token F1 为四平台最高，Answer Relevance 仅略低于 MaxKB*，说明首位命中、答案词面覆盖和问题相关性具有竞争力。
2. **MOI 的短板**：Recall@1 到 Recall@10 只提升 2.45 个百分点；受控 C8 吞吐较低，Unsupported Claim Rate 仍为 64.97%，高 K 候选扩展、流式交付和证据约束是下一步重点。
3. **没有跨维度冠军**：FastGPT 在严格原生高 K 召回、受控吞吐和 10-query 检索时延上领先；Dify 的 Unsupported Claim Rate 最低；MaxKB* 的候选覆盖和答案相关性较高，但检索排序使用诊断代换。

## v1.0 公开数据集结果

v1.0 将五类现有实验结果统一整理，用于比较四平台在文本检索、长文档检索、复杂 PDF 问答、企业多源问答和证据链问答上的表现。

| 数据集 / 核心指标 | MOI | Dify | FastGPT | MaxKB | 核心结论 |
| ----------------- | ---: | ---: | ------: | ----: | -------- |
| **WikiEval**<br>Source R@1 / Keyword Recall | **100.0%** / **65.19%** | **100.0%** / 59.46% | **100.0%** / 41.39% | 98.0% / 62.37% | 三个平台 Source R@1 并列 100%；MOI 关键词覆盖最高 |
| **MMDocIR**<br>Page@1 / Page@10 / Layout@10 / QA (/5) | 43.49% / 84.02% / **61.87%** / 3.91 | **53.51%** / 78.00% / 59.98% / **4.02** | 51.46% / 87.72% / 57.39% / 3.95 | 48.95% / **92.56%** / 59.01% / 3.87 | Dify 的首位页面与 QA 最好，MaxKB 的高 K 页面覆盖最高，MOI 的布局召回最高 |
| **DocBench**<br>Overall / Multimodal / Metadata / Unanswerable | 58.26% / 43.23% / **24.53%** / **85.96%** | **61.32%** / **45.12%** / 20.85% / 79.21% | 54.23% / 40.92% / 22.98% / 84.97% | 59.91% / 42.72% / 22.98% / 80.82% | Dify 的总体和多模态正确率最高；MOI 的 Metadata 与拒答表现最好 |
| **EnterpriseRAG-Bench**<br>Doc R@10 / Complete@10 / Invalid Extras↓ / Correctness / Completeness | 80.59% / 74.68% / **2.345** / 49.20% / **58.74%** | 88.51% / 78.30% / 5.991 / 55.40% / 57.50% | **89.61%** / **85.53%** / 8.685 / **60.27%** / 57.95% | — | FastGPT 的召回与正确率最高；MOI 的无效文档最少、完整性最高 |
| **Lenovo-bench**<br>Evidence R@10 / Complete@10 / Response Correctness / Reference Recall | 50.00% / 41.51% / 88.62% / 18.71% | 45.35% / 36.54% / 68.52% / 8.63% | **75.16%** / **62.26%** / 86.11% / **45.32%** | 60.35% / 58.11% / **91.07%** / 3.60% | FastGPT 的证据召回与答案覆盖最高；MaxKB 的已输出 claim 正确率最高，MOI 次之 |

### v1.0 核心结论

1. **MOI 的主要优势**：WikiEval 关键词覆盖最高，MMDocIR 布局召回最高，EnterpriseRAG-Bench 的无效额外文档最少且 Completeness 最高，体现出稳定文本链路、布局定位和低噪声证据组织能力。
2. **MOI 的主要短板**：EnterpriseRAG-Bench 与 Lenovo-bench 的高 K 证据召回和完整证据集覆盖落后于 FastGPT；Lenovo-bench 的 Reference-claim Recall 偏低，DocBench 总体和多模态正确率仍有提升空间。
3. **整体判断**：四个平台没有跨五类任务的一致冠军。Dify 在生成质量与 DocBench 上更强，FastGPT 在证据召回和答案覆盖上领先，MaxKB 在部分高 K 页面覆盖和已输出 claim 正确率上突出，MOI 的优势集中在布局检索、低噪声和证据完整性。

详细结果与复现说明：

- [MOI RAG Benchmark v2.0](results/reports/MOI_rag_benchmark_v2.0.md)
- [MOI RAG Benchmark v2.0 Provenance](results/reports/MOI_rag_benchmark_v2.0.provenance.json)
- [MOI RAG Benchmark v1.0](results/reports/MOI_rag_benchmark_v1.0.md)
- [MOI RAG 四平台五数据集实验复现报告](results/reports/MOI_rag_reproduction_guide.md)
