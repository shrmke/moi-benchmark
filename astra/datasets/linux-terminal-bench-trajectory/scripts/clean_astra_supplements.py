#!/usr/bin/env python3
"""Normalize and redact Astra's all-verifier-attempts database exports."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re

import clean_trajectories as clean


class SupplementRedactor(clean.Redactor):
    PATTERNS = clean.Redactor.PATTERNS + (
        (re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+'), 'Bearer <REDACTED_CREDENTIAL>'),
        (re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b'), '<REDACTED_JWT>'),
        (re.compile(r'(?i)(\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret[_-]?key|password)\b[\s\"\x27]*[:=]\s*[\"\x27]?)[^\s\"\x27,;}]+'), r'\1<REDACTED_CREDENTIAL>'),
        (re.compile(r'(?i)(\b[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@'), r'\1<REDACTED_CREDENTIAL>@'),
        (re.compile(r'/(?:home|Users)/[^/\s\"\x27]+'), '<USER_HOME>'),
        (re.compile(r'data:image/[^;\s]+;base64,[A-Za-z0-9+/=\s]+'), '<IMAGE_BASE64_OMITTED>'),
    )


def sanitize(value, redactor):
    """Keep JSON structure while removing private fields and image payloads."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, (dict, list)):
            cleaned, hidden = sanitize(decoded, redactor)
            return json.dumps(cleaned, ensure_ascii=False), hidden
        value, count = re.subn(r'<(?:think|thinking|reasoning)>.*?</(?:think|thinking|reasoning)>', '', value, flags=re.S | re.I)
        return redactor.text(value), bool(count)
    if isinstance(value, dict):
        if value.get('type') in ('thinking', 'reasoning', 'reasoning_content'):
            return None, True
        if value.get('type') in ('image', 'image_url'):
            return '<IMAGE_OMITTED>', False
        result, hidden = {}, False
        for key, item in value.items():
            if key in ('reasoning_content', 'thinking', 'reasoning'):
                hidden = True
                continue
            if re.fullmatch(r'(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret[_-]?key|password|authorization|cookie)', key, re.I):
                result[key] = '<REDACTED_CREDENTIAL>'
                redactor.count += 1
            else:
                result[key], omitted = sanitize(item, redactor)
                hidden |= omitted
        return result, hidden
    if isinstance(value, list):
        result, hidden = [], False
        for item in value:
            cleaned, omitted = sanitize(item, redactor)
            if cleaned is not None:
                result.append(cleaned)
            hidden |= omitted
        return result, hidden
    return value, False


def public_content(value, redactor):
    cleaned, hidden = sanitize(value, redactor)
    if cleaned is None:
        return '', hidden
    return cleaned if isinstance(cleaned, str) else json.dumps(cleaned, ensure_ascii=False), hidden


