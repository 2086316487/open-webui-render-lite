from __future__ import annotations

from typing import Any, Mapping


_FINISH_REASON_MAP = {
    'end_turn': 'stop',
    'stop_sequence': 'stop',
    'max_tokens': 'length',
    'max_output_tokens': 'length',
    'tool_use': 'tool_calls',
    'function_call': 'tool_calls',
    'content_filter': 'content_filter',
    'safety': 'content_filter',
}


def normalize_finish_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    normalized = reason.strip().lower()
    return _FINISH_REASON_MAP.get(normalized, normalized)


def normalize_usage(usage: Mapping[str, Any] | None) -> dict[str, int]:
    if not usage:
        return {}

    aliases = {
        'prompt_tokens': ('prompt_tokens', 'input_tokens', 'promptTokenCount'),
        'completion_tokens': ('completion_tokens', 'output_tokens', 'candidatesTokenCount'),
        'total_tokens': ('total_tokens', 'totalTokenCount'),
        'cache_read_input_tokens': ('cache_read_input_tokens', 'cache_read_tokens'),
        'cache_creation_input_tokens': ('cache_creation_input_tokens', 'cache_write_tokens'),
    }

    normalized = {}
    for target, source_names in aliases.items():
        for source_name in source_names:
            value = usage.get(source_name)
            if isinstance(value, int) and not isinstance(value, bool):
                normalized[target] = value
                break
    return normalized


def normalize_error(
    error: Any,
    *,
    status: int | None = None,
    provider: str | None = None,
    protocol: str | None = None,
) -> dict[str, Any]:
    message = '上游服务请求失败'
    error_type = None
    code = None

    if isinstance(error, Mapping):
        nested = error.get('error')
        source = nested if isinstance(nested, Mapping) else error
        message = str(source.get('message') or source.get('detail') or message)
        error_type = source.get('type')
        code = source.get('code')
    elif error:
        message = str(error)

    normalized_error = {'message': message}
    if error_type is not None:
        normalized_error['type'] = error_type
    if code is not None:
        normalized_error['code'] = code
    if status is not None:
        normalized_error['status'] = status
    if provider:
        normalized_error['provider'] = provider
    if protocol:
        normalized_error['protocol'] = protocol

    return {'error': normalized_error}
