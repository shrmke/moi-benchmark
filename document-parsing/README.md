# 文档解析 Benchmark Track

状态：已完成 MOI IDC 4.1.14 的 OmniDocBench v1.6 全量正式评测，1651 页
Overall 为 **90.23**。

本 Track 的材料保存在本目录；正式评测主要使用 `datasets/`、`scripts/`、
`runs/` 和 `evaluate/`，方案与调研材料保存在 `plans/`、`research/` 等目录。

## OmniDocBench 最终正式结果

最终正式结果只采用 MOI IDC 4.1.14、统一 VLM 配置下的 1651 页全量评测：

- [完整评测分析](evaluate/MOI_OmniDocBench评测分析.md)：配置、模型链路、评分细则、公开模型对照和扣分分析；
- [评分摘要](evaluate/moi-omnidocbench-final/summary.md)：正式指标及评分器运行边界；
- [Golden 获取与校验](evaluate/moi-omnidocbench-final/GOLDEN.md)：官方来源、版本及 SHA-256；
- [评分输入说明](runs/omnidocbench-idc-4.1.14-vlm-final-1651-official-md/README.txt)：1651 个预测 Markdown 与适配清单的来源；
- [一键复现评分](scripts/reproduce_omnidocbench_final_score.sh)：校验输入并使用固定 digest 的官方 Docker 镜像复算 90.23；
- [IDC 输出适配器](scripts/adapt_idc_omnidocbench.py)：从原始解析 ZIP 的 `_parse.json` 生成官方评分 Markdown；
- [归档评分结果](evaluate/moi-omnidocbench-final/result/)：官方 evaluator 的完整 JSON 和运行环境记录。

最小复现链路如下：

```text
官方 Golden + 已归档的 1651 个预测 Markdown
  -> reproduce_omnidocbench_final_score.sh
  -> end2end.docker.yaml + 官方 Docker evaluator
  -> evaluate/moi-omnidocbench-final/reproduced-result/
  -> verify_omnidocbench_final_score.py 校验 Overall 90.23
```

已归档预测可以直接复算正式分数，不需要 7.4 GB 原始解析 ZIP。只有重新生成
预测 Markdown 时才需要原始 ZIP 并运行适配器。

## 半导体场景私有数据集正式结果

私有集包含 50 份 PDF、PPTX、DOCX 和 DOC 问题样本，对比 MOI IDC 4.1.14、
MinerU Precision 与 PaddleOCR-VL。正式 MOI 文件内维度等权分为 **84.5%**，
文件内元素数加权分为 **89.4%**；两种口径最后均按 50 个文件等权平均。

- [完整评测报告](evaluate/半导体场景私有数据集评测报告.md)：实验链路、模型与参数、评分细则、分组分析和结论边界；
- [复现入口](evaluate/semiconductor-private-final/README.md)：评分器版本、命令、目录关系和配置权威来源；
- [逐文件评分结果](evaluate/semiconductor-private-final/reproduced-score.json)：三套系统的维度明细及两种聚合分；
- [输入与配置清单](evaluate/semiconductor-private-final/manifest.json)：50 个 case 的 SHA-256、Golden 状态、页数及 MOI 配置组；
- [一键复算脚本](scripts/reproduce_semiconductor_private_score.sh)：固定评分器 commit 并校验正式总分；
- [竞品适配入口](scripts/adapt_semiconductor_private_outputs.py)：从 MinerU/Paddle 原始 JSON 重建统一 Parse Blocks。

最小复现链路如下：

```text
源文件 + Golden + 三套系统正式解析结果
  -> reproduce_semiconductor_private_score.sh
  -> 固定 moi-parse-bench commit 的 BenchmarkRunner
  -> evaluate/semiconductor-private-final/reproduced-score.json
```

MOI 的实际参数以每个结果 ZIP 内 `parser_config` 及 manifest 为准；当前批跑脚本是
后来形成的统一配置，不作为本次 4 个历史配置小批次的替代证据。

## 其他材料

- [评测方案草稿](plans/drafts/v0.1.md)
- [统一评测工具升级方案草稿](plans/drafts/unified-evaluation-tool-upgrade-v0.1.md)
- 本地 OmniDocBench 数据集：`datasets/omnidocbench/`（不提交数据本体）
- 官方评测工具版本：`opendatalab/OmniDocBench` commit
  `2b161d010d2e3aff77a0edef359ea3a6411d23cd`，正式复现使用固定 Docker 镜像

私有数据集评测与 OmniDocBench 官方评测使用不同 Golden 和指标体系，不混合
计算总分。
