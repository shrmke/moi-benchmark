#!/usr/bin/env python3
"""Build the four-product Toolathlon comparison including latest DSH results."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OUT_DIR = Path(__file__).resolve().parent
ROOT = OUT_DIR.parents[2]
DEFAULT_RESULTS = ROOT / "astra/results"
DEFAULT_THREE_PRODUCT_CSV = OUT_DIR / "astra-hermes-pi-toolathlon-108-task-results.csv"
DEFAULT_OUTPUT = OUT_DIR / "astra-hermes-pi-dsh-toolathlon-108-task-comparison.md"
SYSTEMS = ("astra", "hermes", "pi", "dsh")
DISPLAY = {"astra": "Astra", "hermes": "Hermes", "pi": "Pi", "dsh": "DSH"}

sys.path.insert(0, str(OUT_DIR))
from generate_astra_pi_hermes_comparison import summarize_system  # noqa: E402
from generate_dsh_all_attempt_results import collect_rows  # noqa: E402
from generate_dsh_latest_result_report import latest_rows  # noqa: E402


def number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_number(value: Any, digits: int = 2) -> str:
    numeric = number(value)
    if numeric is None:
        return "—"
    if numeric.is_integer():
        return f"{int(numeric):,}"
    return f"{numeric:,.{digits}f}"


def format_whole(value: Any) -> str:
    numeric = number(value)
    return "—" if numeric is None else f"{int(numeric + 0.5):,}"


def format_duration(value: Any) -> str:
    numeric = number(value)
    if numeric is None:
        return "—"
    if numeric >= 3600:
        return f"{numeric / 3600:,.2f} h"
    if numeric >= 120:
        return f"{numeric / 60:,.2f} min"
    return f"{numeric:,.2f} s"


def format_share(value: Any, total: Any) -> str:
    numerator = number(value)
    denominator = number(total)
    if numerator is None or not denominator:
        return "—"
    return f"{int(numerator):,}（{numerator / denominator:.2%}）"


def task_names(tasks: list[dict[str, Any]]) -> str:
    return "、".join(f"`{task['task_id']}`（{task['position']}）" for task in tasks) or "无"


def load_three_product_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 324:
        raise RuntimeError(f"expected 324 Astra/Hermes/Pi rows, found {len(rows)}")
    return rows


def status_code(value: Any) -> str:
    if value == "pass":
        return "P"
    if value == "no_pass":
        return "F"
    return "U"


def outcome_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    by_key = {(int(row["position"]), str(row["system"])): row for row in rows}
    groups: dict[str, list[dict[str, Any]]] = {}
    tasks = []
    for position in range(1, 109):
        current = {system: by_key[(position, system)] for system in SYSTEMS}
        statuses = {system: str(current[system].get("verify_status")) for system in SYSTEMS}
        code = "".join(status_code(statuses[system]) for system in SYSTEMS)
        task = {
            "position": position,
            "task_id": str(current["astra"]["task_id"]),
            **statuses,
        }
        groups.setdefault(code, []).append(task)
        tasks.append(task)
    return groups, tasks


def pairwise(tasks: list[dict[str, Any]], left: str, right: str) -> dict[str, int]:
    eligible = [
        task
        for task in tasks
        if task[left] in {"pass", "no_pass"} and task[right] in {"pass", "no_pass"}
    ]
    counts = Counter()
    for task in eligible:
        left_pass = task[left] == "pass"
        right_pass = task[right] == "pass"
        if left_pass and right_pass:
            counts["both"] += 1
        elif left_pass:
            counts["left"] += 1
        elif right_pass:
            counts["right"] += 1
        else:
            counts["neither"] += 1
    return {"eligible": len(eligible), **counts}


def source_table(rows: list[dict[str, Any]], system: str) -> str:
    counts = Counter(str(row.get("source") or row.get("batch")) for row in rows if row["system"] == system)
    return "\n".join(f"| {DISPLAY[system]} | `{source}` | {count} |" for source, count in counts.items())


def failure_count(summary: dict[str, Any], category: str) -> int:
    return int(summary["failure_categories"].get(category, 0))


def markdown(
    rows: list[dict[str, Any]],
    summaries: dict[str, dict[str, Any]],
    groups: dict[str, list[dict[str, Any]]],
    tasks: list[dict[str, Any]],
) -> str:
    group_rows = "\n".join(
        f"| `{code}` | {len(items)} | {task_names(items)} |"
        for code, items in sorted(
            groups.items(),
            key=lambda item: ("U" in item[0], -item[0].count("P"), item[0]),
        )
    )
    pair_rows = []
    for left, right in (
        ("astra", "hermes"),
        ("astra", "pi"),
        ("astra", "dsh"),
        ("hermes", "pi"),
        ("hermes", "dsh"),
        ("pi", "dsh"),
    ):
        result = pairwise(tasks, left, right)
        pair_rows.append(
            f"| {DISPLAY[left]} vs {DISPLAY[right]} | {result['eligible']} | "
            f"{result.get('both', 0)} | {result.get('left', 0)} | "
            f"{result.get('right', 0)} | {result.get('neither', 0)} |"
        )
    result_rows = []
    for system in SYSTEMS:
        summary = summaries[system]
        passed = summary["verify"].get("pass", 0)
        failed = summary["verify"].get("no_pass", 0)
        unresolved = 108 - summary["evaluated"]
        result_rows.append(
            f"| {DISPLAY[system]} | {passed} | {failed} | {unresolved} | "
            f"{passed / 108:.2%} | {passed / summary['evaluated']:.2%} |"
        )
    failure_rows = "\n".join(
        f"| {DISPLAY[system]} | "
        f"{failure_count(summaries[system], 'completed_but_no_pass')} | "
        f"{failure_count(summaries[system], 'model_request_budget')} | "
        f"{failure_count(summaries[system], 'product_error')} | "
        f"{failure_count(summaries[system], 'agent_deadline')} | "
        f"{sum(count for name, count in summaries[system]['failure_categories'].items() if name not in {'completed_but_no_pass', 'model_request_budget', 'product_error', 'agent_deadline'})} |"
        for system in SYSTEMS
    )
    time_rows = []
    for key, label in (
        ("e2e_seconds", "端到端"),
        ("agent_seconds", "Agent 执行"),
        ("evaluator_seconds", "Evaluator"),
        ("orchestration_seconds", "Orchestration/收尾"),
    ):
        for system in SYSTEMS:
            stats = summaries[system]["time"][key]
            time_rows.append(
                f"| {label} | {DISPLAY[system]} | {stats['n']} | "
                f"{format_duration(stats['sum'])} | {format_duration(stats['mean'])} | "
                f"{format_duration(stats['median'])} | {format_duration(stats['p90'])} |"
            )
    tool_metrics = (
        ("有工具计数的运行", lambda summary: summary["tools"]["calls"]["n"]),
        ("工具调用总数", lambda summary: summary["tools"]["calls"]["sum"]),
        ("单运行平均", lambda summary: summary["tools"]["calls"]["mean"]),
        ("单运行中位数", lambda summary: summary["tools"]["calls"]["median"]),
        ("单运行 P90", lambda summary: summary["tools"]["calls"]["p90"]),
        ("失败工具事件总数", lambda summary: summary["tools"]["failures"]["sum"]),
    )
    tool_rows = "\n".join(
        f"| {label} | " + " | ".join(format_number(getter(summaries[system])) for system in SYSTEMS) + " |"
        for label, getter in tool_metrics
    )
    request_metrics = (
        ("统计运行数", lambda summary: summary["requests"]["model_requests_started"]["n"]),
        ("模型请求 started", lambda summary: summary["requests"]["model_requests_started"]["sum"]),
        ("模型请求 completed event", lambda summary: summary["requests"]["model_requests_completed"]["sum"]),
        ("失败 completed event", lambda summary: summary["requests"]["model_requests_failed"]["sum"]),
        ("单运行 started 平均", lambda summary: summary["requests"]["model_requests_started"]["mean"]),
        ("单运行 started 中位数", lambda summary: summary["requests"]["model_requests_started"]["median"]),
        ("单运行 started P90", lambda summary: summary["requests"]["model_requests_started"]["p90"]),
        ("触及 100 请求上限", lambda summary: summary["request_limit_reached"]),
        ("因请求预算 no-pass", lambda summary: failure_count(summary, "model_request_budget")),
    )
    request_rows = [
        f"| {label} | " + " | ".join(format_number(getter(summaries[system])) for system in SYSTEMS) + " |"
        for label, getter in request_metrics
    ]
    request_rows.append(
        "| 流式请求 | "
        + " | ".join(
            format_share(
                summaries[system]["requests"]["stream_requests"]["sum"],
                summaries[system]["requests"]["model_requests_started"]["sum"],
            )
            for system in SYSTEMS
        )
        + " |"
    )
    request_rows.append(
        "| 非流式请求 | "
        + " | ".join(
            format_share(
                summaries[system]["requests"]["non_stream_requests"]["sum"],
                summaries[system]["requests"]["model_requests_started"]["sum"],
            )
            for system in SYSTEMS
        )
        + " |"
    )
    reliable_token_rows = []
    visible_token_rows = []
    pass_token_rows = []
    for system in SYSTEMS:
        summary = summaries[system]
        token = summary["tokens"]
        reliable_token_rows.append(
            f"| {DISPLAY[system]} | {token['reliable_records']} / {summary['executed']} | "
            f"{format_whole(token['reliable_input']['sum'])} | {format_whole(token['reliable_output']['sum'])} | "
            f"{format_whole(token['reliable_total']['sum'])} | {format_whole(token['reliable_total']['median'])} |"
        )
        visible_token_rows.append(
            f"| {DISPLAY[system]} | {summary['executed']} | "
            f"{token['reported_completed_requests']:,} / {token['missing_usage_completed_requests']:,} | "
            f"{format_whole(token['input_visible']['sum'])} | {format_whole(token['output_visible']['sum'])} | "
            f"{format_whole(token['total_visible']['sum'])} | {format_whole(token['total_visible']['median'])} |"
        )
        pass_count = summary["verify"].get("pass", 0)
        pass_token_rows.append(
            f"| {DISPLAY[system]} | {token['pass_reliable_records']} / {pass_count} | "
            f"{format_whole(token['pass_reliable_total']['sum'])} | "
            f"{format_whole(token['pass_reliable_total']['mean'])} | "
            f"{format_whole(token['pass_reliable_total']['median'])} |"
        )

    dsh_rows = [row for row in rows if row["system"] == "dsh"]
    dsh_transport_total = sum(int(number(row.get("model_transport_errors")) or 0) for row in dsh_rows)
    dsh_transport_tasks = sum((number(row.get("model_transport_errors")) or 0) > 0 for row in dsh_rows)
    dsh_errors: Counter[str] = Counter()
    for row in dsh_rows:
        raw = row.get("model_error_type_counts")
        if raw:
            value = raw if isinstance(raw, dict) else json.loads(str(raw))
            dsh_errors.update({str(name): int(count) for name, count in value.items()})
    dsh_error_text = "、".join(f"`{name}` {count:,}" for name, count in dsh_errors.most_common())
    top_tool_text = "\n".join(
        f"- {DISPLAY[system]}："
        + "、".join(f"`{name}` {count:,}" for name, count in summaries[system]["tools"]["top_terminal_tools"][:5])
        + "。"
        for system in SYSTEMS
    )
    pi_dsh = pairwise(tasks, "pi", "dsh")
    dsh_hermes = pairwise(tasks, "hermes", "dsh")
    date = datetime.now(timezone.utc).date().isoformat()

    return f"""# Astra、Hermes、Pi 与 DSH：Toolathlon 108 题对比分析

