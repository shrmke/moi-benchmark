# Astra Terminal-Bench 2.1 shard-4 测评流程

本文中的文件系统路径均相对于 `MOI benchmark` 仓库根目录；除非命令块显式进入子目录，否则均应在仓库根目录执行。

本文档对应以下固定测评条件：

- Astra 源码：`external/astra-optimize_0731_05`
- case 清单：`astra/runners/astra_terminal_bench/tbench-2.1-shard-4.txt`
- 数据集：Terminal-Bench 2.1，共 89 个任务；本批次包含 shard-4 的 22 个任务
- 模型选择器：`glm-5.2(thinking:high)`
- temperature：请求值为 `0`；Astra thinking 协议不允许同时发送 temperature，因此实际模型请求中省略该字段
- memory：`read_memory=false`，每个 case 使用新隔离用户
- 调度：复用 Pi 的资源队列与多进程调度器
- 服务路线：MatrixOne、Memoria 和 Astra API 均由 Docker 启动；Astra API 从指定源码构建 Linux 镜像，不修改 Astra 源码

## 1. 关键文件

| 用途 | 路径 |
| --- | --- |
| pending 启动脚本 | `astra/runners/scripts/astra-terminal-bench-shard4-pending.sh` |
| 底层 Astra runner | `astra/runners/scripts/astra-terminal-bench-all-c0.sh` |
| 服务总启动脚本 | `astra/runners/scripts/start-astra-matrixone.sh` |
| Harbor 配置 | `astra/runners/astra_terminal_bench/c0-cases-glm52.yaml` |
| API timeout override | `astra/runners/astra_terminal_bench/docker-compose.benchmark-timeout.yml` |
| case 清单 | `astra/runners/astra_terminal_bench/tbench-2.1-shard-4.txt` |
| Terminal-Bench 数据集 | `work/terminal-bench-2-1/tasks` |
| Linux Astra CLI | `work/astra-optimize-0731-05-linux-amd64/target/release/astra` |
| 本地 Astra API 镜像 | `astra-optimize-0731-05:local` |
| 默认结果目录 | `work/astra-glm52-c0-shard4-jobs` |

## 2. 一次性准备

先启动 Docker Desktop。准备模型配置：

```bash
(
  cd external/astra-optimize_0731_05
  test -f .models.yaml || cp .models.yaml.example .models.yaml
)
```

编辑 `.models.yaml`，确保存在名为 `glm-5.2` 的模型，并填写实际的 `api_key` 与 `base_url`。不要把密钥写入测评脚本或提交到 Git。

当前启动路线不在 macOS 上编译 `astra-server`，也不修改 Astra Rust 源码。总启动脚本使用原始源码目录作为只读 Docker build context，在 Linux 构建阶段生成本地镜像 `astra-optimize-0731-05:local`。

## 3. 启动完整服务栈

```bash
./astra/runners/scripts/start-astra-matrixone.sh
```

脚本按以下顺序执行：

1. 使用 `docker-compose.deps.yml` 启动 MatrixOne 和 Memoria。
2. 等待 MatrixOne 容器健康、Memoria 容器健康及 Memoria 存储接口就绪。
3. 如果本地 Astra API 镜像不存在，从指定源码执行一次 Linux Docker 构建；后续启动直接复用镜像。
4. 在同一个 Compose 网络中启动 Astra API，并等待容器健康。
5. 从宿主机请求 API `/health`，确认 MatrixOne 和 Memoria 均已连接后退出脚本；服务继续在后台运行。

启动脚本会叠加 benchmark Compose override，将 API 进程的单次 LLM 总预算上限提高到 `27000` 秒。每个 case 的实际产品时长仍由数据集 `[agent].timeout_sec × 1.0` 控制，因此这个全局上限不会替代 case 级 timeout。

当前无 `.env` 开发路线的端口为：

| 服务 | 宿主机地址 | 容器内地址 |
| --- | --- | --- |
| MatrixOne | `127.0.0.1:6001` | `matrixone:6001` |
| Memoria | `127.0.0.1:18100` | `memoria:8100` |
| Astra API | `localhost:17101` | `api:17001` |

本机 VS Code 的 `Code Helper (Plugin)` 已占用 IPv4 `127.0.0.1:8100` 和 `127.0.0.1:17001`，因此宿主机映射使用 `18100` 和 `17101`。不要根据容器内端口把测评地址改回 `17001`。

首次 Docker 构建耗时较长属于正常现象。源码发生预期变更且需要重建镜像时，显式运行：

