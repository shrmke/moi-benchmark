#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build MOI RAG Benchmark 4.0 Final from frozen evaluation artifacts."""

from __future__ import annotations

import copy
import json
import math
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.build_moi_rag_benchmark_report_v2 import (  # noqa: E402
    maxkb_retrieval_diagnostics,
    read_jsonl,
)


DATASET = ROOT / "datasets/moi-rag-bench-v0.3.1-qa-revision"
READY = DATASET / "ready_for_eval"
API_ARTIFACT = ROOT / "runs/api-test-final-20260826-v2/artifact.json"
MERGED = ROOT / "runs/bench-v0.3.1-merged"
REPORT_DIR = ROOT / "results/reports"
ARTIFACT_OUT = REPORT_DIR / "moi-rag-benchmark-report-v4.0-final.artifact.json"
CHART_MAP_OUT = REPORT_DIR / "moi-rag-benchmark-report-v4.0-final.chart-map.json"

PLATFORMS = {
    "moi": ("MOI", "20260827-moi-v031-merged-275"),
    "dify": ("Dify", "20260827-dify-v031-merged-275"),
    "fastgpt": ("FastGPT", "20260827-fastgpt-v031-merged-275"),
    "maxkb": ("MaxKB*", "20260827-maxkb-v031-merged-275"),
}
SOURCE_NAMES = {
    "docbench": "DocBench",
    "enterprise": "EnterpriseRAG-Bench",
    "multihop": "MultiHop-RAG",
}
SOURCE_DOCS = {"docbench": 130, "enterprise": 47, "multihop": 120}
SOURCE_COVERAGE = {
    "docbench": "长文档事实问答、元数据、结构化抽取与不可回答",
    "enterprise": "企业多源语义、冲突、约束、完整性与拒答",
    "multihop": "跨文档比较、推断、时序与 null query",
}
SOURCE_SQL = {
    "dataset_revision": "WITH q AS (SELECT * FROM questions), l AS (SELECT * FROM question_lineage) SELECT * FROM q LEFT JOIN l USING (question_id)",
    "api_test": "SELECT * FROM event_capacity_aggregate UNION ALL SELECT * FROM lenovo_retrieval_summary",
    "rag_eval": "SELECT * FROM campaign_summary JOIN platform_metrics USING (system_id)",
    "maxkb_offline": "SELECT * FROM maxkb_retrieval_terminal ORDER BY comprehensive_score DESC",
}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def p95(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def pct(value):
    return None if value is None else round(float(value), 6)


def source(source_id: str, label: str, description: str, tables: list[str], metrics: list[str] | None = None):
    return {
        "id": source_id,
        "label": label,
        "query": {
            "engine": "local-filesystem",
            "language": "json/jsonl",
            "sql": SOURCE_SQL[source_id],
            "description": description,
            "tables_used": tables,
            "metric_definitions": metrics or [],
        },
    }


def metric_card(card_id: str, description: str, dataset: str, source_id: str, metrics: list[tuple[str, str, str]]):
    return {
        "id": card_id,
        "description": description,
        "dataset": dataset,
        "sourceId": source_id,
        "metrics": [{"label": label, "field": field, "format": fmt} for label, field, fmt in metrics],
    }


def table(table_id: str, title: str, dataset: str, source_id: str, columns: list[tuple[str, str, str]], subtitle: str = "", sort: tuple[str, str] | None = None):
    spec = {
        "id": table_id,
        "title": title,
        "dataset": dataset,
        "sourceId": source_id,
        "layout": "full",
        "columns": [
            {"field": field, "label": label, **({"type": "text"} if fmt == "text" else {"format": fmt})}
            for field, label, fmt in columns
        ],
    }
    if subtitle:
        spec["subtitle"] = subtitle
    if sort:
        spec["defaultSort"] = {"field": sort[0], "direction": sort[1]}
    return spec


def horizontal_chart(chart_id: str, title: str, subtitle: str, dataset: str, source_id: str, category: str, value: str, category_label: str, value_label: str, question: str):
    return {
        "id": chart_id,
        "title": title,
        "subtitle": subtitle,
        "intent": "comparison",
        "question": question,
        "rationale": "Horizontal bars keep category labels readable and make count differences explicit.",
        "comparisonContext": {"grain": category, "unit": value_label},
        "type": "horizontalBar",
        "dataset": dataset,
        "sourceId": source_id,
        "encodings": {
            "x": {"field": category, "type": "nominal", "label": category_label},
            "y": {"field": value, "type": "quantitative", "label": value_label},
        },
        "valueFormat": "number",
        "layout": "full",
        "labels": {"values": "auto"},
        "settings": {"sort": "descending", "showValues": True},
    }


def grouped_chart(chart_id: str, title: str, subtitle: str, dataset: str, source_id: str, x: str, y: str, color: str, x_label: str, y_label: str, color_label: str, question: str, denominator: str):
    return {
        "id": chart_id,
        "title": title,
        "subtitle": subtitle,
        "intent": "comparison",
        "question": question,
        "rationale": "Grouped bars compare the same normalized metric family across four platforms.",
        "comparisonContext": {"denominator": denominator, "grain": f"{x} × {color}", "unit": "rate"},
        "type": "bar",
        "dataset": dataset,
        "sourceId": source_id,
        "encodings": {
            "x": {"field": x, "type": "nominal", "label": x_label},
            "y": {"field": y, "type": "quantitative", "label": y_label, "format": "percent"},
            "color": {"field": color, "type": "nominal", "label": color_label},
        },
        "valueFormat": "percent",
        "layout": "full",
        "labels": {"values": "auto"},
        "settings": {"groupMode": "grouped", "sort": "none", "showValues": True},
        "legend": {"position": "bottom", "sort": "spec", "title": color_label},
    }


def markdown(block_id: str, body: str, source_id: str | None = None):
    block = {"id": block_id, "type": "markdown", "body": body}
    if source_id:
        block["sourceId"] = source_id
    return block


def main() -> None:
    revision = load_json(DATASET / "qa-revision-summary.json")
    validation = load_json(DATASET / "validation.json")
    manifest = load_json(READY / "manifest.json")
    questions = read_jsonl(READY / "questions.jsonl")
    lineage = read_jsonl(DATASET / "question-lineage.jsonl")
    corpus = read_jsonl(READY / "corpus.jsonl")
    api = load_json(API_ARTIFACT)
    campaign = load_json(MERGED / "campaign-summary.json")

    assert validation["status"] == "PASS"
    assert len(questions) == len(lineage) == 275
    assert manifest["counts"]["documents"] == 297
    assert revision["document_invariants"]["corpus_jsonl_byte_identical"] is True
    assert revision["text_only_signoff_rows"] == 275

    source_questions = Counter(q["source_dataset"] for q in questions)
    source_answerability = Counter((q["source_dataset"], bool(q["answerable"])) for q in questions)
    source_rows = [
        {
            "source": SOURCE_NAMES[key],
            "source_key": key,
            "documents": SOURCE_DOCS[key],
            "questions": source_questions[key],
            "qa_share": source_questions[key] / len(questions),
            "answerable": source_answerability[(key, True)],
            "unanswerable": source_answerability[(key, False)],
            "coverage": SOURCE_COVERAGE[key],
        }
        for key in ("docbench", "enterprise", "multihop")
    ]
    assert sum(row["documents"] for row in source_rows) == 297
    assert sum(row["questions"] for row in source_rows) == 275

    corpus_stats: dict[str, list[tuple[int, int]]] = {key: [] for key in SOURCE_NAMES}
    for row in corpus:
        key = row["metadata"]["source_dataset"]
        doc_path = READY / row["text_path"]
        text = doc_path.read_text(encoding="utf-8")
        corpus_stats[key].append((doc_path.stat().st_size, len(text)))
    corpus_rows = []
    for key in ("docbench", "enterprise", "multihop"):
        pairs = corpus_stats[key]
        chars = [item[1] for item in pairs]
        corpus_rows.append(
            {
                "source": SOURCE_NAMES[key],
                "documents": len(pairs),
                "size_mib": round(sum(item[0] for item in pairs) / 1024 / 1024, 2),
                "characters": sum(chars),
                "median_chars": round(statistics.median(chars)),
                "p95_chars": p95(chars),
                "max_chars": max(chars),
            }
        )

    capability_rows = [
        {"capability": key, "count": count, "share": count / 275}
        for key, count in sorted(revision["capability_counts"].items(), key=lambda item: (-item[1], item[0]))
    ]
    type_rows = [
        {"question_type": key, "count": count, "share": count / 275}
        for key, count in Counter(q["question_type"] for q in questions).most_common()
    ]
    gold_counts = Counter(len(q.get("gold_doc_ids") or []) for q in questions)
    gold_rows = [
        {"gold_documents": "0（Null/无文档）" if count == 0 else str(count), "count": gold_counts[count], "share": gold_counts[count] / 275}
        for count in sorted(gold_counts)
    ]
    lineage_counts = Counter(row["change_kind"] for row in lineage)
    origin_counts = Counter(row["origin"] for row in lineage)
    lineage_rows = [
        {"lineage": "仅重标 ID", "count": lineage_counts["reidentified"], "execution": "沿用 v0.3 结果"},
        {"lineage": "问题改写/重分类", "count": lineage_counts["rewritten"], "execution": "按用户要求沿用 v0.3 结果"},
        {"lineage": "新增构造 QA", "count": lineage_counts["added"], "execution": "四平台重新执行并 Judge"},
    ]
    validation_rows = [
        {"check": "文档与解析树冻结", "result": "PASS", "detail": "297 文档；corpus 与解析树逐字节一致"},
        {"check": "QA / Gold 闭包", "result": "PASS", "detail": "275 QA、275 Gold；顺序一致；引用文档闭包成立"},
        {"check": "重复问题", "result": "PASS", "detail": "归一化重复 0"},
        {"check": "多模态依赖", "result": "PASS", "detail": "图像依赖 0；275 条均完成纯文本签署"},
    ]

    metrics_by_platform = {}
    completion_by_system = {row["system_id"]: row for row in campaign["platforms"]}
    for key, (label, run_name) in PLATFORMS.items():
        aggregate = load_json(MERGED / run_name / "metrics-v1-aggregate.json")
        unified = aggregate["moi_unified"]
        flat = unified["metrics"]
        judge = unified["judge"]
        qa_metrics = aggregate["metrics"]["qa"]
        metrics_by_platform[key] = {
            "platform": label,
            "platform_key": key,
            "recall_at_1": flat["Recall@1"]["value"],
            "recall_at_3": flat["Recall@3"]["value"],
            "recall_at_5": flat["Recall@5"]["value"],
            "recall_at_10": flat["Recall@10"]["value"],
            "mrr_at_10": flat["MRR@10"]["value"],
            "token_f1": qa_metrics["token_f1"],
            "exact_match": qa_metrics["exact_match"],
            "contains_gold": qa_metrics["contains_gold"],
            "answer_relevance": judge["answer_relevance"]["value"],
            "contradiction_free": judge["contradiction_free"]["value"],
            "instruction_compliance": judge["instruction_compliance"]["value"],
            "response_claim_correctness": judge["response_claim_correctness"]["value"],
            "response_claim_missing": judge["response_claim_correctness"]["missing_n"],
            "runtime_context_faithfulness": judge["runtime_context_faithfulness"]["value"],
            "context_faithfulness_missing": judge["runtime_context_faithfulness"]["missing_n"],
            "unsupported_claim_rate": judge["unsupported_claim_rate"]["value"],
            "strict_unanswerable_success": judge["strict_unanswerable_success"]["value"],
            "retrieval_method": "原生/统一层返回顺序",
            "comparability": "严格可比",
            "run_mode": "33 新增实跑 + 242 沿用",
        }

    maxkb_diag = maxkb_retrieval_diagnostics(
        MERGED / PLATFORMS["maxkb"][1], questions
    )
    maxkb_main = maxkb_diag["orderings"]["comprehensive_score_desc"]
    maxkb_row = metrics_by_platform["maxkb"]
    maxkb_row.update(
        {
            "recall_at_1": maxkb_main["recall_at_1"],
            "recall_at_3": maxkb_main["recall_at_3"],
            "recall_at_5": maxkb_main["recall_at_5"],
            "recall_at_10": maxkb_main["recall_at_10"],
            "mrr_at_10": maxkb_main["mrr"],
            "retrieval_method": "诊断命中 + comprehensive_score 离线降序",
            "comparability": "诊断代换；非原生公开 API",
        }
    )
    quality_rows = list(metrics_by_platform.values())

    retrieval_chart_rows = [
        {"platform": row["platform"], "metric": metric_label, "value": row[field]}
        for row in quality_rows
        for field, metric_label in (
            ("recall_at_1", "Recall@1"),
            ("recall_at_10", "Recall@10"),
            ("mrr_at_10", "MRR@10"),
        )
    ]
    lexical_chart_rows = [
        {"platform": row["platform"], "metric": metric_label, "value": row[field]}
        for row in quality_rows
        for field, metric_label in (("token_f1", "Token F1"), ("contains_gold", "Contains Gold"))
    ]
    judge_chart_rows = [
        {"platform": row["platform"], "metric": metric_label, "value": row[field]}
        for row in quality_rows
        for field, metric_label in (
            ("answer_relevance", "答案相关性"),
            ("contradiction_free", "无矛盾"),
            ("instruction_compliance", "指令遵循"),
        )
    ]
    unsupported_rows = [
        {"platform": row["platform"], "unsupported_claim_rate": row["unsupported_claim_rate"]}
        for row in quality_rows
    ]
    maxkb_sensitivity_rows = [
        {
            "ordering": label,
            "recall_at_1": values["recall_at_1"],
            "recall_at_3": values["recall_at_3"],
            "recall_at_5": values["recall_at_5"],
            "recall_at_10": values["recall_at_10"],
            "mrr_at_10": values["mrr"],
        }
        for label, values in (
            ("API 原返回顺序", maxkb_diag["orderings"]["api_return_order"]),
            ("comprehensive_score 离线降序（主口径）", maxkb_main),
        )
    ]
    completion_rows = []
    for key, (label, _) in PLATFORMS.items():
        campaign_row = completion_by_system[f"{key}_local"]
        completion_rows.append(
            {
                "platform": label,
                "new_executed": campaign_row["executed_questions"],
                "inherited": campaign_row["inherited_questions"],
                "inherited_changed": campaign_row["inherited_questions_changed_from_parent"],
                "retrieval_success": campaign_row["retrieval_success"],
                "qa_success": campaign_row["qa_success"],
                "judge_success": campaign_row["judge_success"],
                "status": campaign_row["metrics_status"],
            }
        )

    api_datasets = api["snapshot"]["datasets"]
    api_blocks = {block["id"]: block for block in api["manifest"]["blocks"]}
    minimal_capacity = copy.deepcopy(api_datasets["minimal_capacity"])
    event_capacity = copy.deepcopy(api_datasets["event_capacity"])
    api_c8 = [row for row in event_capacity if row["connections"] == 8]
    api_c8_by_platform = {row["platform_key"]: row for row in api_c8}
    lenovo_summary = copy.deepcopy(api_datasets["lenovo_summary"])
    lenovo_latency = copy.deepcopy(api_datasets["lenovo_latency"])
    contracts = copy.deepcopy(api_datasets["contracts"])

    datasets = {
        "summary_scale": [{"documents": 297, "questions": 275, "gold": 275, "text_only": 275}],
        "summary_revision": [{"added": 33, "removed": 33, "rewritten": 49, "documents_changed": 0}],
        "summary_api": [{
            "moi_qps": api_c8_by_platform["moi"]["request_qps"],
            "dify_qps": api_c8_by_platform["dify"]["request_qps"],
            "fastgpt_qps": api_c8_by_platform["fastgpt"]["request_qps"],
            "maxkb_qps": api_c8_by_platform["maxkb"]["request_qps"],
        }],
        "summary_eval": [{"platforms": 4, "retrieval_success": 1100, "qa_success": 1100, "judge_success": 1100}],
        "source_composition": source_rows,
        "corpus_profile": corpus_rows,
        "capability_distribution": capability_rows,
        "question_type_distribution": type_rows,
        "gold_cardinality": gold_rows,
        "lineage_distribution": lineage_rows,
        "validation_checks": validation_rows,
        "contracts": contracts,
        "minimal_capacity": minimal_capacity,
        "event_capacity": event_capacity,
        "api_c8": api_c8,
        "lenovo_summary": lenovo_summary,
        "lenovo_latency": lenovo_latency,
        "run_completion": completion_rows,
        "quality_summary": quality_rows,
        "retrieval_chart": retrieval_chart_rows,
        "lexical_chart": lexical_chart_rows,
        "judge_chart": judge_chart_rows,
        "unsupported_claims": unsupported_rows,
        "maxkb_sensitivity": maxkb_sensitivity_rows,
    }

    sources = [
        source(
            "dataset_revision",
            "MOI RAG Benchmark v0.3.1 QA revision",
            "Profiles the frozen 297-document corpus, revised 275 QA/Gold rows, lineage, and validation sign-off.",
            ["ready_manifest", "qa_revision_summary", "validation", "questions", "gold", "question_lineage"],
            ["question counts are row counts", "multi-document means at least two Gold document IDs"],
        ),
        source(
            "api_test",
            "API Test Final 20260826 v2",
            "Validated four-platform API contracts, Minimal API and fixed-mock capacity medians, Lenovo ten-query retrieval latency, and deterministic diagnostics.",
            ["api_contracts", "minimal_capacity_aggregate", "event_capacity_aggregate", "lenovo_retrieval_summary", "capacity_diagnostics"],
            [
                "request QPS is successful completed requests divided by measured wall time",
                "TTFE ends at the first complete SSE event and is not TTFT",
                "Event Throughput is native SSE events divided by measured wall time",
            ],
        ),
        source(
            "rag_eval",
            "v0.3.1 four-platform merged evaluation",
            "Profiles the completed MOI, Dify, FastGPT, and MaxKB merged retrieval, QA, and uniform-judge ledgers.",
            ["campaign_summary", "moi_metrics", "dify_metrics", "fastgpt_metrics", "maxkb_metrics"],
            ["retrieval denominator is 265 QA with Gold documents", "general Judge dimensions use 275 QA; answerable-only dimensions use 245; strict unanswerable uses 30"],
        ),
        source(
            "maxkb_offline",
            "MaxKB offline retrieval substitution",
            "Reconstructs MaxKB retrieval ranks by sorting diagnostic hits on comprehensive_score descending; API order is retained as sensitivity evidence.",
            ["maxkb_retrieval_terminal", "questions"],
            ["main MaxKB retrieval is diagnostic and not a strict native public-API result"],
        ),
    ]

    cards = [
        metric_card("scale_card", "v0.3.1 评测包规模。", "summary_scale", "dataset_revision", [
            ("文档", "documents", "number"), ("QA", "questions", "number"), ("Gold", "gold", "number"), ("纯文本", "text_only", "number")
        ]),
        metric_card("revision_card", "相对 v0.3 Final 的 QA-only 修订。", "summary_revision", "dataset_revision", [
            ("新增", "added", "number"), ("移除", "removed", "number"), ("改写/重分类", "rewritten", "number"), ("文档变化", "documents_changed", "number")
        ]),
        metric_card("api_card", "固定 mock、Connections=8 的 Request QPS。", "summary_api", "api_test", [
            ("MOI", "moi_qps", "number"), ("Dify", "dify_qps", "number"), ("FastGPT", "fastgpt_qps", "number"), ("MaxKB", "maxkb_qps", "number")
        ]),
        metric_card("eval_card", "四平台合并账本终态。", "summary_eval", "rag_eval", [
            ("平台", "platforms", "number"), ("Retrieval 成功", "retrieval_success", "number"), ("QA 成功", "qa_success", "number"), ("Judge 成功", "judge_success", "number")
        ]),
    ]

    api_charts = {
        chart["id"]: copy.deepcopy(chart)
        for chart in api["manifest"]["charts"]
        if chart["id"] in {"minimal_qps", "event_qps", "event_ttfe", "event_eps", "lenovo_latency"}
    }
    for chart in api_charts.values():
        chart["sourceId"] = "api_test"

    charts = [
        horizontal_chart(
            "source_qa_chart", "QA 来源构成", "v0.3.1；DocBench 110、Enterprise 70、MultiHop 95。",
            "source_composition", "dataset_revision", "source", "questions", "来源", "QA 数", "修订后的 QA 在三个来源间如何分布？"
        ),
        horizontal_chart(
            "capability_chart", "主能力构成", "每题分配一个主评估维度；总计 275。",
            "capability_distribution", "dataset_revision", "capability", "count", "能力", "QA 数", "数据集主要评估哪些 RAG 能力？"
        ),
        horizontal_chart(
            "gold_cardinality_chart", "Gold 文档基数", "98 条为多文档问题；10 条为无 Gold 文档的 null/refusal 问题。",
            "gold_cardinality", "dataset_revision", "gold_documents", "count", "Gold 文档数", "QA 数", "问题需要多少文档才能回答？"
        ),
        api_charts["minimal_qps"],
        api_charts["event_qps"],
        api_charts["event_ttfe"],
        api_charts["event_eps"],
        api_charts["lenovo_latency"],
        grouped_chart(
            "retrieval_quality_chart", "四平台检索质量", "MaxKB* 使用 comprehensive_score 离线降序；其余为统一层原生顺序。",
            "retrieval_chart", "rag_eval", "platform", "value", "metric", "平台", "得分", "指标", "四平台在有 Gold 文档的 265 条问题上检索效果如何？", "265 条有 Gold 文档 QA"
        ),
        grouped_chart(
            "lexical_quality_chart", "答案词面匹配", "同一参考答案；Token F1 与 Contains Gold 均为高者更好。",
            "lexical_chart", "rag_eval", "platform", "value", "metric", "平台", "得分", "指标", "四平台生成答案与参考答案的词面重合程度如何？", "275 条 QA"
        ),
        grouped_chart(
            "judge_quality_chart", "统一 Judge 质量维度", "DeepSeek V4 Flash；temperature=0；三项均为高者更好。",
            "judge_chart", "rag_eval", "platform", "value", "metric", "平台", "得分", "Judge 指标", "同一 Judge 如何评价四平台答案？", "275 条 QA"
        ),
        horizontal_chart(
            "unsupported_claim_chart", "Unsupported Claim Rate（越低越好）", "单独展示以避免与高者更好指标混淆。",
            "unsupported_claims", "rag_eval", "platform", "unsupported_claim_rate", "平台", "比例", "哪个平台更容易产生参考证据不支持的断言？"
        ),
    ]
    charts[-1]["valueFormat"] = "percent"
    charts[-1]["encodings"]["y"]["format"] = "percent"

    api_tables = {
        item["id"]: copy.deepcopy(item)
        for item in api["manifest"]["tables"]
        if item["id"] in {"contracts", "minimal_table", "event_table", "lenovo_table"}
    }
    for item in api_tables.values():
        item["sourceId"] = "api_test"

    tables = [
        table("source_table", "来源构成", "source_composition", "dataset_revision", [
            ("source", "来源", "text"), ("documents", "文档", "number"), ("questions", "QA", "number"),
            ("qa_share", "QA 占比", "percent"), ("answerable", "可回答", "number"),
            ("unanswerable", "不可回答", "number"), ("coverage", "覆盖重点", "text")
        ], sort=("questions", "desc")),
        table("corpus_table", "Ready Markdown 语料规模", "corpus_profile", "dataset_revision", [
            ("source", "来源", "text"), ("documents", "文档", "number"), ("size_mib", "MiB", "number"),
            ("characters", "字符", "number"), ("median_chars", "中位字符", "number"),
            ("p95_chars", "P95 字符", "number"), ("max_chars", "最大字符", "number")
        ], "字符为 Unicode 字符数，不等同于模型 token。", ("documents", "desc")),
        table("type_table", "Question Type 明细", "question_type_distribution", "dataset_revision", [
            ("question_type", "Question Type", "text"), ("count", "QA", "number"), ("share", "占比", "percent")
        ], sort=("count", "desc")),
        table("lineage_table", "v0.3.1 QA 血缘与执行方式", "lineage_distribution", "dataset_revision", [
            ("lineage", "变更类型", "text"), ("count", "QA", "number"), ("execution", "评测处理", "text")
        ], sort=("count", "desc")),
        table("validation_table", "数据集发布门槛", "validation_checks", "dataset_revision", [
            ("check", "检查", "text"), ("result", "结果", "text"), ("detail", "详情", "text")
        ], sort=("check", "asc")),
        api_tables["contracts"],
        api_tables["minimal_table"],
        api_tables["event_table"],
        api_tables["lenovo_table"],
        table("completion_table", "v0.3.1 四平台合并账本", "run_completion", "rag_eval", [
            ("platform", "平台", "text"), ("new_executed", "新增实跑", "number"),
            ("inherited", "沿用历史", "number"), ("inherited_changed", "其中改写/重分类", "number"),
            ("retrieval_success", "Retrieval 成功", "number"), ("qa_success", "QA 成功", "number"),
            ("judge_success", "Judge 成功", "number"), ("status", "状态", "text")
        ], sort=("platform", "asc")),
        table("retrieval_table", "检索质量精确值", "quality_summary", "rag_eval", [
            ("platform", "平台", "text"), ("recall_at_1", "Recall@1", "percent"),
            ("recall_at_3", "Recall@3", "percent"), ("recall_at_5", "Recall@5", "percent"),
            ("recall_at_10", "Recall@10", "percent"), ("mrr_at_10", "MRR@10", "percent"),
            ("retrieval_method", "排序口径", "text"), ("comparability", "可比性", "text")
        ], "分母 265；MaxKB* 为离线诊断代换。", ("recall_at_10", "desc")),
        table("maxkb_sensitivity_table", "MaxKB 排序敏感性", "maxkb_sensitivity", "maxkb_offline", [
            ("ordering", "排序", "text"), ("recall_at_1", "Recall@1", "percent"),
            ("recall_at_3", "Recall@3", "percent"), ("recall_at_5", "Recall@5", "percent"),
            ("recall_at_10", "Recall@10", "percent"), ("mrr_at_10", "MRR@10", "percent")
        ], "两种排序的 Top-10 候选集合相同，差异主要来自顺序。", ("mrr_at_10", "desc")),
        table("answer_table", "答案与 Judge 精确值", "quality_summary", "rag_eval", [
            ("platform", "平台", "text"), ("token_f1", "Token F1", "percent"),
            ("exact_match", "Exact Match", "percent"), ("contains_gold", "Contains Gold", "percent"),
            ("answer_relevance", "答案相关性", "percent"), ("contradiction_free", "无矛盾", "percent"),
            ("instruction_compliance", "指令遵循", "percent"),
            ("response_claim_correctness", "Response Claim Correctness", "percent"),
            ("response_claim_missing", "该项缺失", "number"),
            ("runtime_context_faithfulness", "Runtime Context Faithfulness", "percent"),
            ("context_faithfulness_missing", "该项缺失", "number"),
            ("unsupported_claim_rate", "Unsupported Claim Rate", "percent"),
            ("strict_unanswerable_success", "严格拒答成功", "percent")
        ], "Judge 使用完全相同参数；通用维度 n=275，回答型维度 n=245，严格拒答 n=30。", ("answer_relevance", "desc")),
    ]

    title = "MOI RAG Benchmark 4.0 Final：四平台综合评测"
    blocks = [
        markdown("title", f"# {title}"),
        markdown(
            "technical_summary",
            "## Technical Summary / 技术摘要\n\n"
            "本报告将 **v0.3.1 QA-only 数据集、最新 API Test Final v2、四平台检索与答案评测** 同步到一份最终技术报告，并把结论拆成不可互相替代的三个层级。API 数据面在 `1 chunk × 0 ms` Minimal 场景的各平台最高中位数为 MOI **0.834@C4**、Dify **0.465@C8**、FastGPT **26.926@C4**、MaxKB **23.242@C8**；在 `64 chunks × 10 ms` 的 C8 场景依次为 **0.364 / 0.450 / 9.926 / 8.616 QPS**，只表示平台调度与流式转发容量。检索系统方面，Lenovo 10-query p50 为 MOI **981.9 ms**、Dify **1369.7 ms**、FastGPT **443.9 ms**、MaxKB **2098.4 ms**；265 条有 Gold 文档 QA 上，FastGPT 原生 Recall@10 为 **85.66%**，MaxKB 离线诊断代换为 **86.23%***。原生端到端答案评测中，MOI Token F1 **30.05%** 最高，MaxKB* 统一 Judge 相关性 **69.31%** 最高，但其 Unsupported Claim Rate 也最高（**72.47%**）。不同层级不能合成单一总分，本报告不宣布总体赢家。"
        ),
        markdown("scope", "### 报告边界\n\n- 数据：v0.3.1 是冻结 v0.3 Final 文档库上的 QA-only 修订，共 297 文档、275 QA/Gold。\n- API 数据面容量：Minimal 与固定 64-chunk mock，只测公开数据面的路由、鉴权、调度和 SSE 转发。\n- 检索系统：Lenovo 10-query 测当前部署的完整检索时延；265 条有 Gold 文档 QA 测检索质量，两者不是同一次实验。\n- 原生端到端 RAG：Embedding 统一使用 MaaS `bge-m3`（1024 维），生成与 Judge 统一使用 DeepSeek V4 Flash；不调用 MLLM。\n- 星号：`MaxKB*` 表示检索指标使用诊断接口命中并按 `comprehensive_score` 离线降序恢复，非严格原生公开 API 结果。"),

        markdown("dataset_heading", "## 1. 数据集构建与介绍\n\n数据集从 v0.3 Final 的 1,000 条候选 QA 质量审核结果继续演进：文档、Ready Markdown、解析树和 embedding/index 均冻结，仅重做问题侧。v0.3.1 移除 33 条缺陷或冗余问题，并在原题库无合适候选时基于冻结语料构造 33 条新 QA，使总量仍为 275；另有 49 条发生问题改写或类型重分类。", "dataset_revision"),
        {"id": "scale_metrics", "type": "metric-strip", "cardIds": ["scale_card"]},
        {"id": "revision_metrics", "type": "metric-strip", "cardIds": ["revision_card"]},
        markdown("source_context", "### 三类来源覆盖互补\n\n文档端仍为 DocBench 130、EnterpriseRAG-Bench 47、MultiHop-RAG 120；QA 端修订为 110 / 70 / 95。相比旧版，Enterprise 问题显著增加，用于补齐完整性、冲突、约束与拒答，而 DocBench 的冗余单文档事实题被压缩。", "dataset_revision"),
        {"id": "source_chart_block", "type": "chart", "chartId": "source_qa_chart", "layout": "full"},
        {"id": "source_table_block", "type": "table", "tableId": "source_table", "layout": "full"},
        markdown("corpus_context", "### 冻结语料库\n\n原始源文件与 Ready-for-eval 解析结果继续物理分离；评测入口只消费 297 份 Ready Markdown。DocBench 长文档占绝大多数体量，Enterprise 与 MultiHop 提供更多短文档、多源组合场景。当前语料合计约 **32.78 MiB、34,207,266 个 Unicode 字符**。", "dataset_revision"),
        {"id": "corpus_table_block", "type": "table", "tableId": "corpus_table", "layout": "full"},
        markdown("capability_context", "### 问题从单一事实问答扩展到七类主能力\n\n主能力分布为 QA 115、Multi-hop 85、Refusal 30、Completeness 15、Conflict 10、Constraint 10、Structured/Meta 10。拒答、完整证据集、冲突与约束题使评测不再只奖励‘检索到一段并复述’。", "dataset_revision"),
        {"id": "capability_chart_block", "type": "chart", "chartId": "capability_chart", "layout": "full"},
        {"id": "type_table_block", "type": "table", "tableId": "type_table", "layout": "full"},
        markdown("gold_context", "### Gold 与可回答性\n\n全部 275 条 QA 都有 Gold 行和参考答案；其中 265 条含 Gold 文档，10 条 null/refusal 问题的正确 Gold 文档集合为空。98 条问题需要至少两份文档，能够区分‘命中任一证据’与‘找全证据集合’。", "dataset_revision"),
        {"id": "gold_chart_block", "type": "chart", "chartId": "gold_cardinality_chart", "layout": "full"},
        markdown("lineage_context", "### 数据血缘与质量门槛\n\n193 条只重标 question_id，49 条改写或重分类，33 条为冻结语料上新增构造。构造前先检查原题库；目标新增类型没有合格候选后才创建新 QA。数据发布验证确认文档未变、Gold 闭包成立、归一化重复为 0、图像依赖为 0。", "dataset_revision"),
        {"id": "lineage_table_block", "type": "table", "tableId": "lineage_table", "layout": "full"},
        {"id": "validation_table_block", "type": "table", "tableId": "validation_table", "layout": "full"},

        markdown("api_heading", "## 2. API 数据面容量\n\n本节只比较公开数据面的路由、鉴权、调度、模型适配与 SSE 转发。Minimal API 与固定 64-chunk mock 均不包含真实检索或真实模型生成，不能替代后续检索质量和端到端 RAG 结论。", "api_test"),
        {"id": "api_metrics", "type": "metric-strip", "cardIds": ["api_card"]},
        markdown("contract_heading", api_blocks["contract_heading"]["body"], "api_test"),
        markdown("contracts_context", api_blocks["contracts_context"]["body"], "api_test"),
        {"id": "contracts_table_block", "type": "table", "tableId": "contracts", "layout": "full"},
        markdown("minimal_heading", api_blocks["minimal_heading"]["body"], "api_test"),
        {"id": "minimal_qps_block", "type": "chart", "chartId": "minimal_qps", "layout": "full"},
        markdown("minimal_table_context", api_blocks["minimal_table_context"]["body"], "api_test"),
        {"id": "minimal_table_block", "type": "table", "tableId": "minimal_table", "layout": "full"},
        markdown("event_heading", api_blocks["event_heading"]["body"], "api_test"),
        {"id": "event_qps_block", "type": "chart", "chartId": "event_qps", "layout": "full"},
        markdown("first_event_context", api_blocks["first_event_context"]["body"], "api_test"),
        {"id": "event_ttfe_block", "type": "chart", "chartId": "event_ttfe", "layout": "full"},
        markdown("eps_heading", api_blocks["eps_heading"]["body"], "api_test"),
        {"id": "event_eps_block", "type": "chart", "chartId": "event_eps", "layout": "full"},
        markdown("event_table_context", api_blocks["event_table_context"]["body"], "api_test"),
        {"id": "event_table_block", "type": "table", "tableId": "event_table", "layout": "full"},

        markdown("retrieval_heading", "## 3. 检索系统性能与质量\n\n本节保留两个独立实验：Lenovo 10-query 只测当前本地部署的完整检索链路时延；v0.3.1 的 265 条有 Gold 文档 QA 测 Recall/MRR。两者数据规模、接口与目标不同，只在各自实验内比较四个平台。"),
        markdown("lenovo_heading", api_blocks["lenovo_heading"]["body"], "api_test"),
        {"id": "lenovo_chart_block", "type": "chart", "chartId": "lenovo_latency", "layout": "full"},
        markdown("lenovo_table_context", api_blocks["lenovo_table_context"]["body"], "api_test"),
        {"id": "lenovo_table_block", "type": "table", "tableId": "lenovo_table", "layout": "full"},
        {"id": "eval_metrics", "type": "metric-strip", "cardIds": ["eval_card"]},
        markdown("incremental_warning", "### 质量评测执行口径\n\n本轮严格实跑的是 **33 条新增 QA**；其余 242 条按用户要求映射历史 v0.3 结果，其中 193 条仅 ID 变化，49 条问题文本、参考答案或类型发生过改写/重分类但没有重新生成和 Judge。因此下列 275 条聚合是**带完整血缘的增量估计**，不是严格意义上的 v0.3.1 全量物理重跑。", "rag_eval"),
        markdown("completion_table_context", "#### 表前实验说明：四平台评测账本完整性\n\n- **测试目的：** 核对进入检索质量和答案效果汇总的四平台样本是否完整，并披露新增实跑与历史沿用的组成。\n- **具体配置：** 同一冻结 297 文档语料、275 QA/Gold；每个平台目标为 275 Retrieval、275 QA、275 Judge 终态记录。\n- **实验方法：** 33 条新增 QA 在四平台实跑，242 条按血缘映射 v0.3 结果；统一检查成功状态、Judge 参数签名和每平台分母。\n- **比较口径：** 本表比较账本完整性，不比较平台性能；四平台均为 275/275/275 成功，但 49 条改写/重分类 QA 沿用旧结果是共同效度限制。", "rag_eval"),
        {"id": "completion_table_block", "type": "table", "tableId": "completion_table", "layout": "full"},
        markdown("retrieval_context", "### 检索：MOI 更集中在首位，FastGPT/Dify 在扩大 Top-K 后提升明显\n\nRecall@K 是每题 Top-K 中找回的 Gold 文档比例，MRR@10 衡量首个相关文档的排序位置，分母均为 265 条含 Gold 文档 QA。MOI Recall@1 为 **59.50%**，高于 Dify **54.34%** 与 FastGPT **56.07%**；但 MOI 到 Recall@10 仅升至 **61.95%**。FastGPT Recall@10 达 **85.66%**，Dify 为 **83.33%**。MaxKB* 经离线分数降序后 Recall@10 为 **86.23%**、MRR@10 为 **77.54%**，但它属于诊断代换，不能与前三者标成同等级原生接口结论。", "rag_eval"),
        {"id": "retrieval_chart_block", "type": "chart", "chartId": "retrieval_quality_chart", "layout": "full"},
        markdown("retrieval_table_context", "#### 表前实验说明：四平台检索质量\n\n- **测试目的：** 比较相同问题与 Gold 文档下的文档召回覆盖和首个相关结果排序。\n- **具体配置：** 同一 297 文档冻结语料；265 条含 Gold 文档 QA；Embedding 统一为 `bge-m3` 1024 维；报告 Recall@1/3/5/10 与 MRR@10。\n- **实验方法：** 读取四平台最终检索终态；MOI、Dify、FastGPT 使用统一层原生顺序，MaxKB* 使用诊断命中并按 `comprehensive_score` 离线降序，同时保留 API 原顺序敏感性对照。\n- **比较口径：** 前三平台严格可比；MaxKB* 为诊断代换，必须带星号，不作为同等级公开 API 结论。", "rag_eval"),
        {"id": "retrieval_table_block", "type": "table", "tableId": "retrieval_table", "layout": "full"},
        markdown("maxkb_context", "### MaxKB 排序代换的影响\n\nAPI 原返回顺序和 `comprehensive_score` 降序拥有相同的 Top-10 候选集合，所以 Recall@10 都是 **86.23%**；排序恢复把 Recall@1 从 **13.81%** 提升到 **55.06%**、MRR@10 从 **34.66%** 提升到 **77.54%**。该敏感性实验只比较 MaxKB 的两种排序，不是四平台排名。", "maxkb_offline"),
        markdown("maxkb_sensitivity_context", "#### 表前实验说明：MaxKB 排序敏感性审计\n\n- **测试目的：** 判断 MaxKB 检索主值对返回顺序解释的敏感程度，并验证离线分数排序是否只改变顺序而不改变 Top-10 候选集合。\n- **具体配置：** 同一 265 条含 Gold 文档 QA、同一 MaxKB 诊断命中集合、Top-10；对比 API 原返回顺序与 `comprehensive_score` 降序。\n- **实验方法：** 对两种顺序分别离线重算 Recall@1/3/5/10 与 MRR@10，并核对候选集合一致性。\n- **比较口径：** 这是单平台方法敏感性审计，不是缺少另外三家的平台横向表；所有正式平台排名仍在上一张四平台检索质量表中。", "maxkb_offline"),
        {"id": "maxkb_sensitivity_block", "type": "table", "tableId": "maxkb_sensitivity_table", "layout": "full"},

        markdown("rag_heading", "## 4. 原生端到端 RAG 效果\n\n四个平台使用同一冻结语料、`bge-m3` embedding、DeepSeek V4 Flash 生成模型与同一 Judge 参数。每个平台均有 275 条 QA 与 275 条 Judge 成功终态；本节评价答案效果，不用 API fixed-mock QPS 或 Lenovo 10-query 延迟替代答案质量。", "rag_eval"),
        markdown("answer_context", "### 答案：MOI 的词面 F1 最高，但词面指标不能代替语义正确性\n\nToken F1 为 MOI **30.05%**、MaxKB* **29.00%**、Dify **24.12%**、FastGPT **19.49%**；Contains Gold 则为 MaxKB* **26.91%**、MOI **24.73%**、Dify **24.36%**、FastGPT **13.45%**。Exact Match 全部低于 1.1%，反映开放式答案措辞差异很大，应以 Judge 和证据指标联合解释。", "rag_eval"),
        {"id": "lexical_chart_block", "type": "chart", "chartId": "lexical_quality_chart", "layout": "full"},
        markdown("judge_context", "### Judge：MaxKB/MOI 相关性较高，但幻觉风险仍是共同短板\n\n答案相关性为 MaxKB* **69.31%**、MOI **68.47%**、FastGPT **57.09%**、Dify **55.49%**；无矛盾率以 MaxKB* **92.84%** 最高。另一方面 Unsupported Claim Rate（越低越好）仍为 Dify **60.64%**、FastGPT **61.09%**、MOI **64.97%**、MaxKB* **72.47%**。高相关性与高不支持断言可以同时出现，不能把单一 Judge 维度当总体质量。\n\n回答型指标仅适用于 245 条可回答问题：Response Claim Correctness 为 MaxKB* **65.80%**、MOI **61.02%**、FastGPT **50.98%**、Dify **47.02%**；Runtime Context Faithfulness 为 MaxKB* **66.73%**、MOI **62.72%**、FastGPT **51.39%**、Dify **50.80%**。同参数重试后的 MaxKB 残余项均来自不可回答问题，按协议不进入回答型分母；MOI 唯一残余项存在显式但无关的运行上下文，按定义计 0。重新聚合后，通用维度 275/275、回答型维度 245/245、严格拒答 30/30 均完整覆盖，未进行均值插补。", "rag_eval"),
        {"id": "judge_chart_block", "type": "chart", "chartId": "judge_quality_chart", "layout": "full"},
        markdown("unsupported_context", "### 不支持断言必须单独看（越低越好）\n\n四平台都还有明显的证据约束空间，尤其 MaxKB* 在较高相关性下仍产生最多参考证据不支持的断言。后续优化应优先做证据引用、拒答阈值和生成约束，而不是只继续提高 Top-K。", "rag_eval"),
        {"id": "unsupported_chart_block", "type": "chart", "chartId": "unsupported_claim_chart", "layout": "full"},
        markdown("answer_table_context", "#### 表前实验说明：四平台答案与统一 Judge\n\n- **测试目的：** 在相同语料、问题、生成模型和 Judge 下，比较答案词面覆盖、相关性、证据一致性、指令遵循和拒答表现。\n- **具体配置：** 275 QA/Gold；生成与 Judge 均为 DeepSeek V4 Flash；Judge temperature=0、max_tokens=2048、thinking disabled，Prompt 与响应 schema 哈希一致。\n- **实验方法：** 四平台逐题生成答案并统一离线评分；Token F1/Exact Match/Contains Gold 与通用 Judge 分母为 275，回答型 Judge 分母为 245，严格拒答分母为 30；结构性不适用样本不进入对应分母。\n- **比较口径：** 表中四平台都存在；MaxKB* 仍保留检索诊断代换标记。单项高分不能替代证据支持率，也不合成总体冠军。", "rag_eval"),
        {"id": "answer_table_block", "type": "table", "tableId": "answer_table", "layout": "full"},

        markdown(
            "cause_analysis",
            api_blocks["cause_analysis"]["body"].replace(
                "## 附录：为什么会出现这些结果（只列确定性原因）",
                "## 5. 为什么出现这些性能结果（只列确定性原因）",
                1,
            ),
            "api_test",
        ),
        markdown("methodology_heading", "## 6. 范围、方法与指标定义\n\n三个评测层分别使用自己的负载、样本和成功标准；只有同一层级、同一配置下的四平台数值可以横向解释。"),
        markdown(
            "api_methodology",
            api_blocks["methodology"]["body"].replace("## 方法与数据质量", "### API 数据面方法与数据质量", 1),
            "api_test",
        ),
        markdown(
            "api_definitions",
            api_blocks["definitions"]["body"].replace("## 指标定义", "### API 指标定义", 1),
            "api_test",
        ),
        markdown("rag_definitions", "### 检索与答案指标定义\n\n- **Recall@K：** 每题在 Top-K 中找回的 Gold 文档比例，分母为 265 条有 Gold 文档问题。\n- **MRR@10：** 首个相关文档排名倒数的均值；无命中记 0。\n- **Token F1 / Exact Match / Contains Gold：** 参考答案词面匹配，分母 275。\n- **Judge：** `deepseek-official / deepseek-v4-flash`，temperature=0，max_tokens=2048，thinking disabled；Prompt 与响应 schema 哈希在四平台完全一致。通用维度分母为 275，回答型证据维度为 245，严格拒答为 30。\n- **Strict Unanswerable Success：** 30 条拒答/不可回答题上的成功率。\n- **Unsupported Claim Rate：** Judge 判断答案中存在参考证据不支持断言的比例，越低越好。", "rag_eval"),
        markdown("limitations", "## 7. 局限性与稳健性\n\n1. 只有 33 条新增问题完成 v0.3.1 实跑；49 条改写/重分类样本沿用旧结果，是质量评测最大的效度限制。\n2. MaxKB 检索来自管理诊断接口和离线排序，主值必须保留星号；API 原顺序已作为敏感性对照。\n3. Minimal 与固定 64-chunk mock 不包含真实检索、rerank 或真实模型生成；四平台最短官方调用图也不完全同构，容量结果不能外推为真实 RAG 排名。\n4. TTFE 是首个完整 SSE event，不筛选事件类型，也不是首个答案 token 的 TTFT；历史样本无法事后还原 TTFT。\n5. Lenovo 只有 10 条查询，MOI 实际并发为 1、其余为 4，且各平台检索合同不同；它只是当前部署诊断，不是向量数据库 kernel microbenchmark。\n6. Dify C1、MOI Minimal 三档与 MaxKB Minimal C4/C8 存在较高轮间 CV；报告保留 min/max/CV，没有用补跑替换低轮次。\n7. 生成模型与 Judge 属于同一模型家族，可能存在自评偏差；Judge 通用、回答型与严格拒答维度使用不同适用分母，不能假设共享 275 条分母。\n8. API 容量来自单机 4 CPU、约 12 GiB Docker VM，不外推到生产集群。"),
        markdown("next_steps", "## 8. 建议的下一步\n\n1. 最小补跑 49 条改写/重分类 QA；正式发布前建议四平台全量 275 再跑一次并冻结 run manifest。\n2. API harness 同时记录首个 event 类型与首个非空答案内容，分别报告 response-start、TTFE 与 TTFT。\n3. 对 Dify 与 MOI 增加 queue/worker/connection-pool tracing；对 FastGPT C3–C8 细分压测，并对 MaxKB、Dify、MOI 高 CV 档做 10 轮稳定性复测。\n4. 为 MaxKB 增加文档化的原生排序输出；在此之前持续同时发布 API 顺序与离线分数顺序。\n5. 扩展 Lenovo 查询数、统一实际并发，并分层记录 embedding、vector search、rerank、serialization 与 queue span。\n6. 对 Unsupported Claim Rate 高的样本做人审切片，优先检查引用约束、拒答阈值和检索上下文裁剪。"),
        markdown("questions", "## 9. Further Questions\n\n- MOI/Dify 的受控流式长尾分别落在哪个可观测内部阶段？\n- MOI Recall@1 较高但 Top-K 增益很小，主要来自候选召回、去重还是返回条数限制？\n- FastGPT 检索领先但答案词面/Judge 未同步领先，瓶颈位于上下文拼装还是生成提示？\n- MaxKB 离线重排改善能否由平台原生配置稳定复现？\n- 30 条拒答题与 98 条多文档题是否需要单独发布平台切片，避免总体均值掩盖行为差异？"),
    ]

    generated_at = datetime.now(timezone.utc).isoformat()
    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": title,
            "description": "MOI RAG Benchmark v0.3.1 数据集、最新四平台 API 数据面容量、检索性能与质量、原生端到端 RAG 效果最终技术报告。",
            "generatedAt": generated_at,
            "blocks": blocks,
            "cards": cards,
            "charts": charts,
            "tables": tables,
            "sources": sources,
        },
        "snapshot": {"version": 1, "generatedAt": generated_at, "status": "ready", "datasets": datasets},
        "sources": sources,
    }

    serialized = json.dumps(artifact, ensure_ascii=False)
    assert "/Users/" not in serialized
    assert "DEEPSEEK_API_KEY" not in serialized
    assert len(datasets["minimal_capacity"]) == len(datasets["event_capacity"]) == 12
    assert len(datasets["quality_summary"]) == 4
    expected_platforms = {"MOI", "Dify", "FastGPT", "MaxKB"}
    assert {row["platform"] for row in datasets["minimal_capacity"]} == expected_platforms
    assert {row["platform"] for row in datasets["event_capacity"]} == expected_platforms
    assert {row["platform"] for row in datasets["lenovo_summary"]} == expected_platforms
    assert {row["platform"].rstrip("*") for row in datasets["quality_summary"]} == expected_platforms
    assert {row["connections"] for row in datasets["minimal_capacity"]} == {1, 4, 8}
    assert {row["connections"] for row in datasets["event_capacity"]} == {1, 4, 8}
    assert all(row["success_rate"] == 1 for row in datasets["minimal_capacity"] + datasets["event_capacity"])
    assert all(row["retrieval_success"] == 275 for row in completion_rows)
    assert round(metrics_by_platform["fastgpt"]["recall_at_10"], 6) == 0.856604
    assert round(metrics_by_platform["maxkb"]["recall_at_10"], 6) == 0.862264

    chart_map = {
        "report": "moi-rag-benchmark-report-v4.0-final",
        "charts": [
            {
                "chart_id": chart["id"],
                "question": chart.get("question"),
                "dataset": chart["dataset"],
                "source_id": chart.get("sourceId"),
                "interpretation": chart.get("subtitle", ""),
            }
            for chart in charts
        ],
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_OUT.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    CHART_MAP_OUT.write_text(json.dumps(chart_map, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(ARTIFACT_OUT)


if __name__ == "__main__":
    main()