生成日期：{date}

## 口径

- Astra、Hermes 和 Pi 沿用既有三产品报告的 effective result；DSH 对所有批次中的每个任务按 `started_at` 选择最新一次 attempt。
- Astra、Hermes、DSH 均有 108 个明确 evaluator 结果；Pi 有 104 个明确结果、3 个 `unavailable` 和 1 个 `incomplete`。涉及 Pi 的组合与配对不会把这 4 题计作失败。
- 通过与否只以 evaluator 为准。时间、工具、模型请求和 token 是各产品整体运行时的观测足迹，不是同构 agent loop。
- Pi 和 DSH 都是多批次 effective projection。DSH 恢复运行还跨越了模型凭据更换和网络稳定性修复，因此本报告不是同一时段、同一凭据下的严格同步实验。
- 所有产品使用相同的 Toolathlon 任务顺序和 `deepseek-v4-flash` 模型请求 ID，但产品主循环、prompt、工具封装、缓存和重试策略不同。

### Effective result 来源

| 产品 | 来源 | 采用题数 |
| --- | --- | ---: |
{source_table(rows, 'pi')}
{source_table(rows, 'dsh')}

Astra/Hermes 继续采用既有正式投影；上表重点披露存在多层覆盖的 Pi 与 DSH。

## 实验产品与配置

