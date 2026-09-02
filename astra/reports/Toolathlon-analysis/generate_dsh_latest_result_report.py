#!/usr/bin/env python3
"""Build the DSH Toolathlon report from each task's latest attempt."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any


OUT_DIR = Path(__file__).resolve().parent
ROOT = OUT_DIR.parents[2]
DEFAULT_RESULTS = ROOT / "astra/results"
DEFAULT_OUTPUT = OUT_DIR / "dsh-toolathlon-108-task-analysis.md"

sys.path.insert(0, str(OUT_DIR))
from generate_astra_hermes_comparison import percentile  # noqa: E402
from generate_dsh_all_attempt_results import collect_rows, parse_timestamp  # noqa: E402


def number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def truthy(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


def values(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [value for row in rows if (value := number(row.get(key))) is not None]


def descriptive(items: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(items),
        "sum": sum(items),
        "mean": mean(items) if items else None,
        "median": median(items) if items else None,
        "p90": percentile(items, 0.9),
    }


def latest_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    for row in rows:
        task_id = str(row["task_id"])
        key = (
            parse_timestamp(row.get("started_at")) or minimum,
            str(row.get("batch") or ""),
            str(row.get("run_id") or ""),
        )
        previous = selected.get(task_id)
        if previous is None:
            selected[task_id] = row
            continue
        previous_key = (
            parse_timestamp(previous.get("started_at")) or minimum,
            str(previous.get("batch") or ""),
            str(previous.get("run_id") or ""),
        )
        if key > previous_key:
            selected[task_id] = row
    result = sorted(selected.values(), key=lambda row: int(row["position"]))
    if len(result) != 108 or [int(row["position"]) for row in result] != list(range(1, 109)):
        raise RuntimeError("latest DSH projection must contain Toolathlon positions 1..108 exactly once")
    return result


def format_number(value: Any, digits: int = 2) -> str:
    numeric = number(value)
    if numeric is None:
        return "—"
    if numeric.is_integer():
        return f"{int(numeric):,}"
    return f"{numeric:,.{digits}f}"


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


def task_names(rows: list[dict[str, Any]]) -> str:
    return "、".join(f"`{row['task_id']}`（{row['position']}）" for row in rows) or "无"


def json_counter(rows: list[dict[str, Any]], key: str) -> Counter[str]:
    total: Counter[str] = Counter()
    for row in rows:
        raw = row.get(key)
        if not raw:
            continue
        value = raw if isinstance(raw, dict) else json.loads(str(raw))
        if isinstance(value, dict):
            total.update({str(name): int(count) for name, count in value.items()})
    return total


def describe_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [row for row in rows if row.get("state") == "complete"]
    evaluated = [row for row in complete if row.get("verify_status") in {"pass", "no_pass"}]
    passed = [row for row in evaluated if row.get("verify_status") == "pass"]
    failed = [row for row in evaluated if row.get("verify_status") == "no_pass"]
    reliable = [row for row in complete if truthy(row.get("token_reliable"))]
    reliable_passed = [row for row in passed if truthy(row.get("token_reliable"))]
    request_limit_reached = sum(
        number(row.get("model_request_limit")) is not None
        and number(row.get("model_requests_started")) is not None
        and number(row.get("model_requests_started")) >= number(row.get("model_request_limit"))
        for row in complete
    )
    return {
        "complete": complete,
        "evaluated": evaluated,
        "passed": passed,
        "failed": failed,
        "incomplete": [row for row in rows if row.get("state") != "complete"],
        "sources": Counter(str(row.get("batch")) for row in rows),
        "failure_categories": Counter(str(row.get("failure_category")) for row in failed),
        "pass_terminal_categories": Counter(
            str(row.get("failure_category")) for row in passed if row.get("failure_category") != "none"
        ),
        "time": {
            key: descriptive(values(complete, key))
            for key in ("e2e_seconds", "agent_seconds", "evaluator_seconds", "orchestration_seconds")
        },
        "tools": {
            "calls": descriptive(values(complete, "tool_calls")),
            "failures": descriptive(values(complete, "tool_failures")),
            "terminal": json_counter(complete, "terminal_tool_counts"),
        },
        "requests": {
            key: descriptive(values(complete, key))
            for key in (
                "model_requests_started",
                "model_requests_completed",
                "model_requests_failed",
                "model_requests_successful_events",
                "stream_requests",
                "non_stream_requests",
                "model_transport_errors",
                "model_http_error_responses",
                "model_provider_error_responses",
            )
        },
        "request_limit_reached": request_limit_reached,
        "transport_affected": sum(number(row.get("model_transport_errors")) > 0 for row in complete),
        "transport_affected_failed": sum(number(row.get("model_transport_errors")) > 0 for row in failed),
        "model_errors": json_counter(complete, "model_error_type_counts"),
        "tokens": {
            "reliable": reliable,
            "reliable_passed": reliable_passed,
            "reliable_input": descriptive(values(reliable, "token_input")),
            "reliable_output": descriptive(values(reliable, "token_output")),
            "reliable_total": descriptive(values(reliable, "token_total")),
            "visible_input": descriptive(values(complete, "token_input")),
            "visible_output": descriptive(values(complete, "token_output")),
            "visible_total": descriptive(values(complete, "token_total")),
            "cache_read": descriptive(values(complete, "cache_read_tokens")),
            "reported_completed": sum(int(number(row.get("token_usage_reported_completed")) or 0) for row in complete),
            "missing_completed": sum(int(number(row.get("token_usage_missing_completed")) or 0) for row in complete),
            "pass_total": descriptive(values(reliable_passed, "token_total")),
        },
    }


def category_label(value: str) -> str:
    return {
        "completed_but_no_pass": "Agent 完成，但 evaluator 未通过",
        "model_request_budget": "达到模型请求预算",
        "product_error": "产品执行错误",
        "agent_deadline": "Agent deadline",
        "infra_incomplete": "基础设施未完成",
    }.get(value, value)


def report(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    complete = summary["complete"]
    evaluated = summary["evaluated"]
    passed = summary["passed"]
    failed = summary["failed"]
    incomplete = summary["incomplete"]
    failures = summary["failure_categories"]
    time = summary["time"]
    tools = summary["tools"]
    requests = summary["requests"]
    tokens = summary["tokens"]
    started_values = [parse_timestamp(row.get("started_at")) for row in rows]
    started_values = [value for value in started_values if value is not None]
    source_rows = "\n".join(
        f"| `{source}` | {count} |" for source, count in summary["sources"].items()
    )
    no_pass_rows = "\n".join(
        "| {position} | `{task_id}` | {terminal} | {category} | {requests} | {transport} | `{batch}` |".format(
            position=row["position"],
            task_id=row["task_id"],
            terminal=row.get("terminal_status") or "—",
            category=category_label(str(row.get("failure_category"))),
            requests=format_number(row.get("model_requests_started")),
            transport=format_number(row.get("model_transport_errors")),
            batch=row.get("batch"),
        )
        for row in failed
    )
    incomplete_rows = "\n".join(
        f"| {row['position']} | `{row['task_id']}` | {row.get('incomplete_stage') or '—'} | {row.get('incomplete_reason') or '—'} |"
        for row in incomplete
    ) or "| — | 无 | — | — |"
    time_rows = "\n".join(
        "| {label} | {n} | {total} | {average} | {middle} | {p90} |".format(
            label=label,
            n=time[key]["n"],
            total=format_duration(time[key]["sum"]),
            average=format_duration(time[key]["mean"]),
            middle=format_duration(time[key]["median"]),
            p90=format_duration(time[key]["p90"]),
        )
        for key, label in (
            ("e2e_seconds", "端到端"),
            ("agent_seconds", "Agent 执行"),
            ("evaluator_seconds", "Evaluator"),
            ("orchestration_seconds", "Orchestration/收尾"),
        )
    )
    top_tools = "、".join(
        f"`{name}` {count:,}" for name, count in tools["terminal"].most_common(10)
    )
    error_types = "、".join(
        f"`{name}` {count:,}" for name, count in summary["model_errors"].most_common()
    ) or "无"
    pass_rate = len(passed) / 108
    evaluated_rate = len(passed) / len(evaluated) if evaluated else math.nan
    visible_requests = requests["model_requests_started"]["sum"]
    reliable_pass_count = len(tokens["reliable_passed"])
    pass_product_errors = sum(summary["pass_terminal_categories"].values())
    date = datetime.now(timezone.utc).date().isoformat()
    first_start = min(started_values).isoformat() if started_values else "—"
    last_start = max(started_values).isoformat() if started_values else "—"

    return f"""# DeepSeek Harness（DSH）：Toolathlon 108 题结果分析

