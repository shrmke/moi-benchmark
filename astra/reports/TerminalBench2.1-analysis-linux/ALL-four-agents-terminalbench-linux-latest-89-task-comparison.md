# 四个 Agent 框架：Terminal-Bench 2.1 Linux 最新 89 题结果对比

生成日期：2026-09-11
范围：Astra、DSH、Hermes、PI 各自现有最新结果报告与配套 CSV；89 道相同任务（包含 tune-mjcf），共 356 条选中尝试记录。

## 结论摘要

| 指标                        | Astra  | DSH    | Hermes | PI     |
| --------------------------- | ------ | ------ | ------ | ------ |
| 任务覆盖                    | 89/89  | 89/89  | 89/89  | 89/89  |
| 有效 verifier（原报告口径） | 89/89  | 89/89  | 89/89  | 89/89  |
| verifier pass               | 48     | 49     | 34     | 52     |
| verifier no-pass            | 41     | 40     | 55     | 37     |
| verifier pass rate          | 53.93% | 55.06% | 38.20% | 58.43% |
| completed                   | 45     | 60     | 52     | 55     |
| timeout                     | 25     | 21     | 28     | 32     |
| failed                      | 19     | 0      | 0      | 0      |
| max_turn                    | 0      | 7      | 5      | 0      |
| max_token                   | 0      | 1      | 4      | 2      |

当前选中结果的任务通过数为 **PI 52/89（58.43%）、DSH 49/89（55.06%）、Astra 48/89（53.93%）、Hermes 34/89（38.20%）**。Astra 比 PI 少 4 题（4.49 个百分点），比 DSH 少 1 题（1.12 个百分点），比 Hermes 多 14 题（15.73 个百分点）。差值描述当前结果，不构成统计显著性或框架因果优势的结论。

四者共同通过 **24 题**；至少一个框架通过 **73 题**；四者均未通过 **16 题**。各自独有通过：Astra 8 题、DSH 7 题、Hermes 0 题、PI 8 题。并集表示任务互补性，不是任何单框架的得分。

Hermes通过率远低于预期，不排除模型、环境的短期波动情况。

| Astra 对照 | 双方通过 | 仅 Astra 通过 | 仅对方通过 | 双方未通过 |
| ---------- | -------- | ------------- | ---------- | ---------- |
| DSH        | 32       | 16            | 17         | 24         |
| Hermes     | 29       | 19            | 5          | 36         |
| PI         | 36       | 12            | 16         | 25         |

## 统计口径与配置

| 配置项                  | Astra                                                      | DSH                                                        | Hermes                                                     | PI                                                         |
| ----------------------- | ---------------------------------------------------------- | ---------------------------------------------------------- | ---------------------------------------------------------- | ---------------------------------------------------------- |
| 版本                    | astra 0.1.0                                                | 0.1.0rc6                                                   | v2026.7.20                                                 | 0.73.1                                                     |
| Astra 产品 commit（README 登记） | `969550b611ceba11653c4b2651079bcb85d1c24e` | — | — | — |
| 模型                    | glm-5.2(thinking:high)                                     | zai/glm-5.2                                                | zai/glm-5.2                                                | zai/glm-5.2                                                |
| 思考/采样配置           | thinking=high；                                            | thinking=high；temperature=0；                             | reasoning_effort=high；temperature=0                       | thinking=high                                              |
| 产品 timeout            | 数据集预算 × 1.0                                          | 数据集预算 × 1.0                                          | 数据集预算 × 1.0                                          | 数据集预算 × 1.0                                          |
| 并发（原报告）          | 串行                                                       | 上限 3                                                     | 上限 3                                                     | 上限 3                                                     |
| finished_at 范围（UTC） | 2026-09-09T07:49:05.827541Z 至 2026-09-11T07:49:26.415917Z | 2026-09-02T11:12:46.241815Z 至 2026-09-03T18:35:49.277983Z | 2026-09-08T07:32:25.013990Z 至 2026-09-09T06:07:44.908398Z | 2026-09-06T19:36:20.360963Z 至 2026-09-07T18:09:27.126454Z |

**硬件与系统环境**

| 环境项         | 配置 / 观测值                                                                                                 |
| -------------- | ------------------------------------------------------------------------------------------------------------- |
| 评测宿主机     | HOST-10-222-4-2；KVM 全虚拟化 Linux 虚拟机                                                                    |
| CPU 型号       | Intel Xeon Platinum 8255C @ 2.50 GHz（虚拟机报告的型号）                                                      |
| CPU 资源       | 8 vCPU；x86_64；虚拟机可见拓扑为 1 socket × 8 cores × 1 thread                                              |
| 内存           | 8,321,896,448 bytes，约 7.75 GiB（虚拟机可见总内存）                                                          |
| Swap           | 8,589,930,496 bytes，约 8.00 GiB                                                                              |
| 存储           | 评测目录位于 /dev/sda1，挂载点 /；文件系统容量约 485 GiB                                                      |
| 操作系统       | Ubuntu 22.04.5 LTS（Jammy Jellyfish）                                                                         |
| Linux 内核     | 5.15.0-190-generic                                                                                            |
| Docker Engine  | 29.1.3                                                                                                        |
| 容器运行环境   | Linux/x86_64；存储驱动 overlayfs；cgroup v2；cgroup driver=systemd                                            |
| 评测并发与调度 | 沿用上方各框架配置；原报告中的 6 CPU / 3 memory token 是调度预算，memory token 不是 GiB，也不等于整台 VM 容量 |

Astra 产品 commit 来源于本仓库 [Astra README](../../README.md) 的 Linux 评测版本登记；它是 Astra 产品源码版本，不是 moi-benchmark 报告或 runner 的提交号。本次文档更新未逐一重新核验 89 个 trial 的二进制构建溯源。

- 四份 CSV 各有 89 个唯一 task，任务集合与难度标注完全一致。每题每框架沿用源报告选中的最后一个尝试，不重新选择历史最好结果。
- Astra、DSH、Hermes、PI 源报告的数据集 commit 均为 5c8eadf1f393183288fa08b8f73ca9a469cc5e00；
- 其他三者原报告的并发还受 3 memory token / 6 CPU 调度约束。累计任务耗时不等于批次墙钟耗时，不能据此直接比较同并发吞吐量。
- Agent 结束状态与 verifier reward 分开统计。Astra failed 中的 budget_exhausted 不自动归入 max_turn/max_token；PI extract-moves-from-video 的 return_code=137 沿用原报告 timeout 分类。
- 本次只汇总现有报告与 CSV，不重新评测或重新判定失败原因。

## 运行结束状态

每格为 **任务数 / verifier pass / verifier no-pass**。

| 结束状态  | Astra       | DSH          | Hermes       | PI           |
| --------- | ----------- | ------------ | ------------ | ------------ |
| completed | 45 / 39 / 6 | 60 / 44 / 16 | 52 / 32 / 20 | 55 / 43 / 12 |
| timeout   | 25 / 2 / 23 | 21 / 4 / 17  | 28 / 2 / 26  | 32 / 9 / 23  |
| failed    | 19 / 7 / 12 | 0 / 0 / 0    | 0 / 0 / 0    | 0 / 0 / 0    |
| max_turn  | 0 / 0 / 0   | 7 / 1 / 6    | 5 / 0 / 5    | 0 / 0 / 0    |
| max_token | 0 / 0 / 0   | 1 / 0 / 1    | 4 / 0 / 4    | 2 / 0 / 2    |

Astra 的 19 个 failed 中仍有 7 题通过，PI 的 32 个 timeout 中有 9 题通过；结束状态不能代替任务得分。max_turn/max_token 的 0 仅表示源记录未归入该类，不证明不存在未细分的预算耗尽。

## Verifier 条目进度

| 指标                       | Astra | DSH   | Hermes | PI    |
| -------------------------- | ----- | ----- | ------ | ----- |
| 有 passed/total 明细的任务 | 89/89 | 89/89 | 89/89  | 89/89 |
| verifier 条目累计通过      | 229   | 222   | 194    | 228   |
| verifier 条目累计执行      | 308   | 308   | 308    | 308   |
| 条目级通过比例（诊断项）   | 74.4% | 72.1% | 63.0%  | 74.0% |

Astra 条目通过数为 229，略高于 PI 的 228，但完整任务通过数更少。各题条目数量与部分通过分布不同，条目级比例不能替代任务级二元 reward。

## 按作者难度

| 难度   | Astra          | DSH            | Hermes         | PI             |
| ------ | -------------- | -------------- | -------------- | -------------- |
| Easy   | 3/4（75.0%）   | 4/4（100.0%）  | 2/4（50.0%）   | 3/4（75.0%）   |
| Medium | 34/55（61.8%） | 31/55（56.4%） | 21/55（38.2%） | 35/55（63.6%） |
| Hard   | 11/30（36.7%） | 14/30（46.7%） | 11/30（36.7%） | 14/30（46.7%） |

Medium 上 PI 35/55、Astra 34/55，差 1 题；Hard 上 PI 与 DSH 均为 14/30，Astra 与 Hermes 均为 11/30。Easy 只有 4 题，百分比需结合小样本量理解。

## 时间、模型响应、Tool 与 Token

| 指标                        | 框架   | 覆盖  | 总计    | 中位数    | P90       |
| --------------------------- | ------ | ----- | ------- | --------- | --------- |
| 端到端时间                  | Astra  | 89/89 | 25.79 h | 14.12 min | 32.58 min |
| 端到端时间                  | DSH    | 89/89 | 23.39 h | 14.66 min | 31.12 min |
| 端到端时间                  | Hermes | 89/89 | 26.14 h | 16.07 min | 31.86 min |
| 端到端时间                  | PI     | 89/89 | 28.88 h | 16.19 min | 36.40 min |
| Agent 执行时间              | Astra  | 89/89 | 21.16 h | 11.18 min | 30.13 min |
| Agent 执行时间              | DSH    | 89/89 | 19.02 h | 10.75 min | 27.16 min |
| Agent 执行时间              | Hermes | 89/89 | 22.77 h | 14.29 min | 30.43 min |
| Agent 执行时间              | PI     | 89/89 | 24.52 h | 15.12 min | 30.38 min |
| Verifier 时间               | Astra  | 89/89 | 3.61 h  | 0.74 min  | 6.44 min  |
| Verifier 时间               | DSH    | 89/89 | 3.01 h  | 0.54 min  | 5.61 min  |
| Verifier 时间               | Hermes | 89/89 | 2.61 h  | 0.60 min  | 3.87 min  |
| Verifier 时间               | PI     | 89/89 | 3.26 h  | 0.61 min  | 6.37 min  |
| 模型响应/API calls（trace） | Astra  | 89/89 | 1,571   | 15.0      | 33.2      |
| 模型响应/API calls（trace） | DSH    | 89/89 | 1,954   | 19.0      | 42.4      |
| 模型响应/API calls（trace） | Hermes | 89/89 | 2,521   | 23.0      | 72.0      |
| 模型响应/API calls（trace） | PI     | 89/89 | 1,968   | 17.0      | 45.2      |
| Tool calls（trace）         | Astra  | 89/89 | 1,899   | 19.0      | 43.0      |
| Tool calls（trace）         | DSH    | 89/89 | 2,050   | 20.0      | 48.4      |
| Tool calls（trace）         | Hermes | 89/89 | 2,809   | 23.0      | 81.2      |
| Tool calls（trace）         | PI     | 89/89 | 2,262   | 19.0      | 54.0      |