| 项目 | Astra | Hermes | Pi | DSH |
| --- | --- | --- | --- | --- |
| 产品版本 | release build，CLI `astra 0.1.0` | project `0.19.0` | `0.73.1` Linux x64 | `0.1.0-rc.7` headless |
| API 模型 ID | `deepseek-v4-flash` | `deepseek-v4-flash` | `deepseek-v4-flash` | `deepseek-v4-flash` |
| 模型提供方 | DeepSeek 官方 API，经本地代理 | 同左 | 同左 | DeepSeek 官方 API，经 Node/Undici sidecar |
| 推理配置 | Thinking enabled，`reasoning_effort=max` | 同左 | 同左 | 同左 |
| Temperature | 发送 `temperature=0` | 同左 | 同左 | 同左 |
| 产品原生 max turns | 300 | 90 | 未显式设置 | 未显式设置 |
| 外部统一请求预算 | 每题最多 100 次 product model request | 同左 | 同左 | 同左 |
| Agent deadline | R1/R2/R3/R4：1800/2700/3600/5400 秒 | 同左 | 同左 | 同左 |
| Prompt 口径 | 原生 system prompt + Toolathlon 公共指令 | 同左 | append 公共指令 | headless 原生 prompt + 公共指令 |
| 工具范围 | 保留内置工具；提供当前任务 MCP | 同左 | 同左 | 同左 |

