import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
os.environ['DATA_DIR']=tempfile.mkdtemp(prefix='pdf-test-')
os.environ['LLAMA_API_KEY']='mock'
os.environ['SYSTEM_PROMPT_FILE']=str(Path(__file__).resolve().parents[1]/'config/system_prompt.example.txt')
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import split_with_page, make_chunks, atomic_text, extract_pdf, init_db, get_job, ModelClient, output_language_instruction

class MockTokenizer:
    def tokenize(self, text):
        return len(text.split())

class TestLogic(unittest.TestCase):
    def test_page_split_preserves_text(self):
        original=' '.join('mot' for _ in range(1800))
        pieces=split_with_page(original, 7, limit=500)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(x['page']==7 for x in pieces))
        self.assertEqual(' '.join(x['text'] for x in pieces).split(), original.split())
    def test_chunk_count(self):
        pages=[{'page':1,'text':' '.join('pomme' for _ in range(5500))},
               {'page':2,'text':' '.join('poire' for _ in range(700))}]
        chunks=make_chunks(pages, MockTokenizer())
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(MockTokenizer().tokenize(x['text'])<=5500 for x in chunks))
        self.assertTrue(any(2 in x['pages'] for x in chunks))
    def test_atomic_file(self):
        d=Path(os.environ['DATA_DIR'])/'unit.md'
        atomic_text(d, 'bonjour')
        self.assertEqual(d.read_text(), 'bonjour')
    def test_output_language_is_configurable(self):
        with patch('app.OUTPUT_LANGUAGE', 'auto'):
            self.assertIn("same language", output_language_instruction())
        with patch('app.OUTPUT_LANGUAGE', 'French'):
            self.assertEqual(output_language_instruction(), 'Write in French.')

    def test_llama_api_key_is_optional(self):
        with patch('app.KEY', ''):
            client = ModelClient()
            self.assertNotIn('Authorization', client.session.headers)
        with patch('app.KEY', 'test-token'):
            client = ModelClient()
            self.assertEqual(client.session.headers['Authorization'], 'Bearer test-token')

    def test_completion_budget_fails_before_api_call(self):
        with patch('app.KEY', ''), patch('app.CONTEXT_TOKENS', 1000):
            client = ModelClient()
            with patch.object(client, 'tokenize', return_value=800), \
                 patch.object(client, 'json_request', side_effect=AssertionError('unexpected API call')):
                with self.assertRaisesRegex(ValueError, 'configured context'):
                    client.complete('Analyze', 'example text', max_tokens=500)

    def test_db(self):
        init_db()
        self.assertIsNone(get_job('none'))

if __name__=='__main__':
    unittest.main()
