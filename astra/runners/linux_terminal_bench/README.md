# Linux Terminal-Bench 2.1 四产品统一测评

本目录是独立于历史 macOS runner 的 Linux 测评入口。它复用已有的
Astra、Hermes、Pi、DSH adapter，不复制产品执行逻辑；队列、资源调度、
resume、verifier 有效性和汇总由这里统一控制。

离线轨迹查看与分析见 [Langfuse 部署、导入和使用说明](LANGFUSE.md)。
该流程复用当前 runner 的得分判定，独立运行，不改变测评执行逻辑。

## 冻结口径

- Host 与 Docker daemon：原生 Linux amd64。`tune-mjcf` 需要这个条件。
- 数据集：Terminal-Bench 2.1 固定提交
  `5c8eadf1f393183288fa08b8f73ca9a469cc5e00`，完整 89 题，不排除
  `tune-mjcf`。
- 模型：四产品统一使用官方 `glm-5.2`，thinking enabled、
  `reasoning_effort=high`。支持 temperature 的调用固定为 `0`。
- 一次命令只运行一个产品；同一产品内部最多 3 个 task 并行。
- DSH runtime wheel 在宿主机缓存并校验一次，再上传到各任务容器；任务容器
  不直接访问 Python 包源。
- 四产品共用 `docker-compose-dns.yaml`，任务容器使用 `8.8.8.8`，避免宿主
  ShellCrash 的 fake-IP DNS 泄漏到 Docker 后造成 TLS 证书域名错配；overlay
  同时注册 `host.docker.internal`，供 Astra 容器访问宿主 API。
- 调度沿用 Pi/DSH 逻辑：3 个 2GB memory token、6 CPU；8GB task 独占，
  4GB task 可与一个 2GB task 并行，最多三个 2GB task 并行。
- 四产品的产品执行预算均为原始 `[agent].timeout_sec × 1.0`。Harbor 顶层
  `timeout_multiplier=1.0`；`agent_timeout_multiplier=1.25` 只提供安装、
  controller、清理和日志落盘的外层余量，不增加产品可执行时间。
- Verifier 独立使用 `verifier_timeout_multiplier=2.0`，为系统依赖和大型 Python
  wheel 下载提供余量；该设置不增加产品执行预算。
- 完成判定统一要求数值 reward 为 0/1，且 `verifier/ctrf.json` 证明至少
  有一个测试实际完成；同时排除已确认的 verifier 前置条件失败：OCaml
  测试套件 TLS 下载失败后断言空输出、C4 测试数据 fixture 网络不可达且
  对应缓存缺失。判定同时检查失败 trace 和因果日志，不仅凭缺文件或网络
  警告排除结果；这些规则不代表已覆盖所有基础设施故障。
  Verifier 基础设施失败和 DSH `finish_reason=error` 保持 pending，不补记 0 分。
- 汇总中的 `raw_reward` 保留 Harbor result 的原值，`reward` 表示有效得分；
  verifier 无效时后者为 null（CSV 空白）。原始 trial 文件不改写。
  运行取消且没有 reward 时单列 `execution_incomplete`，不冒充 verifier 故障。
  下次普通启动会重新计算 pending；已有报告和显式 `--retry-queue` 文件
  不会因修改判定代码而自动刷新，89 题总体范围不变。
- C0 lifecycle audit 与官方 verifier reward 分列；audit 不覆盖或改写 reward。

四个产品保留各自冻结版本与原生模型迭代机制。Pi 0.73.1 使用
`thinking=high`；Astra 使用 `glm-5.2(thinking:high)`；Hermes 使用
`reasoning_effort: high`；DSH 的 GLM profile 将 `high` 交给原生
`llm-pi-ai` 路由。统一项是 task、目标模型、产品时限、外层调度、verifier
和结果选择，不把历史上不同含义的 “turn” 强行改写成同一种内部计数。

## 目录与结果隔离

新运行只写入：

```text
work/linux-terminal-bench/<product>/
  jobs/                 Harbor 原始 trial
  generated/tasks/      Pi/Hermes 预构建 task 副本
  state/resource.queue.tsv
  state/pending.queue.tsv
  state/run-manifest.json
  state/analysis/summary.json
  state/analysis/summary.csv
```

不会读取或追加历史的 `work/*-c0-*-jobs`。Resume 只接受相同 agent、模型、
版本、profile 和 `product_timeout_multiplier=1.0` 的最新 attempt。

## 前置准备

