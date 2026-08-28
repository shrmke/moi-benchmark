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

Benchmark 的原始响应、失败题、恢复轮次和 Judge 输入都应保留在对应 run 中。最终汇总只引用已冻结的 canonical 文件，并同时报告成功数、失败数、有效 Judge分母和协议边界。需要调整指标或结果时，优先修改生成脚本和 manifest，再重新生成汇总。

本轮将 WikiEval、MMDocIR、DocBench、EnterpriseRAG-Bench 和 Lenovo-bench 的现有实验结果统一纳入 MOI RAG Benchmark v1.0，直接比较 MOI、Dify、FastGPT 与 MaxKB 在文本检索、长文档检索、复杂 PDF 问答、企业多源问答和证据链问答上的表现。

## 当前核心结果

| 数据集 / 核心指标 | MOI | Dify | FastGPT | MaxKB | 核心结论 |
| ----------------- | ---: | ---: | ------: | ----: | -------- |
| **WikiEval**<br>Source R@1 / Keyword Recall | **100.0%** / **65.19%** | **100.0%** / 59.46% | **100.0%** / 41.39% | 98.0% / 62.37% | 三个平台 Source R@1 并列 100%；MOI 关键词覆盖最高 |
| **MMDocIR**<br>Page@1 / Page@10 / Layout@10 / QA (/5) | 43.49% / 84.02% / **61.87%** / 3.91 | **53.51%** / 78.00% / 59.98% / **4.02** | 51.46% / 87.72% / 57.39% / 3.95 | 48.95% / **92.56%** / 59.01% / 3.87 | Dify 的首位页面与 QA 最好，MaxKB 的高 K 页面覆盖最高，MOI 的布局召回最高 |
| **DocBench**<br>Overall / Multimodal / Metadata / Unanswerable | 58.26% / 43.23% / **24.53%** / **85.96%** | **61.32%** / **45.12%** / 20.85% / 79.21% | 54.23% / 40.92% / 22.98% / 84.97% | 59.91% / 42.72% / 22.98% / 80.82% | Dify 的总体和多模态正确率最高；MOI 的 Metadata 与拒答表现最好 |
| **EnterpriseRAG-Bench**<br>Doc R@10 / Complete@10 / Invalid Extras↓ / Correctness / Completeness | 80.59% / 74.68% / **2.345** / 49.20% / **58.74%** | 88.51% / 78.30% / 5.991 / 55.40% / 57.50% | **89.61%** / **85.53%** / 8.685 / **60.27%** / 57.95% | — | FastGPT 的召回与正确率最高；MOI 的无效文档最少、完整性最高 |
| **Lenovo-bench**<br>Evidence R@10 / Complete@10 / Response Correctness / Reference Recall | 50.00% / 41.51% / 88.62% / 18.71% | 45.35% / 36.54% / 68.52% / 8.63% | **75.16%** / **62.26%** / 86.11% / **45.32%** | 60.35% / 58.11% / **91.07%** / 3.60% | FastGPT 的证据召回与答案覆盖最高；MaxKB 的已输出 claim 正确率最高，MOI 次之 |

## 核心结论

1. **MOI 的主要优势**：WikiEval 关键词覆盖最高，MMDocIR 布局召回最高，EnterpriseRAG-Bench 的无效额外文档最少且 Completeness 最高，体现出稳定文本链路、布局定位和低噪声证据组织能力。
2. **MOI 的主要短板**：EnterpriseRAG-Bench 与 Lenovo-bench 的高 K 证据召回和完整证据集覆盖落后于 FastGPT；Lenovo-bench 的 Reference-claim Recall 偏低，DocBench 总体和多模态正确率仍有提升空间。
3. **整体判断**：四个平台没有跨五类任务的一致冠军。Dify 在生成质量与 DocBench 上更强，FastGPT 在证据召回和答案覆盖上领先，MaxKB 在部分高 K 页面覆盖和已输出 claim 正确率上突出，MOI 的优势集中在布局检索、低噪声和证据完整性。

详细结果与复现说明：

- [MOI RAG Benchmark v1.0](results/MOI_rag_benchmark_v1.0.md)
- [MOI RAG 四平台五数据集实验复现报告](results/MOI_rag_reproduction_guide.md)
