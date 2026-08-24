# MOI Benchmark Roadmap

五个 Track 独立推进。一个 Track 可以先进入试运行，但不得替代其他 Track 的方案、数据、结果或发布记录。

## 公共治理（当前）

- [X] 建立五个根级 Track 目录
- [X] 规定 Track 内部方案、数据、运行和报告边界
- [ ] 确认各 Track 负责人、审批人和发布人
- [ ] 冻结跨 Track 元数据、隐私、安全和发布规则

## Astra

当前状态：阶段性评测与综合汇报已完成；正式 Track 方案审批、资产冻结和 Release 尚未完成。

- [ ] 审阅并批准 Track 方案
- [ ] 冻结任务、数据、系统、指标与 evaluator 版本
- [X] 建立阶段性可执行 Benchmark 资产
- [X] 完成阶段性运行与结果复核
- [ ] 发布 Astra Track 报告
- [ ] 建立持续回归

方案批准前不采纳草稿中的详细实验承诺。

## Memoria

当前状态：LongMemEval-S、LoCoMo 与 Feature 阶段性评测已完成；正式 Track 方案、资产冻结和 Release 尚未完成。

- [ ] 完成调研与方案审阅
- [ ] 冻结任务、数据、系统、指标与 evaluator 版本
- [X] 建立阶段性可执行 Benchmark 资产
- [X] 完成阶段性运行与结果复核
- [ ] 发布 Memoria Track 报告
- [ ] 建立持续回归

## 文档解析

当前状态：OmniDocBench 与行业私有集阶段性评测已完成；正式 Track 方案、资产冻结和 Release 尚未完成。

- [ ] 完成调研与方案审阅
- [ ] 冻结任务、数据、系统、指标与 evaluator 版本
- [X] 建立阶段性可执行 Benchmark 资产
- [X] 完成阶段性运行与结果复核
- [ ] 发布文档解析 Track 报告
- [ ] 建立持续回归

## RAG

当前状态：公开集与企业私有集阶段性评测已完成；正式 Track 方案、资产冻结和 Release 尚未完成。

- [ ] 完成调研与方案审阅
- [ ] 冻结任务、数据、系统、指标与 evaluator 版本
- [X] 建立阶段性可执行 Benchmark 资产
- [X] 完成阶段性运行与结果复核
- [ ] 发布 RAG Track 报告
- [ ] 建立持续回归

## NLP2SQL

当前状态：Enron Eval 50 与 Spider Mix50 阶段性评测已完成；正式 Track 方案审批、资产冻结和 Release 尚未完成。

- [ ] 审阅并批准 Track 方案
- [ ] 冻结任务、数据、系统、指标与 evaluator 版本
- [X] 建立阶段性可执行 Benchmark 资产
- [X] 完成阶段性运行与结果复核
- [ ] 发布 NLP2SQL Track 报告
- [ ] 建立持续回归

方案批准前不采纳草稿中的详细实验承诺。

## 项目级里程碑

- **M0 / 本地结构基线**：五个根级 Track 和 `external/` 完成审阅。
- **M1 / 方案基线**：五个 Track 均有负责人和批准版本。
- **M2 / Track 试运行**：各 Track 形成首个可追溯 `run_id`。
- **M3 / 首个完整版本**：五份 Track 报告与综合索引通过 Release 冻结。
- **M4 / 持续回归**：任务与 evaluator 版本化，历史结果不被覆盖。