| Token 分量 / 统计口径       | Astra               | DSH                 | Hermes              | PI                  |
| --------------------------- | ------------------- | ------------------- | ------------------- | ------------------- |
| fresh input                 | 10,186,869（89/89） | 7,992,685（89/89）  | 5,651,935（89/89）  | 16,216,107（88/89） |
| cache read                  | 56,577,024（89/89） | 24,351,872（89/89） | 71,739,840（89/89） | 47,470,464（88/89） |
| cache write（独立字段）     | 0（89/89）          | 缺失（0/89）        | 0（89/89）          | 0（88/89）          |
| output                      | 2,625,096（89/89）  | 1,533,489（89/89）  | 1,393,760（89/89）  | 3,033,741（88/89）  |
| reasoning（单列，不再叠加） | 缺失（0/89）        | 缺失（0/89）        | 842,813（89/89）    | 缺失（0/89）        |
| 已观测 Token 合计           | 69,388,989（89/89） | 33,878,046（89/89） | 78,785,535（89/89） | 66,720,312（88/89） |

DSH 的累计 Agent 时间与已观测 Token 最少；PI 通过数最高且累计 Agent 时间最长。Astra 模型响应与工具调用计数最少，但统计粒度不同，不能直接归因于调用效率更高。

- Astra：63 题采用完整原生 turn 汇总，26 题采用同 session 数据库 trace，补充部分限定 Agent execution 窗口内已结束的 primary_agent invocation。Token 为已观测下界；模型计数混合 llm_rounds 与 succeeded invocation，不是统一 HTTP 请求数；工具调用计请求，不保证成功执行。
- DSH：原生 dsh-events.jsonl 去重 assistant/message.data.usage，89/89 有已完成 usage，超时在途请求未计入，因此为下界。cache_write 独立字段缺失，合计沿用 fresh input + cache read + output，不把该缺失值认定为 0。
- Hermes：原生 hermes-session.jsonl 汇总，89/89 有 usage；reasoning 单列，不再次加入合计，避免重复计数。
- PI：原生 assistant message usage，88/89 有 usage；adaptive-rejection-sampler Token 保持缺失，模型/工具列沿用源 CSV 的 0，仅表示 trace 未观测到完成响应，不证明真实消耗为零。
- Token 合计沿用各源报告，通常为 fresh input + cache read + output；Astra 另计 cache write（本批为 0）。不重复叠加 reasoning，不用 Harbor 空值补齐；没有按 Token 推算费用，缓存价格与未知请求消耗尚未统一。
- Astra 有 23 个 session 在 Agent execution 结束后仍有成功模型 invocation，额外观测到 16,277,640 Token，不计入本表。其他框架未报告同等范围的结束后审计，不能将未报告解释为零。

## 每题结果与 verifier 进度

**逐题结果对照（89 题）**

框架单元格依次为 **Reward；Verifier passed/total；结束状态**。

| Task                             | 难度   | Astra               | DSH               | Hermes             | PI                |
| -------------------------------- | ------ | ------------------- | ----------------- | ------------------ | ----------------- |
| adaptive-rejection-sampler       | Medium | 1；9/9；timeout     | 1；9/9；timeout   | 0；0/9；timeout    | 0；0/9；timeout   |
| bn-fit-modify                    | Hard   | 1；9/9；completed   | 1；9/9；completed | 1；9/9；completed  | 1；9/9；completed |
| break-filter-js-from-html        | Medium | 1；1/1；completed   | 1；1/1；completed | 1；1/1；completed  | 1；1/1；completed |
| build-cython-ext                 | Medium | 0；10/11；completed | 0；9/11；max_turn | 0；10/11；max_turn | 1；11/11；timeout |
| build-pmars                      | Medium | 1；4/4；failed      | 1；4/4；completed | 1；4/4；completed  | 1；4/4；completed |
| build-pov-ray                    | Medium | 0；1/3；completed   | 1；3/3；max_turn  | 0；2/3；completed  | 0；2/3；completed |
| caffe-cifar-10                   | Medium | 0；2/6；failed      | 0；2/6；max_turn  | 0；1/6；max_turn   | 1；6/6；completed |
| cancel-async-tasks               | Hard   | 0；5/6；completed   | 1；6/6；completed | 0；5/6；timeout    | 1；6/6；completed |
| chess-best-move                  | Medium | 0；0/1；timeout     | 0；0/1；timeout   | 0；0/1；timeout    | 0；0/1；timeout   |
| circuit-fibsqrt                  | Hard   | 0；2/3；timeout     | 1；3/3；completed | 0；2/3；max_token  | 0；2/3；max_token |
| cobol-modernization              | Easy   | 1；3/3；completed   | 1；3/3；completed | 0；1/3；timeout    | 0；1/3；timeout   |
| code-from-image                  | Medium | 0；0/2；timeout     | 1；2/2；timeout   | 1；2/2；completed  | 1；2/2；completed |
| compile-compcert                 | Medium | 0；0/3；timeout     | 0；0/3；timeout   | 0；0/3；timeout    | 1；3/3；timeout   |
| configure-git-webserver          | Hard   | 1；1/1；completed   | 0；0/1；completed | 0；0/1；completed  | 0；0/1；completed |
| constraints-scheduling           | Medium | 1；3/3；completed   | 1；3/3；completed | 1；3/3；completed  | 1；3/3；completed |
| count-dataset-tokens             | Medium | 1；1/1；completed   | 0；0/1；timeout   | 0；0/1；timeout    | 0；0/1；timeout   |
| crack-7z-hash                    | Medium | 0；0/2；failed      | 1；2/2；completed | 1；2/2；completed  | 1；2/2；completed |
| custom-memory-heap-crash         | Medium | 1；6/6；completed   | 1；6/6；completed | 0；5/6；max_turn   | 1；6/6；completed |
| db-wal-recovery                  | Medium | 1；7/7；completed   | 1；7/7；completed | 0；0/7；timeout    | 1；7/7；completed |
| distribution-search              | Medium | 1；4/4；completed   | 1；4/4；completed | 1；4/4；completed  | 1；4/4；completed |
| dna-assembly                     | Hard   | 0；0/1；timeout     | 1；1/1；completed | 1；1/1；completed  | 0；0/1；timeout   |
| dna-insert                       | Medium | 0；0/1；completed   | 1；1/1；completed | 0；0/1；completed  | 1；1/1；completed |
| extract-elf                      | Medium | 0；0/2；timeout     | 0；0/2；completed | 0；0/2；timeout    | 0；0/2；timeout   |
| extract-moves-from-video         | Hard   | 0；0/2；failed      | 0；0/2；timeout   | 0；0/2；timeout    | 0；0/2；timeout   |
| feal-differential-cryptanalysis  | Hard   | 1；1/1；failed      | 1；1/1；completed | 1；1/1；completed  | 1；1/1；completed |
| feal-linear-cryptanalysis        | Hard   | 1；1/1；completed   | 1；1/1；completed | 0；0/1；max_token  | 1；1/1；completed |
| filter-js-from-html              | Medium | 0；1/2；failed      | 0；1/2；completed | 0；1/2；completed  | 0；1/2；timeout   |
| financial-document-processor     | Medium | 1；7/7；completed   | 1；7/7；completed | 1；7/7；completed  | 1；7/7；completed |
| fix-code-vulnerability           | Hard   | 1；6/6；completed   | 1；6/6；completed | 1；6/6；completed  | 1；6/6；completed |
| fix-git                          | Easy   | 1；2/2；completed   | 1；2/2；completed | 1；2/2；completed  | 1；2/2；completed |
| fix-ocaml-gc                     | Hard   | 1；1/1；completed   | 0；0/1；completed | 1；1/1；timeout    | 1；1/1；timeout   |
| gcode-to-text                    | Medium | 0；0/2；timeout     | 0；0/2；timeout   | 0；0/2；timeout    | 0；0/2；timeout   |
| git-leak-recovery                | Medium | 1；5/5；completed   | 1；5/5；completed | 1；5/5；completed  | 1；5/5；completed |
| git-multibranch                  | Medium | 1；1/1；failed      | 0；0/1；completed | 0；0/1；completed  | 0；0/1；completed |
| gpt2-codegolf                    | Hard   | 0；0/1；timeout     | 0；0/1；timeout   | 0；0/1；timeout    | 0；0/1；timeout   |
| headless-terminal                | Medium | 1；7/7；completed   | 1；7/7；completed | 0；6/7；completed  | 0；0/7；timeout   |
| hf-model-inference               | Medium | 1；4/4；completed   | 0；1/4；completed | 0；2/4；completed  | 0；1/4；completed |
| install-windows-3.11             | Hard   | 0；2/4；failed      | 0；1/4；completed | 0；1/4；completed  | 1；4/4；timeout   |
| kv-store-grpc                    | Medium | 1；7/7；completed   | 0；5/7；completed | 0；5/7；completed  | 0；5/7；completed |
| large-scale-text-editing         | Medium | 1；5/5；completed   | 1；5/5；completed | 1；5/5；completed  | 1；5/5；timeout   |
| largest-eigenval                 | Medium | 1；3/3；timeout     | 0；2/3；timeout   | 0；2/3；completed  | 1；3/3；timeout   |
| llm-inference-batching-scheduler | Hard   | 0；5/6；timeout     | 1；6/6；completed | 1；6/6；completed  | 0；1/6；timeout   |
| log-summary-date-ranges          | Medium | 1；2/2；completed   | 1；2/2；completed | 1；2/2；completed  | 1；2/2；completed |
| mailman                          | Medium | 0；1/3；failed      | 0；0/3；max_turn  | 0；2/3；completed  | 1；3/3；completed |
| make-doom-for-mips               | Hard   | 0；0/3；timeout     | 0；0/3；timeout   | 0；0/3；timeout    | 0；0/3；timeout   |
| make-mips-interpreter            | Hard   | 0；0/3；timeout     | 0；0/3；max_turn  | 0；0/3；timeout    | 0；0/3；timeout   |
| mcmc-sampling-stan               | Hard   | 0；2/6；timeout     | 1；6/6；completed | 0；2/6；timeout    | 1；6/6；timeout   |
| merge-diff-arc-agi-task          | Medium | 1；5/5；completed   | 1；5/5；completed | 1；5/5；completed  | 1；5/5；completed |
| model-extraction-relu-logits     | Hard   | 0；0/1；failed      | 0；0/1；completed | 0；0/1；timeout    | 0；0/1；timeout   |
| modernize-scientific-stack       | Medium | 1；2/2；completed   | 1；2/2；completed | 1；2/2；completed  | 1；2/2；completed |
| mteb-leaderboard                 | Medium | 0；0/2；failed      | 0；0/2；max_turn  | 0；0/2；max_turn   | 1；2/2；completed |
| mteb-retrieve                    | Medium | 0；1/2；completed   | 0；1/2；completed | 0；0/2；max_turn   | 0；1/2；completed |
| multi-source-data-merger         | Medium | 1；3/3；completed   | 1；3/3；completed | 1；3/3；completed  | 1；3/3；completed |
| nginx-request-logging            | Medium | 1；8/8；completed   | 0；3/8；completed | 0；3/8；completed  | 0；3/8；completed |
| openssl-selfsigned-cert          | Medium | 1；6/6；completed   | 1；6/6；completed | 0；5/6；completed  | 1；6/6；completed |
| overfull-hbox                    | Easy   | 0；3/4；timeout     | 1；4/4；completed | 0；3/4；timeout    | 1；4/4；completed |
| password-recovery                | Hard   | 1；2/2；completed   | 0；0/2；timeout   | 1；2/2；completed  | 1；2/2；completed |
| path-tracing                     | Hard   | 0；0/5；timeout     | 0；0/5；timeout   | 0；0/5；timeout    | 0；0/5；timeout   |
| path-tracing-reverse             | Hard   | 0；0/3；timeout     | 0；0/3；timeout   | 0；0/3；timeout    | 1；3/3；timeout   |
| polyglot-c-py                    | Medium | 1；1/1；completed   | 1；1/1；completed | 1；1/1；completed  | 1；1/1；completed |
| polyglot-rust-c                  | Hard   | 1；1/1；failed      | 1；1/1；completed | 1；1/1；completed  | 1；1/1；completed |
| portfolio-optimization           | Medium | 1；4/4；completed   | 1；4/4；completed | 0；1/4；completed  | 1；4/4；completed |
| protein-assembly                 | Hard   | 0；0/1；timeout     | 0；0/1；timeout   | 0；0/1；timeout    | 0；0/1；timeout   |
| prove-plus-comm                  | Easy   | 1；4/4；completed   | 1；4/4；completed | 1；4/4；completed  | 1；4/4；completed |
| pypi-server                      | Medium | 1；1/1；completed   | 0；0/1；completed | 0；0/1；completed  | 0；0/1；completed |
| pytorch-model-cli                | Medium | 1；6/6；completed   | 0；5/6；completed | 1；6/6；completed  | 0；5/6；completed |
| pytorch-model-recovery           | Medium | 1；5/5；completed   | 1；5/5；completed | 1；5/5；completed  | 1；5/5；completed |
| qemu-alpine-ssh                  | Medium | 0；0/1；failed      | 0；0/1；timeout   | 0；0/1；timeout    | 0；0/1；timeout   |
| qemu-startup                     | Medium | 1；1/1；completed   | 0；0/1；completed | 0；0/1；completed  | 1；1/1；timeout   |
| query-optimize                   | Medium | 0；5/6；failed      | 1；6/6；completed | 0；5/6；completed  | 0；5/6；completed |
| raman-fitting                    | Medium | 0；1/3；timeout     | 0；0/3；timeout   | 0；1/3；timeout    | 1；3/3；completed |
| regex-chess                      | Hard   | 0；0/4；failed      | 0；0/4；max_token | 0；0/4；max_token  | 0；0/4；max_token |
| regex-log                        | Medium | 1；1/1；failed      | 1；1/1；completed | 1；1/1；timeout    | 1；1/1；completed |
| reshard-c4-data                  | Medium | 1；1/1；failed      | 0；0/1；completed | 0；0/1；max_token  | 0；0/1；completed |
| rstan-to-pystan                  | Medium | 0；1/6；failed      | 1；6/6；completed | 1；6/6；completed  | 1；6/6；completed |
| sam-cell-seg                     | Hard   | 1；9/9；completed   | 0；1/9；max_turn  | 1；9/9；completed  | 1；9/9；completed |
| sanitize-git-repo                | Medium | 1；3/3；completed   | 0；2/3；completed | 1；3/3；completed  | 1；3/3；completed |
| schemelike-metacircular-eval     | Medium | 0；0/1；timeout     | 1；1/1；timeout   | 0；0/1；timeout    | 0；0/1；timeout   |
| sparql-university                | Hard   | 1；3/3；completed   | 1；3/3；completed | 1；3/3；completed  | 1；3/3；completed |
| sqlite-db-truncate               | Medium | 1；1/1；failed      | 1；1/1；completed | 1；1/1；completed  | 1；1/1；completed |
| sqlite-with-gcov                 | Medium | 1；3/3；completed   | 0；0/3；timeout   | 0；2/3；completed  | 1；3/3；completed |
| torch-pipeline-parallelism       | Hard   | 0；2/3；timeout     | 0；2/3；timeout   | 0；0/3；timeout    | 0；0/3；timeout   |
| torch-tensor-parallelism         | Hard   | 1；3/3；completed   | 1；3/3；completed | 1；3/3；completed  | 1；3/3；completed |
| train-fasttext                   | Hard   | 0；1/2；timeout     | 0；1/2；timeout   | 0；1/2；timeout    | 0；0/2；timeout   |
| tune-mjcf                        | Medium | 0；3/4；timeout     | 1；4/4；timeout   | 0；3/4；timeout    | 0；3/4；timeout   |
| video-processing                 | Hard   | 0；3/5；completed   | 1；5/5；completed | 0；3/5；completed  | 0；4/5；completed |
| vulnerable-secret                | Medium | 1；3/3；completed   | 1；3/3；completed | 1；3/3；completed  | 1；3/3；completed |
| winning-avg-corewars             | Medium | 0；2/3；timeout     | 1；3/3；completed | 0；1/3；completed  | 1；3/3；completed |
| write-compressor                 | Hard   | 0；0/3；timeout     | 1；3/3；completed | 0；0/3；timeout    | 0；2/3；timeout   |

