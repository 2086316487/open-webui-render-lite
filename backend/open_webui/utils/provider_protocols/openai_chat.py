from __future__ import annotations

from typing import Any

from .base import ProtocolCapabilities, UpstreamRequest
from .common import normalize_error
from .constants import OPENAI_CHAT_COMPATIBLE_PROTOCOLS


class OpenAIChatAdapter:
    capabilities = ProtocolCapabilities(
        model_discovery=True,
        text=True,
        images=True,
        streaming=True,
        tools=True,
        reasoning=True,
    )

    def __init__(self, protocol: str):
        self.protocol = protocol

    def build_models_request(self, *, base_url: str) -> UpstreamRequest:
        return UpstreamRequest(method='GET', url=f'{base_url}/models')

    def normalize_models(self, response: Any) -> Any:
        return response

    def build_chat_request(
        self,
        *,
        base_url: str,
        payload: dict[str, Any],
    ) -> UpstreamRequest:
        return UpstreamRequest(
            method='POST',
            url=f'{base_url}/chat/completions',
            payload=payload,
        )

    def normalize_response(self, response: Any) -> Any:
        return response

    def normalize_stream_event(self, event: Any) -> Any:
        return event

    def normalize_error(self, error: Any, *, status: int | None = None) -> dict[str, Any]:
        return normalize_error(error, status=status, protocol=self.protocol)


def get_adapter(protocol: str) -> OpenAIChatAdapter:
    if protocol not in OPENAI_CHAT_COMPATIBLE_PROTOCOLS:
        raise ValueError(f'Unsupported OpenAI-compatible chat protocol: {protocol}')
    return OpenAIChatAdapter(protocol)
