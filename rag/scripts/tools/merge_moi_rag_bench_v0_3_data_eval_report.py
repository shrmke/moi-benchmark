#!/usr/bin/env python3
"""Merge the v0.3 dataset report with the completed three-platform evaluation."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASE_ARTIFACT = ROOT / "results/reports/moi-rag-bench-v0.3-final-dataset-report.artifact.json"
BASE_CHART_MAP = ROOT / "results/reports/moi-rag-bench-v0.3-final-dataset-report.chart-map.json"
EVALUATION = ROOT / "runs/bench-v0.3-final/moi-rag-bench-v0.3-final-serial-report.json"
OUTPUT_STEM = "moi-rag-bench-v0.3-final-data-to-eval-report"
DEFAULT_OUTPUT = ROOT / f"results/reports/{OUTPUT_STEM}.artifact.json"
DEFAULT_CHART_MAP = ROOT / f"results/reports/{OUTPUT_STEM}.chart-map.json"

PLATFORMS = ("dify_local", "fastgpt_local", "maxkb_local")
SERIAL_SOURCE_PATH = "runs/bench-v0.3-final/moi-rag-bench-v0.3-final-serial-report.json"
SERIAL_SOURCE_HREF = "runs/bench-v0.3-final/moi-rag-bench-v0.3-final-serial-report.md"


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def find(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
    return next(item for item in items if item.get("id") == item_id)


def validate_evaluation(evaluation: dict[str, Any]) -> None:
    if evaluation.get("status") != "COMPLETE":
        raise ValueError("Evaluation report is not complete")
    dataset = evaluation.get("dataset") or {}
    if dataset.get("document_files") != 297 or dataset.get("question_records") != 275:
        raise ValueError("Evaluation dataset denominator drifted")
    if evaluation.get("execution", {}).get("order") != list(PLATFORMS):
        raise ValueError("Platform order drifted")
    if evaluation.get("judge_protocol", {}).get("same_parameters_verified") is not True:
        raise ValueError("Judge parameters are not verified equal")
    for platform in PLATFORMS:
        result = evaluation.get("platforms", {}).get(platform) or {}
        for stage in ("retrieval", "qa"):
            value = result.get(stage) or {}
            if not (
                value.get("planned_n") == value.get("terminal_n") == value.get("valid_n") == 275
                and value.get("failed_n") == value.get("unsupported_n") == 0
            ):
                raise ValueError(f"Incomplete {platform} {stage} result")
        judge = result.get("judge") or {}
        denominator = judge.get("denominator") or {}
        if judge.get("status") != "COMPLETE" or denominator.get("terminal_n") != 275:
            raise ValueError(f"Incomplete {platform} Judge result")


def evaluation_datasets(evaluation: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    results = evaluation["platforms"]
    retrieval_quality: list[dict[str, Any]] = []
    latency: list[dict[str, Any]] = []
    qa_quality: list[dict[str, Any]] = []
    judge_quality: list[dict[str, Any]] = []
    unsupported_claims: list[dict[str, Any]] = []
    result_rows: list[dict[str, Any]] = []
    judge_rows: list[dict[str, Any]] = []

    judge_labels = {
        "answer_relevance": "答案相关性",
        "contradiction_free": "无矛盾",
        "instruction_compliance": "指令遵循",
    }
    for rank, platform in enumerate(PLATFORMS, 1):
        result = results[platform]
        label = result["label"]
        retrieval = result["retrieval"]
        qa = result["qa"]
        dimensions = result["judge"]["dimensions"]

        for metric, metric_label in (("evidence_recall_at_10", "Evidence Recall@10"), ("mrr", "MRR")):
            retrieval_quality.append(
                {"platform": label, "platform_order": rank, "metric": metric_label, "value": retrieval[metric]}
            )
        for stage_label, p50_key, p95_key, source in (
            ("Retrieval", "latency_ms_p50", "latency_ms_p95", retrieval),
            ("QA", "latency_ms_p50", "latency_ms_p95", qa),
        ):
            latency.append(
                {
                    "platform": label,
                    "platform_order": rank,
                    "stage": stage_label,
                    "p50_seconds": source[p50_key] / 1000,
                    "p95_seconds": source[p95_key] / 1000,
                }
            )
        for metric, metric_label in (("token_f1", "Token F1"), ("answer_contains_gold_rate", "Contains gold")):
            qa_quality.append(
                {"platform": label, "platform_order": rank, "metric": metric_label, "value": qa[metric]}
            )
        for metric, metric_label in judge_labels.items():
            judge_quality.append(
                {
                    "platform": label,
                    "platform_order": rank,
                    "metric": metric_label,
                    "value": dimensions[metric]["value"],
                    "observed_n": dimensions[metric]["observed_n"],
                }
            )
        unsupported_claims.append(
            {
                "platform": label,
                "platform_order": rank,
                "value": dimensions["unsupported_claim_rate"]["value"],
                "observed_n": dimensions["unsupported_claim_rate"]["observed_n"],
            }
        )
        result_rows.append(
            {
                "platform": label,
                "recall_at_10": retrieval["evidence_recall_at_10"],
                "mrr": retrieval["mrr"],
                "retrieval_p50_s": retrieval["latency_ms_p50"] / 1000,
                "retrieval_p95_s": retrieval["latency_ms_p95"] / 1000,
                "normalized_em": qa["normalized_em"],
                "token_f1": qa["token_f1"],
                "contains_gold": qa["answer_contains_gold_rate"],
                "qa_p50_s": qa["latency_ms_p50"] / 1000,
                "qa_p95_s": qa["latency_ms_p95"] / 1000,
                "retrieval_contract": result["contracts"]["retrieval"],
                "qa_contract": result["contracts"]["qa"],
            }
        )
        response_correctness = dimensions["response_claim_correctness"]
        context_faithfulness = dimensions["runtime_context_faithfulness"]
        judge_rows.append(
            {
                "platform": label,
                "answer_relevance": dimensions["answer_relevance"]["value"],
                "contradiction_free": dimensions["contradiction_free"]["value"],
                "instruction_compliance": dimensions["instruction_compliance"]["value"],
                "response_claim_correctness": response_correctness["value"],
                "runtime_context_faithfulness": context_faithfulness["value"],
                "unsupported_claim_rate": dimensions["unsupported_claim_rate"]["value"],
                "response_missing_n": response_correctness["missing_n"],
                "context_missing_n": context_faithfulness["missing_n"],
            }
        )

    return {
        "eval_completion": [
            {
                "platforms": 3,
                "per_platform_questions": 275,
                "retrieval_success": 825,
                "qa_success": 825,
                "judge_success": 825,
                "failed_or_unsupported": 0,
            }
        ],
        "eval_retrieval_quality": retrieval_quality,
        "eval_latency": latency,
        "eval_qa_quality": qa_quality,
        "eval_judge_quality": judge_quality,
        "eval_unsupported_claims": unsupported_claims,
        "eval_results": result_rows,
        "eval_judge_results": judge_rows,
    }


def chart(
    chart_id: str,
    title: str,
    subtitle: str,
    dataset: str,
    x: dict[str, Any],
    y: dict[str, Any],
    *,
    color: dict[str, Any] | None = None,
    tooltip: list[dict[str, Any]] | None = None,
    chart_type: str = "bar",
    value_format: str = "number",
    unit: str | None = None,
    question: str,
    rationale: str,
    palette_kind: str = "categorical",
) -> dict[str, Any]:
    encodings: dict[str, Any] = {"x": x, "y": y}
    if color:
        encodings["color"] = color
    if tooltip:
        encodings["tooltip"] = tooltip
    value: dict[str, Any] = {
        "id": chart_id,
        "title": title,
        "subtitle": subtitle,
        "intent": "comparison",
        "question": question,
        "rationale": rationale,
        "comparisonContext": {"denominator": "275 QA per platform", "grain": "platform", "unit": unit or value_format},
        "type": chart_type,
        "dataset": dataset,
        "sourceId": "serial_eval",
        "encodings": encodings,
        "valueFormat": value_format,
        "layout": "full",
        "palette": {"kind": palette_kind, "name": "moi-benchmark-platforms"},
        "labels": {"values": "auto"},
        "settings": {
            "groupMode": "grouped" if color else "single",
            "sort": "none",
            "showValues": True,
            "categoryLabelPolicy": "wrap",
        },
    }
    if color:
        value["legend"] = {"position": "bottom", "sort": "spec", "title": "平台"}
    if unit:
        value["unit"] = unit
    return value


def build_artifact(base: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, Any]:
    manifest = base["manifest"]
    generated_at = datetime.now(timezone.utc).isoformat()
    title = "MOI RAG Bench v0.3 Final：数据构建与三平台评测总报告"
    manifest.update(
        {
            "title": title,
            "description": "End-to-end report from curated pure-text benchmark data to completed Dify, FastGPT, and MaxKB evaluation results.",
            "generatedAt": generated_at,
        }
    )
    base["snapshot"]["generatedAt"] = generated_at
    base["snapshot"]["datasets"].update(evaluation_datasets(evaluation))

    find(manifest["blocks"], "title")["body"] = (
        f"# {title}\n\n"
        "**数据版本：** 0.3.0 · `curated-final-v0.3`  \n"
        "**评测条件：** `text-only-no-mllm`  \n"
        "**执行状态：** COMPLETE · Dify → FastGPT → MaxKB  \n"
        "**报告日期：** 2026-08-25"
    )
    abstract = find(manifest["blocks"], "abstract")
    abstract.pop("sourceId", None)
    abstract["body"] = (
        "## Executive Summary\n\n"
        "- **数据已经从候选集收敛为可执行基准。** v0.3 Final 从 1,000 条候选中保留 275 QA，配套 297 份 Ready Markdown；其中 100 条需要多文档、27 条应拒答，全部通过纯文本条件审核。\n"
        "- **三个本地平台都完整跑完。** Dify、FastGPT、MaxKB 的 retrieval、QA 与 Judge 均各有 275/275 成功终态，失败和 unsupported 为 0；Embedding 统一为 MaaS `bge-m3`，生成和 Judge 统一为 `deepseek-v4-flash`。\n"
        "- **Dify 是当前最稳的原生端到端基线。** 它取得最高 Evidence Recall（0.913）与 MRR（0.801），并以 0.283 秒 retrieval p50、1.336 秒 QA p50 显著领先另外两套系统。\n"
        "- **没有一个可无条件宣称的总冠军。** MaxKB 的 Token F1、答案相关性和指令遵循最高，但采用管理端诊断检索＋外部 DeepSeek 生成，并有两个 Judge 维度不完整；FastGPT 的 unsupported-claim rate 最低，但检索、答案和时延整体落后 Dify。"
    )

    preflight = find(manifest["blocks"], "preflight_note")
    preflight.pop("sourceId", None)
    preflight["body"] = (
        "Dify live preflight 先确认 `package_valid=true`、`ready=true`、`scope_verified=true`；随后独立串行评测确认 Dify、FastGPT、MaxKB 均完成 275/275 retrieval、QA 与 Judge。本报告未包含 MOI 在 v0.3 Final 上的同条件结果，因此当前比较是三个竞品平台横评，不是 MOI 与竞品的最终排行榜。"
    )

    limitations = find(manifest["blocks"], "limitations")
    limitations.pop("sourceId", None)
    limitations["body"] += (
        "\n- 当前正式结果只覆盖 Dify、FastGPT、MaxKB；尚无 MOI 的 v0.3 Final 同条件成绩。"
        "\n- MaxKB 使用 `diagnostic_admin_contract` 检索和外部 DeepSeek 生成，不等同于原生 MaxKB app 路径；其结果应单独标注。"
        "\n- MaxKB Judge 的 response-claim correctness 有 8 条 N/A、runtime-context faithfulness 有 3 条 N/A，因此这两个总体值保持 N/A，不能用部分均值补齐。"
        "\n- Normalized EM 对开放答案和 53 条格式敏感题过于苛刻，应与 Token F1、Judge 和分层指标共同解释。"
    )
    find(manifest["blocks"], "next_steps")["body"] = (
        "## 15. 推荐下一步\n\n"
        "1. 在同一数据、Embedding、LLM、Judge 与提示词下补跑 MOI v0.3 Final。\n"
        "2. 将 MaxKB 原生 app 路径修到可证明关闭 thinking 后再复测；在此之前保持诊断结果单列。\n"
        "3. 冻结 53 条格式敏感题的 normalization，并按来源、问题类型、难度和拒答切片发布结果。\n"
        "4. 为核心指标补 fixed-seed bootstrap 95% 置信区间，验证平台排序是否稳定。\n"
        "5. 增补 structured response/reference claims，使当前 N/A 的 claim-level 指标形成统一有效分母。"
    )
    find(manifest["blocks"], "further_questions")["body"] = (
        "## 16. 后续研究问题\n\n"
        "- Dify 的速度与排序优势在 MOI 同条件加入后是否仍成立？\n"
        "- MaxKB 改为原生 app 后，答案质量优势和较高 unsupported-claim rate 会如何变化？\n"
        "- FastGPT 与 MaxKB 接近的 Evidence Recall、但明显不同的 MRR，是否来自切分、排序或检索契约差异？\n"
        "- Quality-first 子集与完整 1,000 QA 的系统排名是否稳定？\n"
        "- 省略 MMDocIR 后，文本检索结论能否外推到页面、布局和视觉任务？"
    )

    manifest["cards"].append(
        {
            "id": "eval_completion_card",
            "description": "三平台 × 275 QA × retrieval/QA/Judge 的完成情况。",
            "dataset": "eval_completion",
            "sourceId": "serial_eval",
            "metrics": [
                {"label": "完成平台", "field": "platforms", "format": "number"},
                {"label": "每平台 QA", "field": "per_platform_questions", "format": "number"},
                {"label": "Retrieval 成功", "field": "retrieval_success", "format": "number"},
                {"label": "QA 成功", "field": "qa_success", "format": "number"},
                {"label": "Judge 成功", "field": "judge_success", "format": "number"},
                {"label": "失败/不支持", "field": "failed_or_unsupported", "format": "number"},
            ],
        }
    )

    platform_color = {"field": "platform", "type": "nominal", "label": "平台"}
    manifest["charts"].extend(
        [
            chart(
                "eval_retrieval_quality_chart",
                "检索质量对比",
                "每平台 275 QA；Evidence Recall@1/@3/@5/@10 在本次汇总中相同，图示 @10 与 MRR。",
                "eval_retrieval_quality",
                {"field": "metric", "type": "nominal", "label": "指标"},
                {"field": "value", "type": "quantitative", "label": "分数", "format": "number"},
                color=platform_color,
                tooltip=[{"field": "platform_order", "type": "quantitative", "label": "串行顺序"}],
                question="三个平台在检索覆盖与首个相关结果排序上谁领先？",
                rationale="两个同量纲检索指标按平台分组，直接展示覆盖与排序质量的差异。",
            ),
            chart(
                "eval_latency_chart",
                "P50 时延对比",
                "每平台 275 QA；柱为 p50，tooltip 保留 p95；单位为秒。",
                "eval_latency",
                {"field": "stage", "type": "nominal", "label": "阶段"},
                {"field": "p50_seconds", "type": "quantitative", "label": "P50 时延", "unit": "s"},
                color=platform_color,
                tooltip=[{"field": "p95_seconds", "type": "quantitative", "label": "P95 时延", "unit": "s"}],
                question="三个平台在 retrieval 与 QA 的典型响应速度差多少？",
                rationale="两个离散阶段共享秒单位，分组柱形图能直接比较平台 p50，并保留 p95 尾延迟。",
                unit="s",
            ),
            chart(
                "eval_qa_quality_chart",
                "答案文本指标对比",
                "每平台 275 QA；Token F1 与 contains-gold 均按 0–1 口径。",
                "eval_qa_quality",
                {"field": "metric", "type": "nominal", "label": "指标"},
                {"field": "value", "type": "quantitative", "label": "比率", "format": "percent"},
                color=platform_color,
                question="三个平台生成答案与参考答案的文本重合度如何？",
                rationale="两个同为 0–1 的文本诊断指标按平台分组；EM 极低且格式敏感，保留在精确表中。",
                value_format="percent",
            ),
            chart(
                "eval_judge_quality_chart",
                "统一 Judge 质量维度",
                "相同 DeepSeek Judge、prompt hash、temperature=0；每项完整覆盖 275 QA。",
                "eval_judge_quality",
                {"field": "metric", "type": "nominal", "label": "Judge 维度"},
                {"field": "value", "type": "quantitative", "label": "分数"},
                color=platform_color,
                tooltip=[{"field": "observed_n", "type": "quantitative", "label": "有效样本"}],
                question="在分母完整的 Judge 维度上，三个平台表现如何？",
                rationale="只展示三个跨平台均有完整 275 分母的高分更优维度，避免将 N/A 当作零。",
            ),
            chart(
                "eval_unsupported_claims_chart",
                "Unsupported-claim rate",
                "每平台 275 QA；越低越好；MaxKB 为诊断检索＋外部生成条件。",
                "eval_unsupported_claims",
                {"field": "platform", "type": "nominal", "label": "平台"},
                {"field": "value", "type": "quantitative", "label": "Unsupported-claim rate", "format": "percent"},
                tooltip=[{"field": "observed_n", "type": "quantitative", "label": "有效样本"}],
                chart_type="horizontalBar",
                question="哪个平台更少产生缺乏支持的答案声明？",
                rationale="单一风险比率按平台水平比较，排序和方向说明比复合质量图更诚实。",
                value_format="percent",
                palette_kind="sequential",
            ),
        ]
    )

    manifest["tables"].extend(
        [
            {
                "id": "eval_results_table",
                "title": "Retrieval 与 QA 精确结果",
                "subtitle": "每平台 275 QA；时延单位为秒；MaxKB 契约与原生平台不同。",
                "dataset": "eval_results",
                "sourceId": "serial_eval",
                "columns": [
                    {"field": "platform", "label": "平台", "type": "text"},
                    {"field": "recall_at_10", "label": "Recall@10", "format": "percent"},
                    {"field": "mrr", "label": "MRR", "format": "number"},
                    {"field": "retrieval_p50_s", "label": "R p50(s)", "format": "number"},
                    {"field": "retrieval_p95_s", "label": "R p95(s)", "format": "number"},
                    {"field": "normalized_em", "label": "EM", "format": "percent"},
                    {"field": "token_f1", "label": "Token F1", "format": "percent"},
                    {"field": "contains_gold", "label": "Contains gold", "format": "percent"},
                    {"field": "qa_p50_s", "label": "QA p50(s)", "format": "number"},
                    {"field": "qa_p95_s", "label": "QA p95(s)", "format": "number"},
                    {"field": "retrieval_contract", "label": "Retrieval contract", "type": "text"},
                    {"field": "qa_contract", "label": "QA contract", "type": "text"},
                ],
                "defaultSort": {"field": "platform", "direction": "asc"},
            },
            {
                "id": "eval_judge_results_table",
                "title": "统一 Judge 精确结果",
                "subtitle": "分数为 0–1；unsupported-claim rate 越低越好；N/A 不做插补。",
                "dataset": "eval_judge_results",
                "sourceId": "serial_eval",
                "columns": [
                    {"field": "platform", "label": "平台", "type": "text"},
                    {"field": "answer_relevance", "label": "相关性", "format": "number"},
                    {"field": "contradiction_free", "label": "无矛盾", "format": "number"},
                    {"field": "instruction_compliance", "label": "指令遵循", "format": "number"},
                    {"field": "response_claim_correctness", "label": "声明正确性", "format": "number"},
                    {"field": "runtime_context_faithfulness", "label": "上下文忠实度", "format": "number"},
                    {"field": "unsupported_claim_rate", "label": "Unsupported rate", "format": "percent"},
                    {"field": "response_missing_n", "label": "声明 N/A", "format": "number"},
                    {"field": "context_missing_n", "label": "上下文 N/A", "format": "number"},
                ],
                "defaultSort": {"field": "platform", "direction": "asc"},
            },
        ]
    )

    eval_blocks = [
        {"id": "eval_completion_metrics", "type": "metric-strip", "cardIds": ["eval_completion_card"]},
        {
            "id": "eval_overview",
            "type": "markdown",
            "sourceId": "serial_eval",
            "body": "## 三平台全部完成，但不存在无条件的单一冠军\n\n三个系统均完成 275/275 retrieval、QA 与 Judge，运行可靠性没有拉开差距。真正的差异集中在检索排序、时延、答案文本匹配和声明风险：Dify 的原生链路最均衡，FastGPT 更保守但整体质量偏弱，MaxKB 的答案分数更高但评测契约不同。",
        },
        {
            "id": "eval_retrieval_finding",
            "type": "markdown",
            "sourceId": "serial_eval",
            "body": "### Dify 的检索排序最强\n\nDify 的 Evidence Recall@10 为 **0.913**，比 FastGPT/MaxKB 的 0.891 高 2.26 个百分点；MRR 为 **0.801**，领先 FastGPT 0.751，并显著高于 MaxKB 0.351。MaxKB 的覆盖率并不差，但首个相关结果更靠后。**这意味着 Dify 更适合作为当前检索基线，MaxKB 需要优先排查排序与检索契约。**",
        },
        {"id": "eval_retrieval_chart_block", "type": "chart", "chartId": "eval_retrieval_quality_chart", "layout": "full"},
        {
            "id": "eval_latency_finding",
            "type": "markdown",
            "sourceId": "serial_eval",
            "body": "### Dify 同时是最快的系统\n\nDify retrieval p50 为 **0.283 秒**，约为 FastGPT 的 21% 和 MaxKB 的 12%；QA p50 为 **1.336 秒**，约为另外两者的三分之一。P95 也保持同方向。**如果线上目标重视交互体验，当前结果优先支持 Dify。**",
        },
        {"id": "eval_latency_chart_block", "type": "chart", "chartId": "eval_latency_chart", "layout": "full"},
        {
            "id": "eval_qa_finding",
            "type": "markdown",
            "sourceId": "serial_eval",
            "body": "### MaxKB 的答案文本匹配最高，但不是原生端到端结果\n\nMaxKB 的 Token F1 为 **0.242**、contains-gold 为 **37.8%**，高于 Dify 的 0.210/34.5% 与 FastGPT 的 0.143/18.9%。但 MaxKB 使用管理端诊断检索与外部 DeepSeek 生成，不能直接解释为原生产品表现。所有平台 normalized EM 都很低（0.36%–1.45%），说明开放答案不应以严格字符串相等作为主结论。",
        },
        {"id": "eval_qa_chart_block", "type": "chart", "chartId": "eval_qa_quality_chart", "layout": "full"},
        {
            "id": "eval_judge_finding",
            "type": "markdown",
            "sourceId": "serial_eval",
            "body": "### Judge 显示“有用性”和“声明风险”并不一致\n\n在分母完整的维度上，MaxKB 的答案相关性、无矛盾和指令遵循最高；Dify 居中；FastGPT 最低。但 unsupported-claim rate 的方向相反：FastGPT **56.0%** 最低，Dify 62.2%，MaxKB 71.5%。**因此不能把单个 Judge 高分当作总质量，必须同时看支持性风险。**",
        },
        {"id": "eval_judge_chart_block", "type": "chart", "chartId": "eval_judge_quality_chart", "layout": "full"},
        {
            "id": "eval_unsupported_finding",
            "type": "markdown",
            "sourceId": "serial_eval",
            "body": "### FastGPT 更保守，MaxKB 的无支持声明风险最高\n\nUnsupported-claim rate 越低越好。FastGPT 虽然相关性和文本匹配较弱，但最少出现缺乏支持的声明；MaxKB 则相反。**实际选型应在答案覆盖、证据支持和拒答策略之间设置业务权重。**",
        },
        {"id": "eval_unsupported_chart_block", "type": "chart", "chartId": "eval_unsupported_claims_chart", "layout": "full"},
        {
            "id": "eval_exact_results_heading",
            "type": "markdown",
            "sourceId": "serial_eval",
            "body": "### 精确结果与契约\n\n下表保留全部核心汇总值、时延和平台契约，供复核与后续复跑对齐。MaxKB 的两个 N/A 维度保持为空，不用部分样本均值替代统一分母。",
        },
        {"id": "eval_results_table_block", "type": "table", "tableId": "eval_results_table", "layout": "full"},
        {"id": "eval_judge_results_table_block", "type": "table", "tableId": "eval_judge_results_table", "layout": "full"},
        {
            "id": "eval_comparability_note",
            "type": "markdown",
            "sourceId": "serial_eval",
            "body": "### 可比性边界\n\nDify 与 FastGPT 采用原生 chat；MaxKB 因本地 v2.10.4 无法证明原生 app 已关闭 nested thinking，使用 `diagnostic_admin_contract` 检索加外部 `deepseek-v4-flash` 生成。三者 Judge 参数完全一致，但 MaxKB 的 response-claim correctness 有 8 条 N/A、runtime-context faithfulness 有 3 条 N/A，因此本报告不计算跨维度综合总分。",
        },
    ]
    insert_at = next(index for index, block in enumerate(manifest["blocks"]) if block.get("id") == "technical_summary")
    manifest["blocks"][insert_at:insert_at] = eval_blocks

    manifest_source = {
        "id": "serial_eval",
        "label": "Dify/FastGPT/MaxKB serial evaluation summary",
        "path": SERIAL_SOURCE_PATH,
        "href": SERIAL_SOURCE_HREF,
    }
    if not any(source.get("id") == "serial_eval" for source in manifest["sources"]):
        manifest["sources"].append(manifest_source)
    root_source = {
        "id": "serial_eval",
        "path": SERIAL_SOURCE_PATH,
        "query": {
            "engine": "duckdb/local-filesystem",
            "language": "sql",
            "sql": "SELECT * FROM read_json_auto('runs/bench-v0.3-final/moi-rag-bench-v0.3-final-serial-report.json');",
            "description": "Aggregates the completed Dify, FastGPT, and MaxKB v0.3 Final retrieval, QA, and Judge outputs.",
            "executed_at": evaluation["generated_at"],
            "tables_used": [
                "runs/bench-v0.3-final/20260825-dify-v03-final-native-bool/retrieval-metrics.json",
                "runs/bench-v0.3-final/20260825-dify-v03-final-native-bool/qa-metrics.json",
                "runs/bench-v0.3-final/20260825-dify-v03-final-native-bool/judge/judge-summary.json",
                "runs/bench-v0.3-final/20260825-fastgpt-v03-final-dsv4f-chunk8k/retrieval-metrics.json",
                "runs/bench-v0.3-final/20260825-fastgpt-v03-final-dsv4f-chunk8k/qa-metrics.json",
                "runs/bench-v0.3-final/20260825-fastgpt-v03-final-dsv4f-chunk8k/judge/judge-summary.json",
                "runs/bench-v0.3-final/20260825-maxkb-v03-final-dsv4f/retrieval-metrics.json",
                "runs/bench-v0.3-final/20260825-maxkb-v03-final-dsv4f/qa-metrics.json",
                "runs/bench-v0.3-final/20260825-maxkb-v03-final-dsv4f/judge/judge-summary.json",
            ],
            "filters": {"dataset": "moi-rag-bench-v0.3-final", "question_denominator": 275},
            "metric_definitions": {
                "Evidence Recall@10": "Macro evidence recall at k=10 over the frozen planned question denominator.",
                "MRR": "Mean reciprocal rank of the first relevant result; misses contribute zero.",
                "Token F1": "Token overlap F1 between generated and reference answers over all 275 QA.",
                "Unsupported-claim rate": "Judge-estimated share of material answer claims not supported by runtime context; lower is better.",
            },
        },
    }
    if not any(source.get("id") == "serial_eval" for source in base["sources"]):
        base["sources"].append(root_source)
    base["package_info"] = {
        "originUrl": "artifact://moi-rag-bench-v0.3-final-data-to-eval-report",
        "controls": {"edit": False, "refresh": False, "persistence": False, "copyAsImage": False},
    }

    assert manifest["blocks"][0]["body"].startswith(f"# {title}")
    assert manifest["blocks"][1]["body"].startswith("## Executive Summary")
    assert len(base["snapshot"]["datasets"]["eval_results"]) == 3
    assert len(base["snapshot"]["datasets"]["eval_judge_quality"]) == 9
    return base


def build_chart_map(base_chart_map: dict[str, Any]) -> dict[str, Any]:
    charts = list(base_chart_map.get("charts") or [])
    charts.extend(
        [
            {"chart_id": "eval_retrieval_quality_chart", "segment": "三平台评测", "question": "检索覆盖与排序谁领先？", "family": "comparison", "type": "bar", "fields": ["metric", "platform", "value"], "source_id": "serial_eval", "takeaway": "Dify 同时取得最高 Recall@10 与 MRR。", "palette_policy": "relaxed multi-category"},
            {"chart_id": "eval_latency_chart", "segment": "三平台评测", "question": "典型时延相差多少？", "family": "comparison", "type": "bar", "fields": ["stage", "platform", "p50_seconds", "p95_seconds"], "source_id": "serial_eval", "takeaway": "Dify retrieval 与 QA p50 均显著更低。", "palette_policy": "relaxed multi-category"},
            {"chart_id": "eval_qa_quality_chart", "segment": "三平台评测", "question": "答案文本匹配如何？", "family": "comparison", "type": "bar", "fields": ["metric", "platform", "value"], "source_id": "serial_eval", "takeaway": "MaxKB 的 Token F1 与 contains-gold 最高，但契约不同。", "palette_policy": "relaxed multi-category"},
            {"chart_id": "eval_judge_quality_chart", "segment": "三平台评测", "question": "完整 Judge 维度表现如何？", "family": "comparison", "type": "bar", "fields": ["metric", "platform", "value", "observed_n"], "source_id": "serial_eval", "takeaway": "MaxKB 在相关性、无矛盾与指令遵循上最高。", "palette_policy": "relaxed multi-category"},
            {"chart_id": "eval_unsupported_claims_chart", "segment": "三平台评测", "question": "谁更少产生无支持声明？", "family": "comparison", "type": "horizontalBar", "fields": ["platform", "value", "observed_n"], "source_id": "serial_eval", "takeaway": "FastGPT 风险最低，MaxKB 最高。", "palette_policy": "single-root preferred"},
        ]
    )
    return {"schema": base_chart_map.get("schema", "moi-rag-bench-report-chart-map-v1"), "charts": charts, "repeated_family_note": "All evaluation visuals use bar-family comparisons because the evidence is a single snapshot across three categorical platforms; no temporal trend is implied."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-artifact", type=Path, default=BASE_ARTIFACT)
    parser.add_argument("--base-chart-map", type=Path, default=BASE_CHART_MAP)
    parser.add_argument("--evaluation", type=Path, default=EVALUATION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--chart-map", type=Path, default=DEFAULT_CHART_MAP)
    args = parser.parse_args()

    evaluation = read_json(args.evaluation)
    validate_evaluation(evaluation)
    artifact = build_artifact(read_json(args.base_artifact), evaluation)
    chart_map = build_chart_map(read_json(args.base_chart_map))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.chart_map.write_text(json.dumps(chart_map, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    print(args.chart_map)


if __name__ == "__main__":
    main()