生成日期：{date}
范围：Toolathlon 第 1–108 题，DeepSeek Harness（DSH）0.1.0-rc.7。

## 口径

- 从所有 DSH 批次的 attempt 中，对每个 `task_id` 按 `started_at` 选择时间最新的一次；时间相同再以批次名和 `run_id` 确定顺序。
- 本投影覆盖 108 个任务，最新 attempt 全部具有 Agent 执行和 evaluator 结果。`pass`/`no_pass` 只以 evaluator 为准。
- 结果是多个基础批次与恢复批次组成的 effective projection，不代表 108 题在同一批次连续执行。采用 attempt 的开始时间范围为 `{first_start}` 至 `{last_start}`。
- 时间、工具调用、模型请求和 token 均为 DSH 产品整体运行时的观测口径；模型请求包含 transport 重试，不等同于用户可见对话轮数。
- Token 仅汇总 provider 已上报的 usage；缺失 usage 不补零。

### 最新结果来源

| 批次 | 采用题数 |
| --- | ---: |
{source_rows}

## 实验产品与配置

| 项目 | DSH |
| --- | --- |
| 产品版本 | DeepSeek Harness `0.1.0-rc.7`，headless profile |
| Node.js | `22.19.0` |
| API 模型 ID | `deepseek-v4-flash` |
| 模型提供方 | DeepSeek 官方 API，经每次运行独立的本地 Node/Undici sidecar |
| 推理配置 | Thinking enabled；`reasoning_effort=max` |
| Temperature | 发送 `temperature=0`；Thinking 模式下不据此推断采样行为 |
| 外部统一请求预算 | 每题最多 100 次 product model request；允许第 100 次，拒绝第 101 次 |
| Agent deadline | 按任务采用 R1/R2/R3/R4：1800/2700/3600/5400 秒 |
| Prompt 口径 | 保留 DSH 原生 headless prompt，并输入 Toolathlon 公共 system/task 指令 |
| 工具范围 | 保留产品内置工具；仅向当前任务暴露相应 MCP 工具 |
| 状态隔离 | 每次 attempt 使用 fresh ephemeral DSH home，不从旧 attempt resume |