def payload(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except ValueError:
            pass
    return {}


def transcript(path, redactor):
    rows = clean.load_jsonl(path)
    if rows is None:
        return [], []
    rows.sort(key=lambda row: row['item_seq'])
    messages = []
    for row in rows:
        role = row.get('role')
        if role not in {'user', 'assistant', 'tool'}:
            continue
        data = payload(row.get('payload_json'))
        text, hidden = public_content(row.get('content'), redactor)
        calls = []
        for call in data.get('tool_calls') or []:
            arguments, args_hidden = public_content(call.get('arguments'), redactor)
            hidden |= args_hidden
            calls.append({'id': call.get('tool_use_id'), 'name': call.get('name'), 'arguments_json': arguments})
        result = data.get('tool_result') or {}
        messages.append(clean.make_message(
            role, text, timestamp=row.get('created_at'), tool_calls=calls,
            tool_call_id=result.get('tool_use_id'), tool_name=result.get('name'),
            is_error=result.get('status') in {'failed', 'error', 'rejected'} if result.get('status') else None,
            internal_omitted=hidden,
        ))
    return clean.sequence(messages), rows


def native_transcript(folder, redactor):
    candidates = []
    for path in sorted((folder / 'native-artifact').rglob('*.jsonl')):
        if path.name in {'server-events.jsonl', 'step_events.jsonl'}:
            continue
        rows = clean.load_jsonl(path) or []
        turns = []
        for row in rows:
            output = []
            for raw in row.get('messages') or []:
                if not isinstance(raw, dict) or raw.get('role') not in {'user', 'assistant', 'tool'}:
                    continue
                text, hidden = public_content(raw.get('content'), redactor)
                calls = []
                for call in raw.get('tool_calls') or []:
                    function = call.get('function') or call
                    arguments, omitted = public_content(function.get('arguments'), redactor)
                    hidden |= omitted
                    calls.append({'id': call.get('id'), 'name': function.get('name'), 'arguments_json': arguments})
                output.append(clean.make_message(raw['role'], text, timestamp=raw.get('timestamp'),
                    tool_calls=calls, tool_call_id=raw.get('tool_call_id'),
                    tool_name=raw.get('tool_name'), internal_omitted=hidden))
            if {'user', 'assistant'} <= {m['role'] for m in output}:
                candidates.append((output, path))
            if row.get('type') == 'turn':
                for role, key in (('user', 'user_input'), ('assistant', 'assistant_output')):
                    text, hidden = public_content(row.get(key), redactor)
                    if text:
                        turns.append(clean.make_message(role, text, timestamp=row.get('ts'), internal_omitted=hidden))
        if {'user', 'assistant'} <= {m['role'] for m in turns}:
            candidates.append((turns, path))
    return max(candidates, key=lambda pair: len(pair[0])) if candidates else ([], None)


def supplement_capture_complete(rows, events, database):
    """Prove database transcript coverage by exact per-run tool counts."""
    if database not in {'current', 'old'}:
        return False
    observed = Counter()
    for row in rows:
        observed[row.get('run_id')] += len(payload(row.get('payload_json')).get('tool_calls') or [])
    expected = {}
    for event in events:
        if event.get('event_type') not in {'run_finished', 'run_interrupted', 'run_error'}:
            continue
        data = payload(event.get('payload_json')).get('data') or {}
        count = data.get('tool_call_count', data.get('tool_calls_completed'))
        if isinstance(count, int) and not isinstance(count, bool):
            expected[event.get('run_id')] = count
    return bool(observed) and set(observed) == set(expected) and all(
        observed[run_id] == expected[run_id] for run_id in observed
    )


def run(args):
    source, raw_root, output = args.source.resolve(), args.raw_root.resolve(), args.output.resolve()
    inventory = clean.load_json(source / 'inventory.json')
    if not inventory or not isinstance(inventory.get('attempts'), list):
        raise ValueError('Missing or invalid inventory.json')
    inventory['attempts'] = [{**item, 'job': Path(item['job']).name}
                             for item in inventory['attempts']]
    records, excluded = [], []
    attempts = defaultdict(list)
    for item in inventory['attempts']:
        attempts[item['task']].append((item['job'], item['trial']))
    indexes = {(task, trial): i for task, values in attempts.items()
               for i, (_, trial) in enumerate(sorted(values), 1)}
    source_counts = Counter()
    for item in inventory['attempts']:
        redactor = SupplementRedactor()
        trial = Path(item['result_path']).parent
        result = clean.load_json(trial / 'result.json')
        folder = source / 'attempts' / item['job'] / item['trial']
        candidates = []
        for database in ('current', 'old'):
            for path in sorted((folder / database).glob('*/session_transcript_items.jsonl')):
                messages, rows = transcript(path, redactor)
                if rows and all(row.get('session_id') == item['session_id'] for row in rows):
                    candidates.append((messages, rows, path, database))
        if not result or not result.get('finished_at') or not candidates:
            excluded.append({'record_id': 'astra/' + item['trial'], 'reason': 'missing_finished_result_or_transcript'})
            continue
        # Exports are alternative sources, not separate turns to concatenate.
        messages, rows, path, database = max(candidates, key=lambda candidate: len(candidate[0]))
        database_path = path
        trace_format = 'astra_database_transcript'
        if not {'user', 'assistant'} <= {m['role'] for m in messages}:
            messages, native_path = native_transcript(folder, redactor)
            if native_path is not None:
                path, database, trace_format = native_path, 'native-artifact', 'astra_native_supplement'
        if not {'user', 'assistant'} <= {m['role'] for m in messages}:
            excluded.append({'record_id': 'astra/' + item['trial'], 'reason': 'metadata_only_missing_user_or_assistant'})
            continue
        call_ids = [call['id'] for m in messages for call in m['tool_calls']]
        result_ids = [m['tool_call_id'] for m in messages if m['role'] == 'tool']
        paired = clean.paired(call_ids, result_ids)
        events = clean.load_jsonl(database_path.parent / 'agent_run_events.jsonl') or []
        terminal = any(row.get('event_type') in {'run_finished', 'run_interrupted', 'run_error'} for row in events)
        capture_complete = supplement_capture_complete(rows, events, database)
        reasons = [] if capture_complete else ['supplement_coverage_unproven']
        reported = clean.metadata(result).get('tool_calls_count')
        if reported and not call_ids:
            paired = False
        if not paired:
            reasons.append('unpaired_tool_call')
        if not terminal:
            reasons.append('missing_terminal')
        # Source documentation explicitly says neither SQL success nor native
        # capture flags establish full model/tool-output coverage.
        trace = clean.parsed_trace(messages, path, trace_format, raw_root,
            found=True, parseable=True, terminal=terminal, capture_complete=capture_complete,
            tool_calls_paired=paired, reported_tool_calls=None, reasons=reasons)
        record = clean.build_record('astra', trial, result, clean.load_json(trial / 'config.json'),
            trace, redactor, indexes[(item['task'], item['trial'])], clean.dataset_revision(raw_root), raw_root)
        # Astra supplement splits describe trace completeness. Verifier validity
        # remains recorded independently and does not downgrade proven capture.
        record['quality']['verifier_independent_tier'] = True
        if capture_complete and terminal and paired:
            record['quality']['tier'] = 'complete'
        record['instruction'], _ = public_content(record['instruction'], redactor)
        record['source'].update(database_source=database, supplement_path=clean.relative(folder, raw_root))
        record['quality']['redaction_count'] = redactor.count
        records.append(record)
        source_counts[database] += 1
    clean.validate(records)
    records.sort(key=lambda r: (r['benchmark']['task_id'], r['trial']['attempt_index']))
    for tier in ('complete', 'partial'):
        clean.write_jsonl(output / 'data' / tier / 'astra.jsonl', [r for r in records if r['quality']['tier'] == tier])
    reasons = Counter(reason for r in records if r['quality']['tier'] == 'partial'
                      for reason in r['quality']['reasons'])
    stats = {
        'discovered_trials': len(inventory['attempts']), 'complete': sum(r['quality']['tier'] == 'complete' for r in records),
        'partial': sum(r['quality']['tier'] == 'partial' for r in records), 'retained': len(records),
        'excluded_metadata_only': len(excluded),
        'excluded_reason_counts_nonexclusive': dict(Counter(r['reason'] for r in excluded)),
        'partial_reason_counts_nonexclusive': dict(reasons),
        'messages': sum(len(r['messages']) for r in records),
        'tool_calls': sum(r['usage']['tool_call_count'] for r in records),
        'redactions': sum(r['quality']['redaction_count'] for r in records),
    }
    now = datetime.now(timezone.utc).isoformat()
    report = {'generated_at': now, 'source': clean.relative(source, raw_root), 'statistics': stats,
              'database_sources': dict(source_counts), 'excluded': excluded,
              'scope': 'inventory.json only; canonical database transcripts with native fallback; no cross-source concatenation'}
    (output / 'astra_supplement_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    combined = clean.load_json(output / 'quality_report.json') or {'products': {}}
    combined['products']['astra'] = stats
    combined['generated_at'] = now
    combined['astra_source'] = report['source']
    combined['totals'] = {key: sum(p.get(key, 0) for p in combined['products'].values())
                          for key in ('discovered_trials', 'complete', 'partial', 'retained',
                                      'excluded_metadata_only', 'messages', 'tool_calls', 'redactions')}
    (output / 'quality_report.json').write_text(json.dumps(combined, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report['statistics'], ensure_ascii=False))


def main():
    output = Path(__file__).resolve().parents[1]
    raw = output.parents[2] / 'work/linux-terminal-bench'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=raw / 'astra/trace-supplements/all-verifier-attempts')
    parser.add_argument('--raw-root', type=Path, default=raw)
    parser.add_argument('--output', type=Path, default=output)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
