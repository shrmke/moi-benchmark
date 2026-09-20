#!/usr/bin/env python3
"""Measure official APIs on the frozen 50-case private corpus, without overwriting prior runs."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import statistics
import time
import zipfile

import requests
import run_mineru_precision_semiconductor as mineru
import run_paddleocr_vl_semiconductor as paddle

BASE = Path(__file__).resolve().parents[1]


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def percentile(values, q):
    s = sorted(values)
    p = (len(s) - 1) * q
    i = int(p)
    return s[i] + (s[min(i + 1, len(s) - 1)] - s[i]) * (p - i)


def clean_error(exc, secrets):
    msg = str(exc)
    for secret in secrets:
        if secret:
            msg = msg.replace(secret, '[REDACTED]')
    return re.sub(r'https?://\S+', '[URL]', msg)[:1000]


def summary(rows):
    good = [r for r in rows if r.get('parse_success')]
    if not good:
        return {'attempted': len(rows), 'parse_successes': 0}
    seconds = sum(r['until_success_seconds'] for r in good)
    reference_pages = sum(r['moi_reference_pages'] for r in good)
    times = [r['until_success_seconds'] for r in good]
    known_pages = [r for r in good if r.get('api_output_pages', 0) > 0]
    return {
        'attempted': len(rows), 'parse_successes': len(good),
        'download_successes': sum(bool(r.get('download_success')) for r in rows),
        'successful_reference_pages': reference_pages,
        'sum_successful_request_seconds': seconds,
        'mean_seconds_per_file': statistics.mean(times),
        'mean_seconds_per_moi_reference_page': seconds / reference_pages,
        'api_output_page_count_coverage': len(known_pages),
        'api_output_pages': sum(r['api_output_pages'] for r in known_pages),
        'mean_seconds_per_api_output_page': (
            sum(r['until_success_seconds'] for r in known_pages)
            / sum(r['api_output_pages'] for r in known_pages) if known_pages else None),
        'file_latency_percentiles_seconds': {
            k: percentile(times, q) for k, q in [('p50', .5), ('p95', .95), ('p99', .99)]},
        'note': 'Successful calls only; page means are aggregate amortized durations, not individual-page latency or throughput.'}


def audit_existing(output):
    """Audit empty results without treating nonempty results as quality-correct."""
    providers = {}
    effective_rows = {}
    for name in ['mineru', 'paddle']:
        rows = []
        for record in sorted((output / name).glob('*/timing.json')):
            row = json.loads(record.read_text())
            row['content_nonempty'] = False
            if row.get('download_success'):
                if name == 'paddle':
                    data = json.loads(record.with_name('result.json').read_text())
                    row['content_nonempty'] = bool(record.with_name('result.md').read_bytes().strip()) or any(
                        page.get(k) for page in data.get('pages', [])
                        for k in ['layouts', 'images', 'inner_images', 'tables', 'text', 'markdown'])
                else:
                    with zipfile.ZipFile(record.with_name('result.zip')) as archive:
                        row['content_nonempty'] = any(archive.read(n).strip() for n in archive.namelist()
                                                     if n.endswith('.md'))
                        # Native Office layout page groups are not physical rendered pages.
                        if row['source_file'].lower().endswith('.pdf'):
                            for n in archive.namelist():
                                if n.endswith('layout.json') or n.endswith('_middle.json'):
                                    data = json.loads(archive.read(n))
                                    if isinstance(data, dict) and isinstance(data.get('pdf_info'), list):
                                        row['api_output_pages'] = len(data['pdf_info'])
                                        break
            rows.append(row)
        good = [r for r in rows if r.get('parse_success') and r.get('download_success') and r['content_nonempty']]
        effective_rows[name] = good
        providers[name] = {
            'api_success_statistics': summary(rows),
            'nonempty_downloaded_result_statistics': summary(good),
            'failed_or_empty_cases': [
                {'source_file': r['source_file'], 'parse_success': r.get('parse_success'),
                 'download_success': r.get('download_success'), 'content_nonempty': r['content_nonempty'],
                 'error': r.get('error')} for r in rows if r not in good],
            'prior_failed_attempts': sum(len(r.get('previous_attempts', [])) for r in rows)}
    shared_ids = set(r['case_id'] for r in effective_rows['mineru']) & set(
        r['case_id'] for r in effective_rows['paddle'])
    analysis = {
        'created_at_utc': utc(), 'providers': providers,
        'matched_nonempty_cases': {name: summary([r for r in rows if r['case_id'] in shared_ids])
                                  for name, rows in effective_rows.items()},
        'note': 'Nonempty output is only a sanity check, not a quality score. Page averages use the same MOI rendered reference pages.'}
    save(output / 'analysis.json', analysis)
    print(json.dumps(analysis, ensure_ascii=False, indent=2), flush=True)
    return analysis


def run_provider(provider, cases, output, poll, timeout, credentials, retry_failed=False):
    directory = output / provider
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    session = requests.Session()
    token, key, secret = credentials
    access = None
    if provider == 'paddle':
        access = paddle.get_access_token(session, key, secret, 30)
    headers = {'Authorization': 'Bearer ' + token}
    started = utc()
    batch_clock = time.perf_counter()
    for index, case in enumerate(cases, 1):
        source = BASE / case['source_file']
        case_dir = directory / f"{source.name}--{case['case_id']}"
        case_dir.mkdir(parents=True, exist_ok=True)
        record = case_dir / 'timing.json'
        previous_attempts = []
        if record.exists():
            old = json.loads(record.read_text())
            if old.get('finished'):
                if not retry_failed or old.get('parse_success'):
                    rows.append(old)
                    continue
                previous_attempts = old.pop('previous_attempts', []) + [old]
            else:
                raise RuntimeError('Unfinished case exists; do not resubmit automatically: ' + source.name)
        row = {
            'provider': provider, 'source_file': case['source_file'],
            'source_sha256': case['source_sha256'], 'source_bytes': source.stat().st_size,
            'case_id': case['case_id'], 'moi_reference_pages': case['reference_pages'],
            'started_at_utc': utc(), 'poll_interval_seconds': poll,
            'parse_success': False, 'download_success': False, 'finished': False}
        if previous_attempts:
            row['previous_attempts'] = previous_attempts
        save(record, row)
        clock = time.perf_counter()
        stage = 'submit'
        print(f"[{provider} {index}/{len(cases)}] {source.name}", flush=True)
        try:
            if provider == 'mineru':
                task, url = mineru.request_upload_url(
                    session, headers, source, case['case_id'], 'vlm', 'ch', 30)
                row['task_id'] = task
                row['create_upload_seconds'] = time.perf_counter() - clock
                save(record, row)
                stage = 'upload'
                mineru.upload_file(session, url, source, 30)
                row['submit_and_upload_seconds'] = time.perf_counter() - clock
                stage = 'poll'
                result_url = mineru.wait_for_result(session, headers, task, poll, timeout, 30)
            else:
                task = paddle.submit_task(session, access, source, 30)
                row['task_id'] = task
                row['submission_seconds'] = time.perf_counter() - clock
                save(record, row)
                stage = 'poll'
                md_url, result_url = paddle.wait_for_result(session, access, task, poll, timeout, 30)
            row['until_success_seconds'] = time.perf_counter() - clock
            row['parse_success'] = True
            save(record, row)
            stage = 'download'
            if provider == 'mineru':
                response = session.get(result_url, timeout=300)
                response.raise_for_status()
                blob = response.content
                with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                    members = archive.namelist()
                    row['result_zip_entries'] = len(members)
                    for member in members:
                        if member.endswith('_middle.json') or member.endswith('/middle.json'):
                            middle = json.loads(archive.read(member))
                            if isinstance(middle.get('pdf_info'), list):
                                row['api_output_pages'] = len(middle['pdf_info'])
                                break
                (case_dir / 'result.zip').write_bytes(blob)
                row['download_bytes'] = len(blob)
            else:
                blob = paddle.download_bytes(session, result_url, 30)
                data = json.loads(blob)
                md = paddle.download_bytes(session, md_url, 30)
                (case_dir / 'result.json').write_bytes(blob)
                (case_dir / 'result.md').write_bytes(md)
                if isinstance(data, dict) and isinstance(data.get('pages'), list):
                    row['api_output_pages'] = len(data['pages'])
                row['download_bytes'] = len(blob) + len(md)
            row['download_success'] = True
            row['until_download_complete_seconds'] = time.perf_counter() - clock
        except Exception as exc:
            row['failed_stage'] = stage
            row['error_type'] = type(exc).__name__
            row['error'] = clean_error(exc, [token, key, secret, access])
            row['elapsed_until_failure_seconds'] = time.perf_counter() - clock
        row['finished'] = True
        row['completed_at_utc'] = utc()
        save(record, row)
        rows.append(row)
        save(directory / 'summary.json', {'started_at_utc': started, 'updated_at_utc': utc(),
             'summary': summary(rows), 'cases': rows})
        print(f"[{provider}] {'done' if row['download_success'] else 'failed'} "
              f"{source.name}: {row.get('until_success_seconds', row.get('error'))}", flush=True)
    result = {'started_at_utc': started, 'completed_at_utc': utc(),
              'batch_wall_seconds_including_failures_downloads': time.perf_counter() - batch_clock,
              'summary': summary(rows), 'cases': rows}
    save(directory / 'summary.json', result)
    print(json.dumps({'provider': provider, 'summary': result['summary']}, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--poll-interval', type=float, default=1)
    parser.add_argument('--timeout', type=float, default=600)
    parser.add_argument('--provider', choices=['all', 'mineru', 'paddle'], default='all')
    parser.add_argument('--retry-failed', action='store_true', help='Explicitly retry failed parsing cases once; retain prior attempts')
    parser.add_argument('--summarize-only', action='store_true', help='Audit existing outputs and recompute statistics, without API calls')
    args = parser.parse_args()
    if args.summarize_only:
        audit_existing(args.output)
        return
    creds = (os.environ['MINERU_API_TOKEN'], os.environ['BAIDU_OCR_API_KEY'],
             os.environ['BAIDU_OCR_SECRET_KEY'])
    manifest = BASE / 'evaluate/semiconductor-private-final/manifest.json'
    cases = json.loads(manifest.read_text())['cases']
    if len(cases) != 50:
        raise ValueError('Expected frozen 50-case corpus')
    perf_pages = {}
    for archive_path in (BASE / 'runs/半导体场景私有数据集-idc-4.1.14').glob('*.zip'):
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.namelist():
                if member.endswith('_performance.json'):
                    perf_pages[archive_path.name.split('--')[0]] = json.loads(archive.read(member))['page_count']
    for case in cases:
        source = BASE / case['source_file']
        if hashlib.sha256(source.read_bytes()).hexdigest() != case['source_sha256']:
            raise ValueError('Source changed: ' + source.name)
        case['reference_pages'] = perf_pages[source.name]
    args.output.mkdir(parents=True, exist_ok=True)
    experiment = {
        'created_at_utc': utc(), 'corpus_manifest': str(manifest.relative_to(BASE)),
        'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
        'case_count': len(cases), 'moi_reference_pages': sum(c['reference_pages'] for c in cases),
        'concurrency_per_provider': 1, 'providers_run_in_parallel': True,
        'poll_interval_seconds': args.poll_interval, 'automatic_retries': False,
        'auth_excluded': True, 'latency_boundary': 'Before submission/upload URL request to detection of API success',
        'mineru_options': {'model_version': 'vlm', 'language': 'ch', 'enable_table': True, 'enable_formula': True},
        'paddle_options': {'analysis_chart': True, 'merge_tables': True, 'relevel_titles': True,
                          'recognize_seal': True, 'return_span_boxes': True},
        'mineru_endpoint': mineru.API_BASE_URL + '/file-urls/batch',
        'paddle_endpoint': paddle.TASK_URL,
        'note': 'Current hosted APIs, model weights not pinned. API success is not a quality score. Page averages are not individual-page latency.'}
    experiment_path = args.output / 'experiment.json'
    if not experiment_path.exists():
        save(experiment_path, experiment)
    providers = ['mineru', 'paddle'] if args.provider == 'all' else [args.provider]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {name: pool.submit(run_provider, name, cases, args.output,
                                    args.poll_interval, args.timeout, creds, args.retry_failed)
                   for name in providers}
        results = {name: future.result() for name, future in futures.items()}
    # Keep the other provider's completed summary when running only one provider.
    combined = {}
    for name in ['mineru', 'paddle']:
        path = args.output / name / 'summary.json'
        if path.exists():
            combined[name] = json.loads(path.read_text())['summary']
    save(args.output / 'summary.json', combined)


if __name__ == '__main__':
    main()
