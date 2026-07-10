from __future__ import annotations

from importlib import import_module
from typing import Any, Mapping
from urllib.parse import urlparse

from .constants import (
    ANTHROPIC_MESSAGES,
    GEMINI_GENERATE_CONTENT,
    KNOWN_PROTOCOLS,
    NATIVE_PROTOCOLS,
    OPENAI_CHAT_COMPATIBLE_PROTOCOLS,
    OPENAI_CHAT_COMPLETIONS,
    OPENAI_RESPONSES,
    OPENROUTER_CHAT,
    RESPONSES_PROTOCOLS,
    XAI_CHAT,
)


class UnsupportedProtocolError(ValueError):
    pass


class ProtocolAdapterUnavailableError(LookupError):
    pass


_ADAPTER_MODULES = {
    OPENAI_CHAT_COMPLETIONS: '.openai_chat',
    OPENROUTER_CHAT: '.openai_chat',
    XAI_CHAT: '.openai_chat',
}
_ADAPTER_CACHE: dict[str, Any] = {}


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or '').lower()
    except ValueError:
        return ''


def resolve_protocol(
    config: Mapping[str, Any] | None,
    url: str,
    *,
    native_adapters_enabled: bool = True,
) -> str:
    config = config or {}
    explicit_protocol = str(config.get('protocol') or '').strip().lower()
    if explicit_protocol:
        if explicit_protocol not in KNOWN_PROTOCOLS:
            raise UnsupportedProtocolError(explicit_protocol)
        return explicit_protocol

    if str(config.get('api_type') or '').strip().lower() == 'responses':
        return OPENAI_RESPONSES

    provider = str(config.get('provider') or '').strip().lower()
    hostname = _hostname(url)
    normalized_url = url.lower().rstrip('/')

    if provider == 'openrouter' or hostname == 'openrouter.ai':
        return OPENROUTER_CHAT
    if provider in {'xai', 'x.ai'} or hostname == 'api.x.ai':
        return XAI_CHAT

    if provider == 'anthropic' or hostname == 'api.anthropic.com':
        return (
            ANTHROPIC_MESSAGES
            if native_adapters_enabled
            else OPENAI_CHAT_COMPLETIONS
        )

    if (
        provider in {'gemini', 'google', 'google-gemini'}
        or hostname == 'generativelanguage.googleapis.com'
    ):
        if normalized_url.endswith('/openai'):
            return OPENAI_CHAT_COMPLETIONS
        return (
            GEMINI_GENERATE_CONTENT
            if native_adapters_enabled
            else OPENAI_CHAT_COMPLETIONS
        )

    return OPENAI_CHAT_COMPLETIONS


def is_native_protocol(protocol: str) -> bool:
    return protocol in NATIVE_PROTOCOLS


def is_responses_protocol(protocol: str) -> bool:
    return protocol in RESPONSES_PROTOCOLS


def is_openai_chat_compatible_protocol(protocol: str) -> bool:
    return protocol in OPENAI_CHAT_COMPATIBLE_PROTOCOLS


def get_adapter(protocol: str):
    if protocol not in KNOWN_PROTOCOLS:
        raise UnsupportedProtocolError(protocol)
    if protocol in _ADAPTER_CACHE:
        return _ADAPTER_CACHE[protocol]

    module_name = _ADAPTER_MODULES.get(protocol)
    if module_name is None:
        raise ProtocolAdapterUnavailableError(protocol)

    module = import_module(module_name, package=__package__)
    adapter = module.get_adapter(protocol)
    _ADAPTER_CACHE[protocol] = adapter
    return adapter