**逐题时间、模型响应、Tool 与 Token（356 条记录）**

时间单位为分钟；Token 总量及缺失含义沿用上一节。原始路径、结束时间、结束原因与 trace 来源可在配套源 CSV 中按 task 追溯。

| Task                             | 框架   | 模型响应 | Tool calls | Agent min | Verifier min | 端到端 min | Fresh input | Cache read | Output  | Token 合计 |
| -------------------------------- | ------ | -------- | ---------- | --------- | ------------ | ---------- | ----------- | ---------- | ------- | ---------- |
| adaptive-rejection-sampler       | Astra  | 14       | 14         | 15.12     | 0.67         | 17.25      | 153,028     | 439,808    | 39,001  | 631,837    |
| adaptive-rejection-sampler       | DSH    | 6        | 6          | 15.12     | 0.59         | 16.72      | 88,231      | 117,248    | 42,146  | 247,625    |
| adaptive-rejection-sampler       | Hermes | 3        | 4          | 15.14     | 0.62         | 16.24      | 1,858       | 42,752     | 40,355  | 84,965     |
| adaptive-rejection-sampler       | PI     | 0        | 0          | 15.12     | 0.58         | 16.80      | 缺失        | 缺失       | 缺失    | 缺失       |
| bn-fit-modify                    | Astra  | 23       | 22         | 6.04      | 1.07         | 7.54       | 57,265      | 897,152    | 8,284   | 962,701    |
| bn-fit-modify                    | DSH    | 17       | 16         | 14.15     | 7.48         | 22.72      | 26,918      | 57,024     | 5,984   | 89,926     |
| bn-fit-modify                    | Hermes | 28       | 27         | 20.06     | 5.33         | 25.87      | 58,396      | 516,032    | 9,693   | 584,121    |
| bn-fit-modify                    | PI     | 16       | 16         | 5.66      | 0.67         | 6.93       | 67,838      | 108,352    | 11,024  | 187,214    |
| break-filter-js-from-html        | Astra  | 12       | 11         | 6.30      | 0.64         | 7.43       | 42,374      | 392,704    | 10,087  | 445,165    |
| break-filter-js-from-html        | DSH    | 18       | 19         | 6.13      | 0.67         | 7.97       | 39,341      | 127,424    | 10,740  | 177,505    |
| break-filter-js-from-html        | Hermes | 32       | 32         | 11.82     | 0.58         | 12.87      | 66,201      | 596,736    | 28,735  | 691,672    |
| break-filter-js-from-html        | PI     | 17       | 18         | 4.11      | 0.62         | 5.29       | 60,945      | 64,512     | 9,196   | 134,653    |
| build-cython-ext                 | Astra  | 45       | 58         | 12.48     | 0.22         | 13.23      | 305,198     | 1,856,512  | 14,592  | 2,176,302  |
| build-cython-ext                 | DSH    | 50       | 50         | 11.59     | 0.20         | 12.79      | 46,723      | 430,336    | 6,021   | 483,080    |
| build-cython-ext                 | Hermes | 90       | 104        | 14.29     | 0.18         | 15.12      | 80,408      | 3,116,288  | 8,549   | 3,205,245  |
| build-cython-ext                 | PI     | 45       | 59         | 15.13     | 0.23         | 15.93      | 214,495     | 1,179,776  | 28,480  | 1,422,751  |
| build-pmars                      | Astra  | 35       | 39         | 8.66      | 0.40         | 9.55       | 166,879     | 1,308,288  | 8,353   | 1,483,520  |
| build-pmars                      | DSH    | 26       | 26         | 4.06      | 0.37         | 5.08       | 25,419      | 129,728    | 3,894   | 159,041    |
| build-pmars                      | Hermes | 28       | 33         | 4.99      | 0.41         | 5.86       | 26,139      | 478,976    | 2,909   | 508,024    |
| build-pmars                      | PI     | 25       | 33         | 6.37      | 0.40         | 7.37       | 134,099     | 293,376    | 14,592  | 442,067    |
| build-pov-ray                    | Astra  | 33       | 43         | 8.25      | 0.75         | 9.75       | 297,151     | 1,719,104  | 9,192   | 2,025,447  |
| build-pov-ray                    | DSH    | 50       | 54         | 10.57     | 4.46         | 16.73      | 208,583     | 678,592    | 7,182   | 894,357    |
| build-pov-ray                    | Hermes | 54       | 81         | 20.29     | 1.33         | 22.26      | 80,938      | 1,608,640  | 8,800   | 1,698,378  |
| build-pov-ray                    | PI     | 91       | 90         | 31.31     | 0.74         | 33.16      | 542,685     | 4,134,464  | 64,559  | 4,741,708  |
| caffe-cifar-10                   | Astra  | 47       | 62         | 18.10     | 2.61         | 21.66      | 267,323     | 1,970,560  | 17,454  | 2,255,337  |
| caffe-cifar-10                   | DSH    | 50       | 50         | 25.88     | 0.34         | 27.14      | 114,442     | 448,512    | 14,217  | 577,171    |
| caffe-cifar-10                   | Hermes | 90       | 90         | 37.17     | 0.41         | 38.20      | 113,483     | 2,390,528  | 8,828   | 2,512,839  |
| caffe-cifar-10                   | PI     | 32       | 49         | 51.69     | 1.02         | 53.73      | 468,141     | 1,100,544  | 70,800  | 1,639,485  |
| cancel-async-tasks               | Astra  | 7        | 6          | 1.86      | 0.60         | 2.90       | 15,060      | 245,248    | 3,825   | 264,133    |
| cancel-async-tasks               | DSH    | 12       | 11         | 3.89      | 0.54         | 4.97       | 29,031      | 80,768     | 11,518  | 121,317    |
| cancel-async-tasks               | Hermes | 72       | 72         | 15.14     | 0.63         | 16.24      | 76,237      | 1,939,392  | 37,240  | 2,052,869  |
| cancel-async-tasks               | PI     | 18       | 20         | 8.71      | 0.70         | 9.98       | 112,677     | 295,680    | 24,995  | 433,352    |
| chess-best-move                  | Astra  | 16       | 18         | 15.12     | 0.49         | 16.07      | 146,050     | 547,072    | 45,751  | 738,873    |
| chess-best-move                  | DSH    | 27       | 27         | 15.12     | 0.49         | 16.47      | 142,379     | 350,720    | 30,412  | 523,511    |
| chess-best-move                  | Hermes | 22       | 22         | 15.36     | 0.45         | 16.28      | 18,929      | 397,376    | 26,106  | 442,411    |
| chess-best-move                  | PI     | 11       | 12         | 15.13     | 0.49         | 16.18      | 47,820      | 40,320     | 17,284  | 105,424    |
| circuit-fibsqrt                  | Astra  | 8        | 6          | 60.10     | 0.63         | 61.16      | 66,719      | 244,672    | 236,373 | 547,764    |
| circuit-fibsqrt                  | DSH    | 9        | 10         | 18.56     | 1.66         | 21.21      | 223,302     | 282,368    | 69,017  | 574,687    |
| circuit-fibsqrt                  | Hermes | 1        | 2          | 30.61     | 0.74         | 31.81      | 335         | 14,080     | 170     | 14,585     |
| circuit-fibsqrt                  | PI     | 3        | 2          | 19.31     | 0.33         | 20.21      | 8,677       | 2,560      | 67,027  | 78,264     |
| cobol-modernization              | Astra  | 24       | 24         | 14.97     | 0.38         | 15.81      | 146,423     | 894,464    | 44,810  | 1,085,697  |
| cobol-modernization              | DSH    | 21       | 21         | 7.26      | 0.35         | 8.39       | 71,357      | 181,824    | 18,334  | 271,515    |
| cobol-modernization              | Hermes | 9        | 14         | 15.13     | 0.40         | 15.99      | 11,593      | 149,824    | 17,953  | 179,370    |
| cobol-modernization              | PI     | 14       | 16         | 15.13     | 0.47         | 16.17      | 141,483     | 113,280    | 49,037  | 303,800    |
| code-from-image                  | Astra  | 34       | 37         | 20.13     | 0.74         | 21.27      | 90,823      | 1,358,016  | 21,021  | 1,469,860  |
| code-from-image                  | DSH    | 40       | 40         | 20.11     | 0.27         | 20.97      | 211,875     | 940,800    | 27,644  | 1,180,319  |
| code-from-image                  | Hermes | 5        | 4          | 1.50      | 0.37         | 2.32       | 16,984      | 57,600     | 575     | 75,159     |
| code-from-image                  | PI     | 19       | 18         | 5.87      | 0.30         | 6.73       | 16,594      | 43,008     | 4,270   | 63,872     |
| compile-compcert                 | Astra  | 30       | 43         | 40.18     | 2.33         | 44.22      | 137,546     | 1,102,080  | 12,323  | 1,251,949  |
| compile-compcert                 | DSH    | 27       | 29         | 40.13     | 0.35         | 41.77      | 32,648      | 99,840     | 2,363   | 134,851    |
| compile-compcert                 | Hermes | 43       | 45         | 40.42     | 0.39         | 41.74      | 47,032      | 756,288    | 3,284   | 806,604    |
| compile-compcert                 | PI     | 53       | 59         | 40.09     | 0.33         | 41.51      | 306,538     | 1,128,768  | 40,418  | 1,475,724  |
| configure-git-webserver          | Astra  | 20       | 19         | 3.97      | 1.61         | 6.61       | 38,024      | 649,024    | 4,333   | 691,381    |
| configure-git-webserver          | DSH    | 16       | 15         | 2.26      | 0.32         | 3.72       | 12,398      | 59,456     | 1,282   | 73,136     |
| configure-git-webserver          | Hermes | 13       | 12         | 1.88      | 0.35         | 2.70       | 3,432       | 191,232    | 1,048   | 195,712    |
| configure-git-webserver          | PI     | 15       | 17         | 10.62     | 0.41         | 11.96      | 73,902      | 121,984    | 21,289  | 217,175    |
| constraints-scheduling           | Astra  | 7        | 7          | 2.37      | 0.58         | 3.38       | 25,464      | 222,720    | 5,684   | 253,868    |
| constraints-scheduling           | DSH    | 6        | 7          | 2.39      | 0.58         | 3.60       | 9,319       | 25,728     | 5,427   | 40,474     |
| constraints-scheduling           | Hermes | 6        | 7          | 2.54      | 0.56         | 3.55       | 19,757      | 100,160    | 7,497   | 127,414    |
| constraints-scheduling           | PI     | 6        | 8          | 4.84      | 0.71         | 6.13       | 21,411      | 57,600     | 16,546  | 95,557     |
| count-dataset-tokens             | Astra  | 27       | 33         | 11.18     | 0.38         | 12.07      | 146,732     | 1,007,872  | 12,402  | 1,167,006  |
| count-dataset-tokens             | DSH    | 33       | 33         | 15.12     | 0.39         | 16.08      | 74,549      | 212,608    | 9,849   | 297,006    |
| count-dataset-tokens             | Hermes | 23       | 23         | 15.34     | 0.76         | 16.58      | 49,960      | 372,544    | 2,610   | 425,114    |
| count-dataset-tokens             | PI     | 16       | 16         | 15.12     | 0.39         | 16.04      | 35,266      | 85,248     | 7,859   | 128,373    |
| crack-7z-hash                    | Astra  | 26       | 32         | 25.90     | 0.44         | 27.35      | 109,004     | 864,448    | 29,698  | 1,003,150  |
| crack-7z-hash                    | DSH    | 28       | 28         | 22.72     | 0.59         | 25.10      | 49,582      | 236,352    | 5,165   | 291,099    |
| crack-7z-hash                    | Hermes | 66       | 65         | 24.51     | 0.37         | 25.50      | 124,277     | 1,325,184  | 6,781   | 1,456,242  |
| crack-7z-hash                    | PI     | 17       | 18         | 8.81      | 0.40         | 10.14      | 33,676      | 44,928     | 1,387   | 79,991     |
| custom-memory-heap-crash         | Astra  | 31       | 44         | 17.28     | 1.04         | 18.76      | 193,930     | 1,188,288  | 46,791  | 1,429,009  |
| custom-memory-heap-crash         | DSH    | 32       | 32         | 10.51     | 1.02         | 14.68      | 114,115     | 421,504    | 23,687  | 559,306    |
| custom-memory-heap-crash         | Hermes | 90       | 98         | 18.95     | 0.69         | 20.16      | 207,910     | 4,020,992  | 20,614  | 4,249,516  |
| custom-memory-heap-crash         | PI     | 38       | 54         | 21.13     | 1.52         | 23.19      | 365,477     | 1,053,504  | 54,275  | 1,473,256  |
| db-wal-recovery                  | Astra  | 11       | 10         | 2.02      | 0.56         | 3.04       | 29,299      | 385,152    | 2,482   | 416,933    |
| db-wal-recovery                  | DSH    | 8        | 9          | 1.45      | 0.62         | 2.96       | 18,024      | 31,168     | 1,374   | 50,566     |
| db-wal-recovery                  | Hermes | 83       | 83         | 15.13     | 0.52         | 16.10      | 110,217     | 2,233,344  | 27,255  | 2,370,816  |
| db-wal-recovery                  | PI     | 11       | 13         | 2.46      | 0.76         | 3.81       | 53,691      | 63,424     | 6,878   | 123,993    |
| distribution-search              | Astra  | 5        | 4          | 3.30      | 6.49         | 10.25      | 117,930     | 73,856     | 8,687   | 200,473    |
| distribution-search              | DSH    | 6        | 5          | 3.17      | 3.60         | 7.69       | 20,509      | 17,408     | 8,350   | 46,267     |
| distribution-search              | Hermes | 8        | 7          | 8.52      | 0.51         | 9.48       | 4,990       | 123,648    | 33,226  | 161,864    |
| distribution-search              | PI     | 9        | 8          | 6.78      | 0.31         | 7.67       | 76,636      | 133,504    | 25,593  | 235,733    |
| dna-assembly                     | Astra  | 14       | 21         | 30.10     | 0.65         | 31.74      | 106,948     | 504,768    | 102,378 | 714,094    |
| dna-assembly                     | DSH    | 20       | 24         | 14.35     | 0.45         | 15.85      | 141,913     | 424,640    | 47,141  | 613,694    |
| dna-assembly                     | Hermes | 28       | 30         | 22.48     | 0.60         | 23.54      | 140,907     | 1,059,776  | 60,130  | 1,260,813  |
| dna-assembly                     | PI     | 6        | 9          | 30.12     | 0.46         | 31.70      | 95,349      | 61,568     | 101,003 | 257,920    |
| dna-insert                       | Astra  | 15       | 20         | 7.71      | 0.45         | 9.82       | 130,018     | 463,040    | 19,056  | 612,114    |
| dna-insert                       | DSH    | 21       | 20         | 6.33      | 0.35         | 7.60       | 67,766      | 130,496    | 13,210  | 211,472    |
| dna-insert                       | Hermes | 23       | 22         | 5.95      | 0.63         | 7.03       | 39,753      | 440,896    | 14,957  | 495,606    |
| dna-insert                       | PI     | 12       | 14         | 14.37     | 0.44         | 16.76      | 133,787     | 223,360    | 39,995  | 397,142    |
| extract-elf                      | Astra  | 7        | 10         | 15.11     | 0.52         | 16.11      | 63,286      | 214,528    | 46,311  | 324,125    |
| extract-elf                      | DSH    | 10       | 14         | 4.35      | 0.51         | 6.18       | 25,264      | 97,920     | 11,301  | 134,485    |
| extract-elf                      | Hermes | 11       | 17         | 15.16     | 0.60         | 16.40      | 118,615     | 279,936    | 27,423  | 425,974    |
| extract-elf                      | PI     | 12       | 23         | 15.14     | 0.70         | 16.51      | 127,016     | 70,336     | 48,477  | 245,829    |
| extract-moves-from-video         | Astra  | 35       | 37         | 27.08     | 0.70         | 29.05      | 161,522     | 1,210,688  | 28,208  | 1,400,418  |
| extract-moves-from-video         | DSH    | 24       | 27         | 30.11     | 0.40         | 31.65      | 59,940      | 124,032    | 7,783   | 191,755    |
| extract-moves-from-video         | Hermes | 25       | 32         | 30.16     | 0.36         | 31.02      | 49,224      | 530,368    | 2,592   | 582,184    |
| extract-moves-from-video         | PI     | 30       | 32         | 19.24     | 0.44         | 21.33      | 52,048      | 132,672    | 6,841   | 191,561    |
| feal-differential-cryptanalysis  | Astra  | 7        | 5          | 5.56      | 2.14         | 8.13       | 39,464      | 245,120    | 18,997  | 303,581    |
| feal-differential-cryptanalysis  | DSH    | 8        | 7          | 10.17     | 0.87         | 11.66      | 56,010      | 9,600      | 11,169  | 76,779     |
| feal-differential-cryptanalysis  | Hermes | 7        | 6          | 5.75      | 1.01         | 7.23       | 8,010       | 109,632    | 19,422  | 137,064    |
| feal-differential-cryptanalysis  | PI     | 15       | 14         | 8.36      | 11.60        | 20.49      | 130,590     | 121,600    | 24,306  | 276,496    |
| feal-linear-cryptanalysis        | Astra  | 6        | 8          | 13.23     | 0.38         | 14.08      | 136,388     | 141,952    | 31,611  | 309,951    |
| feal-linear-cryptanalysis        | DSH    | 18       | 19         | 18.72     | 0.31         | 19.81      | 210,642     | 356,800    | 41,362  | 608,804    |
| feal-linear-cryptanalysis        | Hermes | 1        | 4          | 29.42     | 0.39         | 30.29      | 161         | 14,144     | 68      | 14,373     |
| feal-linear-cryptanalysis        | PI     | 19       | 21         | 18.77     | 2.07         | 21.36      | 258,002     | 599,168    | 55,155  | 912,325    |
| filter-js-from-html              | Astra  | 9        | 8          | 7.27      | 8.29         | 16.22      | 91,787      | 217,152    | 20,107  | 329,046    |
| filter-js-from-html              | DSH    | 48       | 48         | 12.09     | 6.94         | 19.49      | 169,866     | 748,928    | 29,640  | 948,434    |
| filter-js-from-html              | Hermes | 7        | 6          | 1.48      | 7.35         | 9.41       | 51,889      | 67,776     | 2,297   | 121,962    |
| filter-js-from-html              | PI     | 14       | 14         | 30.13     | 6.51         | 37.31      | 445,723     | 367,616    | 107,162 | 920,501    |
| financial-document-processor     | Astra  | 23       | 56         | 13.31     | 0.74         | 14.94      | 118,195     | 862,656    | 26,802  | 1,007,653  |
| financial-document-processor     | DSH    | 19       | 19         | 11.90     | 0.96         | 13.76      | 42,462      | 118,144    | 10,535  | 171,141    |
| financial-document-processor     | Hermes | 21       | 20         | 10.24     | 11.33        | 22.03      | 27,736      | 342,208    | 6,330   | 376,274    |
| financial-document-processor     | PI     | 22       | 24         | 9.44      | 4.98         | 15.69      | 122,362     | 289,984    | 17,141  | 429,487    |
| fix-code-vulnerability           | Astra  | 9        | 9          | 1.58      | 0.19         | 2.24       | 57,534      | 293,312    | 1,115   | 351,961    |
| fix-code-vulnerability           | DSH    | 23       | 27         | 2.94      | 0.23         | 4.04       | 31,114      | 177,536    | 2,094   | 210,744    |
| fix-code-vulnerability           | Hermes | 30       | 32         | 3.88      | 0.22         | 4.70       | 35,497      | 582,912    | 2,297   | 620,706    |
| fix-code-vulnerability           | PI     | 30       | 35         | 3.84      | 0.21         | 4.72       | 68,379      | 411,968    | 7,907   | 488,254    |
| fix-git                          | Astra  | 10       | 15         | 1.63      | 0.40         | 2.50       | 55,423      | 321,280    | 1,750   | 378,453    |
| fix-git                          | DSH    | 9        | 8          | 1.35      | 0.36         | 2.42       | 11,822      | 10,752     | 1,522   | 24,096     |
| fix-git                          | Hermes | 8        | 7          | 1.11      | 0.34         | 1.95       | 2,791       | 115,264    | 727     | 118,782    |
| fix-git                          | PI     | 11       | 20         | 4.60      | 0.53         | 5.72       | 46,521      | 32,512     | 13,193  | 92,226     |
| fix-ocaml-gc                     | Astra  | 33       | 43         | 31.86     | 29.61        | 62.34      | 320,853     | 1,264,064  | 50,009  | 1,634,926  |
| fix-ocaml-gc                     | DSH    | 42       | 51         | 35.22     | 3.21         | 41.40      | 96,003      | 875,904    | 10,830  | 982,737    |
| fix-ocaml-gc                     | Hermes | 34       | 39         | 60.32     | 30.85        | 91.83      | 344,620     | 1,423,040  | 17,174  | 1,784,834  |
| fix-ocaml-gc                     | PI     | 46       | 54         | 60.14     | 32.05        | 93.24      | 546,771     | 1,580,352  | 30,395  | 2,157,518  |
| gcode-to-text                    | Astra  | 34       | 35         | 15.13     | 0.72         | 16.27      | 255,422     | 1,263,616  | 37,276  | 1,556,314  |
| gcode-to-text                    | DSH    | 40       | 40         | 15.11     | 0.30         | 15.96      | 240,175     | 763,904    | 28,942  | 1,033,021  |
| gcode-to-text                    | Hermes | 31       | 31         | 15.15     | 0.42         | 16.07      | 67,538      | 719,360    | 17,464  | 804,362    |
| gcode-to-text                    | PI     | 47       | 49         | 15.14     | 0.37         | 16.08      | 582,430     | 2,653,760  | 33,248  | 3,269,438  |
| git-leak-recovery                | Astra  | 7        | 6          | 1.43      | 0.38         | 2.61       | 43,201      | 213,696    | 1,216   | 258,113    |
| git-leak-recovery                | DSH    | 12       | 15         | 1.96      | 0.78         | 4.00       | 12,811      | 19,072     | 1,796   | 33,679     |
| git-leak-recovery                | Hermes | 8        | 12         | 1.20      | 0.52         | 2.23       | 3,875       | 118,336    | 888     | 123,099    |
| git-leak-recovery                | PI     | 12       | 18         | 3.35      | 0.42         | 4.71       | 52,336      | 40,064     | 10,554  | 102,954    |
| git-multibranch                  | Astra  | 25       | 32         | 8.14      | 3.34         | 12.08      | 93,783      | 855,232    | 18,396  | 967,411    |
| git-multibranch                  | DSH    | 22       | 21         | 8.92      | 0.58         | 10.48      | 28,823      | 51,648     | 3,734   | 84,205     |
| git-multibranch                  | Hermes | 32       | 31         | 5.21      | 0.69         | 6.42       | 24,679      | 548,928    | 3,890   | 577,497    |
| git-multibranch                  | PI     | 21       | 24         | 8.03      | 0.87         | 9.47       | 131,855     | 201,792    | 24,790  | 358,437    |
| gpt2-codegolf                    | Astra  | 6        | 6          | 15.13     | 0.41         | 16.44      | 117,154     | 106,112    | 47,663  | 270,929    |
| gpt2-codegolf                    | DSH    | 17       | 20         | 15.11     | 0.42         | 16.44      | 122,244     | 202,624    | 39,137  | 364,005    |
| gpt2-codegolf                    | Hermes | 0        | 0          | 15.13     | 0.45         | 16.04      | 0           | 0          | 0       | 0          |
| gpt2-codegolf                    | PI     | 9        | 10         | 15.12     | 0.35         | 16.38      | 74,450      | 82,624     | 34,687  | 191,761    |
| headless-terminal                | Astra  | 34       | 34         | 12.05     | 0.52         | 13.05      | 116,136     | 1,336,320  | 27,053  | 1,479,509  |
| headless-terminal                | DSH    | 12       | 11         | 3.58      | 0.88         | 5.07       | 17,054      | 22,720     | 4,333   | 44,107     |
| headless-terminal                | Hermes | 21       | 21         | 3.71      | 0.87         | 5.04       | 18,094      | 370,176    | 5,127   | 393,397    |
| headless-terminal                | PI     | 3        | 5          | 15.14     | 1.07         | 16.77      | 2,166       | 2,816      | 388     | 5,370      |
| hf-model-inference               | Astra  | 15       | 16         | 7.64      | 0.74         | 8.91       | 52,929      | 516,160    | 5,117   | 574,206    |
| hf-model-inference               | DSH    | 14       | 13         | 6.06      | 0.37         | 7.64       | 33,486      | 80,384     | 3,580   | 117,450    |
| hf-model-inference               | Hermes | 16       | 18         | 6.98      | 0.38         | 7.89       | 19,481      | 305,536    | 2,791   | 327,808    |
| hf-model-inference               | PI     | 12       | 14         | 6.41      | 0.36         | 7.32       | 38,588      | 72,896     | 7,421   | 118,905    |
| install-windows-3.11             | Astra  | 25       | 33         | 19.68     | 18.43        | 38.65      | 102,935     | 860,864    | 11,377  | 975,176    |
| install-windows-3.11             | DSH    | 40       | 41         | 14.01     | 1.03         | 15.44      | 58,786      | 286,080    | 11,742  | 356,608    |
| install-windows-3.11             | Hermes | 64       | 69         | 22.14     | 1.51         | 24.13      | 66,649      | 1,444,800  | 9,373   | 1,520,822  |
| install-windows-3.11             | PI     | 96       | 104        | 60.12     | 1.16         | 61.82      | 697,104     | 5,073,216  | 99,571  | 5,869,891  |
| kv-store-grpc                    | Astra  | 11       | 9          | 2.23      | 0.28         | 3.00       | 59,135      | 324,224    | 1,971   | 385,330    |
| kv-store-grpc                    | DSH    | 10       | 10         | 1.49      | 0.32         | 2.39       | 7,669       | 14,336     | 1,694   | 23,699     |
| kv-store-grpc                    | Hermes | 10       | 10         | 1.99      | 0.19         | 2.64       | 8,217       | 153,600    | 1,418   | 163,235    |
| kv-store-grpc                    | PI     | 7        | 8          | 2.09      | 0.20         | 2.81       | 5,526       | 25,728     | 2,875   | 34,129     |
| large-scale-text-editing         | Astra  | 13       | 12         | 8.37      | 1.41         | 10.29      | 35,730      | 396,608    | 17,087  | 449,425    |
| large-scale-text-editing         | DSH    | 13       | 12         | 9.31      | 1.38         | 11.34      | 30,842      | 71,872     | 8,310   | 111,024    |
| large-scale-text-editing         | Hermes | 11       | 13         | 6.41      | 1.42         | 8.29       | 7,513       | 173,824    | 13,907  | 195,244    |
| large-scale-text-editing         | PI     | 24       | 24         | 20.12     | 1.34         | 22.01      | 192,877     | 392,832    | 44,956  | 630,665    |
| largest-eigenval                 | Astra  | 20       | 22         | 15.13     | 0.18         | 15.81      | 151,958     | 661,632    | 40,776  | 854,366    |
| largest-eigenval                 | DSH    | 33       | 33         | 15.12     | 0.25         | 16.02      | 64,927      | 307,072    | 12,764  | 384,763    |
| largest-eigenval                 | Hermes | 11       | 11         | 2.85      | 0.21         | 3.51       | 14,729      | 169,472    | 3,698   | 187,899    |
| largest-eigenval                 | PI     | 28       | 32         | 15.13     | 0.20         | 15.90      | 163,867     | 510,912    | 46,095  | 720,874    |
| llm-inference-batching-scheduler | Astra  | 16       | 19         | 30.13     | 0.42         | 31.04      | 144,593     | 702,720    | 81,024  | 928,337    |
| llm-inference-batching-scheduler | DSH    | 18       | 20         | 23.92     | 0.38         | 24.87      | 127,544     | 492,288    | 51,553  | 671,385    |
| llm-inference-batching-scheduler | Hermes | 16       | 18         | 23.02     | 0.40         | 23.90      | 67,878      | 423,232    | 83,020  | 574,130    |
| llm-inference-batching-scheduler | PI     | 21       | 22         | 30.13     | 0.71         | 31.35      | 326,035     | 602,752    | 82,843  | 1,011,630  |
| log-summary-date-ranges          | Astra  | 6        | 5          | 1.64      | 0.40         | 2.50       | 158,254     | 35,712     | 1,434   | 195,400    |
| log-summary-date-ranges          | DSH    | 4        | 4          | 0.90      | 0.52         | 2.00       | 7,039       | 3,072      | 1,741   | 11,852     |
| log-summary-date-ranges          | Hermes | 7        | 6          | 1.18      | 0.39         | 2.02       | 7,300       | 102,656    | 1,524   | 111,480    |
| log-summary-date-ranges          | PI     | 5        | 5          | 1.15      | 0.37         | 2.09       | 26,540      | 13,376     | 3,265   | 43,181     |
| mailman                          | Astra  | 25       | 45         | 16.63     | 2.87         | 20.00      | 140,768     | 1,000,768  | 43,501  | 1,185,037  |
| mailman                          | DSH    | 50       | 53         | 11.34     | 1.61         | 13.38      | 225,778     | 1,234,752  | 19,065  | 1,479,595  |
| mailman                          | Hermes | 89       | 104        | 15.54     | 1.60         | 17.61      | 121,506     | 3,402,944  | 18,177  | 3,542,627  |
| mailman                          | PI     | 61       | 77         | 25.08     | 2.75         | 28.37      | 429,427     | 2,749,888  | 55,113  | 3,234,428  |
| make-doom-for-mips               | Astra  | 16       | 23         | 15.13     | 1.14         | 16.79      | 80,637      | 663,104    | 30,526  | 774,267    |
| make-doom-for-mips               | DSH    | 44       | 55         | 15.11     | 1.04         | 16.97      | 249,513     | 1,957,440  | 24,923  | 2,231,876  |
| make-doom-for-mips               | Hermes | 82       | 93         | 15.16     | 1.17         | 16.96      | 163,562     | 5,211,520  | 27,479  | 5,402,561  |
| make-doom-for-mips               | PI     | 40       | 58         | 15.13     | 1.28         | 16.97      | 493,372     | 1,310,784  | 36,622  | 1,840,778  |
| make-mips-interpreter            | Astra  | 20       | 45         | 30.13     | 1.14         | 31.77      | 275,671     | 765,504    | 102,495 | 1,143,670  |
| make-mips-interpreter            | DSH    | 50       | 50         | 7.40      | 1.24         | 10.06      | 144,117     | 972,416    | 7,441   | 1,123,974  |
| make-mips-interpreter            | Hermes | 36       | 52         | 30.19     | 1.25         | 32.08      | 147,321     | 1,409,344  | 26,153  | 1,582,818  |
| make-mips-interpreter            | PI     | 42       | 50         | 30.12     | 2.52         | 33.16      | 513,634     | 1,643,584  | 75,966  | 2,233,184  |
| mcmc-sampling-stan               | Astra  | 18       | 21         | 30.16     | 0.42         | 31.18      | 199,715     | 524,096    | 13,844  | 737,655    |
| mcmc-sampling-stan               | DSH    | 23       | 25         | 26.42     | 4.18         | 31.04      | 51,324      | 94,272     | 6,822   | 152,418    |
| mcmc-sampling-stan               | Hermes | 26       | 29         | 30.31     | 0.43         | 31.21      | 80,778      | 418,560    | 4,052   | 503,390    |
| mcmc-sampling-stan               | PI     | 37       | 44         | 30.10     | 5.62         | 36.24      | 351,073     | 1,462,400  | 52,601  | 1,866,074  |
| merge-diff-arc-agi-task          | Astra  | 16       | 17         | 4.08      | 0.57         | 5.52       | 41,337      | 577,472    | 9,492   | 628,301    |
| merge-diff-arc-agi-task          | DSH    | 16       | 15         | 3.74      | 0.48         | 5.11       | 25,269      | 71,552     | 7,945   | 104,766    |
| merge-diff-arc-agi-task          | Hermes | 21       | 22         | 5.96      | 0.37         | 6.80       | 60,027      | 636,480    | 12,000  | 708,507    |
| merge-diff-arc-agi-task          | PI     | 18       | 20         | 5.08      | 0.39         | 7.01       | 33,694      | 131,392    | 12,586  | 177,672    |
| model-extraction-relu-logits     | Astra  | 8        | 6          | 7.48      | 6.78         | 14.67      | 203,019     | 111,232    | 16,844  | 331,095    |
| model-extraction-relu-logits     | DSH    | 22       | 22         | 13.48     | 0.57         | 14.66      | 111,405     | 688,192    | 38,187  | 837,784    |
| model-extraction-relu-logits     | Hermes | 15       | 15         | 15.14     | 0.60         | 16.20      | 33,482      | 306,624    | 49,745  | 389,851    |
| model-extraction-relu-logits     | PI     | 2        | 2          | 15.13     | 0.64         | 16.34      | 14,009      | 2,560      | 51,225  | 67,794     |
| modernize-scientific-stack       | Astra  | 6        | 7          | 1.42      | 5.63         | 7.60       | 57,556      | 141,312    | 1,726   | 200,594    |
| modernize-scientific-stack       | DSH    | 9        | 12         | 1.43      | 0.43         | 2.30       | 9,206       | 21,888     | 1,905   | 32,999     |
| modernize-scientific-stack       | Hermes | 20       | 21         | 3.66      | 0.77         | 4.95       | 67,117      | 320,896    | 4,135   | 392,148    |
| modernize-scientific-stack       | PI     | 8        | 10         | 1.89      | 0.73         | 3.20       | 16,048      | 21,888     | 4,552   | 42,488     |
| mteb-leaderboard                 | Astra  | 35       | 54         | 10.08     | 2.70         | 13.31      | 201,468     | 1,602,432  | 8,765   | 1,812,665  |
| mteb-leaderboard                 | DSH    | 50       | 51         | 21.36     | 0.38         | 22.21      | 142,624     | 413,184    | 7,840   | 563,648    |
| mteb-leaderboard                 | Hermes | 90       | 90         | 21.90     | 0.35         | 22.87      | 173,532     | 2,105,728  | 8,545   | 2,287,805  |
| mteb-leaderboard                 | PI     | 51       | 57         | 34.96     | 0.39         | 36.04      | 427,523     | 1,876,736  | 55,667  | 2,359,926  |
| mteb-retrieve                    | Astra  | 12       | 14         | 4.36      | 6.43         | 11.44      | 54,662      | 400,576    | 3,269   | 458,507    |
| mteb-retrieve                    | DSH    | 23       | 22         | 10.75     | 19.72        | 30.96      | 56,213      | 148,352    | 5,010   | 209,575    |
| mteb-retrieve                    | Hermes | 90       | 91         | 22.73     | 6.26         | 29.60      | 100,638     | 3,171,712  | 18,073  | 3,290,423  |
| mteb-retrieve                    | PI     | 34       | 45         | 22.20     | 10.13        | 32.88      | 196,045     | 508,608    | 35,680  | 740,333    |
| multi-source-data-merger         | Astra  | 8        | 7          | 1.98      | 0.71         | 3.19       | 57,672      | 245,184    | 3,467   | 306,323    |
| multi-source-data-merger         | DSH    | 6        | 8          | 1.85      | 1.31         | 4.05       | 12,611      | 7,616      | 3,466   | 23,693     |
| multi-source-data-merger         | Hermes | 13       | 16         | 2.58      | 0.73         | 3.78       | 10,624      | 201,600    | 2,865   | 215,089    |
| multi-source-data-merger         | PI     | 7        | 9          | 5.31      | 0.67         | 6.56       | 41,448      | 27,648     | 18,389  | 87,485     |
| nginx-request-logging            | Astra  | 22       | 26         | 6.04      | 1.84         | 8.39       | 91,127      | 777,792    | 11,601  | 880,520    |
| nginx-request-logging            | DSH    | 8        | 9          | 1.32      | 0.46         | 2.36       | 6,099       | 12,864     | 1,329   | 20,292     |
| nginx-request-logging            | Hermes | 16       | 19         | 2.51      | 0.47         | 3.44       | 13,643      | 271,168    | 3,003   | 287,814    |
| nginx-request-logging            | PI     | 12       | 14         | 2.02      | 0.61         | 3.19       | 17,996      | 35,456     | 4,705   | 58,157     |
| openssl-selfsigned-cert          | Astra  | 12       | 11         | 2.69      | 0.38         | 3.55       | 35,908      | 427,008    | 5,815   | 468,731    |
| openssl-selfsigned-cert          | DSH    | 9        | 8          | 1.22      | 0.57         | 2.36       | 8,967       | 8,064      | 1,486   | 18,517     |
| openssl-selfsigned-cert          | Hermes | 18       | 17         | 2.51      | 0.37         | 3.34       | 9,046       | 282,752    | 1,734   | 293,532    |
| openssl-selfsigned-cert          | PI     | 14       | 13         | 2.58      | 0.45         | 3.59       | 28,287      | 44,416     | 7,309   | 80,012     |
| overfull-hbox                    | Astra  | 21       | 24         | 12.61     | 2.66         | 16.35      | 137,660     | 739,328    | 30,940  | 907,928    |
| overfull-hbox                    | DSH    | 17       | 19         | 10.90     | 1.23         | 13.03      | 67,345      | 155,520    | 10,649  | 233,514    |
| overfull-hbox                    | Hermes | 32       | 35         | 12.62     | 1.41         | 14.49      | 52,490      | 850,816    | 16,323  | 919,629    |
| overfull-hbox                    | PI     | 16       | 19         | 9.31      | 1.22         | 12.44      | 154,233     | 181,952    | 20,674  | 356,859    |
| password-recovery                | Astra  | 25       | 29         | 7.63      | 0.34         | 8.84       | 55,385      | 938,048    | 17,496  | 1,010,929  |
| password-recovery                | DSH    | 24       | 35         | 15.12     | 0.39         | 16.70      | 72,862      | 292,224    | 24,532  | 389,618    |
| password-recovery                | Hermes | 25       | 30         | 6.49      | 0.56         | 7.55       | 44,218      | 587,968    | 13,647  | 645,833    |
| password-recovery                | PI     | 12       | 15         | 5.75      | 0.43         | 7.17       | 110,042     | 100,608    | 17,904  | 228,554    |
| path-tracing                     | Astra  | 20       | 21         | 30.14     | 0.59         | 31.27      | 186,583     | 718,784    | 105,774 | 1,011,141  |
| path-tracing                     | DSH    | 31       | 31         | 30.12     | 0.51         | 31.89      | 259,981     | 838,272    | 58,265  | 1,156,518  |
| path-tracing                     | Hermes | 30       | 32         | 30.38     | 0.66         | 31.52      | 368,155     | 1,451,328  | 52,165  | 1,871,648  |
| path-tracing                     | PI     | 53       | 53         | 30.14     | 0.71         | 31.37      | 834,142     | 2,438,080  | 75,118  | 3,347,340  |
| path-tracing-reverse             | Astra  | 22       | 32         | 30.13     | 1.41         | 32.44      | 146,604     | 1,013,440  | 15,146  | 1,175,190  |
| path-tracing-reverse             | DSH    | 35       | 35         | 30.12     | 0.41         | 31.74      | 529,992     | 623,744    | 40,325  | 1,194,061  |
| path-tracing-reverse             | Hermes | 33       | 42         | 30.17     | 0.54         | 31.17      | 335,344     | 1,827,904  | 25,152  | 2,188,400  |
| path-tracing-reverse             | PI     | 22       | 27         | 30.13     | 1.96         | 33.21      | 664,998     | 657,280    | 104,446 | 1,426,724  |
| polyglot-c-py                    | Astra  | 9        | 7          | 4.68      | 0.38         | 6.00       | 22,614      | 285,568    | 12,933  | 321,115    |
| polyglot-c-py                    | DSH    | 8        | 7          | 5.42      | 0.42         | 6.81       | 43,279      | 51,328     | 17,418  | 112,025    |
| polyglot-c-py                    | Hermes | 9        | 11         | 3.91      | 0.61         | 4.99       | 3,931       | 132,416    | 11,613  | 147,960    |
| polyglot-c-py                    | PI     | 7        | 6          | 3.55      | 0.43         | 4.99       | 28,289      | 36,096     | 11,370  | 75,755     |
| polyglot-rust-c                  | Astra  | 11       | 10         | 8.01      | 0.42         | 9.44       | 23,336      | 353,216    | 19,971  | 396,523    |
| polyglot-rust-c                  | DSH    | 5        | 5          | 8.31      | 0.48         | 9.71       | 34,531      | 101,888    | 33,232  | 169,651    |
| polyglot-rust-c                  | Hermes | 13       | 16         | 9.21      | 1.23         | 10.92      | 8,119       | 201,856    | 28,026  | 238,001    |
| polyglot-rust-c                  | PI     | 12       | 15         | 12.19     | 0.44         | 13.69      | 97,816      | 188,928    | 38,689  | 325,433    |
| portfolio-optimization           | Astra  | 23       | 25         | 27.27     | 2.01         | 29.80      | 145,457     | 844,288    | 22,241  | 1,011,986  |
| portfolio-optimization           | DSH    | 19       | 18         | 8.33      | 5.52         | 14.71      | 35,347      | 119,104    | 11,190  | 165,641    |
| portfolio-optimization           | Hermes | 16       | 19         | 9.67      | 0.50         | 10.68      | 32,573      | 291,584    | 8,746   | 332,903    |
| portfolio-optimization           | PI     | 17       | 21         | 17.23     | 2.01         | 19.82      | 167,620     | 326,784    | 45,324  | 539,728    |
| protein-assembly                 | Astra  | 16       | 48         | 30.13     | 2.25         | 32.91      | 214,764     | 627,328    | 75,270  | 917,362    |
| protein-assembly                 | DSH    | 34       | 38         | 30.11     | 0.56         | 31.44      | 105,943     | 505,984    | 21,780  | 633,707    |
| protein-assembly                 | Hermes | 32       | 34         | 30.14     | 0.67         | 31.27      | 86,365      | 1,021,056  | 13,824  | 1,121,245  |
| protein-assembly                 | PI     | 17       | 22         | 30.15     | 6.36         | 37.04      | 119,071     | 129,280    | 22,779  | 271,130    |
| prove-plus-comm                  | Astra  | 4        | 3          | 0.84      | 0.54         | 1.87       | 9,262       | 136,448    | 729     | 146,439    |
| prove-plus-comm                  | DSH    | 8        | 8          | 1.09      | 0.60         | 2.12       | 4,100       | 11,648     | 1,404   | 17,152     |
| prove-plus-comm                  | Hermes | 5        | 5          | 0.96      | 0.46         | 1.88       | 1,867       | 71,168     | 733     | 73,768     |
| prove-plus-comm                  | PI     | 9        | 8          | 1.41      | 0.60         | 2.50       | 10,449      | 17,792     | 2,652   | 30,893     |
| pypi-server                      | Astra  | 11       | 13         | 2.24      | 1.35         | 4.10       | 128,516     | 254,208    | 1,995   | 384,719    |
| pypi-server                      | DSH    | 15       | 14         | 1.67      | 0.54         | 2.64       | 10,287      | 25,408     | 1,740   | 37,435     |
| pypi-server                      | Hermes | 38       | 37         | 4.64      | 1.08         | 6.37       | 31,796      | 690,304    | 4,330   | 726,430    |
| pypi-server                      | PI     | 14       | 16         | 1.65      | 0.63         | 2.85       | 15,555      | 33,856     | 2,462   | 51,873     |
| pytorch-model-cli                | Astra  | 21       | 21         | 7.13      | 2.93         | 10.60      | 44,481      | 795,712    | 9,054   | 849,247    |
| pytorch-model-cli                | DSH    | 36       | 35         | 10.67     | 2.56         | 13.69      | 122,166     | 484,928    | 24,704  | 631,798    |
| pytorch-model-cli                | Hermes | 32       | 35         | 11.63     | 3.69         | 15.90      | 22,267      | 605,376    | 4,600   | 632,243    |
| pytorch-model-cli                | PI     | 19       | 26         | 7.59      | 5.01         | 13.19      | 70,112      | 209,792    | 22,166  | 302,070    |
| pytorch-model-recovery           | Astra  | 10       | 10         | 6.34      | 3.74         | 10.66      | 80,905      | 300,032    | 15,170  | 396,107    |
| pytorch-model-recovery           | DSH    | 9        | 9          | 3.80      | 4.14         | 8.40       | 46,226      | 31,104     | 8,838   | 86,168     |
| pytorch-model-recovery           | Hermes | 22       | 21         | 12.80     | 4.63         | 17.93      | 25,535      | 393,600    | 8,486   | 427,621    |
| pytorch-model-recovery           | PI     | 15       | 14         | 8.58      | 7.35         | 16.57      | 98,252      | 115,136    | 22,217  | 235,605    |
| qemu-alpine-ssh                  | Astra  | 26       | 28         | 14.49     | 2.68         | 17.70      | 99,527      | 843,520    | 31,450  | 974,497    |
| qemu-alpine-ssh                  | DSH    | 30       | 30         | 15.11     | 0.45         | 18.29      | 120,862     | 70,592     | 7,768   | 199,222    |
| qemu-alpine-ssh                  | Hermes | 54       | 55         | 15.35     | 0.32         | 16.13      | 72,413      | 1,057,920  | 9,086   | 1,139,419  |
| qemu-alpine-ssh                  | PI     | 14       | 17         | 15.13     | 0.54         | 16.19      | 26,673      | 134,528    | 25,338  | 186,539    |
| qemu-startup                     | Astra  | 10       | 10         | 7.86      | 1.14         | 9.49       | 49,640      | 318,976    | 15,169  | 383,785    |
| qemu-startup                     | DSH    | 13       | 12         | 7.07      | 0.38         | 8.06       | 14,987      | 17,280     | 2,512   | 34,779     |
| qemu-startup                     | Hermes | 29       | 28         | 6.20      | 0.35         | 7.01       | 13,010      | 471,104    | 5,380   | 489,494    |
| qemu-startup                     | PI     | 28       | 28         | 15.12     | 0.40         | 16.04      | 111,691     | 273,472    | 25,309  | 410,472    |
| query-optimize                   | Astra  | 14       | 16         | 9.51      | 14.92        | 25.30      | 109,712     | 403,584    | 7,924   | 521,220    |
| query-optimize                   | DSH    | 12       | 11         | 6.72      | 14.37        | 22.10      | 19,411      | 19,968     | 2,410   | 41,789     |
| query-optimize                   | Hermes | 25       | 26         | 7.38      | 15.41        | 23.27      | 22,003      | 450,816    | 8,746   | 481,565    |
| query-optimize                   | PI     | 13       | 19         | 9.57      | 14.90        | 25.40      | 27,095      | 59,136     | 6,895   | 93,126     |
| raman-fitting                    | Astra  | 14       | 14         | 15.11     | 2.78         | 18.31      | 137,920     | 402,240    | 16,251  | 556,411    |
| raman-fitting                    | DSH    | 17       | 18         | 15.12     | 0.38         | 15.94      | 101,420     | 128,960    | 47,554  | 277,934    |
| raman-fitting                    | Hermes | 27       | 27         | 15.15     | 0.39         | 16.03      | 79,368      | 523,968    | 49,998  | 653,334    |
| raman-fitting                    | PI     | 21       | 20         | 10.33     | 0.39         | 11.29      | 231,407     | 268,352    | 32,522  | 532,281    |
| regex-chess                      | Astra  | 2        | 2          | 38.56     | 1.88         | 40.97      | 43,828      | 69,504     | 131,498 | 244,830    |
| regex-chess                      | DSH    | 2        | 2          | 17.16     | 2.79         | 20.61      | 2,790       | 1,408      | 65,661  | 69,859     |
| regex-chess                      | Hermes | 8        | 8          | 42.01     | 0.50         | 42.97      | 39,134      | 93,184     | 537     | 132,855    |
| regex-chess                      | PI     | 11       | 10         | 25.66     | 0.41         | 26.65      | 65,214      | 179,200    | 96,001  | 340,415    |
| regex-log                        | Astra  | 9        | 9          | 6.45      | 0.50         | 9.40       | 109,259     | 249,984    | 19,181  | 378,424    |
| regex-log                        | DSH    | 7        | 6          | 9.61      | 0.48         | 11.03      | 87,739      | 154,944    | 39,078  | 281,761    |
| regex-log                        | Hermes | 3        | 3          | 15.14     | 0.94         | 16.53      | 3,718       | 42,176     | 132,704 | 178,598    |
| regex-log                        | PI     | 9        | 8          | 10.19     | 0.49         | 11.69      | 117,029     | 177,920    | 39,499  | 334,448    |
| reshard-c4-data                  | Astra  | 21       | 19         | 11.30     | 4.19         | 16.09      | 90,538      | 842,624    | 23,812  | 956,974    |
| reshard-c4-data                  | DSH    | 24       | 23         | 6.86      | 18.59        | 28.89      | 104,457     | 323,776    | 17,243  | 445,476    |
| reshard-c4-data                  | Hermes | 6        | 8          | 43.88     | 2.15         | 46.53      | 4,218       | 93,696     | 64,740  | 162,654    |
| reshard-c4-data                  | PI     | 20       | 19         | 16.85     | 7.51         | 24.98      | 155,767     | 650,368    | 52,329  | 858,464    |
| rstan-to-pystan                  | Astra  | 15       | 21         | 13.23     | 1.01         | 14.75      | 60,823      | 569,088    | 6,420   | 636,331    |
| rstan-to-pystan                  | DSH    | 27       | 31         | 17.01     | 0.42         | 17.88      | 92,758      | 276,096    | 9,059   | 377,913    |
| rstan-to-pystan                  | Hermes | 39       | 40         | 13.80     | 0.60         | 14.96      | 62,822      | 903,296    | 4,792   | 970,910    |
| rstan-to-pystan                  | PI     | 29       | 32         | 20.38     | 0.44         | 21.45      | 156,843     | 358,848    | 19,523  | 535,214    |
| sam-cell-seg                     | Astra  | 23       | 27         | 11.84     | 4.98         | 17.50      | 112,423     | 865,088    | 14,392  | 991,903    |
| sam-cell-seg                     | DSH    | 50       | 58         | 49.84     | 7.79         | 58.16      | 69,579      | 596,992    | 16,147  | 682,718    |
| sam-cell-seg                     | Hermes | 36       | 39         | 20.38     | 3.41         | 24.59      | 107,541     | 839,488    | 12,346  | 959,375    |
| sam-cell-seg                     | PI     | 68       | 72         | 60.50     | 2.78         | 63.94      | 593,514     | 2,877,120  | 79,056  | 3,549,690  |
| sanitize-git-repo                | Astra  | 24       | 40         | 7.85      | 1.24         | 9.61       | 151,803     | 969,024    | 20,175  | 1,141,002  |
| sanitize-git-repo                | DSH    | 24       | 32         | 3.98      | 0.31         | 4.73       | 104,851     | 455,232    | 4,648   | 564,731    |
| sanitize-git-repo                | Hermes | 47       | 76         | 10.38     | 0.44         | 11.45      | 319,228     | 3,330,560  | 11,616  | 3,661,404  |
| sanitize-git-repo                | PI     | 22       | 34         | 9.78      | 0.42         | 10.74      | 305,162     | 464,128    | 29,535  | 798,825    |
| schemelike-metacircular-eval     | Astra  | 14       | 27         | 40.13     | 0.56         | 42.49      | 64,946      | 536,640    | 116,031 | 717,617    |
| schemelike-metacircular-eval     | DSH    | 31       | 37         | 40.12     | 3.50         | 44.26      | 508,173     | 876,224    | 59,889  | 1,444,286  |
| schemelike-metacircular-eval     | Hermes | 9        | 36         | 40.15     | 0.57         | 41.18      | 50,825      | 193,472    | 729     | 245,026    |
| schemelike-metacircular-eval     | PI     | 27       | 27         | 40.14     | 0.76         | 41.43      | 889,845     | 1,701,184  | 111,625 | 2,702,654  |
| sparql-university                | Astra  | 15       | 13         | 6.16      | 0.46         | 7.53       | 99,392      | 484,480    | 15,351  | 599,223    |
| sparql-university                | DSH    | 20       | 19         | 4.87      | 0.40         | 6.24       | 36,166      | 203,648    | 12,115  | 251,929    |
| sparql-university                | Hermes | 15       | 14         | 7.59      | 0.71         | 8.75       | 22,911      | 299,456    | 26,766  | 349,133    |
| sparql-university                | PI     | 19       | 18         | 9.25      | 0.54         | 10.84      | 166,303     | 327,744    | 31,926  | 525,973    |
| sqlite-db-truncate               | Astra  | 12       | 10         | 3.36      | 0.39         | 4.21       | 81,651      | 322,816    | 7,821   | 412,288    |
| sqlite-db-truncate               | DSH    | 6        | 7          | 1.58      | 0.27         | 2.30       | 12,212      | 6,912      | 3,420   | 22,544     |
| sqlite-db-truncate               | Hermes | 7        | 9          | 1.98      | 0.41         | 2.85       | 20,882      | 93,376     | 3,306   | 117,564    |
| sqlite-db-truncate               | PI     | 10       | 11         | 1.95      | 0.41         | 2.92       | 18,484      | 22,976     | 6,044   | 47,504     |
| sqlite-with-gcov                 | Astra  | 19       | 18         | 6.93      | 0.40         | 8.31       | 78,451      | 653,248    | 3,168   | 734,867    |
| sqlite-with-gcov                 | DSH    | 19       | 20         | 15.11     | 0.42         | 16.59      | 15,261      | 31,616     | 1,097   | 47,974     |
| sqlite-with-gcov                 | Hermes | 26       | 29         | 10.02     | 0.52         | 11.01      | 26,337      | 451,648    | 1,757   | 479,742    |
| sqlite-with-gcov                 | PI     | 22       | 31         | 10.11     | 0.38         | 11.55      | 95,867      | 165,952    | 9,731   | 271,550    |
| torch-pipeline-parallelism       | Astra  | 8        | 9          | 15.13     | 10.94        | 27.19      | 54,810      | 251,648    | 36,974  | 343,432    |
| torch-pipeline-parallelism       | DSH    | 10       | 11         | 15.11     | 7.19         | 23.27      | 91,778      | 58,240     | 56,044  | 206,062    |
| torch-pipeline-parallelism       | Hermes | 6        | 7          | 15.33     | 7.13         | 23.04      | 45,974      | 45,568     | 35,123  | 126,665    |
| torch-pipeline-parallelism       | PI     | 27       | 33         | 15.12     | 4.29         | 20.46      | 110,069     | 172,224    | 32,286  | 314,579    |
| torch-tensor-parallelism         | Astra  | 11       | 9          | 5.38      | 7.63         | 14.12      | 105,008     | 289,600    | 10,387  | 404,995    |
| torch-tensor-parallelism         | DSH    | 19       | 18         | 8.37      | 5.96         | 15.18      | 43,073      | 127,168    | 11,746  | 181,987    |
| torch-tensor-parallelism         | Hermes | 13       | 12         | 7.99      | 8.15         | 16.74      | 47,103      | 195,264    | 15,044  | 257,411    |
| torch-tensor-parallelism         | PI     | 12       | 12         | 11.16     | 6.42         | 18.57      | 117,925     | 264,640    | 43,163  | 425,728    |
| train-fasttext                   | Astra  | 26       | 25         | 60.14     | 5.05         | 65.70      | 238,966     | 808,192    | 9,122   | 1,056,280  |
| train-fasttext                   | DSH    | 30       | 33         | 60.11     | 17.96        | 79.14      | 54,236      | 119,936    | 4,782   | 178,954    |
| train-fasttext                   | Hermes | 42       | 43         | 60.40     | 2.36         | 63.32      | 179,383     | 1,693,952  | 6,507   | 1,879,842  |
| train-fasttext                   | PI     | 29       | 32         | 60.19     | 16.68        | 77.56      | 159,225     | 138,048    | 17,600  | 314,873    |
| tune-mjcf                        | Astra  | 11       | 12         | 15.12     | 1.19         | 16.78      | 111,412     | 326,528    | 16,097  | 454,037    |
| tune-mjcf                        | DSH    | 25       | 26         | 15.11     | 0.78         | 16.32      | 52,450      | 159,680    | 13,770  | 225,900    |
| tune-mjcf                        | Hermes | 24       | 26         | 15.30     | 1.12         | 16.90      | 32,528      | 467,904    | 15,614  | 516,046    |
| tune-mjcf                        | PI     | 26       | 27         | 15.13     | 2.75         | 18.45      | 121,669     | 267,392    | 23,893  | 412,954    |
| video-processing                 | Astra  | 24       | 25         | 22.04     | 9.93         | 32.50      | 211,730     | 1,076,928  | 59,117  | 1,347,775  |
| video-processing                 | DSH    | 33       | 32         | 22.65     | 1.33         | 24.95      | 511,428     | 1,192,512  | 56,115  | 1,760,055  |
| video-processing                 | Hermes | 72       | 82         | 37.04     | 2.34         | 39.86      | 212,404     | 3,889,408  | 25,505  | 4,127,317  |
| video-processing                 | PI     | 15       | 17         | 19.34     | 0.49         | 20.42      | 328,336     | 184,064    | 65,513  | 577,913    |
| vulnerable-secret                | Astra  | 10       | 11         | 1.95      | 0.39         | 2.80       | 69,928      | 319,040    | 2,301   | 391,269    |
| vulnerable-secret                | DSH    | 9        | 8          | 1.57      | 0.28         | 2.28       | 14,669      | 28,672     | 3,122   | 46,463     |
| vulnerable-secret                | Hermes | 13       | 12         | 2.05      | 0.39         | 2.89       | 18,468      | 223,232    | 3,055   | 244,755    |
| vulnerable-secret                | PI     | 17       | 16         | 2.53      | 0.37         | 3.47       | 39,864      | 65,088     | 7,760   | 112,712    |
| winning-avg-corewars             | Astra  | 35       | 39         | 60.13     | 0.73         | 61.29      | 299,417     | 1,285,568  | 210,760 | 1,795,745  |
| winning-avg-corewars             | DSH    | 28       | 32         | 12.93     | 0.37         | 14.52      | 218,920     | 208,128    | 21,356  | 448,404    |
| winning-avg-corewars             | Hermes | 28       | 61         | 12.79     | 0.48         | 13.89      | 33,331      | 574,080    | 8,011   | 615,422    |
| winning-avg-corewars             | PI     | 25       | 29         | 21.15     | 1.83         | 23.57      | 236,688     | 698,752    | 52,585  | 988,025    |
| write-compressor                 | Astra  | 2        | 3          | 15.10     | 0.40         | 16.40      | 5,708       | 67,328     | 55,754  | 128,790    |
| write-compressor                 | DSH    | 8        | 8          | 10.79     | 0.43         | 12.05      | 117,333     | 102,912    | 37,938  | 258,183    |
| write-compressor                 | Hermes | 1        | 2          | 15.16     | 0.65         | 16.27      | 166         | 14,080     | 47      | 14,293     |
| write-compressor                 | PI     | 7        | 8          | 15.12     | 0.41         | 16.53      | 105,929     | 139,648    | 51,615  | 297,192    |

