from __future__ import annotations

from argparse import Namespace
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from astra.runners.linux_terminal_bench import langfuse_import as importer
from astra.runners.linux_terminal_bench.tests import test_results


class LangfuseImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        jobs = self.root / 'pi/jobs'
        test_results.LinuxResultTests._write_result(jobs, multiplier=1.0, with_ctrf=True,
                                      trial='trial', finished_at='2026-01-01T00:00:01Z')
        self.path = jobs / 'job/trial/result.json'
        self.result = json.loads(self.path.read_text())
        self.record = {
            'record_id': 'pi/trial', 'agent': {'product': 'pi', 'model_name': 'zai/glm-5.2'},
            'benchmark': {'task_id': 'task'}, 'instruction': 'Run a command',
            'trial': {'started_at': '2026-01-01T00:00:00Z',
                      'finished_at': '2026-01-01T00:00:01Z', 'attempt_index': 1},
            'usage': {'input_tokens': None}, 'timing': {'total_seconds': 1},
            'quality': {'tier': 'complete'},
            'source': {'trial_path': 'pi/jobs/job/trial'},
            'messages': [
                {'role': 'user', 'content': 'Run a command'},
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'call1', 'name': 'bash', 'arguments_json': '{"command":"pwd"}'}]},
                {'role': 'tool', 'content': '/app', 'tool_call_id': 'call1', 'tool_name': 'bash'},
            ],
        }
        for seq, msg in enumerate(self.record['messages']):
            msg.update(seq=seq, timestamp=None, is_error=False, internal_content_omitted=False)

    def item(self, selected=True):
        return importer.make_item(self.record, self.result, self.path,
                                  self.path if selected else None, 'batch')

    def test_missing_values_and_tool_pair_survive_conversion(self):
        item = self.item()
        span = item['payload']['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
        attrs = {a['key']: a['value'] for a in span['attributes']}
        usage = json.loads(attrs['langfuse.trace.metadata.usage']['stringValue'])
        self.assertIsNone(usage['input_tokens'])
        output = json.loads(attrs['langfuse.observation.output']['stringValue'])
        self.assertEqual(output[1]['tool_calls'][0]['id'], output[2]['tool_call_id'])
        self.assertEqual(output[1]['tool_calls'][0]['function']['arguments'], '{"command":"pwd"}')
        self.assertEqual(int(span['endTimeUnixNano']) - int(span['startTimeUnixNano']), 10**9)
        self.assertEqual(attrs['langfuse.observation.type']['stringValue'], 'agent')
        self.assertNotIn('langfuse.observation.usage_details', attrs)

    def test_runner_overrides_cleaner_validity_and_does_not_make_zero_score(self):
        (self.path.parent / 'verifier/ctrf.json').unlink()
        item = self.item()
        self.assertIsNone(item['reward'])
        self.assertEqual(item['verifier_status'], 'verifier_infra_failure')
        self.assertEqual(item['scores'], [])

    def test_only_selected_attempt_has_latest_score(self):
        self.assertEqual([s['name'] for s in self.item()['scores']],
                         ['runner_reward', 'runner_reward_latest'])
        self.assertEqual([s['name'] for s in self.item(False)['scores']], ['runner_reward'])

    def test_metadata_only_latest_does_not_promote_old_attempt(self):
        test_results.LinuxResultTests._write_result(self.root / 'pi/jobs', multiplier=1.0, with_ctrf=True,
                                      trial='metadata-only', finished_at='2026-01-02T00:00:00Z')
        cleaner = importer.load_cleaner()
        with patch.object(cleaner, 'clean_product', return_value=([self.record], {})), \
                patch.object(cleaner, 'dataset_revision', return_value=None):
            bundle = self.root / 'bundle.jsonl'
            importer.prepare(Namespace(source=self.root, products=['pi'], output=bundle))
        report, items = importer.read_bundle(bundle)
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]['runner_selected_latest'])
        self.assertEqual(report['products']['pi']['selected_valid_traces'], 0)

    def test_retry_reuses_observation_and_score_ids_and_readback_checks_output(self):
        item = self.item()
        bundle = self.root / 'bundle.jsonl'
        bundle.write_text(importer.dump({'report': {'import_batch': 'batch'}}) + '\n' + importer.dump(item) + '\n')
        calls = []
        def request(method, path, body=None):
            calls.append((method, path, copy.deepcopy(body)))
            if method == 'POST':
                return {}
            if 'observations?' in path:
                return {'data': [{'id': item['span_id'], 'output': importer.chat_messages(self.record['messages']),
                                  'startTime': self.record['trial']['started_at'],
                                  'endTime': self.record['trial']['finished_at'],
                                  'metadata': {'usage': self.record['usage'], 'timing': self.record['timing'],
                                               'reward': item['reward'], 'runner_selected_latest': True,
                                               'import_batch': 'batch', 'verifier_passed': None, 'verifier_failed': None}}]}
            return {'data': item['scores']}
        with patch.object(importer, 'Client') as client:
            client.return_value.request.side_effect = request
            importer.upload(Namespace(bundle=bundle))
            first = copy.deepcopy(calls)
            calls.clear()
            importer.upload(Namespace(bundle=bundle))
            self.assertEqual(first, calls)
            args = Namespace(bundle=bundle, api_version='v4', report=self.root / 'verify.json')
            self.assertEqual(importer.verify(args), 0)
            self.record['messages'][0]['content'] = 'truncated or corrupted'
            self.assertEqual(importer.verify(args), 1)

    def test_partial_otlp_success_is_failure(self):
        with patch.dict('os.environ', {'LANGFUSE_BASE_URL': 'http://localhost:3000',
                                     'LANGFUSE_PUBLIC_KEY': 'public', 'LANGFUSE_SECRET_KEY': 'secret'}):
            client = importer.Client()
        with patch.object(client.opener, 'open', return_value=io.BytesIO(
                b'{"partialSuccess":{"rejectedSpans":"1"}}')):
            with self.assertRaisesRegex(RuntimeError, 'partial success'):
                client.request('POST', '/api/public/otel/v1/traces', {})

    def test_transient_failure_retries_but_auth_failure_does_not(self):
        with patch.dict('os.environ', {'LANGFUSE_BASE_URL': 'http://localhost:3000',
                                     'LANGFUSE_PUBLIC_KEY': 'public', 'LANGFUSE_SECRET_KEY': 'secret'}):
            client = importer.Client()
        error = HTTPError('url', 503, 'unavailable', {}, None)
        with patch.object(client.opener, 'open', side_effect=[error, io.BytesIO(b'{}')]) as call, \
                patch.object(importer.time, 'sleep'):
            self.assertEqual(client.request('POST', '/api/public/otel/v1/traces', {}), {})
            self.assertEqual(call.call_count, 2)
        with patch.object(client.opener, 'open', side_effect=[ConnectionResetError(), io.BytesIO(b'{}')]), \
                patch.object(importer.time, 'sleep'):
            self.assertEqual(client.request('POST', '/api/public/otel/v1/traces', {}), {})
        error = HTTPError('url', 401, 'unauthorized', {}, None)
        with patch.object(client.opener, 'open', side_effect=error) as call:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 401'):
                client.request('POST', '/api/public/otel/v1/traces', {})
            self.assertEqual(call.call_count, 1)

    def test_naive_harbor_timestamps_are_utc(self):
        self.assertEqual(importer.nanos('2026-01-01T00:00:00.123456'),
                         importer.nanos('2026-01-01T00:00:00.123456Z'))


if __name__ == '__main__':
    unittest.main()