```bash
ASTRA_DOCKER_REBUILD=1 \
  ./astra/runners/scripts/start-astra-matrixone.sh
```

可选的超时覆盖：

```bash
SERVICE_START_TIMEOUT_SECONDS=300 \
API_START_TIMEOUT_SECONDS=300 \
  ./astra/runners/scripts/start-astra-matrixone.sh
```

脚本不会删除 MatrixOne 数据卷。只有明确需要重建数据库时，才单独删除 `matrixone-data`；不要把清理操作加入日常启动流程。

## 4. 健康检查与启动排障

```bash
curl --fail --silent --show-error \
  --connect-timeout 2 --max-time 5 \
  http://localhost:17101/health
```

期望响应至少包含：

```json
{"status":"healthy","database":"connected","memoria":"connected"}
```

如果启动失败，查看：

```bash
docker inspect all-in-one-matrixone-1 \
  --format 'status={{.State.Status}} health={{.State.Health.Status}} restarts={{.RestartCount}}'
docker inspect all-in-one-memoria-1 \
  --format 'status={{.State.Status}} health={{.State.Health.Status}} restarts={{.RestartCount}}'
docker inspect all-in-one-api-1 \
  --format 'status={{.State.Status}} health={{.State.Health.Status}} restarts={{.RestartCount}}'

docker logs --tail=200 all-in-one-matrixone-1
docker logs --tail=200 all-in-one-memoria-1
docker logs --tail=200 all-in-one-api-1
```

如果 Docker 显示 API 为 `healthy`，但宿主机请求超时，先用 `lsof -nP -iTCP:<端口> -sTCP:LISTEN` 检查是否被 VS Code 端口转发占用。当前已验证的宿主机 API 端口是 `17101`。

## 5. 导入并检查 glm-5.2

API 运行在 Linux 容器内，不使用 macOS 本机编译的 `astra-server`。首次初始化数据库时，将模型文件复制进 API 容器，再使用同一镜像内的 Astra CLI 创建或登录管理员并导入模型：

```bash
(
cd external/astra-optimize_0731_05

docker exec -i all-in-one-api-1 \
  sh -c 'umask 077; cat > /tmp/models.yaml' < .models.yaml

docker exec -it \
  -e ASTRA_API_URL=http://127.0.0.1:17001 \
  all-in-one-api-1 astra admin register
docker exec -it \
  -e ASTRA_API_URL=http://127.0.0.1:17001 \
  all-in-one-api-1 astra admin login
docker exec \
  -e ASTRA_API_URL=http://127.0.0.1:17001 \
  all-in-one-api-1 astra admin model load /tmp/models.yaml --update-existing
docker exec \
  -e ASTRA_API_URL=http://127.0.0.1:17001 \
  all-in-one-api-1 astra admin model check glm-5.2
docker exec \
  -e ASTRA_API_URL=http://127.0.0.1:17001 \
  all-in-one-api-1 astra admin model list
)
```

这些命令中的 `17001` 是 API 容器内部端口，不是 Terminal-Bench 使用的宿主机端口。`model check` 必须成功并使模型处于 active 状态。测评 runner 使用的完整选择器是 `glm-5.2(thinking:high)`，其中 `glm-5.2` 是已注册模型，`thinking:high` 是本次推理模式。

## 6. 测评前预检

```bash
export ASTRA_API_URL="http://host.docker.internal:17101"
./astra/runners/scripts/astra-terminal-bench-shard4-pending.sh --check
```

预检会完成以下检查：

1. 读取 shard-4 的 22 个 case，并检查空清单、重复项和不存在的任务。
2. 从默认结果目录识别已经得到有效 verifier 结果的 case。
3. 只输出仍为 pending 的 case。
4. 校验 Harbor 版本、Terminal-Bench 数据集版本和任务目录状态。
5. 校验 Astra API `/health`、Linux x86-64 Astra CLI 和模型参数。
6. 打印一个 case 的 Harbor 最终解析配置，但不创建正式 trial。

当前首次预检状态（2026-08-31）为 `0 completed / 22 pending`。预检会把 `http://host.docker.internal:17101` 转换为宿主机 `http://localhost:17101` 后检查 `/health`。如果 API 未启动，预检会在这里停止；这属于服务前置条件失败，不是 case 执行失败。

`ASTRA_API_URL` 是当前端口冲突规避路线的必要参数。打开新终端后需要重新导出，不要依赖 runner 的 `17001` 默认值。

