from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ProtocolCapabilities:
    model_discovery: bool
    text: bool
    images: bool
    streaming: bool
    tools: bool
    reasoning: bool


@dataclass(frozen=True, slots=True)
class UpstreamRequest:
    method: str
    url: str
    payload: dict[str, Any] | None = None
    headers: dict[str, str] | None = None


class ProviderProtocolAdapter(Protocol):
    protocol: str
    capabilities: ProtocolCapabilities

    def build_models_request(self, *, base_url: str) -> UpstreamRequest: ...

    def normalize_models(self, response: Any) -> Any: ...

    def build_chat_request(
        self,
        *,
        base_url: str,
        payload: dict[str, Any],
    ) -> UpstreamRequest: ...

    def normalize_response(self, response: Any) -> Any: ...

    def normalize_stream_event(self, event: Any) -> Any: ...

    def normalize_error(self, error: Any, *, status: int | None = None) -> dict[str, Any]: ...