从仓库根目录执行。需要 Harbor 0.20.0、Docker、Git、curl/file，以及本地
Terminal-Bench checkout：

```bash
git clone https://github.com/harbor-framework/terminal-bench-2-1.git \
  work/terminal-bench-2-1
git -C work/terminal-bench-2-1 checkout --detach \
  5c8eadf1f393183288fa08b8f73ca9a469cc5e00

export MOI_BENCH_DATA_ROOT="$PWD"
export HARBOR_BIN="$HOME/.local/share/uv/tools/harbor/bin/harbor"

docker info
"$HARBOR_BIN" --version
git -C work/terminal-bench-2-1 rev-parse HEAD
```

当前宿主使用 ShellCrash 时，还需要让 Docker daemon 通过其 mixed proxy
拉取任务镜像；容器内 DNS overlay 不能影响 daemon 自身的镜像请求：

```bash
sudo install -d /etc/systemd/system/docker.service.d
sudo install -m 0644 \
  astra/runners/linux_terminal_bench/docker-shellcrash-proxy.conf \
  /etc/systemd/system/docker.service.d/shellcrash-proxy.conf
sudo systemctl daemon-reload
sudo systemctl restart docker
```

### Astra `optimize_0731_05`

将新分支 checkout 放在独立目录，不覆盖历史 `external/astra`：

```bash
git clone --branch optimize_0731_05 --single-branch \
  https://github.com/matrixorigin/astra.git \
  external/astra-optimize_0731_05

ASTRA_SOURCE_ROOT="$PWD/external/astra-optimize_0731_05" \
ASTRA_LINUX_BUILD_ROOT="$PWD/work/astra-optimize-0731-05-linux-amd64" \
  /bin/bash astra/runners/astra_terminal_bench/build-linux-portable.sh \
  --arch amd64
```

启动该分支的 Astra API/MatrixOne 服务：

```bash
ASTRA_SOURCE_DIR="$PWD/external/astra-optimize_0731_05" \
  /bin/bash astra/runners/scripts/start-astra-matrixone.sh

export ASTRA_SOURCE_ROOT="$PWD/external/astra-optimize_0731_05"
export ASTRA_TBENCH_LINUX_BINARY="$PWD/work/astra-optimize-0731-05-linux-amd64/target/release/astra"
export ASTRA_API_URL="http://host.docker.internal:17101"
```

Astra 服务端的 `.models.yaml` 需要将 `glm-5.2` 注册到 Z.AI Coding Plan
官方 API，并填写 `ZAI_API_KEY` 对应的密钥；密钥不要写入仓库。

Runner 要求 checkout 当前分支名为 `optimize_0731_05`，并把实际 Git commit
写入 run manifest；不额外引入分支内容 hash。

## 预检与运行

预检会验证 Linux/amd64、Docker、89 题、数据集 Git 状态、Harbor 版本及
产品静态前置条件，不构建镜像、不调用模型：

```bash
python3 -m astra.runners.linux_terminal_bench.run \
  --product pi --check
```

先用一题 smoke：

```bash
export ZAI_API_KEY='...'
python3 -m astra.runners.linux_terminal_bench.run \
  --product pi --case modernize-scientific-stack
```

正式运行 89 题：

```bash
# Astra：先按上节启动 API
python3 -m astra.runners.linux_terminal_bench.run --product astra

# Hermes
export ZAI_API_KEY='...'
python3 -m astra.runners.linux_terminal_bench.run --product hermes

# Pi
export ZAI_API_KEY='...'
python3 -m astra.runners.linux_terminal_bench.run --product pi

# DSH
export ZAI_API_KEY='...'
python3 -m astra.runners.linux_terminal_bench.run --product dsh
```

中断后重复同一命令即可基于有效 verifier evidence 继续 pending task。
每个并行 `harbor run` 都使用包含微秒时间和 task 名的独立 job name，避免
多个 task 在同一秒启动时复用 Harbor 默认目录。
`--max-tasks N` 限制本次启动数量，`--max-workers N` 可降低产品内并行度。
需要明确重跑已完成任务时使用 `--rerun-completed`，或传入 canonical TSV
子集：

```bash
python3 -m astra.runners.linux_terminal_bench.run \
  --product dsh \
  --retry-queue work/linux-terminal-bench/dsh/state/retry.queue.tsv
```

不要同时启动两个不同产品的正式命令；“并行”只指当前单个产品内部的
Terminal-Bench task 并行。
