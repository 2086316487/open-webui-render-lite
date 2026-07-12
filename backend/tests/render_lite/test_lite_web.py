from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


class _DummyClientResponse:
    pass


aiohttp_stub = types.ModuleType('aiohttp')
aiohttp_stub.ClientResponse = _DummyClientResponse
aiohttp_stub.ClientSession = object
aiohttp_stub.ClientTimeout = lambda **kwargs: kwargs
aiohttp_stub.ClientError = Exception
aiohttp_stub.ClientResponseError = Exception
sys.modules.setdefault('aiohttp', aiohttp_stub)

bs4_stub = types.ModuleType('bs4')
bs4_stub.BeautifulSoup = object
sys.modules.setdefault('bs4', bs4_stub)

BACKEND_DIR = Path(__file__).resolve().parents[2]
MODULE_PATH = BACKEND_DIR / 'open_webui' / 'utils' / 'lite_web.py'
SPEC = importlib.util.spec_from_file_location('_lite_web_test', MODULE_PATH)
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
assert SPEC.loader is not None
SPEC.loader.exec_module(module)


class LiteWebTests(unittest.TestCase):
    def test_rejects_private_and_non_http_urls(self):
        rejected = [
            'file:///etc/passwd',
            'http://localhost/test',
            'http://127.0.0.1/',
            'http://10.0.0.1/',
            'http://169.254.169.254/latest/meta-data/',
            'http://[::1]/',
            'http://user:pass@example.com/',
        ]
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(module.LiteWebError):
                module.validate_public_url(url)

    def test_accepts_public_http_urls(self):
        self.assertEqual(module.validate_public_url('https://example.com/a')[1], 'example.com')
        self.assertEqual(module.validate_public_url('http://8.8.8.8/')[1], '8.8.8.8')

    def test_provider_payloads_are_normalized_and_limited(self):
        fixtures = {
            'tavily': {'results': [{'url': 'https://example.com/t', 'title': 'T', 'content': 'ts'}]},
            'brave': {'web': {'results': [{'url': 'https://example.com/b', 'title': 'B', 'description': 'bs'}]}},
            'bing': {'webPages': {'value': [{'url': 'https://example.com/i', 'name': 'I', 'snippet': 'is'}]}},
            'serper': {'organic': [{'link': 'https://example.com/s2', 'title': 'S2', 'snippet': '2', 'position': 2}, {'link': 'https://example.com/s1', 'title': 'S1', 'snippet': '1', 'position': 1}]},
            'searxng': {'results': [{'url': 'https://example.com/x1', 'title': 'X1', 'content': '1', 'score': 1}, {'url': 'https://example.com/x2', 'title': 'X2', 'content': '2', 'score': 2}]},
        }
        for engine, payload in fixtures.items():
            with self.subTest(engine=engine):
                results = module.normalize_provider_payload(engine, payload, 1)
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0].link.startswith('https://example.com/'))
        self.assertEqual(
            module.normalize_provider_payload('serper', fixtures['serper'], 1)[0].title,
            'S1',
        )
        self.assertEqual(
            module.normalize_provider_payload('searxng', fixtures['searxng'], 1)[0].title,
            'X2',
        )

    def test_duplicate_and_private_results_are_removed(self):
        payload = {
            'results': [
                {'url': 'http://127.0.0.1/private', 'title': 'private'},
                {'url': 'https://example.com/a', 'title': 'A'},
                {'url': 'https://example.com/a', 'title': 'duplicate'},
            ]
        }
        results = module.normalize_provider_payload('tavily', payload, 10)
        self.assertEqual([item.title for item in results], ['A'])


if __name__ == '__main__':
    unittest.main()