**四者均未通过的任务**

chess-best-move、extract-elf、extract-moves-from-video、filter-js-from-html、gcode-to-text、gpt2-codegolf、make-doom-for-mips、make-mips-interpreter、model-extraction-relu-logits、mteb-retrieve、path-tracing、protein-assembly、qemu-alpine-ssh、regex-chess、torch-pipeline-parallelism、train-fasttext。

**各框架独有通过任务**

- Astra：configure-git-webserver、count-dataset-tokens、git-multibranch、hf-model-inference、kv-store-grpc、nginx-request-logging、pypi-server、reshard-c4-data。
- DSH：build-pov-ray、circuit-fibsqrt、query-optimize、schemelike-metacircular-eval、tune-mjcf、video-processing、write-compressor。
- Hermes：无。
- PI：build-cython-ext、caffe-cifar-10、compile-compcert、install-windows-3.11、mailman、mteb-leaderboard、path-tracing-reverse、raman-fitting。

## 数据来源

本文件与源报告保存在同一目录，沿用相同八个二级章节及顺序。

- Astra：[原报告](astra-terminalbench-linux-latest-89-task-report.md)；[逐题 CSV](astra-terminalbench-linux-latest-89-task-details.csv)。
- DSH：[原报告](dsh-terminalbench-linux-latest-89-task-report.md)；[逐题 CSV](dsh-terminalbench-linux-latest-89-task-details.csv)。
- Hermes：[原报告](hermes-terminalbench-linux-latest-89-task-report.md)；[逐题 CSV](hermes-terminalbench-linux-latest-89-task-details.csv)。
- PI：[原报告](pi-terminalbench-linux-latest-89-task-report.md)；[逐题 CSV](pi-terminalbench-linux-latest-89-task-details.csv)。

Astra 选中结果来自 work/linux-terminal-bench/astra/jobs/latest-results/results.jsonl，补充 trace 来自 work/linux-terminal-bench/astra/trace-supplements/all-verifier-attempts。其他框架的具体 result_path 与 trace_source 见各 CSV。本文仅汇总这些现有结果，未更改原报告或原始评测结果。
