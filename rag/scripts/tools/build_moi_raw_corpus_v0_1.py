#!/usr/bin/env python3
"""Materialize the local source corpus for the MOI RAG benchmark v0.1.

The unified benchmark stores normalized document content, while this builder
walks back to local source assets and copies them into a new, self-contained
folder. Source assets, ready-for-eval text, and provenance metadata are kept
in three disjoint top-level directories. A text-projection competitor-eval
package is emitted entirely under ``ready_for_eval/`` so the existing local
QA runner can be preflighted without network access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
BENCH_ROOT = ROOT / "datasets" / "moi-rag-bench-v0.1"
READY_ROOT = ROOT / ".local-services" / "competitor-eval-ready" / "v1"
DOCBENCH_PDF_ROOT = ROOT / "datasets" / "downloads" / "document-rag" / "docbench" / "data"
ENTERPRISE_ROOT = (
    ROOT
    / "datasets"
    / "downloads"
    / "prepared"
    / "moi-ragbench-20260805-full-enterprise"
    / "enterprise-rag-bench"
    / "corpus"
)
MULTIHOP_ROOT = ROOT / "datasets" / "downloads" / "prepared" / "multihop-rag-dify" / "upload-to-dify"
MULTIHOP_PARSED_ROOT = PARSED_ROOT = ROOT / "outputs" / "parsed-documents" / "moi-ready-v1" / "datasets" / "multihop-rag" / "documents"
MMDOCIR_PAGE_ROOT = ROOT / "datasets" / "downloads" / "document-rag" / "mmdocir" / "data" / "extracted" / "page_images"
DEFAULT_OUTPUT = ROOT / "datasets" / "moi-rag-bench-v0.1-raw-corpus"


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} is not a JSON object")
            rows.append(value)
    return rows


def json_dump(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def repo_relative(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return f"external_snapshot_reference:{path.name}"


def resolve_path(value: Any) -> Optional[Path]:
    if value in (None, ""):
        return None
    candidate = Path(str(value)).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    candidate = candidate.resolve()
    return candidate if candidate.exists() and candidate.is_file() else None


def copy_asset(source: Path, target: Path, output: Path) -> Dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return {
        "path": str(target),
        "relative_path": str(target.relative_to(output)),
        "source_path": repo_relative(source),
        "bytes": target.stat().st_size,
        "sha256": sha256_file(target),
    }


def load_maps() -> Dict[str, Any]:
    """Load the original source indexes needed to resolve selected rows."""

    docbench_corpus = read_jsonl(READY_ROOT / "docbench" / "controlled-parsed-text" / "corpus.jsonl")
    docbench_by_id = {str(row["doc_id"]): row for row in docbench_corpus}
    docbench_pdf_by_stem: Dict[str, Path] = {}
    for path in DOCBENCH_PDF_ROOT.rglob("*.pdf"):
        docbench_pdf_by_stem.setdefault(path.stem, path)

    enterprise_corpus = read_jsonl(READY_ROOT / "enterprise-rag-bench" / "corpus.jsonl")
    enterprise_by_id = {str(row["doc_id"]): row for row in enterprise_corpus}

    multihop_corpus = read_jsonl(READY_ROOT / "multihop-rag" / "corpus.jsonl")
    multihop_by_id = {str(row["doc_id"]): row for row in multihop_corpus}

    mmdocir_pages = read_jsonl(READY_ROOT / "mmdocir" / "page" / "corpus.jsonl")
    mmdocir_by_scope: MutableMapping[str, List[Dict[str, Any]]] = defaultdict(list)
    for page in mmdocir_pages:
        scope = str(page.get("scope_id") or (page.get("metadata") or {}).get("scope_id") or "")
        if scope:
            mmdocir_by_scope[scope].append(page)
    for scope in mmdocir_by_scope:
        mmdocir_by_scope[scope].sort(
            key=lambda row: ((row.get("metadata") or {}).get("page_number", 0), str(row.get("doc_id")))
        )

    return {
        "docbench_by_id": docbench_by_id,
        "docbench_pdf_by_stem": docbench_pdf_by_stem,
        "enterprise_by_id": enterprise_by_id,
        "multihop_by_id": multihop_by_id,
        "mmdocir_by_scope": mmdocir_by_scope,
    }


def resolve_docbench_pdf(source_id: str, maps: Mapping[str, Any]) -> Optional[Path]:
    row = maps["docbench_by_id"].get(source_id, {})
    metadata = row.get("metadata") or {}
    parsed_manifest = metadata.get("parsed_manifest_row") or {}
    for value in (
        metadata.get("parsed_source_pdf_path"),
        parsed_manifest.get("source_path"),
    ):
        path = resolve_path(value)
        if path is not None:
            return path
    stem = source_id.split(":")[-1]
    return maps["docbench_pdf_by_stem"].get(stem)


def resolve_multihop_md(source_id: str, maps: Mapping[str, Any]) -> Tuple[Optional[Path], str]:
    row = maps["multihop_by_id"].get(source_id, {})
    original = resolve_path(row.get("text_path"))
    if original is not None:
        return original, "original_md"
    metadata = row.get("metadata") or {}
    prepared = MULTIHOP_ROOT / Path(source_id).name
    if prepared.exists():
        return prepared, "original_md"
    official_index = metadata.get("official_index")
    if official_index is not None:
        parsed = MULTIHOP_PARSED_ROOT / f"article-{int(official_index):04d}" / "payload" / "plain-text.txt"
        if parsed.exists():
            return parsed, "moi_ready_plain_text_fallback"
    return None, "unresolved"


def source_asset_plan(dataset: str, source_id: str, maps: Mapping[str, Any]) -> Dict[str, Any]:
    if dataset == "docbench":
        source = resolve_docbench_pdf(source_id, maps)
        return {"mode": "original_pdf" if source else "benchmark_text_fallback", "source": source, "assets": []}
    if dataset == "enterprise":
        row = maps["enterprise_by_id"].get(source_id, {})
        source = resolve_path(row.get("text_path"))
        return {"mode": "original_md" if source else "benchmark_text_fallback", "source": source, "assets": []}
    if dataset == "multihop":
        source, mode = resolve_multihop_md(source_id, maps)
        return {"mode": mode, "source": source, "assets": []}
    if dataset == "mmdocir":
        pages = maps["mmdocir_by_scope"].get(source_id, [])
        assets: List[Dict[str, Any]] = []
        missing = 0
        for page in pages:
            metadata = page.get("metadata") or {}
            source_path = resolve_path(metadata.get("source_binary_path") or page.get("binary_path"))
            if source_path is None:
                missing += 1
                continue
            page_number = int(metadata.get("page_number", 0) or 0)
            assets.append({"source": source_path, "page_number": page_number})
        return {
            "mode": "original_page_images",
            "source": None,
            "assets": assets,
            "missing_page_assets": missing,
            "declared_page_count": len(pages),
        }
    raise ValueError(f"Unknown dataset: {dataset}")


def make_question_rows(questions: Sequence[Mapping[str, Any]], gold_by_id: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for question in questions:
        qid = str(question["question_id"])
        gold = gold_by_id.get(qid, {})
        gold_docs = list(question.get("gold_doc_ids") or gold.get("gold_doc_ids") or [])
        evidence = question.get("gold_evidence") or gold.get("gold_evidence") or []
        answer = question.get("reference_answer") or gold.get("reference_answer") or ""
        rows.append(
            {
                "question_id": qid,
                "question": question.get("question", ""),
                "answer": answer,
                "reference_answer": answer,
                "answerable": question.get("answerable", True),
                "document_ids": [],
                "scope_doc_ids": [],
                "gold_doc_ids": gold_docs,
                "gold_evidence": evidence,
                "gold": {"document_ids": gold_docs, "evidence": evidence},
                "source_dataset": question.get("source_dataset"),
                "source_question_id": question.get("source_question_id"),
                "question_type": question.get("question_type"),
                "metadata": question.get("metadata") or {},
            }
        )
    return rows


def build(output: Path, dry_run: bool = False, overwrite: bool = False) -> Dict[str, Any]:
    if output.exists() and not overwrite and not dry_run:
        raise RuntimeError(f"Output already exists; pass --overwrite for this exact target: {output}")
    if output.exists() and overwrite and not dry_run:
        shutil.rmtree(output)

    documents = read_jsonl(BENCH_ROOT / "documents.jsonl")
    questions = read_jsonl(BENCH_ROOT / "questions.jsonl")
    gold = read_jsonl(BENCH_ROOT / "gold.jsonl")
    if (len(documents), len(questions), len(gold)) != (500, 1000, 1000):
        raise RuntimeError("The source v0.1 benchmark must contain 500 documents and 1000 QA before materialization")

    maps = load_maps()
    gold_by_id = {str(row["question_id"]): row for row in gold}
    qa_counts = Counter()
    for question in questions:
        for doc_id in question.get("gold_doc_ids") or []:
            qa_counts[doc_id] += 1
    plans: List[Dict[str, Any]] = []
    total_source_bytes = 0
    original_file_count = 0
    fallback_count = 0
    missing_page_asset_count = 0
    for document in documents:
        dataset = str(document["source_dataset"])
        source_id = str(document["source_document_id"])
        plan = source_asset_plan(dataset, source_id, maps)
        plan.update({"document": document, "dataset": dataset, "source_id": source_id})
        plans.append(plan)
        if plan.get("source"):
            original_file_count += 1
            total_source_bytes += plan["source"].stat().st_size
        elif dataset != "mmdocir":
            fallback_count += 1
        missing_page_asset_count += int(plan.get("missing_page_assets", 0) or 0)
        total_source_bytes += sum(asset["source"].stat().st_size for asset in plan.get("assets", []))

    summary = {
        "documents": len(documents),
        "questions": len(questions),
        "gold": len(gold),
        "original_file_count": original_file_count,
        "fallback_document_count": fallback_count,
        "mmdocir_page_asset_count": sum(len(plan.get("assets", [])) for plan in plans),
        "mmdocir_missing_page_asset_count": missing_page_asset_count,
        "estimated_source_bytes": total_source_bytes,
        "estimated_source_mib": round(total_source_bytes / 1024 / 1024, 2),
    }
    if dry_run:
        print(json.dumps({"status": "DRY_RUN", "output": str(output), "summary": summary}, ensure_ascii=False))
        return summary

    output.mkdir(parents=True, exist_ok=True)
    source_root = output / "source_files"
    ready_root = output / "ready_for_eval"
    ready_documents_root = ready_root / "documents"
    provenance_root = output / "provenance"
    source_root.mkdir()
    ready_root.mkdir()
    ready_documents_root.mkdir()
    provenance_root.mkdir()

    raw_rows: List[Dict[str, Any]] = []
    corpus_rows: List[Dict[str, Any]] = []
    for ordinal, plan in enumerate(plans, start=1):
        document = plan["document"]
        dataset = plan["dataset"]
        source_id = plan["source_id"]
        global_id = str(document["doc_id"])
        source_dir = source_root / dataset / global_id
        source_dir.mkdir(parents=True, exist_ok=True)
        content = str(document.get("content") or "")
        ready_text_path = ready_documents_root / f"{global_id}.md"
        # Preserve the benchmark content byte-for-byte so content_sha256 is
        # also the hash of the text projection used by the QA package.
        ready_text_path.write_text(content, encoding="utf-8")
        copied_assets: List[Dict[str, Any]] = []
        missing_sources: List[str] = []
        if plan.get("source") is not None:
            suffix = plan["source"].suffix.lower()
            filename = "original.pdf" if suffix == ".pdf" else "original.md"
            target = source_dir / filename
            copied_assets.append(copy_asset(plan["source"], target, output))
        elif dataset != "mmdocir":
            missing_sources.append("original_source_file_unresolved")

        for asset_index, asset in enumerate(plan.get("assets", []), start=1):
            source = asset["source"]
            page_number = int(asset.get("page_number", asset_index) or asset_index)
            target = source_dir / "pages" / f"{page_number:04d}_{source.name}"
            copied_assets.append(copy_asset(source, target, output))
        if dataset == "mmdocir":
            if plan.get("missing_page_assets"):
                missing_sources.append(f"missing_page_assets:{plan['missing_page_assets']}")

        copied_bytes = sum(int(asset["bytes"]) for asset in copied_assets)
        ready_text_mode = (
            "reconstructed_from_mmdocir_page_text"
            if dataset == "mmdocir"
            else "benchmark_parsed_text_projection"
        )
        source_asset_available = bool(plan.get("source") or plan.get("assets"))
        raw_rows.append(
            {
                "doc_id": global_id,
                "source_dataset": dataset,
                "source_document_id": source_id,
                "title": document.get("title"),
                "source_directory": str(source_dir.relative_to(output)),
                "ready_text_path": str(ready_text_path.relative_to(output)),
                "source_mode": plan["mode"],
                "ready_text_mode": ready_text_mode,
                "source_asset_available": source_asset_available,
                "native_document_available": bool(plan.get("source")),
                "copied_assets": [
                    {
                        **asset,
                        "path": str(Path(asset["path"]).relative_to(output)),
                    }
                    for asset in copied_assets
                ],
                "missing_sources": missing_sources,
                "source_asset_count": len(copied_assets),
                "source_asset_bytes": copied_bytes,
                "content_sha256": sha256_text(content),
                "source_document_sha256": document.get("source_sha256"),
                "linked_question_count": qa_counts[global_id],
                "source_metadata": document.get("source_metadata") or {},
            }
        )
        corpus_rows.append(
            {
                "doc_id": global_id,
                "scope_id": "moi-rag-bench-v0.1-global",
                "ingest_role": "source_document",
                "text_path": str(Path("documents") / f"{global_id}.md"),
                "media_type": "text/markdown",
                "title": document.get("title") or global_id,
                "sha256": sha256_text(content),
                "metadata": {
                    "source_dataset": dataset,
                    "source_document_id": source_id,
                    "source_mode": plan["mode"],
                    "provenance_id": global_id,
                },
            }
        )

    question_rows = make_question_rows(questions, gold_by_id)
    gold_rows = []
    for row in gold:
        gold_rows.append(
            {
                **row,
                "gold": {
                    "document_ids": list(row.get("gold_doc_ids") or []),
                    "evidence": row.get("gold_evidence") or [],
                },
            }
        )

    write_jsonl(provenance_root / "documents.jsonl", raw_rows)
    write_jsonl(ready_root / "corpus.jsonl", corpus_rows)
    write_jsonl(ready_root / "questions.jsonl", question_rows)
    write_jsonl(ready_root / "gold.jsonl", gold_rows)

    package_manifest = {
        "schema": "competitor-eval-ready-v1",
        "schema_version": "competitor-eval-ready-v1",
        "package_schema": "competitor-eval-ready-v1",
        "dataset_id": "moi-rag-bench-v0.1-ready-for-eval",
        "dataset_name": "MOI RAG Benchmark v0.1 ready-for-eval text projection",
        "dataset_revision": "local-raw-v0.1",
        "revision": "local-raw-v0.1",
        "split": "evaluation",
        "scope": "global",
        "scope_policy": "global",
        "condition": "ready-for-eval-text-projection",
        "protocol_tag": "MOI_RAG_BENCH_V0_1_RAW_CORPUS_TEXT_PROJECTION",
        "status": "READY_LOCAL_RAW_CORPUS",
        "readiness_status": "READY",
        "source_complete": False,
        "ingest_representation": "source_document",
        "evaluation": {"scope": "global", "ingest_representation": "source_document"},
        "documents": "corpus.jsonl",
        "corpus_path": "corpus.jsonl",
        "questions": "questions.jsonl",
        "questions_path": "questions.jsonl",
        "gold": "gold.jsonl",
        "gold_path": "gold.jsonl",
        "counts": {
            "documents": 500,
            "corpus_rows": 500,
            "runner_documents": 500,
            "questions": 1000,
            "question_rows": 1000,
            "gold_rows": 1000,
        },
        "conditions": {
            "raw_asset_copy": True,
            "text_projection": True,
            "native_pdf_ingest": False,
            "mmdocir_page_images_copied": True,
            "source_ready_separated": True,
            "note": "All ingest paths stay inside ready_for_eval/documents. Source assets are isolated under ../source_files and are not package inputs.",
        },
        "platform_support": {
            "dify_local": {"text": True, "native_qa": True, "multimodal": False},
            "fastgpt_local": {"text": True, "native_qa": True, "multimodal": False},
            "maxkb_local": {"text": True, "native_qa": True, "multimodal": False},
            "moi_local": {"text": True, "native_qa": True, "multimodal": False},
        },
        "source_manifest": "../provenance/documents.jsonl",
        "benchmark_manifest": "../../moi-rag-bench-v0.1/manifest.json",
    }
    json_dump(ready_root / "manifest.json", package_manifest)

    qa_plan = {
        "schema": "moi-rag-bench-qa-eval-plan-v0.1",
        "package_manifest": "ready_for_eval/manifest.json",
        "scope": "global",
        "question_count": 1000,
        "document_count": 500,
        "default_ingest_projection": "ready_for_eval/documents",
        "preflight": {
            "network": False,
            "command": "python3 local-rag-platforms/scripts/evaluation/competitor_eval_runner.py preflight --system fastgpt_local --package datasets/moi-rag-bench-v0.1-raw-corpus/ready_for_eval/manifest.json --dry-run",
        },
        "next_stages": [
            "preflight",
            "ingest",
            "retrieval",
            "qa",
        ],
        "native_asset_note": "The package uses only ready_for_eval/documents for the first uniform text-only pass. Native PDF and MMDocIR page-image ingestion requires a separate platform protocol rooted at source_files/.",
    }
    json_dump(output / "qa-eval-plan.json", qa_plan)

    raw_manifest = {
        "schema": "moi-rag-bench-raw-corpus-v0.1",
        "version": "0.1.1",
        "layout_revision": "source-ready-separated-v1",
        "generated_at": "2026-08-19",
        "source_benchmark": "datasets/moi-rag-bench-v0.1",
        "grain": "one source-document directory per selected unified document",
        "counts": summary,
        "source_modes": dict(Counter(row["source_mode"] for row in raw_rows)),
        "source_datasets": dict(Counter(row["source_dataset"] for row in raw_rows)),
        "qa_question_types": dict(Counter(row["question_type"] for row in question_rows)),
        "qa_question_sources": dict(Counter(row["source_dataset"] for row in question_rows)),
        "separation_contract": {
            "source_files_root": "source_files/",
            "ready_for_eval_root": "ready_for_eval/",
            "provenance_root": "provenance/",
            "source_files_contain_generated_eval_text": False,
            "ready_for_eval_contains_original_source_assets": False,
            "ready_ingest_paths_must_remain_under": "ready_for_eval/documents/",
            "only_bridge": "provenance/documents.jsonl",
        },
        "artifact_layout": {
            "source_files": "source_files/<source_dataset>/<doc_id>/",
            "ready_documents": "ready_for_eval/documents/<doc_id>.md",
            "ready_package": "ready_for_eval/manifest.json",
            "provenance": "provenance/documents.jsonl",
        },
        "quality_notes": [
            "DocBench source PDFs are copied when the local parsed manifest or local data index resolves them.",
            "EnterpriseRAG-Bench and MultiHop-RAG local source Markdown files stay under source_files and are never used as generated eval outputs.",
            "MMDocIR currently exposes local page JPG assets rather than expanded source PDFs; those JPG files stay under source_files while reconstructed text stays under ready_for_eval.",
            "The QA package uses only ready_for_eval/documents for a uniform first text-only run; native/source-asset ingestion remains a separate protocol.",
        ],
        "qa_package": {
            "manifest": "ready_for_eval/manifest.json",
            "plan": "qa-eval-plan.json",
            "recommended_first_command": "python3 local-rag-platforms/scripts/evaluation/competitor_eval_runner.py preflight --system fastgpt_local --package datasets/moi-rag-bench-v0.1-raw-corpus/ready_for_eval/manifest.json --dry-run",
        },
    }
    json_dump(output / "manifest.json", raw_manifest)

    readme = f"""# MOI RAG Benchmark v0.1 分离语料库（初版）

