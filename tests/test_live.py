"""Test the SSE stream and legacy jobs without llama.cpp."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pdf-live-test-"))
os.environ.setdefault("SYSTEM_PROMPT_FILE", str(Path(__file__).resolve().parents[1] / "config/system_prompt.example.txt"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ModelClient
from document_processor.jobs import JobStore
from document_processor.model_client import ThinkingSplitter


class FakeStream:
    """Simulate a streaming HTTP response."""

    def __init__(self, events):
        """Store events from the fake stream."""
        self.events = events
    def __enter__(self):
        """Return the fake stream when entering a with block."""
        return self
    def __exit__(self, *args):
        """Let exceptions propagate when leaving the with block."""
        return False
    def raise_for_status(self):
        """Simulate a successful HTTP status without raising."""
        return None
    def iter_lines(self, **kwargs):
        """Emit SSE events and their completion marker.

        Accept kwargs used by requests without needing them for this fake
        stream. Return an iterator of bytes.
        """
        for event in self.events:
            yield ('data: ' + json.dumps(event)).encode()
        yield b'data: [DONE]'


def event(delta, finish=None):
    """Create an SSE event with delta and an optional finish reason."""
    return {"choices": [{"delta": delta, "finish_reason": finish}]}


class LiveTests(unittest.TestCase):
    """Verify the SSE stream and legacy databases."""

    def test_tags_can_split_at_any_position(self):
        """Recognize a <think> tag split across stream fragments."""
        for split in range(1, len('<think>') + 1):
            parser = ThinkingSplitter()
            text = 'Before <think>work</think> After'
            for fragment in [text[:7+split], text[7+split:]]:
                parser.feed(fragment)
            parser.finish()
            self.assertEqual(parser.content, 'Before  After')
            self.assertEqual(parser.reasoning, 'work')

    def test_separated_reasoning_field(self):
        """Separate reasoning from final text."""
        client = ModelClient()
        events = [event({'reasoning_content': 'checking'}), event({'content': 'Answer '}),
                  event({'content': 'done'}, finish='stop')]
        preview = []
        with patch.object(client, 'tokenize', return_value=10), \
             patch.object(client, 'model', return_value='qwen'), \
             patch.object(client.session, 'post', return_value=FakeStream(events)):
            result = client.complete('Analyze', 'PDF', max_tokens=50,
                                     on_update=lambda content, reasoning, done: preview.append((content, reasoning, done)))
        self.assertEqual(result, 'Answer done')
        self.assertEqual(preview[-1], ('Answer done', 'checking', True))

    def test_raw_thinking_tags(self):
        """Separate text from reasoning tags."""
        client = ModelClient()
        events = [event({'content': '<thi'}), event({'content': 'nk>working</think>Result'}, finish='stop')]
        preview = []
        with patch.object(client, 'tokenize', return_value=5), \
             patch.object(client, 'model', return_value='qwen'), \
             patch.object(client.session, 'post', return_value=FakeStream(events)):
            result = client.complete('Analyze', 'PDF', 100,
                                     on_update=lambda content, reasoning, done: preview.append((content, reasoning, done)))
        self.assertEqual(result, 'Result')
        self.assertEqual(preview[-1][1], 'working')

    def test_truncated_output_is_not_accepted(self):
        """Reject output truncated by the token limit."""
        client = ModelClient()
        events = [event({'content': 'Incomplete'}, finish='length')]
        with patch.object(client, 'tokenize', return_value=5), \
             patch.object(client, 'model', return_value='qwen'), \
             patch.object(client.session, 'post', return_value=FakeStream(events)):
            with self.assertRaisesRegex(RuntimeError, 'max_tokens'):
                client.complete('Analyze', 'PDF', 100)

    def test_existing_jobs_table_is_migrated(self):
        """Migrate the table while preserving legacy jobs.

        Create a temporary database without the progress column. Verify that
        legacy jobs are presented at 100 percent through the API while the
        stored value and timestamp remain unchanged.
        """
        import app
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app, 'store', JobStore(Path(tmp))):
                with app.store.connect() as con:
                    con.execute("""CREATE TABLE jobs (
                        id TEXT PRIMARY KEY, filename TEXT NOT NULL, question TEXT NOT NULL,
                        status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
                        pages INTEGER DEFAULT 0, total_chunks INTEGER DEFAULT 0, done_chunks INTEGER DEFAULT 0,
                        error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    )""")
                    con.execute("""INSERT INTO jobs
                        (id, filename, question, status, stage, created_at, updated_at)
                        VALUES (?, 'example.pdf', 'Analyze', 'done', 'Analysis completed', ?, ?)""",
                        ('a' * 32, '2025-01-01', '2025-01-01'))
                app.store.initialize()
                with app.store.connect() as con:
                    fields = {r['name'] for r in con.execute('PRAGMA table_info(jobs)')}
                    stored = dict(con.execute('SELECT * FROM jobs').fetchone())
                self.assertIn('progress_percent', fields)
                self.assertEqual(stored['progress_percent'], 0)
                self.assertEqual(stored['updated_at'], '2025-01-01')
                self.assertEqual(app.store.get_job('a' * 32)['progress_percent'], 100)
                client = app.app.test_client()
                self.assertEqual(client.get('/api/jobs').get_json()['jobs'][0]['progress_percent'], 100)
                self.assertEqual(client.get('/api/jobs/' + 'a' * 32).get_json()['progress_percent'], 100)

if __name__ == '__main__':
    unittest.main()