四种产品的 turn 定义不同。本报告统一使用代理观测到的 `model_request.started`，不把模型请求数直接解释为用户可见回合。

## 基础运行环境

| 环境项 | 配置 |
| --- | --- |
| 数据集 | Toolathlon，固定 108 题；涉及 Pi 的严格比较仅覆盖其 104 个明确 evaluator 结果 |
| Host | Ubuntu 22.04；同一 VM/硬件环境，Astra/Hermes/Pi 冻结记录为 Linux `5.15.0-186`，DSH 运行期为 `5.15.0-190` |
| CPU | Intel Xeon Platinum 8255C @ 2.50 GHz，x86_64，8 vCPU |
| 内存与 Swap | 约 7.8 GiB RAM、8 GiB swap |
| 虚拟化与容器 | Vagrant/KVM；rootful Docker，Toolathlon 固定任务镜像 |
| 单任务资源上限 | 8 CPU、8 GiB RAM、8 GiB swap |
| 外部应用状态 | 由 preprocess 恢复；部分本地应用和 Kind 服务使用共享部署 |
| 网络边界 | 未统一关闭公开互联网出口；模型请求均通过各自本地代理/sidecar |
| Evaluator | Agent 终止后独立执行 Toolathlon 每题原生 evaluator |

四产品运行时间不同，宿主机内核、外部服务和模型凭据并非完全冻结为同一瞬时状态；这些差异必须作为结果解释边界。

## 任务完成结果

| 产品 | pass | no-pass | 未明确/未完成 | 按 108 题通过率 | 已测评题通过率 |
| --- | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(result_rows)}

### 四方逐题组合

`P/F/U` 顺序固定为 Astra/Hermes/Pi/DSH；`U` 表示该产品没有明确 evaluator 判定。

| 结果组 | 题数 | 任务 |
| --- | ---: | --- |
{group_rows}

### 两两配对

| 配对 | 可比较题 | 两者均通过 | 仅左侧通过 | 仅右侧通过 | 两者均未通过 |
| --- | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(pair_rows)}