本目录由 `datasets/moi-rag-bench-v0.1` 生成。原始源文件、ready-for-eval 解析结果和二者之间的 provenance 映射位于三个互斥目录中。

## 数量

- 文档：{summary['documents']}
- QA：{summary['questions']}
- 原始文件直接复制：{summary['original_file_count']} 个文档
- unresolved source 文档：{summary['fallback_document_count']} 个（均在 `provenance/documents.jsonl` 中标明）
- MMDocIR 页面 JPG：{summary['mmdocir_page_asset_count']} 张
- 预计原始资产体积：{summary['estimated_source_mib']} MiB

## 目录

- `source_files/`：只保存原始 PDF、本地源 MD 和 MMDocIR 页面 JPG；不保存任何评测解析文本。
- `ready_for_eval/`：只保存 500 份评测 Markdown、corpus/questions/gold 和 runner manifest；不保存原始 PDF/JPG。
- `provenance/documents.jsonl`：唯一桥接层，记录 source asset、ready text、hash 和 QA 关联。
- `ready_for_eval/manifest.json`：兼容 `competitor_eval_runner.py` 的 text-only package。
- `qa-eval-plan.json`：QA 测评阶段和首个无网络 preflight 命令。

## QA 测评准备

先做不联网的 package preflight：

```bash
python3 local-rag-platforms/scripts/evaluation/competitor_eval_runner.py preflight \\
  --system fastgpt_local \\
  --package datasets/moi-rag-bench-v0.1-raw-corpus/ready_for_eval/manifest.json \\
  --dry-run
```

