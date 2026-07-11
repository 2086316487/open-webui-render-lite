from __future__ import annotations

import importlib
import json
import sys
import types
import unittest
from asyncio import run
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[2]
PROTOCOL_DIR = BACKEND_DIR / 'open_webui' / 'utils' / 'provider_protocols'
FIXTURE_DIR = Path(__file__).resolve().parent / 'fixtures'
PACKAGE_NAME = '_provider_protocols_fixture_test'

package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PROTOCOL_DIR)]
package.__package__ = PACKAGE_NAME
sys.modules[PACKAGE_NAME] = package

constants = importlib.import_module(f'{PACKAGE_NAME}.constants')
common = importlib.import_module(f'{PACKAGE_NAME}.common')
registry = importlib.import_module(f'{PACKAGE_NAME}.registry')


class ProviderProtocolRegistryTests(unittest.TestCase):
    def setUp(self):
        registry._ADAPTER_CACHE.clear()
        sys.modules.pop(f'{PACKAGE_NAME}.openai_chat', None)
        sys.modules.pop(f'{PACKAGE_NAME}.anthropic_messages', None)

    def test_openai_adapter_is_loaded_lazily(self):
        module_name = f'{PACKAGE_NAME}.openai_chat'
        self.assertNotIn(module_name, sys.modules)

        adapter = registry.get_adapter(constants.OPENAI_CHAT_COMPLETIONS)

        self.assertIn(module_name, sys.modules)
        self.assertEqual(adapter.protocol, constants.OPENAI_CHAT_COMPLETIONS)
        self.assertTrue(adapter.capabilities.streaming)
        self.assertTrue(adapter.capabilities.tools)

    def test_openai_chat_fixture_preserves_existing_request_shape(self):
        fixture = json.loads(
            (FIXTURE_DIR / 'openai_chat_request.json').read_text(encoding='utf-8')
        )
        payload = fixture['payload']
        adapter = registry.get_adapter(constants.OPENAI_CHAT_COMPLETIONS)

        request = adapter.build_chat_request(
            base_url=fixture['base_url'],
            payload=payload,
        )

        self.assertEqual(request.method, fixture['expected']['method'])
        self.assertEqual(request.url, fixture['expected']['url'])
        self.assertIs(request.payload, payload)
        self.assertEqual(request.payload, fixture['payload'])

        models_request = adapter.build_models_request(base_url=fixture['base_url'])
        self.assertEqual(models_request.method, 'GET')
        self.assertEqual(models_request.url, f"{fixture['base_url']}/models")
        self.assertIsNone(models_request.payload)

    def test_legacy_protocol_resolution(self):
        cases = [
            (
                {'api_type': 'responses'},
                'https://api.example.com/v1',
                True,
                constants.OPENAI_RESPONSES,
            ),
            ({}, 'https://api.anthropic.com/v1', True, constants.ANTHROPIC_MESSAGES),
            (
                {},
                'https://api.anthropic.com/v1',
                False,
                constants.OPENAI_CHAT_COMPLETIONS,
            ),
            (
                {},
                'https://generativelanguage.googleapis.com/v1beta/openai',
                True,
                constants.OPENAI_CHAT_COMPLETIONS,
            ),
            (
                {},
                'https://generativelanguage.googleapis.com/v1beta',
                True,
                constants.GEMINI_GENERATE_CONTENT,
            ),
            (
                {},
                'https://generativelanguage.googleapis.com/v1beta',
                False,
                constants.OPENAI_CHAT_COMPLETIONS,
            ),
            ({}, 'https://openrouter.ai/api/v1', True, constants.OPENROUTER_CHAT),
            ({}, 'https://api.x.ai/v1', True, constants.XAI_CHAT),
            ({}, 'https://api.example.com/v1', True, constants.OPENAI_CHAT_COMPLETIONS),
        ]

        for config, url, enabled, expected in cases:
            with self.subTest(url=url, enabled=enabled):
                self.assertEqual(
                    registry.resolve_protocol(
                        config,
                        url,
                        native_adapters_enabled=enabled,
                    ),
                    expected,
                )

    def test_explicit_protocol_wins_over_legacy_fields(self):
        protocol = registry.resolve_protocol(
            {
                'protocol': constants.XAI_CHAT,
                'api_type': 'responses',
                'provider': 'anthropic',
            },
            'https://api.anthropic.com/v1',
            native_adapters_enabled=False,
        )
        self.assertEqual(protocol, constants.XAI_CHAT)

    def test_unknown_protocol_is_rejected(self):
        with self.assertRaises(registry.UnsupportedProtocolError):
            registry.resolve_protocol(
                {'protocol': 'unknown_wire_format'},
                'https://api.example.com/v1',
            )

    def test_anthropic_adapter_is_loaded_lazily(self):
        module_name = f'{PACKAGE_NAME}.anthropic_messages'
        self.assertNotIn(module_name, sys.modules)

        adapter = registry.get_adapter(constants.ANTHROPIC_MESSAGES)

        self.assertIn(module_name, sys.modules)
        self.assertEqual(adapter.protocol, constants.ANTHROPIC_MESSAGES)
        self.assertTrue(adapter.capabilities.images)
        self.assertTrue(adapter.capabilities.tools)

    def test_anthropic_adapter_is_not_silently_fallbacked_to_openai(self):
        adapter = registry.get_adapter(constants.ANTHROPIC_MESSAGES)
        self.assertEqual(
            adapter.build_chat_request(
                base_url='https://api.anthropic.com/v1',
                payload={'model': 'claude-test', 'messages': [{'role': 'user', 'content': 'hi'}]},
                api_key='sk-test',
            ).url,
            'https://api.anthropic.com/v1/messages',
        )

    def test_unimplemented_native_adapter_is_not_silently_fallbacked(self):
        with self.assertRaises(registry.ProtocolAdapterUnavailableError):
            registry.get_adapter(constants.GEMINI_GENERATE_CONTENT)

    def test_anthropic_request_fixture_maps_openai_payload(self):
        fixture = json.loads(
            (FIXTURE_DIR / 'anthropic_messages_request.json').read_text(encoding='utf-8')
        )
        adapter = registry.get_adapter(constants.ANTHROPIC_MESSAGES)
        request = adapter.build_chat_request(
            base_url=fixture['base_url'],
            payload=fixture['payload'],
            api_key=fixture['api_key'],
        )

        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.url, fixture['expected_url'])
        self.assertEqual(request.headers, fixture['expected_headers'])
        self.assertEqual(request.payload, fixture['expected_payload'])

    def test_anthropic_response_fixture_normalizes_content_tools_and_usage(self):
        fixture = json.loads(
            (FIXTURE_DIR / 'anthropic_messages_response.json').read_text(encoding='utf-8')
        )
        adapter = registry.get_adapter(constants.ANTHROPIC_MESSAGES)
        normalized = adapter.normalize_response(fixture['response'])

        self.assertEqual(normalized['id'], 'msg_fixture')
        self.assertEqual(normalized['model'], 'claude-test')
        self.assertEqual(normalized['choices'][0]['finish_reason'], 'tool_calls')
        message = normalized['choices'][0]['message']
        self.assertEqual(message['content'], '回答正文')
        self.assertEqual(message['reasoning_content'], '先思考')
        self.assertEqual(message['tool_calls'][0]['function']['name'], 'lookup')
        self.assertEqual(json.loads(message['tool_calls'][0]['function']['arguments']), {'q': '北京'})
        self.assertEqual(normalized['usage'], {'prompt_tokens': 12, 'completion_tokens': 7})

    def test_anthropic_stream_fixture_handles_split_sse_and_tool_deltas(self):
        fixture = json.loads(
            (FIXTURE_DIR / 'anthropic_messages_stream.json').read_text(encoding='utf-8')
        )

        class ByteStream:
            def __init__(self, chunks):
                self.chunks = [chunk.encode() for chunk in chunks]

            def __aiter__(self):
                self._iterator = iter(self.chunks)
                return self

            async def __anext__(self):
                try:
                    return next(self._iterator)
                except StopIteration as exc:
                    raise StopAsyncIteration from exc

        async def collect():
            adapter = registry.get_adapter(constants.ANTHROPIC_MESSAGES)
            return [chunk async for chunk in adapter.stream_response(ByteStream(fixture['chunks']))]

        output = b''.join(run(collect())).decode()
        events = [
            json.loads(line[6:])
            for line in output.splitlines()
            if line.startswith('data: {')
        ]

        self.assertEqual(events[0]['choices'][0]['delta']['role'], 'assistant')
        self.assertTrue(any(event['choices'][0]['delta'].get('reasoning_content') == '思考片段' for event in events))
        self.assertTrue(any('tool_calls' in event['choices'][0]['delta'] for event in events))
        self.assertTrue(any(event['choices'][0]['delta'].get('content') == '正文' for event in events))
        self.assertEqual(events[-1]['choices'][0]['finish_reason'], 'tool_calls')
        self.assertEqual(events[-1]['usage'], {'prompt_tokens': 12, 'completion_tokens': 7})
        self.assertTrue(output.endswith('data: [DONE]\n\n'))


class ProviderProtocolCommonTests(unittest.TestCase):
    def test_finish_reason_and_usage_normalization(self):
        self.assertEqual(common.normalize_finish_reason('tool_use'), 'tool_calls')
        self.assertEqual(common.normalize_finish_reason('max_tokens'), 'length')
        self.assertEqual(
            common.normalize_usage(
                {
                    'input_tokens': 12,
                    'output_tokens': 5,
                    'cache_read_input_tokens': 3,
                }
            ),
            {
                'prompt_tokens': 12,
                'completion_tokens': 5,
                'cache_read_input_tokens': 3,
            },
        )

    def test_error_normalization_keeps_protocol_context(self):
        self.assertEqual(
            common.normalize_error(
                {'error': {'message': 'rate limited', 'type': 'rate_limit'}},
                status=429,
                provider='openrouter',
                protocol=constants.OPENROUTER_CHAT,
            ),
            {
                'error': {
                    'message': 'rate limited',
                    'type': 'rate_limit',
                    'status': 429,
                    'provider': 'openrouter',
                    'protocol': constants.OPENROUTER_CHAT,
                }
            },
        )


if __name__ == '__main__':
    unittest.main()
