# MOI 实验结果归档包

本目录原有五个数据集整理自 `runs/final-results/moi/20260817-final/` 的 canonical 实验结果；另收录后续的 MOI RAG Benchmark v0.3 final 严格复跑。所有原始运行目录均未移动或覆盖。

每个数据集目录包含：

- `results.jsonl`：每题一条的最终结果；
- `judge-input.jsonl`：可直接交给 Judge 的输入；
- `judgements.jsonl`、`judge-attempts.jsonl`：逐题判分及尝试记录；
- `metrics.json`：数据集汇总指标；
- `sources.json`：原始运行文件 provenance；
- `README.md`：该数据集的口径与限制。

数据集：`wikieval`、`mmdocir`、`docbench`、`enterpriserag-bench`、`lenovo-bench`；另新增独立归档 `moi-rag-bench-v0.3-final/`，对应 MOI 的 275 题最终严格复跑。

根目录的 `canonical-manifest.json` 和 `final-score-summary.json` 保存整体 manifest 与最终汇总。DocBench 的 4 个最终失败题仍保留在 `results.jsonl`，用于维持全量分母。

`moi-rag-bench-v0.3-final/` 晚于上述五数据集冻结包，因此不反向改写 2026-08-17 的根 manifest；该次运行的来源与哈希记录在其目录内的 `sources.json`。

## 上传策略

本目录默认允许上传。为控制仓库体积，只有超过 10 MB 的 JSON/JSONL 载荷保持本地、不纳入 Git：

- `docbench/judge-input.jsonl`；
- `docbench/results.jsonl`；
- `enterpriserag-bench/judge-input.jsonl`；
- `enterpriserag-bench/results.jsonl`；
- `mmdocir/judge-input.jsonl`；
- `mmdocir/results.jsonl`。

其余较小的 `metrics.json`、`sources.json`、manifest、`judgements.jsonl`、`judge-attempts.jsonl`、其他 JSONL，以及 README/报告均保持可见、可上传。规则按具体大文件生效，不会出现某个数据集的小结果文件被误忽略的情况。