涉及 Pi 的配对只覆盖 104 个明确结果。在这 104 题上，DSH 相对 Pi 为 {pi_dsh.get('right', 0)} 个 DSH-only 对 {pi_dsh.get('left', 0)} 个 Pi-only；在 108 题上，DSH 相对 Hermes 为 {dsh_hermes.get('right', 0)} 个 DSH-only 对 {dsh_hermes.get('left', 0)} 个 Hermes-only。这是固定 benchmark 上的逐题描述，不是统计显著性结论。

## No-pass 原因

| 产品 | 完成但 evaluator 未通过 | 模型请求预算 | 产品执行错误 | Agent deadline | 其他 |
| --- | ---: | ---: | ---: | ---: | ---: |
{failure_rows}

该表只对明确 `no_pass` 做直接终态分类，不推断模型内部原因。DSH 另有 3 个 evaluator 已通过任务记录了产品执行错误终态，说明产品终态和 evaluator 结果不能机械等同。

## 时间消耗

| 阶段 | 产品 | 样本数 | 总计 | 平均 | 中位数 | P90 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(time_rows)}

时间包含通过和未通过任务。`orchestration` 是端到端减去 Agent 与 evaluator 后的剩余时间，不是纯准备时间。不同产品的收尾、重试和内部请求边界不同。

## 工具调用

| 指标 | Astra | Hermes | Pi | DSH |
| --- | ---: | ---: | ---: | ---: |
{tool_rows}

常见终态工具：

{top_tool_text}

工具名称、封装粒度和失败事件采集方式不同。失败事件可能在重试后恢复，不能直接作为产品可靠性排名。

## 模型请求

| 指标 | Astra | Hermes | Pi | DSH |
| --- | ---: | ---: | ---: | ---: |
{chr(10).join(request_rows)}

DSH 的最新投影中，{dsh_transport_tasks} / 108 题记录过 transport 错误，共 {dsh_transport_total:,} 次，类型为 {dsh_error_text}。这些 DSH 失败 completed event 主要表示 sidecar 未获得完整上游响应，并不是 DeepSeek HTTP/API 业务错误响应。四产品的请求结构和重试策略不同，因此请求总量不能直接解释为模型效率。

## Token 数据

### 保守可靠记录

若一个 effective run 的 completed response 存在缺失 usage，该运行不进入本表：

| 产品 | 完整 usage 运行 | 输入 token 总量 | 输出 token 总量 | total token 总量 | 单运行 total 中位数 |
| --- | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(reliable_token_rows)}

“保守可靠记录”采用运行级完备性口径：只有一个 effective run 的每个 `model_request.completed` 都具有 provider 明确上报的 input/output/total token，该运行才进入本表。对 DSH 而言，41 / 108 表示只有 41 题可以完整求和；其余 67 题至少有一次请求因 transport 中断缺失 usage，并不表示这些任务没有 token 数据或 token 为 0。

### 全部可见 token 下界

| 产品 | 运行数 | 已上报 / 缺 usage 的 completed 请求 | 输入 token 下界 | 输出 token 下界 | total token 下界 | 单运行 total 中位数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(visible_token_rows)}

“全部可见 token 下界”覆盖所有有完整 run artifact 的任务，只累加每个请求已经明确上报的 usage，缺失 usage 保持未知，不补零。DSH 的 142,673,137 个可见 total token 来自 2,257 次有 usage 的请求，其中包含成功响应、断开前已经拿到 usage 的失败响应，以及流失败后成功重试的新请求；另外 832 次失败请求没有 usage，可能仍已消耗完整输入和部分输出。因此该数值是 DSH 实际资源消耗的严格下界，而不是账单总量。

DSH 断流后会重放完整请求，所以可见下界可能接近“每个逻辑步骤一次成功”时的工作量，但不能当作无链路波动的反事实估计：部分失败请求已有可见 usage，重试生成也可能改变后续 Agent 路径。Pi 和 DSH 的 input 包含 provider 单独报告的 cache-read token；工具 schema、上下文组织、缓存和内部请求边界也不同，不能据此直接计算产品成本或底层模型效率。

### Pass 任务的可靠 Token

| 产品 | 有可靠 token 的 pass 任务 | total token 总量 | 单任务平均 | 单任务中位数 |
| --- | ---: | ---: | ---: | ---: |
{chr(10).join(pass_token_rows)}

四者通过的任务集合不同；该表只描述各自产生的可靠 token 足迹，不是单位成功成本的严格对比。