## 基础运行环境

| 环境项 | 配置 |
| --- | --- |
| 数据集 | Toolathlon，固定 108 题 |
| Host OS | Ubuntu 22.04，Linux `5.15.0-190-generic`，UTC |
| CPU | Intel Xeon Platinum 8255C @ 2.50 GHz，x86_64，8 vCPU |
| 内存与 Swap | 约 7.8 GiB RAM、8 GiB swap |
| 虚拟化 | Vagrant 虚拟机（KVM） |
| 容器运行时 | rootful Docker；任务容器使用 Toolathlon 固定镜像 |
| 单任务资源上限 | 8 CPU、8 GiB RAM、8 GiB swap |
| 外部应用状态 | 由任务 preprocess 恢复；部分本地应用和 Kind 服务由任务间共享部署提供 |
| 网络边界 | 未统一关闭公开互联网出口；任务 MCP 限于当前任务，模型请求经本地 sidecar 转发 |
| Evaluator | Agent 终止后独立运行 Toolathlon 每题原生 evaluator |

这里记录的是本次 DSH 实验环境口径；当前宿主机状态的后续变化不追溯修改历史运行记录。

## 任务完成结果

| 指标 | DSH |
| --- | ---: |
| 基准题目 | 108 |
| 有 Agent + Evaluator 完整结果 | {len(complete)} |
| pass | {len(passed)} |
| no-pass | {len(failed)} |
| 未完成 | {len(incomplete)} |
| 按 108 题通过率 | {pass_rate:.2%} |
| 已测评题通过率 | {evaluated_rate:.2%} |

通过（{len(passed)}）：{task_names(passed)}。

### No-pass 逐题结果

| 序号 | 任务 | 产品终态 | 直接分类 | 模型请求 | Transport 错误 | 最新来源 |
| ---: | --- | --- | --- | ---: | ---: | --- |
{no_pass_rows}

### 未完成任务

| 序号 | 任务 | 阶段 | 原因 |
| ---: | --- | --- | --- |
{incomplete_rows}

## No-pass 原因

| 原因 | 题数 |
| --- | ---: |
| Agent 完成，但 evaluator 未通过 | {failures.get('completed_but_no_pass', 0)} |
| 达到模型请求预算 | {failures.get('model_request_budget', 0)} |
| 产品执行错误 | {failures.get('product_error', 0)} |
| Agent deadline | {failures.get('agent_deadline', 0)} |
| 其他 | {sum(count for name, count in failures.items() if name not in {'completed_but_no_pass', 'model_request_budget', 'product_error', 'agent_deadline'})} |

