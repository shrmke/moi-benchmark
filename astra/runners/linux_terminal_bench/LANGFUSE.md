# Linux Terminal-Bench 离线轨迹导入 Langfuse

脚本位于 `astra/runners/linux_terminal_bench/langfuse_import.py`，在仓库根目录通过 Python module 运行。仅依赖 Python 标准库和本仓库现有 runner、轨迹清洗器；无需安装 Langfuse SDK。所有生成数据写入 `work/linux-terminal-bench/langfuse/`，不修改原始 trial、清洗数据集或 runner 的正式汇总。

## 数据与评分口径

- `prepare --dataset astra/datasets/linux-terminal-bench-trajectory` 直接读取最新清洗 JSONL，包括 Astra 数据库补充轨迹；仍读取原始 result/verifier 证据并复用 runner 判定。独立重跑 jobs 也参与最新 attempt 选择。
- 未指定 `--dataset` 时，`prepare` 读取当前原始 trial，复用 `astra/datasets/linux-terminal-bench-trajectory/scripts/clean_trajectories.py` 的四产品解析和脱敏。不依赖之前发布的三产品 JSONL 快照。
- 排除未结束、无法解析、没有 user 或 assistant 消息的记录（包括 metadata-only）。其余 complete 和 partial 均保留。
- 一次 attempt 对应一个 trace，含一个 agent root observation。Input 是任务指令，Output 是有序对话；工具调用转成 OpenAI message 格式，参数和结果由 `tool_call_id` 配对。
- root 时间来自真实 trial 起止时间。没有完整调用边界，不生成逐调用 span 或 Generation；不把 message 时间戳当成工具耗时，不把任务 token 总量摊到每轮。
- token、费用、阶段耗时保存在 metadata 的 `usage` / `timing`。源数据缺失保留 JSON `null`；`missing_fields` 明确列出缺项。Langfuse 原生 token/费用栏可能为空，应查看这些 metadata，而不能将空栏理解为零。
- `verifier_passed` / `verifier_failed` 保存在 metadata，来自清洗数据的 outcome；缺失为 `null`，不改变正式 reward。回读验证会检查这两个计数。
- `message_details` 按原消息顺序记录 seq、timestamp、is_error、internal_content_omitted；隐藏 reasoning 不恢复。
- 正式 reward 调用当前 `results.verifier_status()`，含 CTRF 和基础设施异常判定；`raw_reward`、`reward`、`verifier_status` 分别保存。无效结果不产生数值评分。
- `quality_tier` 是清洗器的数据完整性分类，和 runner 的结果有效性独立。不要用 complete 表示通过。
- `runner_reward`：每个具有有效 verifier 的已导入 attempt 的得分，用于单条复盘。
- `runner_reward_latest`：额外只给 `latest_results()` 选中的 attempt 打分；`runner_selected_latest=true` 标识选择结果。选择在原始 jobs 中进行，沿用产品配置与时间规则；如果最新 attempt 是被排除的 metadata-only，旧 attempt 不会被递补。
- **成功率必须筛选单个 `import_batch`，只聚合 `runner_reward_latest`。** 这个结果是可分析轨迹子集的成功率；正式全部任务成绩仍查看 runner 的 `state/analysis/summary.json`。

## 1. 在本机部署

部署使用官方 Langfuse Docker 镜像和独立 Compose 项目 `moi-langfuse`。Web 绑定 `127.0.0.1:17300`，对象存储绑定 `127.0.0.1:17390`；数据库不暴露宿主端口。每个容器有内存上限，Web、Worker 和 ClickHouse 有 CPU 上限。与 benchmark 同机仍共享宿主资源，资源密集的正式测评期间可以停止此分析服务。

```bash
cd /home/vagrant/moi-benchmark
python3 astra/runners/linux_terminal_bench/langfuse/init_env.py \
  --output work/linux-terminal-bench/langfuse/server.env

docker compose \
  --env-file work/linux-terminal-bench/langfuse/server.env \
  -f astra/runners/linux_terminal_bench/langfuse/compose.yaml up -d

curl -fsS http://localhost:17300/api/public/health
```

凭据文件权限为 `0600`，在已忽略的 `work/` 目录中。初始化脚本不会覆盖已有密钥，避免破坏已有实例。初始用户为 `admin@moi-benchmark.local`，密码在文件的 `LANGFUSE_INIT_USER_PASSWORD`，项目 API 密钥是 `LANGFUSE_PUBLIC_KEY` 和 `LANGFUSE_SECRET_KEY`。不要提交该文件。

远程浏览器通过 SSH 隧道访问，在个人电脑执行：

```bash
ssh -N -L 17300:127.0.0.1:17300 -L 17390:127.0.0.1:17390 vagrant@服务器地址
```

打开 `http://localhost:17300` 登录，进入 **MOI Benchmark → Linux Terminal-Bench**。隧道端口需与 Compose 的 NEXTAUTH_URL 一致。

## 2. 准备离线导入包

```bash
python3 -m astra.runners.linux_terminal_bench.langfuse_import prepare \
  --dataset astra/datasets/linux-terminal-bench-trajectory \
  --output work/linux-terminal-bench/langfuse/import.jsonl
```

默认读取四款产品。可用 `--products pi hermes` 选择产品，或用 `--source /path/to/linux-terminal-bench` 指向另一份原始数据。

包的第一行是统计 report，后续每行是一个 attempt 的 OTLP 请求和评分请求。报告含产品轨迹数、被排除记录的原因、选中的有效任务数及子集成功率。准备过程无模型调用、无网络上传。

