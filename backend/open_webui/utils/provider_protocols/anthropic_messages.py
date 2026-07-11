from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any, Mapping

from .base import ProtocolCapabilities, UpstreamRequest
from .common import normalize_error, normalize_finish_reason, normalize_usage
from .constants import ANTHROPIC_MESSAGES


_DATA_URL_RE = re.compile(r'^data:([^;,]+);base64,(.*)$', re.DOTALL)


def _copy_cache_control(source: Mapping[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    cache_control = source.get('cache_control')
    if isinstance(cache_control, Mapping):
        target['cache_control'] = dict(cache_control)
    return target


def _image_source(url: str) -> dict[str, Any] | None:
    match = _DATA_URL_RE.match(url)
    if match:
        return {
            'type': 'base64',
            'media_type': match.group(1),
            'data': match.group(2),
        }
    if url.startswith(('http://', 'https://')):
        return {'type': 'url', 'url': url}
    return None


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content else []
    if not isinstance(content, list):
        return [{'type': 'text', 'text': str(content)}] if content is not None else []

    blocks = []
    for part in content:
        if isinstance(part, str):
            if part:
                blocks.append({'type': 'text', 'text': part})
            continue
        if not isinstance(part, Mapping):
            continue

        part_type = part.get('type')
        if part_type in {'text', 'input_text'}:
            blocks.append(
                _copy_cache_control(
                    part,
                    {'type': 'text', 'text': str(part.get('text') or '')},
                )
            )
            continue

        if part_type in {'image_url', 'input_image'}:
            image = part.get('image_url')
            url = image.get('url') if isinstance(image, Mapping) else image
            if isinstance(url, str):
                source = _image_source(url)
                if source:
                    blocks.append(
                        _copy_cache_control(
                            part,
                            {'type': 'image', 'source': source},
                        )
                    )
            continue

        if part_type == 'image' and isinstance(part.get('source'), Mapping):
            blocks.append(
                _copy_cache_control(
                    part,
                    {'type': 'image', 'source': dict(part['source'])},
                )
            )
            continue

        if part_type in {'thinking', 'redacted_thinking'}:
            allowed = {'type': part_type}
            for key in ('thinking', 'signature', 'data'):
                if key in part:
                    allowed[key] = part[key]
            blocks.append(allowed)

    return blocks


def _reasoning_blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    details = message.get('reasoning_details')
    if not isinstance(details, list):
        return []

    def detail_index(item: Mapping[str, Any]) -> int:
        try:
            return int(item.get('index', 0))
        except (TypeError, ValueError):
            return 0

    blocks = []
    for detail in sorted(
        (item for item in details if isinstance(item, Mapping)),
        key=detail_index,
    ):
        detail_type = detail.get('type')
        if detail_type == 'anthropic_thinking':
            text = detail.get('text') or detail.get('thinking') or ''
            signature = detail.get('signature')
            block = {'type': 'thinking', 'thinking': text}
            if signature:
                block['signature'] = signature
            blocks.append(block)
        elif detail_type == 'anthropic_redacted_thinking' and detail.get('data'):
            blocks.append({'type': 'redacted_thinking', 'data': detail['data']})
    return blocks


def _tool_use_blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    blocks = []
    for tool_call in message.get('tool_calls') or []:
        if not isinstance(tool_call, Mapping):
            continue
        function = tool_call.get('function')
        if not isinstance(function, Mapping):
            continue
        arguments = function.get('arguments', '{}')
        if isinstance(arguments, str):
            try:
                tool_input = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                tool_input = {'input': arguments}
        else:
            tool_input = arguments
        if not isinstance(tool_input, Mapping):
            tool_input = {'input': tool_input}
        blocks.append(
            {
                'type': 'tool_use',
                'id': str(tool_call.get('id') or ''),
                'name': str(function.get('name') or ''),
                'input': dict(tool_input),
            }
        )
    return blocks


def _tool_result_block(message: Mapping[str, Any]) -> dict[str, Any]:
    content = _content_blocks(message.get('content'))
    if not content:
        content_value: str | list[dict[str, Any]] = ''
    elif len(content) == 1 and content[0].get('type') == 'text' and 'cache_control' not in content[0]:
        content_value = content[0].get('text', '')
    else:
        content_value = content

    block = {
        'type': 'tool_result',
        'tool_use_id': str(message.get('tool_call_id') or message.get('name') or ''),
        'content': content_value,
    }
    if message.get('is_error'):
        block['is_error'] = True
    return block


def _convert_messages(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    system_blocks = []
    messages = []

    for message in payload.get('messages') or []:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get('role') or 'user')

        if role in {'system', 'developer'}:
            system_blocks.extend(_content_blocks(message.get('content')))
            continue

        if role in {'tool', 'function'}:
            messages.append({'role': 'user', 'content': [_tool_result_block(message)]})
            continue

        if role not in {'user', 'assistant'}:
            role = 'user'

        blocks = []
        if role == 'assistant':
            blocks.extend(_reasoning_blocks(message))
        blocks.extend(_content_blocks(message.get('content')))
        if role == 'assistant':
            blocks.extend(_tool_use_blocks(message))

        if blocks:
            messages.append({'role': role, 'content': blocks})

    return messages, system_blocks


def _convert_tools(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    tools = []
    for tool in payload.get('tools') or []:
        if not isinstance(tool, Mapping):
            continue
        function = tool.get('function') if tool.get('type') == 'function' else tool
        if not isinstance(function, Mapping):
            continue
        converted = {
            'name': str(function.get('name') or ''),
            'description': str(function.get('description') or ''),
            'input_schema': function.get('parameters') or function.get('input_schema') or {'type': 'object'},
        }
        _copy_cache_control(tool, converted)
        tools.append(converted)
    return tools


def _convert_tool_choice(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    choice = payload.get('tool_choice')
    if choice is None:
        return None
    if choice == 'auto':
        converted = {'type': 'auto'}
    elif choice == 'required':
        converted = {'type': 'any'}
    elif choice == 'none':
        converted = {'type': 'none'}
    elif isinstance(choice, Mapping):
        function = choice.get('function')
        if choice.get('type') == 'function' and isinstance(function, Mapping):
            converted = {'type': 'tool', 'name': str(function.get('name') or '')}
        else:
            return None
    else:
        return None

    if payload.get('parallel_tool_calls') is False:
        converted['disable_parallel_tool_use'] = True
    return converted


def _convert_thinking(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    thinking = payload.get('thinking')
    if not isinstance(thinking, Mapping):
        return None
    thinking_type = thinking.get('type')
    if thinking_type == 'disabled':
        return {'type': 'disabled'}
    budget_tokens = thinking.get('budget_tokens')
    if thinking_type == 'enabled' and isinstance(budget_tokens, int) and budget_tokens > 0:
        return {'type': 'enabled', 'budget_tokens': budget_tokens}
    return None


def _build_anthropic_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    messages, system_blocks = _convert_messages(payload)
    max_tokens = payload.get('max_tokens', payload.get('max_completion_tokens', 4096))
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        max_tokens = 4096

    converted = {
        'model': payload.get('model', ''),
        'messages': messages,
        'max_tokens': max_tokens,
    }

    if system_blocks:
        converted['system'] = system_blocks

    for key in ('stream', 'temperature', 'top_p', 'top_k', 'service_tier'):
        if key in payload:
            converted[key] = payload[key]

    stop = payload.get('stop')
    if isinstance(stop, str):
        converted['stop_sequences'] = [stop]
    elif isinstance(stop, list):
        converted['stop_sequences'] = stop

    tools = _convert_tools(payload)
    if tools:
        converted['tools'] = tools

    tool_choice = _convert_tool_choice(payload)
    if tool_choice:
        converted['tool_choice'] = tool_choice

    thinking = _convert_thinking(payload)
    if thinking:
        converted['thinking'] = thinking

    metadata = payload.get('metadata')
    user_id = metadata.get('user_id') if isinstance(metadata, Mapping) else None
    if not user_id:
        user_id = payload.get('user')
    if user_id:
        converted['metadata'] = {'user_id': str(user_id)}

    return converted


def _reasoning_detail(block: Mapping[str, Any], index: int) -> dict[str, Any] | None:
    if block.get('type') == 'thinking':
        detail = {
            'type': 'anthropic_thinking',
            'index': index,
            'text': str(block.get('thinking') or ''),
        }
        if block.get('signature'):
            detail['signature'] = block['signature']
        return detail
    if block.get('type') == 'redacted_thinking' and block.get('data'):
        return {
            'type': 'anthropic_redacted_thinking',
            'index': index,
            'data': block['data'],
        }
    return None


def _normalized_usage(usage: Mapping[str, Any] | None) -> dict[str, int]:
    return normalize_usage(usage)


def _chat_chunk(
    *,
    message_id: str,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    chunk = {
        'id': message_id,
        'object': 'chat.completion.chunk',
        'created': int(time.time()),
        'model': model,
        'choices': [
            {
                'index': 0,
                'delta': delta,
                'finish_reason': finish_reason,
            }
        ],
    }
    if usage:
        chunk['usage'] = usage
    return chunk


def _sse_data(payload: Mapping[str, Any]) -> bytes:
    return ('data: ' + json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n\n').encode()


async def _iter_stream_bytes(stream: Any) -> AsyncIterator[bytes]:
    if hasattr(stream, 'iter_chunks'):
        async for chunk, _ in stream.iter_chunks():
            if chunk:
                yield chunk
        return
    async for chunk in stream:
        if chunk:
            yield chunk


async def _iter_sse_events(stream: Any) -> AsyncIterator[dict[str, Any]]:
    buffer = ''
    async for chunk in _iter_stream_bytes(stream):
        buffer += chunk.decode('utf-8', errors='replace').replace('\r\n', '\n')
        while '\n\n' in buffer:
            raw_event, buffer = buffer.split('\n\n', 1)
            data_lines = []
            for line in raw_event.split('\n'):
                if line.startswith('data:'):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            data = '\n'.join(data_lines)
            if data == '[DONE]':
                return
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


class AnthropicMessagesAdapter:
    protocol = ANTHROPIC_MESSAGES
    capabilities = ProtocolCapabilities(
        model_discovery=True,
        text=True,
        images=True,
        streaming=True,
        tools=True,
        reasoning=True,
    )

    def build_models_request(self, *, base_url: str) -> UpstreamRequest:
        return UpstreamRequest(method='GET', url=f'{base_url.rstrip("/")}/models')

    def normalize_models(self, response: Any) -> Any:
        if not isinstance(response, Mapping):
            return response
        return {
            'object': 'list',
            'data': [
                {
                    'id': model.get('id'),
                    'object': 'model',
                    'created': 0,
                    'owned_by': 'anthropic',
                    'name': model.get('display_name', model.get('id')),
                }
                for model in response.get('data', [])
                if isinstance(model, Mapping)
            ],
        }

    def build_chat_request(
        self,
        *,
        base_url: str,
        payload: dict[str, Any],
        api_key: str | None = None,
    ) -> UpstreamRequest:
        headers = {'anthropic-version': '2023-06-01'}
        if api_key:
            headers['x-api-key'] = api_key
        return UpstreamRequest(
            method='POST',
            url=f'{base_url.rstrip("/")}/messages',
            payload=_build_anthropic_payload(payload),
            headers=headers,
        )

    def normalize_response(self, response: Any) -> Any:
        if not isinstance(response, Mapping):
            return response

        text_parts = []
        tool_calls = []
        reasoning_parts = []
        reasoning_details = []
        for index, block in enumerate(response.get('content') or []):
            if not isinstance(block, Mapping):
                continue
            block_type = block.get('type')
            if block_type == 'text':
                text_parts.append(str(block.get('text') or ''))
            elif block_type == 'tool_use':
                tool_calls.append(
                    {
                        'id': str(block.get('id') or ''),
                        'type': 'function',
                        'function': {
                            'name': str(block.get('name') or ''),
                            'arguments': json.dumps(
                                block.get('input') or {},
                                ensure_ascii=False,
                                separators=(',', ':'),
                            ),
                        },
                    }
                )
            else:
                detail = _reasoning_detail(block, index)
                if detail:
                    reasoning_details.append(detail)
                    if block_type == 'thinking' and block.get('thinking'):
                        reasoning_parts.append(str(block['thinking']))

        message = {'role': 'assistant', 'content': ''.join(text_parts)}
        if tool_calls:
            message['tool_calls'] = tool_calls
        if reasoning_parts:
            message['reasoning_content'] = ''.join(reasoning_parts)
        if reasoning_details:
            message['reasoning_details'] = reasoning_details

        return {
            'id': response.get('id', ''),
            'object': 'chat.completion',
            'created': int(time.time()),
            'model': response.get('model', ''),
            'choices': [
                {
                    'index': 0,
                    'message': message,
                    'finish_reason': normalize_finish_reason(response.get('stop_reason')),
                }
            ],
            'usage': _normalized_usage(response.get('usage')),
        }

    def normalize_stream_event(self, event: Any) -> Any:
        return event

    async def stream_response(self, stream: Any) -> AsyncIterator[bytes]:
        message_id = ''
        model = ''
        usage: dict[str, int] = {}
        tool_indexes: dict[int, int] = {}
        finish_emitted = False

        async for event in _iter_sse_events(stream):
            event_type = event.get('type')
            if event_type == 'message_start':
                message = event.get('message') or {}
                message_id = str(message.get('id') or '')
                model = str(message.get('model') or '')
                usage.update(_normalized_usage(message.get('usage')))
                yield _sse_data(
                    _chat_chunk(
                        message_id=message_id,
                        model=model,
                        delta={'role': 'assistant'},
                    )
                )
                continue

            if event_type == 'content_block_start':
                block_index = int(event.get('index') or 0)
                block = event.get('content_block') or {}
                block_type = block.get('type')
                if block_type == 'tool_use':
                    tool_index = len(tool_indexes)
                    tool_indexes[block_index] = tool_index
                    arguments = block.get('input') or {}
                    arguments_text = '' if not arguments else json.dumps(arguments, ensure_ascii=False, separators=(',', ':'))
                    yield _sse_data(
                        _chat_chunk(
                            message_id=message_id,
                            model=model,
                            delta={
                                'tool_calls': [
                                    {
                                        'index': tool_index,
                                        'id': str(block.get('id') or ''),
                                        'type': 'function',
                                        'function': {
                                            'name': str(block.get('name') or ''),
                                            'arguments': arguments_text,
                                        },
                                    }
                                ]
                            },
                        )
                    )
                elif block_type == 'text' and block.get('text'):
                    yield _sse_data(
                        _chat_chunk(
                            message_id=message_id,
                            model=model,
                            delta={'content': str(block['text'])},
                        )
                    )
                elif block_type in {'thinking', 'redacted_thinking'}:
                    detail = _reasoning_detail(block, block_index)
                    delta = {'reasoning_details': [detail]} if detail else {}
                    if block_type == 'thinking' and block.get('thinking'):
                        delta['reasoning_content'] = str(block['thinking'])
                    if delta:
                        yield _sse_data(
                            _chat_chunk(
                                message_id=message_id,
                                model=model,
                                delta=delta,
                            )
                        )
                continue

            if event_type == 'content_block_delta':
                block_index = int(event.get('index') or 0)
                block_delta = event.get('delta') or {}
                delta_type = block_delta.get('type')
                delta: dict[str, Any] = {}
                if delta_type == 'text_delta':
                    delta['content'] = str(block_delta.get('text') or '')
                elif delta_type == 'thinking_delta':
                    thinking = str(block_delta.get('thinking') or '')
                    delta['reasoning_content'] = thinking
                    delta['reasoning_details'] = [
                        {
                            'type': 'anthropic_thinking',
                            'index': block_index,
                            'text': thinking,
                        }
                    ]
                elif delta_type == 'signature_delta':
                    delta['reasoning_details'] = [
                        {
                            'type': 'anthropic_thinking',
                            'index': block_index,
                            'signature': block_delta.get('signature', ''),
                        }
                    ]
                elif delta_type == 'input_json_delta':
                    tool_index = tool_indexes.get(block_index, len(tool_indexes))
                    delta['tool_calls'] = [
                        {
                            'index': tool_index,
                            'function': {
                                'arguments': str(block_delta.get('partial_json') or ''),
                            },
                        }
                    ]
                if delta:
                    yield _sse_data(
                        _chat_chunk(
                            message_id=message_id,
                            model=model,
                            delta=delta,
                        )
                    )
                continue

            if event_type == 'message_delta':
                usage.update(_normalized_usage(event.get('usage')))
                finish_reason = normalize_finish_reason((event.get('delta') or {}).get('stop_reason'))
                yield _sse_data(
                    _chat_chunk(
                        message_id=message_id,
                        model=model,
                        delta={},
                        finish_reason=finish_reason,
                        usage=usage,
                    )
                )
                finish_emitted = True
                continue

            if event_type == 'error':
                yield _sse_data(self.normalize_error(event.get('error') or event))
                finish_emitted = True
                continue

            if event_type == 'message_stop':
                if not finish_emitted:
                    yield _sse_data(
                        _chat_chunk(
                            message_id=message_id,
                            model=model,
                            delta={},
                            finish_reason='stop',
                            usage=usage,
                        )
                    )
                yield b'data: [DONE]\n\n'
                return

        if not finish_emitted:
            yield _sse_data(
                _chat_chunk(
                    message_id=message_id,
                    model=model,
                    delta={},
                    finish_reason='stop',
                    usage=usage,
                )
            )
        yield b'data: [DONE]\n\n'

    def normalize_error(self, error: Any, *, status: int | None = None) -> dict[str, Any]:
        normalized = normalize_error(
            error,
            status=status,
            provider='anthropic',
            protocol=self.protocol,
        )
        if status in {401, 403}:
            normalized['error']['message'] = 'Anthropic 认证失败，请检查 API Key 和连接权限。'
        elif status == 429:
            normalized['error']['message'] = 'Anthropic 请求过于频繁，请稍后重试。'
        return normalized


def get_adapter(protocol: str) -> AnthropicMessagesAdapter:
    if protocol != ANTHROPIC_MESSAGES:
        raise ValueError(f'Unsupported Anthropic protocol: {protocol}')
    return AnthropicMessagesAdapter()