这是直接终态分类，不推断模型内部原因。另有 {pass_product_errors} 个 evaluator 已通过的任务记录了产品执行错误终态；因此报告坚持以 evaluator 作为任务通过与否的唯一判据，不把产品终态机械等同于 benchmark 结果。

## 时间消耗

| 阶段 | 样本数 | 总计 | 平均 | 中位数 | P90 |
| --- | ---: | ---: | ---: | ---: | ---: |
{time_rows}

时间包含通过与未通过任务。`orchestration` 是端到端减去 Agent 和 evaluator 后的剩余时间，包含准备、adapter 收尾及 post-terminal drain，不是纯环境准备时间。

## 工具调用

| 指标 | DSH |
| --- | ---: |
| 有工具计数的运行 | {tools['calls']['n']} |
| 工具调用总数 | {format_number(tools['calls']['sum'])} |
| 单运行平均 | {format_number(tools['calls']['mean'])} |
| 单运行中位数 | {format_number(tools['calls']['median'])} |
| 单运行 P90 | {format_number(tools['calls']['p90'])} |
| 失败工具事件总数 | {format_number(tools['failures']['sum'])} |

常见终态工具：{top_tools}。

失败工具事件可能在重试后恢复，不等同于失败任务数；不同 MCP 的工具粒度也不一致。

## 模型请求

| 指标 | DSH |
| --- | ---: |
| 统计运行数 | {requests['model_requests_started']['n']} |
| 模型请求 started | {format_number(visible_requests)} |
| 模型请求 completed event | {format_number(requests['model_requests_completed']['sum'])} |
| 成功 completed event | {format_number(requests['model_requests_successful_events']['sum'])} |
| 失败 completed event | {format_number(requests['model_requests_failed']['sum'])} |
| Transport 错误事件 | {format_number(requests['model_transport_errors']['sum'])} |
| HTTP 错误响应 | {format_number(requests['model_http_error_responses']['sum'])} |
| Provider API 错误响应 | {format_number(requests['model_provider_error_responses']['sum'])} |
| 单运行 started 平均 | {format_number(requests['model_requests_started']['mean'])} |
| 单运行 started 中位数 | {format_number(requests['model_requests_started']['median'])} |
| 单运行 started P90 | {format_number(requests['model_requests_started']['p90'])} |
| 触及 100 请求上限 | {summary['request_limit_reached']} |
| 因请求预算 no-pass | {failures.get('model_request_budget', 0)} |
| 流式请求 | {format_share(requests['stream_requests']['sum'], visible_requests)} |
| 非流式请求 | {format_share(requests['non_stream_requests']['sum'], visible_requests)} |

共有 {summary['transport_affected']} / {len(complete)} 个最新运行记录过 transport 错误，其中 {summary['transport_affected_failed']} 个最终为 no-pass。错误类型累计为：{error_types}。这些计数描述 sidecar 观测到的连接/流事件；没有 HTTP 响应的 transport 失败不能解释为 DeepSeek API 业务错误码。

## Token 数据

### 保守可靠记录

若一个运行的 completed response 存在缺失 usage，该运行不进入本表：

| 指标 | DSH |
| --- | ---: |
| 有完整 provider usage 的运行 | {len(tokens['reliable'])} / {len(complete)} |
| 输入 token 总量 | {format_number(tokens['reliable_input']['sum'])} |
| 输出 token 总量 | {format_number(tokens['reliable_output']['sum'])} |
| total token 总量 | {format_number(tokens['reliable_total']['sum'])} |
| 单运行 total 中位数 | {format_number(tokens['reliable_total']['median'])} |

“保守可靠记录”是运行级完备性口径，不是只挑选成功请求：一个任务必须让其每个 `model_request.completed` 都同时具有 provider 明确上报的 input/output/total token，才进入本表。只要一次 transport 失败没有返回 usage，即使任务最终通过、其他请求都有 usage，整题也会被排除。因此 {len(tokens['reliable'])} / {len(complete)} 表示“可以完整求和的任务数”，不表示其余任务没有 token 数据或 token 为 0。

### 全部可见 token 下界

