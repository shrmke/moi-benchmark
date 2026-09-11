import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import clean_astra_supplements as cleaner


class SupplementTests(unittest.TestCase):
    def test_redaction_preserves_nested_arguments(self):
        value = {'items': [1, {'OPENAI_API_KEY': 'private-key-example', 'command': 'echo ok'}],
                 'thinking': 'private-reasoning',
                 'image': {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,QUJD'}}}
        text, hidden = cleaner.public_content(json.dumps(value), cleaner.SupplementRedactor())
        decoded = json.loads(text)
        self.assertTrue(hidden)
        for private in ('private-key-example', 'private-reasoning', 'QUJD'):
            self.assertNotIn(private, text)
        self.assertEqual(decoded['items'][0], 1)
        self.assertEqual(decoded['items'][1]['command'], 'echo ok')

    def test_text_credentials_and_user_paths(self):
        redactor = cleaner.SupplementRedactor()
        text = redactor.text('ZAI_API_KEY=private-example Authorization: Bearer bearer-example /home/alice/file')
        for private in ('private-example', 'bearer-example', '/home/alice'):
            self.assertNotIn(private, text)

    def test_database_order_and_tool_pairing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session_transcript_items.jsonl'
            rows = [
                {'item_seq': 3, 'role': 'tool', 'content': 'ok', 'payload_json': json.dumps(
                    {'tool_result': {'tool_use_id': 'call-1', 'name': 'bash', 'status': 'error'}})},
                {'item_seq': 1, 'role': 'user', 'content': 'task'},
                {'item_seq': 2, 'role': 'assistant', 'content': '', 'payload_json': json.dumps(
                    {'tool_calls': [{'tool_use_id': 'call-1', 'name': 'bash', 'arguments': '{"command":"pwd"}'}]})},
            ]
            path.write_text('\n'.join(json.dumps(row) for row in rows))
            messages, _ = cleaner.transcript(path, cleaner.SupplementRedactor())
            self.assertEqual([m['role'] for m in messages], ['user', 'assistant', 'tool'])
            self.assertEqual(messages[1]['tool_calls'][0]['id'], messages[2]['tool_call_id'])
            self.assertTrue(messages[2]['is_error'])
            self.assertEqual(json.loads(messages[1]['tool_calls'][0]['arguments_json']), {'command': 'pwd'})

    def test_native_snapshot_is_not_concatenated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'native-artifact/conversation_log.jsonl'
            path.parent.mkdir()
            message = [{'role': 'user', 'content': 'task'}, {'role': 'assistant', 'content': 'answer'}]
            path.write_text('\n'.join(json.dumps({'messages': message}) for _ in range(2)))
            messages, _ = cleaner.native_transcript(root, cleaner.SupplementRedactor())
            self.assertEqual(len(messages), 2)


if __name__ == '__main__':
    unittest.main()