每次 prepare 都创建新的 `import_batch` 和 API 主键；它表示一次当前数据/runner 判定的分析快照。**导入中断时直接重复 import，使用同一个包；不要重新 prepare。** 同一个包的 trace、observation、score ID 不变。重新清洗时请使用新文件名，保留旧包用于复盘。多批导入后必须按 import_batch 筛选，不能把快照混合统计。

## 3. 导入

```bash
set -a
. work/linux-terminal-bench/langfuse/server.env
set +a

python3 -m astra.runners.linux_terminal_bench.langfuse_import import \
  --bundle work/linux-terminal-bench/langfuse/import.jsonl
```

使用 `/api/public/otel/v1/traces` 的 OTLP/HTTP JSON 接口写轨迹、`/api/public/scores` 写评分。每条 trace 单独上传，429、部分 5xx 和网络故障自动重试；认证错误和 OTLP partial success 会返回非零退出码。导入完成只表示 API 已接收，后台入库是异步的，必须继续回读验证。

使用已有实例时，设置该实例的 `LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`，无需运行 Compose。请求不跟随重定向，base URL 应直接指向正确服务。

## 4. 回读验证

```bash
python3 -m astra.runners.linux_terminal_bench.langfuse_import verify \
  --bundle work/linux-terminal-bench/langfuse/import.jsonl \
  --api-version v4 \
  --report work/linux-terminal-bench/langfuse/verification.json
```

验证逐条查找 observation，比较完整对话、历史时间（API 毫秒精度）、usage/timing 缺失值和结果选择 metadata，并回读评分值；任一缺失或不一致返回退出码 1，报告列出 record_id 和原因。入库尚未完成时稍后重复 verify。对 Langfuse v3 使用 `--api-version v3`，切换到对应读接口。不要因 API 接收成功就认定已验证。

## 5. 页面使用

1. 选择覆盖历史 trial 的时间范围（本批轨迹发生于 2026 年 9 月，不能只选导入当天）。
2. 通过标签 `linux-terminal-bench` 和 `import:<批次 UUID>` 选中本批数据。
3. 按 metadata 的 `product`、`task`、`verifier_status`、`quality_tier` 筛选；按 task 查看不同产品或 attempt。
4. 打开一条 trace，查看 root 的 Input / Output。Output 展示任务期间的完整有序对话和工具调用；metadata 保存缺失值、数据来源和消息细节。
5. 失败复盘用 `verifier_status=failed`；基础设施故障单独筛选，不能解释为模型答错。
6. 在单批次范围内，仅 `runner_reward_latest` 的均值可以用于所选有效轨迹的成功率。`runner_reward` 的全量均值会混入多次尝试。

当前版本不产生伪造的逐步耗时瀑布图，也不提供容器重放。图形界面的具体字段布局随 Langfuse 版本变化；完整数据是否入库以 verify 的 API 回读为准。

## 运维和测试

```bash
# 状态；启动失败时查看此项目 web/worker 日志
# 用 sudo 还是 docker 用户组取决于服务器的 Docker 权限配置。
docker compose --env-file work/linux-terminal-bench/langfuse/server.env \
  -f astra/runners/linux_terminal_bench/langfuse/compose.yaml ps

# 停止分析服务，保留所有数据卷
docker compose --env-file work/linux-terminal-bench/langfuse/server.env \
  -f astra/runners/linux_terminal_bench/langfuse/compose.yaml stop

# 本地测试（不连接 Langfuse）
python3 -m unittest astra.runners.linux_terminal_bench.tests.test_langfuse_import
```

备份需保留 `server.env`、导入包和 Compose 的 postgres/clickhouse/minio/redis 数据卷。不要使用 `down -v`，它会删除持久数据。部署镜像使用官方 major 标签；升级前备份并核对官方升级说明。

参考：[官方 OTLP 接口](https://langfuse.com/integrations/native/opentelemetry)、[公开 API](https://langfuse.com/docs/api-and-data-platform/features/public-api)、[官方自托管说明](https://langfuse.com/self-hosting)。

## 当前导入包与执行记录（2026-09-14）

- 2026-09-11 在 VM 的 Langfuse **4.33.0** 中完成 433 条轨迹、751 条评分的导入和逐条回读，验证失败为 0。该次完成后 VM 上的六个 Langfuse 容器已停止，数据卷保留。
- 已验证批次为 `175587a6-fa60-4d25-930d-eb24f3e8dc45`。重试和增量导入继续复用该批次及原有记录 ID，不重新执行 `prepare` 覆盖历史身份。
- 2026-09-14 将 28 条新 Hermes complete 轨迹追加到数据集，并按当前 runner 选择更新 27 条旧 Hermes attempt 的 `runner_reward_latest`。当前包共 **461 条轨迹、780 条评分**。
- 可变工作副本为 `work/linux-terminal-bench/langfuse/import-cleaned-106-complete.jsonl`；可提交和分发的副本为 `astra/datasets/linux-terminal-bench-trajectory/langfuse/import-cleaned`。
- 当前 461/780 包尚未执行新的上传与回读验证。导入后必须运行 `verify --api-version v4`；在验证报告失败数为 0 前，不应宣称增量数据已经入库。
- 核对范围包括完整对话、trial 起止时间、usage/timing（含 null）、有效 reward、最新 attempt 标记和评分值。API accepted 只表示接收请求，不代表后台持久化完成。

| 产品 | 当前包轨迹 | 选中且具有有效 reward 的轨迹 |
| --- | ---: | ---: |
| Astra | 133 | 85 |
| Hermes | 136 | 89 |
| Pi | 97 | 87 |
| DSH | 95 | 88 |
| 合计 | 461 | 349 |

该表右列遵循当前包生成时 runner 的 attempt 选择和有效性规则。metadata-only 被排除后，不会回退选择旧 attempt；成功率分析必须限定批次并只聚合 `runner_reward_latest`。
