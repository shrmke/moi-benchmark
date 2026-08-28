#!/usr/bin/env python3
"""Build and validate the derived pure-text MOI RAG condition.

The parent ``ready_for_eval`` package is treated as immutable.  This builder
copies its 500 Markdown documents into a separate package, removes the known
visual-gap questions, and fills those slots with unused local source QA
whose linked documents are already in the same 500-document set.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import html
import json
import re
import shutil
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PARENT = ROOT / "datasets" / "moi-rag-bench-v0.1-raw-corpus" / "ready_for_eval"
DEFAULT_OUTPUT = ROOT / "datasets" / "moi-rag-bench-v0.1-raw-corpus" / "text_only_ready_for_eval"
CONDITION = "text-only-no-mllm"
IMAGE_LLM = "NOT_APPLICABLE"
SELECTION_SEED = "moi-rag-bench-v0.1:text-only-no-mllm:20260820"

SOURCE_ROOTS = {
    "docbench": ROOT / ".local-services" / "competitor-eval-ready" / "v1" / "docbench" / "controlled-parsed-text",
    "enterprise": ROOT / ".local-services" / "competitor-eval-ready" / "v1" / "enterprise-rag-bench",
    "multihop": ROOT / ".local-services" / "competitor-eval-ready" / "v1" / "multihop-rag",
    "mmdocir": ROOT / ".local-services" / "competitor-eval-ready" / "v1" / "mmdocir" / "page",
}

SOURCE_QUOTAS = {
    "docbench": 450,
    "enterprise": 150,
    "multihop": 250,
    "mmdocir": 150,
}

EXCLUDED_QUESTION_IDS = frozenset(
    {
        "moi500d_qa_docbench_03833189506a",
        "moi500d_qa_docbench_13b28646165c",
        "moi500d_qa_docbench_1798dc75f934",
        "moi500d_qa_docbench_2cb6c35273f6",
        "moi500d_qa_docbench_307cd8975679",
        "moi500d_qa_docbench_33b222900d60",
        "moi500d_qa_docbench_6ee69e551d74",
        "moi500d_qa_docbench_8042559cabfc",
        "moi500d_qa_docbench_c1ed84fac277",
        "moi500d_qa_docbench_c94ef8f381a6",
        "moi500d_qa_docbench_e831a88fb280",
        "moi500d_qa_docbench_ffd377e70c21",
        "moi500d_qa_docbench_f584df39ce05",
        "moi500d_qa_mmdocir_028344f1ff69",
        "moi500d_qa_mmdocir_0b0655d8105f",
        "moi500d_qa_mmdocir_351d93c335c6",
        "moi500d_qa_mmdocir_4680b0da4694",
        "moi500d_qa_mmdocir_4708cefb7c62",
        "moi500d_qa_mmdocir_49cc77ee585f",
        "moi500d_qa_mmdocir_6821935ef19c",
        "moi500d_qa_mmdocir_6d9b91e94456",
        "moi500d_qa_mmdocir_ba77165231ad",
        "moi500d_qa_mmdocir_d1f295424406",
        "moi500d_qa_mmdocir_d6bcfff4b36b",
        "moi500d_qa_mmdocir_fc1ed1865075",
        # Independent semantic review found these retained rows still require
        # pixel colour, position, or layout interpretation even though their
        # answer strings also occur somewhere in the parsed Markdown.
        "moi500d_qa_docbench_ac894f2653d5",
        "moi500d_qa_mmdocir_05dc922c6b20",
        "moi500d_qa_mmdocir_50203698a731",
        "moi500d_qa_mmdocir_843445a9ac75",
        "moi500d_qa_mmdocir_8b40cde96436",
        "moi500d_qa_mmdocir_8b5ab98961b2",
        "moi500d_qa_mmdocir_98d44bce8409",
        "moi500d_qa_mmdocir_c951a69042d8",
        "moi500d_qa_mmdocir_cc2f6a357b19",
        "moi500d_qa_mmdocir_f43a1188def0",
        # Semantic MLLM audit of the retained DocBench rows: these either
        # depend on an un-serialized visual comparison or have an answer/text
        # projection integrity gap.  Replace them with unused text-safe rows.
        "moi500d_qa_docbench_0ac6b215da1c",
        "moi500d_qa_docbench_815d28d96b1d",
        "moi500d_qa_docbench_a6a5e07f8217",
        "moi500d_qa_docbench_e579b4406d5c",
    }
)

EXCLUSION_REASON_OVERRIDES: dict[str, dict[str, str]] = {
    "moi500d_qa_docbench_0ac6b215da1c": {
        "classification": "semantic_visual_gap",
        "reason": "The answer is the largest pie-chart segment and its 61% value; the linked Markdown has no answer-bearing textual statement for that segment.",
    },
    "moi500d_qa_docbench_815d28d96b1d": {
        "classification": "semantic_visual_gap",
        "reason": "The DAE-versus-VAE style-cluster comparison is only represented by the t-SNE plot; the Markdown does not state which model has clearer separation.",
    },
    "moi500d_qa_docbench_a6a5e07f8217": {
        "classification": "text_projection_gap",
        "reason": "The gold answer 25,523 is absent from the linked Markdown, whose debt table contains 22,902 and 6,295 instead.",
    },
    "moi500d_qa_docbench_e579b4406d5c": {
        "classification": "text_projection_gap",
        "reason": "The parsed Markdown loses the minus sign on the informal formality score, so the exact gold comparison is not reproducible from eval text.",
    },
}

# These markers are deliberately narrower than a generic visual-word scan.
# A table body or an explicitly quoted evidence passage is text-bearing; a
# question asking for a colour, count of people, or spatial/figure content is
# not a safe replacement candidate unless it has an explicit textual answer.
VISUAL_MARKER_RE = re.compile(
    r"(?:\b(?:color|colour|yellow|red|green|blue|shape|chart|figure|plot|diagram|map|image|photo|picture|visual|graph|bar|pie|doughnut|axis|axes|slide|screen|icon|logo)\b"
    r"|\b(?:left|right|top|bottom|above|below|next\s+to)\b"
    r"|\b(?:highlighted|spatial|layout)\b"
    r"|\bhow\s+many\s+(?:people|persons|men|women|soldiers|cars|figures)\b"
    r"|\b(?:number|count)\s+of\s+(?:people|persons|men|women|soldiers|cars|figures)\b)",
    re.IGNORECASE,
)

DROP_KEYS = {
    "binary_path",
    "image",
    "image_path",
    "image_paths",
    "images",
    "layout_mapping",
    "parsed_manifest_path",
    "parsed_source_pdf_path",
    "source_binary_path",
    "source_path",
    "source_qa_path",
    "text_path",
}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Expected JSON object at {path}:{line_number}")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def package_fingerprint(root: Path) -> str:
    """Hash a package by sorted relative file names and their content hashes."""

    entries = []
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        entries.append(f"{path.relative_to(root).as_posix()}\0{sha256_file(path)}\n")
    return sha256_bytes("".join(entries).encode("utf-8"))


def validate_separated_roots(parent_root: Path, output: Path) -> None:
    """Reject package roots whose directory trees overlap in either direction."""

    parent_resolved = parent_root.resolve()
    output_resolved = output.resolve()
    try:
        output_resolved.relative_to(parent_resolved)
        output_inside_parent = True
    except ValueError:
        output_inside_parent = False
    try:
        parent_resolved.relative_to(output_resolved)
        parent_inside_output = True
    except ValueError:
        parent_inside_output = False
    if output_inside_parent or parent_inside_output:
        raise RuntimeError(
            "Parent and derived output package roots must be fully separated "
            "(neither may contain the other)"
        )


def stable_hash(*parts: Any) -> str:
    return sha256_bytes("|".join(str(part) for part in parts).encode("utf-8"))


def global_question_id(dataset: str, source_question_id: str) -> str:
    return f"moi500d_qa_{dataset}_{stable_hash(dataset, source_question_id)[:12]}"


def normalize_text(value: Any) -> str:
    text = html.unescape(unicodedata.normalize("NFKC", str(value or "")).casefold())
    text = text.replace("–", "-").replace("—", "-").replace("−", "-")
    return re.sub(r"[^a-z0-9]+", "", text)


def compact_value(value: Any, depth: int = 0) -> Any:
    """Keep portable metadata while removing source/asset paths."""

    if depth > 4:
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        result = [compact_value(item, depth + 1) for item in value[:100]]
        return [item for item in result if item is not None]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.casefold() in DROP_KEYS:
                continue
            compacted = compact_value(item, depth + 1)
            if compacted is not None:
                result[key_text] = compacted
        return result
    return str(value)


def sanitized_record(value: Mapping[str, Any]) -> dict[str, Any]:
    compacted = compact_value(dict(value))
    return compacted if isinstance(compacted, dict) else {}


def source_question_id(row: Mapping[str, Any]) -> str:
    value = row.get("question_id") or row.get("id")
    if not value:
        raise ValueError(f"Question has no id: {row}")
    return str(value)


def source_answer(question: Mapping[str, Any], gold: Mapping[str, Any]) -> Any:
    return (
        question.get("reference_answer")
        or gold.get("reference_answer")
        or question.get("answer")
        or gold.get("gold_answer")
    )


def source_refs(dataset: str, question: Mapping[str, Any], gold: Mapping[str, Any]) -> list[str]:
    if dataset == "mmdocir":
        values = question.get("scope_doc_ids") or gold.get("scope_doc_ids") or []
    else:
        values = question.get("gold_doc_ids") or gold.get("gold_doc_ids") or []
    if isinstance(values, str):
        values = [values]
    return list(dict.fromkeys(str(value) for value in values if value))


def question_domain(dataset: str, row: Mapping[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
    value = metadata.get("domain") or metadata.get("category")
    if value:
        return str(value)
    if dataset == "enterprise":
        values = metadata.get("source_types") or ["enterprise"]
        return "source:" + "/".join(str(item) for item in values)
    return dataset


def question_type(row: Mapping[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
    return str(row.get("question_type") or metadata.get("question_type") or metadata.get("original_type") or "unknown")


def question_media_is_visual(row: Mapping[str, Any]) -> bool:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
    evidence_type = str(metadata.get("evidence_type") or "")
    original_type = str(metadata.get("original_type") or row.get("question_type") or "")
    searchable = " ".join(
        (
            str(row.get("question") or ""),
            evidence_type,
            original_type,
        )
    )
    return bool(VISUAL_MARKER_RE.search(searchable)) or original_type.casefold() in {"multimodal-f", "multimodal"}


def answer_parts(answer: Any) -> list[str]:
    if isinstance(answer, (list, tuple)):
        return [str(item) for item in answer if str(item).strip()]
    text = str(answer or "").strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            parsed = None
        if isinstance(parsed, (list, tuple)):
            return [str(item) for item in parsed if str(item).strip()]
    return [text] if text else []


def evidence_parts(question: Mapping[str, Any], gold: Mapping[str, Any]) -> list[str]:
    value = question.get("gold_evidence") or gold.get("gold_evidence") or []
    if not isinstance(value, list):
        value = [value]
    result = []
    for item in value:
        if isinstance(item, Mapping):
            text = item.get("evidence") or item.get("quote") or ""
        else:
            text = item
        if str(text).strip():
            result.append(str(text))
    return result


def deterministic_reference_answer_fallback(
    question: Mapping[str, Any], gold: Mapping[str, Any]
) -> str:
    """Return a transparent answer repair for answerable evidence-only rows.

    A small number of upstream parsed rows mark a question as answerable and
    carry resolved Gold evidence, but leave both answer fields empty.  The
    derived text-only condition may use that evidence as its reference answer
    because it is already the frozen oracle; the repair is recorded in the
    output metadata and manifest so it cannot be mistaken for new annotation.
    """

    if not bool(question.get("answerable", True)):
        return ""
    existing = (
        question.get("reference_answer")
        or question.get("answer")
        or gold.get("reference_answer")
        or gold.get("gold_answer")
    )
    if str(existing or "").strip():
        return ""
    return "\n\n".join(evidence_parts(question, gold)).strip()


def normalized_projection(text: str) -> tuple[str, list[int]]:
    prepared = html.unescape(unicodedata.normalize("NFKC", text).casefold())
    prepared = prepared.replace("–", "-").replace("—", "-").replace("−", "-")
    characters: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(prepared):
        if ("a" <= character <= "z") or ("0" <= character <= "9"):
            characters.append(character)
            offsets.append(index)
    return "".join(characters), offsets


def find_excerpt(text: str, values: Sequence[str], maximum: int = 600) -> tuple[str, str]:
    """Return a local excerpt and whether it came from answer/evidence text."""

    for value in values:
        if not str(value).strip():
            continue
        exact_index = text.casefold().find(str(value).casefold())
        if exact_index >= 0:
            start = max(0, exact_index - 180)
            end = min(len(text), exact_index + len(str(value)) + 180)
            return text[start:end].strip()[:maximum], "exact"

        target = normalize_text(value)
        projected, offsets = normalized_projection(text)
        normalized_index = projected.find(target) if target else -1
        if normalized_index >= 0 and target:
            original_start = offsets[normalized_index]
            original_end = offsets[normalized_index + len(target) - 1] + 1
            start = max(0, original_start - 180)
            end = min(len(text), original_end + 180)
            return text[start:end].strip()[:maximum], "normalized"

        tokens = re.findall(r"[A-Za-z0-9]+", str(value))
        for token in sorted(tokens, key=len, reverse=True):
            if len(token) < 3:
                continue
            match = re.search(re.escape(token), text, re.IGNORECASE)
            if match:
                start = max(0, match.start() - 180)
                end = min(len(text), match.end() + 180)
                return text[start:end].strip()[:maximum], "token"
    return "", ""


def validate_answer_bearing_text(
    answer: Any,
    evidence: Sequence[str],
    linked_text: str,
) -> dict[str, Any]:
    normalized_document = normalize_text(linked_text)
    parts = [part for part in answer_parts(answer) if normalize_text(part)]
    normalized_parts = [normalize_text(part) for part in parts]
    answer_matches = [part for part, normalized in zip(parts, normalized_parts) if normalized in normalized_document]
    evidence_matches = [item for item in evidence if normalize_text(item) in normalized_document]
    answer_passed = bool(parts) and len(answer_matches) == len(parts)
    evidence_passed = bool(evidence_matches)
    if answer_passed:
        excerpt, match_mode = find_excerpt(linked_text, answer_matches)
        match_kind = "answer"
    elif evidence_passed:
        excerpt, match_mode = find_excerpt(linked_text, evidence_matches)
        match_kind = "explicit_evidence"
    else:
        excerpt, match_mode, match_kind = "", "", "none"
    return {
        "passed": answer_passed or evidence_passed,
        "answer_matches": answer_matches,
        "evidence_matches": evidence_matches,
        "matched_excerpt": excerpt,
        "match_kind": match_kind,
        "match_mode": match_mode,
    }


def aggregate_document_hash(document_hashes: Mapping[str, str]) -> str:
    if len(document_hashes) == 1:
        return next(iter(document_hashes.values()))
    payload = "".join(f"{key}\0{document_hashes[key]}\n" for key in sorted(document_hashes))
    return sha256_bytes(payload.encode("utf-8"))


def load_parent(parent: Path) -> dict[str, Any]:
    required = [parent / "manifest.json", parent / "corpus.jsonl", parent / "questions.jsonl", parent / "gold.jsonl"]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("Parent package is incomplete: " + ", ".join(missing))
    manifest = read_json(parent / "manifest.json")
    documents = read_jsonl(parent / "corpus.jsonl")
    questions = read_jsonl(parent / "questions.jsonl")
    gold = read_jsonl(parent / "gold.jsonl")
    if (len(documents), len(questions), len(gold)) != (500, 1000, 1000):
        raise RuntimeError(f"Unexpected parent counts: docs={len(documents)} questions={len(questions)} gold={len(gold)}")
    document_ids = [str(row.get("doc_id")) for row in documents]
    question_ids = [str(row.get("question_id")) for row in questions]
    gold_ids = [str(row.get("question_id")) for row in gold]
    if len(set(document_ids)) != 500 or len(set(question_ids)) != 1000 or set(question_ids) != set(gold_ids):
        raise RuntimeError("Parent IDs are not unique/aligned")
    docs_by_id = {str(row["doc_id"]): row for row in documents}
    text_by_doc: dict[str, str] = {}
    for row in documents:
        path = (parent / str(row.get("text_path"))).resolve()
        documents_root = (parent / "documents").resolve()
        try:
            path.relative_to(documents_root)
        except ValueError as exc:
            raise RuntimeError(f"Parent ingest path escapes documents: {row.get('text_path')}") from exc
        if path.suffix.casefold() != ".md" or not path.is_file():
            raise RuntimeError(f"Parent Markdown document missing: {path}")
        text = path.read_text(encoding="utf-8")
        if sha256_bytes(text.encode("utf-8")) != str(row.get("sha256")):
            raise RuntimeError(f"Parent document hash mismatch: {path}")
        text_by_doc[str(row["doc_id"])] = text
    return {
        "manifest": manifest,
        "documents": documents,
        "questions": questions,
        "gold": gold,
        "docs_by_id": docs_by_id,
        "text_by_doc": text_by_doc,
        "fingerprint": package_fingerprint(parent),
        "manifest_sha256": sha256_file(parent / "manifest.json"),
    }


def load_source_packages() -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = {}
    for dataset, root in SOURCE_ROOTS.items():
        if not root.is_dir():
            raise FileNotFoundError(f"Source package missing: {root}")
        questions = read_jsonl(root / "questions.jsonl")
        gold_rows = read_jsonl(root / "gold.jsonl")
        gold_by_id = {source_question_id(row): row for row in gold_rows}
        if len(gold_by_id) != len(gold_rows):
            raise RuntimeError(f"Duplicate source gold IDs in {dataset}")
        # Read the corpus as an input-integrity check.  Replacement links use
        # the parent Markdown so that every output ingest path stays derived.
        corpus = read_jsonl(root / "corpus.jsonl")
        loaded[dataset] = {"root": root, "questions": questions, "gold_by_id": gold_by_id, "corpus": corpus}
    return loaded


def parent_source_document_map(parent: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for row in parent["documents"]:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
        dataset = str(metadata.get("source_dataset") or "")
        source_id = str(metadata.get("source_document_id") or "")
        if dataset and source_id:
            result[(dataset, source_id)] = str(row["doc_id"])
    return result


def source_candidate_rows(
    dataset: str,
    source: Mapping[str, Any],
    used_source_ids: set[str],
    source_to_global_doc: Mapping[tuple[str, str], str],
    text_by_global_doc: Mapping[str, str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for question in source["questions"]:
        source_id = source_question_id(question)
        if source_id in used_source_ids:
            continue
        gold = source["gold_by_id"].get(source_id, {})
        refs = source_refs(dataset, question, gold)
        global_refs = [source_to_global_doc.get((dataset, ref), "") for ref in refs]
        if not refs or any(not global_id for global_id in global_refs):
            continue
        answer = source_answer(question, gold)
        if not str(answer or "").strip():
            continue
        linked_text = "\n\n".join(text_by_global_doc[global_id] for global_id in global_refs)
        evidence = evidence_parts(question, gold)
        validation = validate_answer_bearing_text(answer, evidence, linked_text)
        if not validation["passed"]:
            continue
        if question_media_is_visual(question):
            continue
        candidates.append(
            {
                "dataset": dataset,
                "question": question,
                "gold": gold,
                "source_question_id": source_id,
                "source_document_ids": refs,
                "global_document_ids": global_refs,
                "answer": answer,
                "evidence": evidence,
                "question_type": question_type(question),
                "domain": question_domain(dataset, question),
                "validation": validation,
            }
        )
    return candidates


def candidate_sort_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    answer = str(candidate.get("answer") or "")
    validation = candidate["validation"]
    return (
        0 if validation.get("match_kind") == "answer" else 1,
        -len(normalize_text(answer)),
        stable_hash(SELECTION_SEED, candidate["dataset"], candidate["source_question_id"]),
    )


def excluded_by_dataset(parent: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {dataset: [] for dataset in SOURCE_QUOTAS}
    for question in parent["questions"]:
        if str(question["question_id"]) in EXCLUDED_QUESTION_IDS:
            result[str(question["source_dataset"])].append(question)
    if sum(len(rows) for rows in result.values()) != len(EXCLUDED_QUESTION_IDS):
        raise RuntimeError("The parent package does not contain exactly the requested visual exclusions")
    return result


def select_replacements(
    candidates: Sequence[Mapping[str, Any]],
    excluded: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    target_count = len(excluded)
    target_domains = Counter(
        str((row.get("metadata") or {}).get("selection_domain") or question_domain(str(row["source_dataset"]), row))
        for row in excluded
    )
    target_types = Counter(str(row.get("question_type") or "unknown") for row in excluded)
    available = [dict(candidate) for candidate in candidates]
    selected: list[dict[str, Any]] = []

    # Fill requested domains first.  If a domain has no text-safe unused
    # candidates, the remaining slots are filled globally and the manifest
    # records the resulting delta.
    for domain, target in sorted(target_domains.items()):
        pool = [candidate for candidate in available if candidate["domain"] == domain]
        pool.sort(key=candidate_sort_key)
        take = min(target, len(pool))
        selected.extend(pool[:take])
        selected_ids = {item["source_question_id"] for item in selected}
        available = [candidate for candidate in available if candidate["source_question_id"] not in selected_ids]

    def fill_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
        type_penalty = 0 if candidate["question_type"] in target_types else 1
        domain_penalty = 0 if candidate["domain"] in target_domains else 1
        return (domain_penalty, type_penalty, candidate_sort_key(candidate))

    available.sort(key=fill_key)
    selected.extend(available[: max(0, target_count - len(selected))])
    if len(selected) != target_count:
        raise RuntimeError(
            f"Insufficient safe replacement candidates: required={target_count} available={len(selected)}"
        )
    selected_ids = [str(item["source_question_id"]) for item in selected]
    if len(set(selected_ids)) != len(selected_ids):
        raise RuntimeError("Replacement candidates are not unique")
    return selected


def global_document_ids_for_question(row: Mapping[str, Any]) -> list[str]:
    values = row.get("gold_doc_ids") or row.get("document_ids") or []
    if isinstance(values, str):
        values = [values]
    return list(dict.fromkeys(str(value) for value in values if value))


def make_replacement_question(candidate: Mapping[str, Any], global_refs: Sequence[str]) -> dict[str, Any]:
    source_question = candidate["question"]
    source_gold = candidate["gold"]
    source_id = str(candidate["source_question_id"])
    dataset = str(candidate["dataset"])
    question_id = global_question_id(dataset, source_id)
    metadata = sanitized_record(source_question.get("metadata") or {})
    metadata.update(
        {
            "condition": CONDITION,
            "replacement": True,
            "selection_bucket": str(candidate["question_type"]),
            "selection_domain": str(candidate["domain"]),
            "selection_evidence_class": "text",
            "source_candidate_question_id": source_id,
        }
    )
    evidence = compact_value(candidate["evidence"])
    return {
        "answer": candidate["answer"],
        "answerable": bool(source_question.get("answerable", True)),
        "document_ids": [],
        "gold": {"document_ids": list(global_refs), "evidence": evidence or []},
        "gold_doc_ids": list(global_refs),
        "gold_evidence": evidence or [],
        "metadata": metadata,
        "question": source_question.get("question") or "",
        "question_id": question_id,
        "question_type": str(candidate["question_type"]),
        "reference_answer": candidate["answer"],
        "source_dataset": dataset,
        "source_question_id": source_id,
    }


def make_replacement_gold(candidate: Mapping[str, Any], global_refs: Sequence[str], question_id: str) -> dict[str, Any]:
    source_gold = sanitized_record(candidate["gold"])
    source_gold.pop("gold_doc_ids", None)
    source_gold.pop("scope_doc_ids", None)
    evidence = compact_value(candidate["evidence"])
    return {
        "gold": {"document_ids": list(global_refs), "evidence": evidence or []},
        "gold_doc_ids": list(global_refs),
        "gold_evidence": evidence or [],
        "question_id": question_id,
        "reference_answer": candidate["answer"],
        "source_dataset": candidate["dataset"],
        "source_gold": source_gold,
        "source_gold_doc_ids": list(candidate["source_document_ids"]),
        "source_question_id": candidate["source_question_id"],
    }


def distribution_delta(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    replacement_count: int,
) -> dict[str, Any]:
    def domain(row: Mapping[str, Any]) -> str:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
        return str(metadata.get("selection_domain") or question_domain(str(row.get("source_dataset") or ""), row))

    dimensions = {
        "source_dataset": lambda row: str(row.get("source_dataset") or "unknown"),
        "question_type": lambda row: str(row.get("question_type") or "unknown"),
        "domain": domain,
    }
    result: dict[str, Any] = {}
    for name, getter in dimensions.items():
        before_counts = Counter(getter(row) for row in before)
        after_counts = Counter(getter(row) for row in after)
        keys = sorted(set(before_counts) | set(after_counts))
        result[name] = {
            "before": {key: before_counts[key] for key in keys},
            "after": {key: after_counts[key] for key in keys},
            "delta": {key: after_counts[key] - before_counts[key] for key in keys if after_counts[key] != before_counts[key]},
            "total_replaced": replacement_count,
        }
    return result


def make_visual_exclusion_rows(parent: Mapping[str, Any]) -> list[dict[str, Any]]:
    docs_by_id = parent["docs_by_id"]
    rows = []
    for question in sorted(parent["questions"], key=lambda row: str(row["question_id"])):
        question_id = str(question["question_id"])
        if question_id not in EXCLUDED_QUESTION_IDS:
            continue
        linked = global_document_ids_for_question(question)
        document_hashes = {doc_id: str(docs_by_id[doc_id]["sha256"]) for doc_id in linked}
        override = EXCLUSION_REASON_OVERRIDES.get(question_id, {})
        rows.append(
            {
                "classification": override.get("classification", "semantic_visual_gap"),
                "document_id": linked[0] if linked else "",
                "document_ids": linked,
                "document_hashes": document_hashes,
                "document_sha256": aggregate_document_hash(document_hashes),
                "evidence_type": (question.get("metadata") or {}).get("selection_evidence_class"),
                "question": question.get("question") or "",
                "question_id": question_id,
                "question_type": question.get("question_type"),
                "reason": override.get(
                    "reason",
                    "The selected source annotation requires visual/figure/layout interpretation without a text-safe answer-bearing passage.",
                ),
                "replacement_required": True,
                "source_dataset": question.get("source_dataset"),
                "source_question_id": question.get("source_question_id"),
            }
        )
    return rows


def build_distribution_after(parent_questions: Sequence[Mapping[str, Any]], replacements: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    excluded_ids = {str(row["excluded_question_id"]) for row in replacements}
    retained = [row for row in parent_questions if str(row["question_id"]) not in excluded_ids]
    return retained + [row["replacement_question"] for row in replacements]


def output_hashes(output: Path) -> dict[str, str]:
    return {
        path.relative_to(output).as_posix(): sha256_file(path)
        for path in sorted(output.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }


def write_readme(output: Path, manifest: Mapping[str, Any]) -> None:
    delta = manifest["distribution_delta"]
    lines = [
        "# MOI RAG Benchmark v0.1 — text-only-no-mllm",
        "",
        "This is a derived pure-text condition. The parent `ready_for_eval` package is unchanged; its 500 Markdown documents are physically copied into this package.",
        "",
        "## Contract",
        "",
        f"- Documents: {manifest['counts']['documents']} Markdown files",
        f"- Questions: {manifest['counts']['questions']}",
        f"- Gold rows: {manifest['counts']['gold']}",
        f"- Excluded visual-gap QA: {manifest['excluded_visual_qa_count']}",
        f"- Text-safe replacements: {manifest['replacement_count']}",
        f"- Deterministic answer repairs from frozen Gold evidence: {len(manifest.get('answer_repairs', []))}",
        "- MLLM required: false",
        "- Image LLM: NOT_APPLICABLE",
        "- Ingest paths: `documents/<doc_id>.md` only",
        "",
        "## Artifacts",
        "",
        "- `corpus.jsonl`, `questions.jsonl`, `gold.jsonl`: package records",
        "- `documents/`: copied Markdown ingest files",
        f"- `visual-exclusions.jsonl`: the {manifest['excluded_visual_qa_count']} removed visual-gap records",
        "- `replacement-audit.jsonl`: one mechanically checked replacement audit per removed QA",
        "- `manifest.json`: parent fingerprint, output hashes, quotas, and distribution deltas",
        "",
        "## Replacement distribution",
        "",
        f"- Question-type delta: `{json.dumps(delta['question_type']['delta'], ensure_ascii=False, sort_keys=True)}`",
        f"- Domain delta: `{json.dumps(delta['domain']['delta'], ensure_ascii=False, sort_keys=True)}`",
        "",
        "## Reproduction",
        "",
        "```bash",
        "moi-prototypes/local-bge-m3-embedding/.venv/bin/python scripts/tools/build_moi_text_only_condition_v0_1.py \\",
        "  --output datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval",
        "moi-prototypes/local-bge-m3-embedding/.venv/bin/python scripts/tools/build_moi_text_only_condition_v0_1.py \\",
        "  --output datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval --check-only",
        "```",
        "",
    ]
    (output / "README.md").write_text("\n".join(lines), encoding="utf-8")


def build(parent_root: Path, output: Path, overwrite: bool = False) -> dict[str, Any]:
    validate_separated_roots(parent_root, output)
    parent = load_parent(parent_root)
    source_packages = load_source_packages()
    excluded = excluded_by_dataset(parent)
    source_to_global_doc = parent_source_document_map(parent)
    used_source_ids = {
        dataset: {
            str(row["source_question_id"])
            for row in parent["questions"]
            if str(row.get("source_dataset")) == dataset
        }
        for dataset in SOURCE_QUOTAS
    }

    candidates_by_dataset: dict[str, list[dict[str, Any]]] = {}
    for dataset in SOURCE_QUOTAS:
        candidates_by_dataset[dataset] = source_candidate_rows(
            dataset,
            source_packages[dataset],
            used_source_ids[dataset],
            source_to_global_doc,
            parent["text_by_doc"],
        )

    replacements: list[dict[str, Any]] = []
    for dataset, excluded_rows in excluded.items():
        if not excluded_rows:
            continue
        selected = select_replacements(candidates_by_dataset[dataset], excluded_rows)
        unpaired_excluded = sorted(excluded_rows, key=lambda row: str(row["question_id"]))
        for candidate in selected:
            candidate_domain = str(candidate["domain"])
            matching_rows = [
                row
                for row in unpaired_excluded
                if str((row.get("metadata") or {}).get("selection_domain") or question_domain(dataset, row))
                == candidate_domain
            ]
            excluded_row = (matching_rows or unpaired_excluded)[0]
            unpaired_excluded.remove(excluded_row)
            global_refs = list(candidate["global_document_ids"])
            replacement_question = make_replacement_question(candidate, global_refs)
            replacement_gold = make_replacement_gold(candidate, global_refs, replacement_question["question_id"])
            document_hashes = {
                doc_id: str(parent["docs_by_id"][doc_id]["sha256"]) for doc_id in global_refs
            }
            validation = candidate["validation"]
            replacements.append(
                {
                    "excluded_question_id": str(excluded_row["question_id"]),
                    "excluded_source_question_id": excluded_row.get("source_question_id"),
                    "replacement_question_id": replacement_question["question_id"],
                    "replacement_source_question_id": candidate["source_question_id"],
                    "source_dataset": dataset,
                    "source_question_type": excluded_row.get("question_type"),
                    "replacement_question_type": candidate["question_type"],
                    "source_domain": (excluded_row.get("metadata") or {}).get("selection_domain")
                    or question_domain(dataset, excluded_row),
                    "replacement_domain": candidate["domain"],
                    "source_document_ids": candidate["source_document_ids"],
                    "linked_document_ids": global_refs,
                    "document_hash": aggregate_document_hash(document_hashes),
                    "document_hashes": document_hashes,
                    "reference_answer": candidate["answer"],
                    "answer_bearing_text": True,
                    "evidence_validation": "passed",
                    "matched_kind": validation["match_kind"],
                    "matched_excerpt": validation["matched_excerpt"],
                    "match_mode": validation["match_mode"],
                    "visual_semantic_gap": False,
                    "replacement_question": replacement_question,
                    "replacement_gold": replacement_gold,
                }
            )

    if len(replacements) != len(EXCLUDED_QUESTION_IDS):
        raise RuntimeError(f"Expected {len(EXCLUDED_QUESTION_IDS)} replacements, got {len(replacements)}")
    if {row["excluded_question_id"] for row in replacements} != EXCLUDED_QUESTION_IDS:
        raise RuntimeError("Replacement audit does not cover exactly the requested exclusions")

    replacement_questions = {row["replacement_question_id"]: row["replacement_question"] for row in replacements}
    replacement_gold = {row["replacement_question_id"]: row["replacement_gold"] for row in replacements}
    gold_by_id = {str(row["question_id"]): sanitized_record(row) for row in parent["gold"]}
    answer_repairs: list[dict[str, Any]] = []
    questions = []
    for row in parent["questions"]:
        if str(row["question_id"]) in EXCLUDED_QUESTION_IDS:
            continue
        normalized = sanitized_record(row)
        # `scope_doc_ids` in source packages names source-space resources, not
        # the copied corpus IDs.  The global derived package does not need that
        # field; gold_doc_ids remains the canonical output-space link.
        normalized.pop("scope_doc_ids", None)
        metadata = normalized.get("metadata")
        if isinstance(metadata, dict):
            metadata["condition"] = CONDITION
            metadata["text_only_projection"] = True
        question_id = str(normalized["question_id"])
        fallback = deterministic_reference_answer_fallback(row, gold_by_id[question_id])
        if fallback:
            normalized["answer"] = fallback
            normalized["reference_answer"] = fallback
            if not isinstance(metadata, dict):
                metadata = {}
                normalized["metadata"] = metadata
            metadata["reference_answer_fallback"] = "gold_evidence"
            answer_repairs.append(
                {
                    "question_id": question_id,
                    "method": "gold_evidence",
                    "evidence_count": len(evidence_parts(row, gold_by_id[question_id])),
                }
            )
        questions.append(normalized)
    questions.extend(replacement_questions.values())
    questions.sort(key=lambda row: str(row["question_id"]))

    gold_rows = []
    for question in questions:
        question_id = str(question["question_id"])
        if question_id in replacement_gold:
            gold_rows.append(replacement_gold[question_id])
        else:
            gold_row = gold_by_id[question_id]
            if question.get("metadata", {}).get("reference_answer_fallback") == "gold_evidence":
                gold_row["reference_answer"] = question["reference_answer"]
            gold_rows.append(gold_row)
    gold_rows.sort(key=lambda row: str(row["question_id"]))

    documents = []
    for row in parent["documents"]:
        normalized_document = sanitized_record(row)
        normalized_document["text_path"] = row["text_path"]
        documents.append(normalized_document)
    documents.sort(key=lambda row: str(row["doc_id"]))
    exclusions = make_visual_exclusion_rows(parent)
    audit_rows = []
    for row in sorted(replacements, key=lambda item: str(item["excluded_question_id"])):
        audit_rows.append({key: value for key, value in row.items() if key not in {"replacement_question", "replacement_gold"}})

    if output.exists():
        if not overwrite:
            raise FileExistsError(f"Output already exists; pass --overwrite to rebuild this derived package: {output}")
        allowed_output = DEFAULT_OUTPUT.resolve()
        if output.resolve() != allowed_output or output.is_symlink():
            raise RuntimeError("--overwrite is restricted to the exact derived text_only_ready_for_eval directory")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    (output / "documents").mkdir()
    for row in documents:
        source_path = (parent_root / str(row["text_path"])).resolve()
        destination = output / str(row["text_path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination)

    write_jsonl(output / "corpus.jsonl", documents)
    write_jsonl(output / "questions.jsonl", questions)
    write_jsonl(output / "gold.jsonl", gold_rows)
    write_jsonl(output / "visual-exclusions.jsonl", exclusions)
    write_jsonl(output / "replacement-audit.jsonl", audit_rows)

    after_questions = build_distribution_after(parent["questions"], replacements)
    manifest: dict[str, Any] = {
        "schema": "competitor-eval-ready-v1",
        "schema_version": "competitor-eval-ready-v1",
        "package_schema": "competitor-eval-ready-v1",
        "dataset_id": "moi-rag-bench-v0.1-text-only-no-mllm",
        "dataset_name": "MOI RAG Benchmark v0.1 derived text-only-no-mllm condition",
        "dataset_revision": "derived-local-v0.1",
        "revision": "derived-local-v0.1",
        "condition": CONDITION,
        "mllm_required": False,
        "image_llm": IMAGE_LLM,
        "ingest_representation": "source_document",
        "protocol_tag": "MOI_RAG_BENCH_V0_1_TEXT_ONLY_NO_MLLM",
        "scope": "global",
        "scope_policy": "global",
        "split": "evaluation",
        "status": "READY_LOCAL_DERIVED_TEXT_ONLY",
        "readiness_status": "READY",
        "evaluation": {"scope": "global", "ingest_representation": "source_document"},
        "documents": "corpus.jsonl",
        "corpus_path": "corpus.jsonl",
        "questions": "questions.jsonl",
        "questions_path": "questions.jsonl",
        "gold": "gold.jsonl",
        "gold_path": "gold.jsonl",
        "counts": {"documents": len(documents), "questions": len(questions), "gold": len(gold_rows)},
        "allocation": {"source_qa": SOURCE_QUOTAS, "source_documents": dict(Counter(row["metadata"]["source_dataset"] for row in documents))},
        "excluded_visual_qa_count": len(EXCLUDED_QUESTION_IDS),
        "replacement_count": len(replacements),
        "answer_repairs": answer_repairs,
        "replacement_policy": {
            # Record logical source identities only. Runtime checkout paths are
            # build inputs and must not leak into the portable output package.
            "candidate_sources": sorted(SOURCE_ROOTS),
            "unused_source_question_required": True,
            "linked_documents_must_be_in_parent_500": True,
            "visual_candidate_policy": "exclude visual-marker candidates; require normalized answer or explicit evidence containment",
        },
        "parent": {
            "sha256": parent["fingerprint"],
            "manifest_sha256": parent["manifest_sha256"],
        },
        "distribution_delta": distribution_delta(parent["questions"], after_questions, len(replacements)),
        "artifacts": {
            "documents": "documents/",
            "corpus": "corpus.jsonl",
            "questions": "questions.jsonl",
            "gold": "gold.jsonl",
            "visual_exclusions": "visual-exclusions.jsonl",
            "replacement_audit": "replacement-audit.jsonl",
            "readme": "README.md",
        },
        "known_semantic_visual_gaps_removed": sorted(EXCLUDED_QUESTION_IDS),
    }
    write_readme(output, manifest)
    manifest["output_hashes"] = output_hashes(output)
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def validate_output(parent_root: Path, output: Path) -> dict[str, Any]:
    validate_separated_roots(parent_root, output)
    parent = load_parent(parent_root)
    manifest = read_json(output / "manifest.json")
    if manifest.get("condition") != CONDITION:
        raise RuntimeError("Derived condition is not text-only-no-mllm")
    if manifest.get("parent", {}).get("sha256") != parent["fingerprint"]:
        raise RuntimeError("Parent SHA256 does not match the current ready_for_eval package")
    if manifest.get("parent", {}).get("manifest_sha256") != parent["manifest_sha256"]:
        raise RuntimeError("Parent manifest SHA256 does not match")

    documents = read_jsonl(output / "corpus.jsonl")
    questions = read_jsonl(output / "questions.jsonl")
    gold = read_jsonl(output / "gold.jsonl")
    if (len(documents), len(questions), len(gold)) != (500, 1000, 1000):
        raise RuntimeError("Derived package counts are not 500/1000/1000")
    document_ids = {str(row["doc_id"]) for row in documents}
    question_ids = {str(row["question_id"]) for row in questions}
    gold_ids = {str(row["question_id"]) for row in gold}
    if len(document_ids) != 500 or len(question_ids) != 1000 or gold_ids != question_ids:
        raise RuntimeError("Derived IDs are not unique/aligned")
    if EXCLUDED_QUESTION_IDS & question_ids:
        raise RuntimeError("A requested visual-gap question remains in the derived condition")

    source_counts = Counter(str(row.get("source_dataset")) for row in questions)
    if dict(source_counts) != SOURCE_QUOTAS:
        raise RuntimeError(f"Source quotas changed: {dict(source_counts)}")
    for row in questions:
        if set(row.get("gold_doc_ids", [])) - document_ids:
            raise RuntimeError(f"Question links outside derived docs: {row['question_id']}")
        if bool(row.get("answerable", True)) and not str(
            row.get("reference_answer") or row.get("answer") or ""
        ).strip():
            raise RuntimeError(f"Answerable question has no reference answer: {row['question_id']}")
        if any(key in row for key in ("image_path", "image_paths", "images")):
            raise RuntimeError(f"Question has image path/media field: {row['question_id']}")
        if "media" in row and "image" in str(row["media"]).casefold():
            raise RuntimeError(f"Question has image media: {row['question_id']}")
    for row in gold:
        if set(row.get("gold_doc_ids", [])) - document_ids:
            raise RuntimeError(f"Gold links outside derived docs: {row['question_id']}")

    documents_root = (output / "documents").resolve()
    parent_documents_root = (parent_root / "documents").resolve()
    files = [path for path in (output / "documents").rglob("*") if path.is_file()]
    if len(files) != 500 or any(path.suffix.casefold() != ".md" or path.is_symlink() for path in files):
        raise RuntimeError("Derived documents are not 500 physical Markdown files")
    for row in documents:
        path = (output / str(row["text_path"])).resolve()
        try:
            path.relative_to(documents_root)
        except ValueError as exc:
            raise RuntimeError(f"Derived ingest path escapes documents: {row['text_path']}") from exc
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"Derived ingest path is missing/symlink: {path}")
        parent_path = (parent_root / str(row["text_path"])).resolve()
        try:
            parent_path.relative_to(parent_documents_root)
        except ValueError as exc:
            raise RuntimeError(f"Parent ingest path escapes documents: {row['text_path']}") from exc
        if not parent_path.is_file() or path.read_bytes() != parent_path.read_bytes() or os_samefile(path, parent_path):
            raise RuntimeError(f"Derived document is not a physical parent copy: {path}")
        if sha256_file(path) != str(row.get("sha256")):
            raise RuntimeError(f"Derived document hash mismatch: {path}")
    forbidden = [path for path in output.rglob("*") if path.is_file() and path.suffix.casefold() in {".pdf", ".jpg", ".jpeg", ".png"}]
    if forbidden:
        raise RuntimeError(f"Forbidden media in derived output: {forbidden[:3]}")

    exclusions = read_jsonl(output / "visual-exclusions.jsonl")
    audits = read_jsonl(output / "replacement-audit.jsonl")
    if (
        {str(row["question_id"]) for row in exclusions} != EXCLUDED_QUESTION_IDS
        or len(audits) != len(EXCLUDED_QUESTION_IDS)
    ):
        raise RuntimeError("Visual exclusion/replacement audit coverage is incomplete")
    question_by_id = {str(row["question_id"]): row for row in questions}
    for audit in audits:
        if audit.get("evidence_validation") != "passed" or not audit.get("answer_bearing_text"):
            raise RuntimeError(f"Replacement evidence validation failed: {audit.get('replacement_question_id')}")
        if audit.get("visual_semantic_gap") is not False or not str(audit.get("matched_excerpt") or "").strip():
            raise RuntimeError(f"Replacement visual/text audit failed: {audit.get('replacement_question_id')}")
        replacement = question_by_id.get(str(audit["replacement_question_id"]))
        if replacement is None or not str(replacement.get("reference_answer") or "").strip():
            raise RuntimeError(f"Replacement question is missing a reference answer: {audit}")
        if set(audit.get("linked_document_ids", [])) - document_ids:
            raise RuntimeError(f"Replacement links outside docs: {audit}")
        for doc_id, document_hash in (audit.get("document_hashes") or {}).items():
            if document_hash != str(parent["docs_by_id"][doc_id]["sha256"]):
                raise RuntimeError(f"Replacement document hash mismatch: {doc_id}")

    actual_hashes = output_hashes(output)
    if manifest.get("output_hashes") != actual_hashes:
        raise RuntimeError("Output hash ledger does not match generated files")
    answer_repairs = manifest.get("answer_repairs")
    if not isinstance(answer_repairs, list):
        raise RuntimeError("Answer repair audit is missing")
    repaired_ids = {str(item.get("question_id")) for item in answer_repairs}
    observed_repaired_ids = {
        str(row["question_id"])
        for row in questions
        if (row.get("metadata") or {}).get("reference_answer_fallback") == "gold_evidence"
    }
    if repaired_ids != observed_repaired_ids:
        raise RuntimeError("Answer repair audit does not match question metadata")
    return {
        "status": "OK",
        "documents": 500,
        "questions": 1000,
        "gold": 1000,
        "replacements": len(EXCLUDED_QUESTION_IDS),
        "answer_repairs": len(answer_repairs),
    }


def os_samefile(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except FileNotFoundError:
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, default=DEFAULT_PARENT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def resolve_from_root(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def main() -> None:
    args = parse_args()
    parent = resolve_from_root(args.parent)
    output = resolve_from_root(args.output)
    if args.check_only:
        result = validate_output(parent, output)
    else:
        result = build(parent, output, overwrite=args.overwrite)
        result = {"status": "BUILT", "output": str(output), "counts": result["counts"], "replacement_count": result["replacement_count"]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