## 综合结论

1. **DSH 的已确认通过数最多，但 Pi 覆盖不完整。** DSH 为 79 / 108，Pi 为 77 / 104 个明确结果，Hermes 为 72 / 108，Astra 为 61 / 108。DSH 按全部 108 题通过率最高（73.15%），Pi 按已测评题通过率为 74.04%；Pi 的 4 个未明确 slot 使二者不能形成无条件总排名。
2. **逐题优势不是包含关系。** 在 Pi 与 DSH 可比较的 104 题中，DSH-only 为 {pi_dsh.get('right', 0)} 题，Pi-only 为 {pi_dsh.get('left', 0)} 题；产品主循环、工具策略与自检行为都会改变结果。
3. **No-pass 结构明显不同。** Astra 有 20 题因请求预算终止；Hermes 和 Pi 分别为 3、2 题；DSH 没有预算型 no-pass，但有 8 个产品执行错误和 1 个 Agent deadline。
4. **DSH 的典型运行时间接近 Pi，但网络重试污染请求足迹。** DSH 端到端中位数为 {format_duration(summaries['dsh']['time']['e2e_seconds']['median'])}，Pi 为 {format_duration(summaries['pi']['time']['e2e_seconds']['median'])}；DSH 的 {dsh_transport_total:,} 次 transport 错误使请求量和 token 完整率不能按正常稳定链路解释。
5. **模型请求不是同构 turn。** 四产品 started 中位数分别为 {format_number(summaries['astra']['requests']['model_requests_started']['median'])}、{format_number(summaries['hermes']['requests']['model_requests_started']['median'])}、{format_number(summaries['pi']['requests']['model_requests_started']['median'])}、{format_number(summaries['dsh']['requests']['model_requests_started']['median'])}；内部请求、流式策略和重试边界不同。
6. **Token 对比受完整率与缓存影响。** DSH 只有 {summaries['dsh']['tokens']['reliable_records']} / 108 个 latest run 具备完整 usage，明显受到 transport 失败影响；总 token 不能作为四产品能力或成本排名。
7. **Effective projection 是必要但有限的汇总。** Pi 混合 3 个批次，DSH 混合 8 个批次；最新或优先覆盖解决了 slot 选择问题，但没有消除运行时段、服务状态、凭据和网络条件差异。

## 附件

- 原三产品报告：[`astra-hermes-pi-toolathlon-108-task-comparison.md`](astra-hermes-pi-toolathlon-108-task-comparison.md)
- DSH 单产品报告：[`dsh-toolathlon-108-task-analysis.md`](dsh-toolathlon-108-task-analysis.md)
- Astra/Hermes/Pi 逐 slot 数据：[`astra-hermes-pi-toolathlon-108-task-results.csv`](astra-hermes-pi-toolathlon-108-task-results.csv)
- DSH 全批次逐 attempt 数据：[`dsh-toolathlon-all-attempt-results.csv`](dsh-toolathlon-all-attempt-results.csv)
- 本报告生成脚本：[`generate_astra_hermes_pi_dsh_comparison.py`](generate_astra_hermes_pi_dsh_comparison.py)
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the four-product Toolathlon report")
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--three-product-csv", type=Path, default=DEFAULT_THREE_PRODUCT_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    three_product = load_three_product_rows(args.three_product_csv.resolve())
    dsh = latest_rows(collect_rows(args.results_root.resolve()))
    system_order = {system: index for index, system in enumerate(SYSTEMS)}
    rows = sorted(
        three_product + dsh,
        key=lambda row: (int(row["position"]), system_order[str(row["system"])]),
    )
    if len(rows) != 432:
        raise RuntimeError(f"expected 432 four-product rows, found {len(rows)}")
    summaries = {
        system: summarize_system([row for row in rows if row["system"] == system])
        for system in SYSTEMS
    }
    groups, tasks = outcome_rows(rows)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown(rows, summaries, groups, tasks), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "systems": {
                    system: {
                        "pass": summaries[system]["verify"].get("pass", 0),
                        "no_pass": summaries[system]["verify"].get("no_pass", 0),
                        "evaluated": summaries[system]["evaluated"],
                    }
                    for system in SYSTEMS
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
