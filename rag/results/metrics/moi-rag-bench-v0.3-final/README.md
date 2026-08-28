# MOI final results: moi-rag-bench-v0.3-final

本目录按既有 `results/metrics/<dataset>/` 七件套格式，归档 MOI 的最终严格复跑：

`runs/bench-v0.3-final/20260826-moi-v03-final-strict-rerun/`

- `results.jsonl`：原生最终结果，275 题、全部 `ok`；与源文件逐字节一致。
- `judge-input.jsonl`：275 条统一 Judge 输入，使用最终 terminal ledger 中实际暴露的 Top-10 context。
- `judgements.jsonl`：每题选取最新一条成功判分，共 275 条 `valid`。
- `judge-attempts.jsonl`：完整 Judge terminal ledger，与源文件逐字节一致；共 276 次尝试，其中 1 次失败后恢复成功。
- `metrics.json`：最终 `competitor-eval-metrics-v1` 聚合结果，与源文件逐字节一致。
- `sources.json`：源文件、选择规则、数量和 SHA-256 provenance。

数据切片为 DocBench 130 题、Enterprise 40 题、MultiHop 105 题；总计 275 题，其中可回答 248 题、不可回答 27 题。Runner、检索、问答和最终 Judge 均完成全量分母。

## 口径说明

- `results.jsonl` 保留完整原生 MOI 结果；文件约 464 MiB，按仓库规则仅保存在本地，不纳入 Git。
- Judge 协议字段原样保留运行时的 `moi-rag-bench-v0.1-adapted-reference-rubric-v1` 名称。它是该 v0.3 final run 实际使用的 evaluator 协议标签，不代表数据集退回 v0.1。
- 根目录现有 `canonical-manifest.json` 和 `final-score-summary.json` 仍是 2026-08-17 五数据集冻结包，不在本次归档中改写；本目录的 provenance 以 `sources.json` 为准。