## 7. 启动 pending case

建议在 macOS 上用 `caffeinate` 防止长任务期间休眠：

```bash
export ASTRA_API_URL="http://host.docker.internal:17101"
caffeinate -dimsu \
  ./astra/runners/scripts/astra-terminal-bench-shard4-pending.sh \
  --yes \
  --concurrency 3 \
  --run-name "astra-glm52-shard4-$(date '+%Y%m%d-%H%M%S')"
```

不要更换默认 `jobs-dir` 后再期待脚本识别上一轮完成结果。pending 状态只在同一个结果目录中连续计算。

### 并发资源策略

调度器复用 Pi 的资源策略：

- 总计 3 个内存 token、6 个 CPU 配额。
- 2 GB case 占 1 个 token，最多可并发 3 个。
- 4 GB case 占 2 个 token，可与一个 2 GB case 并发。
- 8 GB case 占满 3 个 token，必须独占运行。
- `--concurrency 3` 是 Harbor 子进程数上限；资源 token 仍可能把实际并发降到 1 或 2。
- 单个 case 失败不会终止其他 case，调度器结束时会返回非零状态。

### timeout 策略

- 每个 case 的基础时长直接读取其 `task.toml` 中的 `[agent].timeout_sec`。
- Astra 产品内任务 timeout 为数据集时长乘 `1.0`。
- Harbor agent 阶段 timeout 为数据集时长乘 `2.5`，包含安装、清理和轨迹写入开销。
- Harbor 总体 `timeout_multiplier=2.0`。
- 单次流式传输允许 2 次重试，并复用同一个 Astra session。

## 8. pending 的判定

脚本不会用“目录存在”或 `reward.txt` 是否存在判断完成。一个 case 只有同时满足以下条件才从 pending 队列移除：

1. 是当前 Astra C0 配置对应的正式 trial，不是 install-only。
2. 存在 `finished_at`。
3. `result.json` 中 verifier reward 是合法二值 `0` 或 `1`。
4. `verifier/ctrf.json` 存在，并记录至少一个实际执行的测试。
5. 不属于 verifier 基础设施异常。

因此 `reward=0` 但 verifier 证据完整表示“已评分失败”，不再 pending；缺失 `ctrf.json`、零测试或 verifier 基础设施异常仍然 pending。存在多次匹配尝试时，以 `finished_at` 最新的一次为准。

## 9. 断点续跑

服务或网络故障后，保持相同的 `jobs-dir`，重新执行预检：

```bash
export ASTRA_API_URL="http://host.docker.internal:17101"
./astra/runners/scripts/astra-terminal-bench-shard4-pending.sh --check
```

确认 pending 清单后，使用新的 `run-name` 继续：

```bash
export ASTRA_API_URL="http://host.docker.internal:17101"
caffeinate -dimsu \
  ./astra/runners/scripts/astra-terminal-bench-shard4-pending.sh \
  --yes \
  --concurrency 3 \
  --run-name "astra-glm52-shard4-retry-$(date '+%Y%m%d-%H%M%S')"
```

已经具有有效 verifier 证据的 case 会被自动跳过；未评分和 verifier 基础设施失败的 case 会再次进入队列。

## 10. 结果位置与检查

Harbor trial 默认写入：

```text
work/astra-glm52-c0-shard4-jobs
```

每个 trial 重点检查：

- `result.json`：任务、异常、结束时间、reward，以及 `agent_result.metadata` 中的模型、thinking、temperature 和 timeout 运行元数据。
- `verifier/ctrf.json`：实际执行测试的证据。
- `agent/controller.jsonl`：C0 生命周期控制器事件。
- `agent/astra-session.json`：Astra session 与产品终态。
- `agent/astra-trajectory/manifest.json`：服务端与本地轨迹导出的清单。

每批次的复现信息位于：

```text
work/astra-glm52-c0-shard4-jobs/.reproduction/<run-name>.tsv
work/astra-glm52-c0-shard4-jobs/.reproduction/<run-name>.queue.tsv
```

再次运行 `--check` 是查看“有效完成数 / pending 数”的推荐方式。报告统计时应将以下状态分开：

- `reward=1`：verifier 判定通过。
- `reward=0` 且证据有效：已评分失败。
- 无有效 verifier 证据：未评分/pending，不能当作 `reward=0`。
- Agent、Astra API、模型 provider、Harbor、Docker 环境和 verifier 异常应分别归因。