通过 preflight 后，再按实际要测的平台执行 `ingest`、`retrieval` 或 `qa`。当前 package 的所有 ingest 路径均限制在 `ready_for_eval/documents/`；原始 PDF、MD 和 MMDocIR 页面图片仅位于 `source_files/`。

## 重要 provenance 说明

MMDocIR 本地数据当前可直接取得的是 page JPG 和 page-level parsed content，未把压缩包中的 PDF 全量展开。页面 JPG 只位于 `source_files/mmdocir/`，由页面文本重构的 Markdown 只位于 `ready_for_eval/documents/`。
"""
    (output / "README.md").write_text(readme, encoding="utf-8")
    print(json.dumps({"status": "BUILT", "output": str(output), "summary": summary}, ensure_ascii=False))
    return summary


def check_output(output: Path) -> None:
    source_root = output / "source_files"
    ready_root = output / "ready_for_eval"
    provenance_root = output / "provenance"
    documents = read_jsonl(provenance_root / "documents.jsonl")
    corpus = read_jsonl(ready_root / "corpus.jsonl")
    questions = read_jsonl(ready_root / "questions.jsonl")
    gold = read_jsonl(ready_root / "gold.jsonl")
    if (len(documents), len(corpus), len(questions), len(gold)) != (500, 500, 1000, 1000):
        raise RuntimeError(
            f"Unexpected raw corpus counts: documents={len(documents)} corpus={len(corpus)} questions={len(questions)} gold={len(gold)}"
        )
    doc_ids = {row["doc_id"] for row in documents}
    question_ids = {row["question_id"] for row in questions}
    gold_ids = {row["question_id"] for row in gold}
    if len(doc_ids) != 500 or len(question_ids) != 1000 or gold_ids != question_ids:
        raise RuntimeError("Raw corpus ID uniqueness/alignment check failed")
    outside = [
        row["question_id"]
        for row in questions
        if any(doc_id not in doc_ids for doc_id in row.get("gold_doc_ids", []))
    ]
    if outside:
        raise RuntimeError(f"Gold links outside raw corpus: {outside[:5]}")
    allowed_source_suffixes = {
        "docbench": {".pdf"},
        "enterprise": {".md"},
        "multihop": {".md"},
        "mmdocir": {".jpg", ".jpeg", ".png"},
    }
    for row in documents:
        ready_path = output / row["ready_text_path"]
        try:
            ready_path.resolve().relative_to(ready_root.resolve())
        except ValueError as exc:
            raise RuntimeError(f"Ready text escapes ready_for_eval: {ready_path}") from exc
        if not ready_path.exists() or not ready_path.read_text(encoding="utf-8").strip():
            raise RuntimeError(f"Missing or empty ready text: {ready_path}")
        if sha256_text(ready_path.read_text(encoding="utf-8")) != row["content_sha256"]:
            raise RuntimeError(f"Ready text hash does not match benchmark content hash: {ready_path}")
        for asset in row.get("copied_assets", []):
            path = output / asset["path"]
            try:
                path.resolve().relative_to(source_root.resolve())
            except ValueError as exc:
                raise RuntimeError(f"Source asset escapes source_files: {path}") from exc
            if not path.exists():
                raise RuntimeError(f"Missing copied asset: {path}")
            if path.suffix.lower() not in allowed_source_suffixes[row["source_dataset"]]:
                raise RuntimeError(f"Unexpected source asset type for {row['source_dataset']}: {path}")
            if path.stat().st_size != int(asset["bytes"]):
                raise RuntimeError(f"Copied asset size changed: {path}")
            if sha256_file(path) != asset["sha256"]:
                raise RuntimeError(f"Copied asset hash changed: {path}")
    ready_documents = list((ready_root / "documents").glob("*.md"))
    if len(ready_documents) != 500:
        raise RuntimeError(f"ready_for_eval/documents must contain 500 Markdown files, got {len(ready_documents)}")
    forbidden_ready_assets = [
        path for path in ready_root.rglob("*") if path.is_file() and path.suffix.lower() in {".pdf", ".jpg", ".jpeg", ".png"}
    ]
    if forbidden_ready_assets:
        raise RuntimeError(f"Original assets leaked into ready_for_eval: {forbidden_ready_assets[:5]}")
    forbidden_source_names = [
        path
        for path in source_root.rglob("*")
        if path.is_file() and any(marker in path.name.casefold() for marker in ("reconstructed", "eval_text", "ready_for_eval"))
    ]
    if forbidden_source_names:
        raise RuntimeError(f"Generated eval text leaked into source_files: {forbidden_source_names[:5]}")
    old_mixed_roots = [output / name for name in ("corpus", "eval_text", "qa-package") if (output / name).exists()]
    if old_mixed_roots:
        raise RuntimeError(f"Legacy mixed-layout roots still exist: {old_mixed_roots}")
    package_root = ready_root
    package = json.loads((package_root / "manifest.json").read_text(encoding="utf-8"))
    for key in ("documents", "questions", "gold"):
        package_path = package_root / package[key]
        try:
            package_path.resolve().relative_to(ready_root.resolve())
        except ValueError as exc:
            raise RuntimeError(f"QA package artifact escapes ready_for_eval: {key}={package[key]}") from exc
        if not package_path.exists():
            raise RuntimeError(f"QA package artifact missing: {key}")
    for row in corpus:
        ingest_path = (ready_root / row["text_path"]).resolve()
        try:
            ingest_path.relative_to((ready_root / "documents").resolve())
        except ValueError as exc:
            raise RuntimeError(f"Corpus ingest path escapes ready_for_eval/documents: {row['text_path']}") from exc
    print(
        json.dumps(
            {
                "status": "OK",
                "documents": 500,
                "questions": 1000,
                "gold": 1000,
                "source_ready_separated": True,
                "source_assets": sum(row["source_asset_count"] for row in documents),
                "ready_documents": len(ready_documents),
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    if args.check_only:
        check_output(output)
    else:
        build(output, dry_run=args.dry_run, overwrite=args.overwrite)


if __name__ == "__main__":
    main()