| 指标 | DSH |
| --- | ---: |
| 运行数 | {len(complete)} |
| 已上报 / 缺 usage 的 completed 请求 | {tokens['reported_completed']:,} / {tokens['missing_completed']:,} |
| 输入 token 下界 | {format_number(tokens['visible_input']['sum'])} |
| 输出 token 下界 | {format_number(tokens['visible_output']['sum'])} |
| total token 下界 | {format_number(tokens['visible_total']['sum'])} |
| 可见 cache-read token | {format_number(tokens['cache_read']['sum'])} |
| 单运行 total 中位数 | {format_number(tokens['visible_total']['median'])} |

“全部可见 token 下界”覆盖全部 {len(complete)} 个任务，但只累加 {tokens['reported_completed']:,} 次明确返回 usage 的请求；{tokens['missing_completed']:,} 次缺失 usage 的请求保持未知，不补零。已计入部分包含所有成功响应，以及在断开前已经拿到 usage 的失败响应；流式失败后成功重试所返回的完整 usage 也会作为一笔新的请求计入。未计入部分仍可能已经消耗完整输入和部分输出 token，因此该数值是实际资源消耗的严格下界，而不是账单总量。

由于 DSH 断流后会用相同历史重新发起完整请求，这个可见下界可能接近“每个逻辑步骤一次成功”时的工作量，但不能当作无链路波动的反事实估计：部分失败请求本身已有可见 usage，重新生成还可能改变输出、工具调用和后续 Agent 路径。Token 反映 DSH 主循环、工具 schema、累积上下文及缓存策略的整体足迹，不能直接解释为底层模型的单位推理效率。

### Pass 任务的可靠 Token

| 指标 | DSH |
| --- | ---: |
| 有可靠 token 的 pass 任务 | {reliable_pass_count} / {len(passed)} |
| total token 总量 | {format_number(tokens['pass_total']['sum'])} |
| 单任务平均 | {format_number(tokens['pass_total']['mean'])} |
| 单任务中位数 | {format_number(tokens['pass_total']['median'])} |

## 综合结论

1. **最终覆盖完整。** 108 个任务的最新 attempt 都形成了 Agent 与 evaluator 结果；79 题通过、29 题未通过，通过率为 {pass_rate:.2%}。
2. **No-pass 以完成后未满足 evaluator 为主。** {failures.get('completed_but_no_pass', 0)} 题正常完成 Agent 流程但未满足验证，{failures.get('product_error', 0)} 题以产品执行错误结束，{failures.get('agent_deadline', 0)} 题触及 Agent deadline；没有任务因 100 次模型请求预算直接 no-pass。
3. **网络事件仍影响部分 effective result。** {summary['transport_affected']} 题记录过 transport 错误，累计 {format_number(requests['model_transport_errors']['sum'])} 次；它们既可能被重试恢复，也可能成为产品错误的一部分，不能仅按事件数推导任务失败。
4. **请求量必须按产品足迹解释。** 单任务模型请求中位数为 {format_number(requests['model_requests_started']['median'])}，P90 为 {format_number(requests['model_requests_started']['p90'])}；请求包含重试，不等同于 Agent 的语义回合。
5. **工具和 token 是观测足迹，不是单独的能力指标。** DSH 单任务工具调用中位数为 {format_number(tools['calls']['median'])}；可靠 token 记录覆盖 {len(tokens['reliable'])} / {len(complete)} 题。任务复杂度、工具 schema 和上下文增长都会影响这些数值。
6. **结果来自混合批次。** 本报告严格采用每题开始时间最新的 attempt；恢复批次只替换实际重跑的任务，未重跑任务继续保留其较早结果，因此结论应按 effective projection 理解。

## 附件

- 全批次逐 attempt 数据：[`dsh-toolathlon-all-attempt-results.csv`](dsh-toolathlon-all-attempt-results.csv)
- 全 attempt CSV 生成脚本：[`generate_dsh_all_attempt_results.py`](generate_dsh_all_attempt_results.py)
- 本报告生成脚本：[`generate_dsh_latest_result_report.py`](generate_dsh_latest_result_report.py)
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the DSH Toolathlon report using each task's latest attempt"
    )
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selected = latest_rows(collect_rows(args.results_root.resolve()))
    summary = describe_rows(selected)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report(selected, summary), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "tasks": len(selected),
                "complete": len(summary["complete"]),
                "pass": len(summary["passed"]),
                "no_pass": len(summary["failed"]),
                "incomplete": len(summary["incomplete"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
