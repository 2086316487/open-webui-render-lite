from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import aiohttp
from bs4 import BeautifulSoup


ALLOWED_SCHEMES = {'http', 'https'}
TEXT_CONTENT_TYPES = ('text/html', 'text/plain', 'text/markdown', 'application/xhtml+xml')
METADATA_HOSTS = {'metadata.google.internal'}
MAX_SEARCH_RESULTS = 10
TRACKING_QUERY_KEYS = {'fbclid', 'gclid', 'dclid', 'msclkid', 'mc_cid', 'mc_eid'}
LOW_QUALITY_MARKERS = ('captcha', 'verify you are human', 'access denied', 'sign in', 'log in')


class LiteTTLCache:
    def __init__(self, max_entries: int = 64, ttl_seconds: int = 600):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= time.monotonic():
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        return deepcopy(value)

    def set(self, key: str, value: Any) -> None:
        self._items[key] = (time.monotonic() + self.ttl_seconds, deepcopy(value))
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)


SEARCH_CACHE = LiteTTLCache()
DOCUMENT_CACHE = LiteTTLCache()


def _cache_key(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def normalize_url(url: str) -> str:
    parsed = urlparse((url or '').strip())
    hostname = (parsed.hostname or '').lower().rstrip('.')
    port = parsed.port
    netloc = hostname
    if port and not ((parsed.scheme == 'http' and port == 80) or (parsed.scheme == 'https' and port == 443)):
        netloc = f'{hostname}:{port}'
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
         if not key.lower().startswith('utm_') and key.lower() not in TRACKING_QUERY_KEYS],
        doseq=True,
    )
    return urlunparse((parsed.scheme.lower(), netloc, parsed.path or '/', '', query, ''))


def is_low_quality_document(title: str, content: str) -> bool:
    sample = f'{title}\n{content[:1000]}'.casefold()
    return len(content.strip()) < 80 or any(marker in sample for marker in LOW_QUALITY_MARKERS)


class LiteWebError(ValueError):
    pass


@dataclass(slots=True)
class LiteSearchResult:
    link: str
    title: str
    snippet: str
    rank: int = 0
    domain: str = ''

    def as_dict(self) -> dict[str, Any]:
        return {
            'link': self.link,
            'title': self.title,
            'snippet': self.snippet,
            'rank': self.rank,
            'domain': self.domain,
        }


@dataclass(slots=True)
class LiteWebDocument:
    url: str
    title: str
    content: str
    truncated: bool = False
    content_mode: str = 'body'

    def as_doc(self) -> dict[str, Any]:
        return {
            'content': self.content,
            'metadata': {
                'source': self.url,
                'title': self.title or self.url,
                'link': self.url,
                'truncated': self.truncated,
                'domain': urlparse(self.url).hostname or '',
                'content_mode': self.content_mode,
                'lite_web_context': True,
            },
        }


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split('%', 1)[0])
    except ValueError:
        return False
    return bool(address.is_global)


def validate_public_url(url: str) -> tuple[str, str]:
    value = (url or '').strip()
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise LiteWebError('网址格式无效。') from exc

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise LiteWebError('只支持 HTTP 或 HTTPS 网址。')
    if not parsed.hostname:
        raise LiteWebError('网址缺少有效域名。')
    if parsed.username or parsed.password:
        raise LiteWebError('网址中不能包含用户名或密码。')
    if port is not None and not 1 <= port <= 65535:
        raise LiteWebError('网址端口无效。')

    hostname = parsed.hostname.rstrip('.').lower()
    if hostname == 'localhost' or hostname.endswith('.localhost') or hostname in METADATA_HOSTS:
        raise LiteWebError('不允许访问本机、内网或云平台元数据地址。')
    try:
        literal_address = ipaddress.ip_address(hostname.split('%', 1)[0])
    except ValueError:
        literal_address = None
    if literal_address is not None and not literal_address.is_global:
        raise LiteWebError('不允许访问本机、内网或保留地址。')
    return value, hostname


async def resolve_public_host(hostname: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LiteWebError('无法解析网址域名。') from exc

    addresses = list(dict.fromkeys(item[4][0] for item in infos))
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise LiteWebError('不允许访问解析到本机、内网或保留地址的网址。')
    return addresses


def _peer_ip(response: aiohttp.ClientResponse) -> str | None:
    connection = response.connection
    transport = connection.transport if connection else None
    if transport is None:
        protocol = getattr(response, '_protocol', None)
        transport = getattr(protocol, 'transport', None)
    peer = transport.get_extra_info('peername') if transport else None
    return str(peer[0]) if peer else None


def validate_response_peer(response: aiohttp.ClientResponse) -> None:
    peer_ip = _peer_ip(response)
    if not peer_ip or not _is_public_ip(peer_ip):
        raise LiteWebError('网络请求连接到了不允许访问的地址。')


def reject_provider_redirect(response: aiohttp.ClientResponse) -> None:
    if 300 <= response.status < 400:
        raise LiteWebError('联网搜索服务返回了不允许的重定向。')


async def validate_public_endpoint(url: str) -> None:
    _, hostname = validate_public_url(url)
    parsed = urlparse(url)
    await resolve_public_host(hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))


def extract_readable_text(payload: bytes, content_type: str, fallback_title: str, max_chars: int) -> tuple[str, str, bool]:
    text = payload.decode('utf-8', errors='replace')
    if 'html' in content_type.lower():
        soup = BeautifulSoup(text, 'html.parser')
        title = soup.title.get_text(' ', strip=True) if soup.title else fallback_title
        for tag in soup(['script', 'style', 'noscript', 'svg', 'canvas', 'form', 'nav', 'footer', 'header']):
            tag.decompose()
        text = soup.get_text('\n')
    else:
        title = fallback_title

    lines = []
    for line in text.splitlines():
        normalized = re.sub(r'\s+', ' ', line).strip()
        if normalized and (not lines or normalized != lines[-1]):
            lines.append(normalized)
    content = '\n'.join(lines).strip()
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars].rstrip() + '\n[网页正文已按长度上限截断]'
    return title[:500], content, truncated


async def fetch_web_document(
    url: str,
    *,
    session: aiohttp.ClientSession,
    timeout_seconds: int = 15,
    max_bytes: int = 2 * 1024 * 1024,
    max_chars: int = 12000,
    max_redirects: int = 3,
) -> LiteWebDocument:
    validate_public_url(url)
    cache_key = _cache_key('document', normalize_url(url), max_chars)
    cached = DOCUMENT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    current_url = url
    for redirect_index in range(max_redirects + 1):
        current_url, hostname = validate_public_url(current_url)
        parsed = urlparse(current_url)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        await resolve_public_host(hostname, port)

        try:
            async with session.get(
                current_url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                headers={'Accept': 'text/html,text/plain,text/markdown;q=0.9,*/*;q=0.1'},
            ) as response:
                validate_response_peer(response)

                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get('Location')
                    if not location:
                        raise LiteWebError('网页重定向缺少目标地址。')
                    if redirect_index >= max_redirects:
                        raise LiteWebError('网页重定向次数超过上限。')
                    current_url = urljoin(current_url, location)
                    continue

                if response.status >= 400:
                    raise LiteWebError(f'网页返回 HTTP {response.status}。')
                content_type = response.headers.get('Content-Type', '').split(';', 1)[0].lower()
                if content_type and not content_type.startswith(TEXT_CONTENT_TYPES):
                    raise LiteWebError('该网址返回的内容不是可读取的网页或文本。')
                content_length = response.content_length
                if content_length is not None and content_length > max_bytes:
                    raise LiteWebError('网页内容超过 2MB 限制。')

                chunks = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        raise LiteWebError('网页内容超过 2MB 限制。')

                title, content, truncated = extract_readable_text(
                    bytes(chunks), content_type, hostname, max_chars
                )
                if not content:
                    raise LiteWebError('网页中没有提取到可读文字。')
                if is_low_quality_document(title, content):
                    raise LiteWebError('The page body is too short or requires authentication.')
                document = LiteWebDocument(normalize_url(str(response.url)), title, content, truncated)
                DOCUMENT_CACHE.set(cache_key, document)
                return document
        except asyncio.TimeoutError as exc:
            raise LiteWebError('网页读取超时。') from exc
        except aiohttp.ClientError as exc:
            raise LiteWebError('网页连接失败。') from exc

    raise LiteWebError('网页重定向次数超过上限。')


def _normalized_results(items: list[dict[str, Any]], count: int) -> list[LiteSearchResult]:
    results = []
    seen = set()
    domain_counts: dict[str, int] = {}
    for rank, item in enumerate(items, start=1):
        link = str(item.get('link') or item.get('url') or '').strip()
        if not link:
            continue
        try:
            validate_public_url(link)
        except LiteWebError:
            continue
        link = normalize_url(link)
        domain = (urlparse(link).hostname or '').lower()
        if link in seen or domain_counts.get(domain, 0) >= 2:
            continue
        seen.add(link)
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        results.append(
            LiteSearchResult(
                link=link,
                title=str(item.get('title') or item.get('name') or link).strip()[:500],
                snippet=str(item.get('snippet') or item.get('description') or item.get('content') or '').strip()[:4000],
                rank=rank,
                domain=domain,
            )
        )
        if len(results) >= min(max(count, 1), MAX_SEARCH_RESULTS):
            break
    return results


def normalize_provider_payload(engine: str, payload: dict[str, Any], count: int) -> list[LiteSearchResult]:
    engine = (engine or '').strip().lower()
    if engine == 'tavily':
        items = payload.get('results', [])
    elif engine == 'brave':
        items = payload.get('web', {}).get('results', [])
    elif engine == 'serper':
        items = sorted(payload.get('organic', []), key=lambda item: item.get('position', 0))
    elif engine == 'bing':
        items = payload.get('webPages', {}).get('value', [])
    elif engine == 'searxng':
        items = sorted(payload.get('results', []), key=lambda item: item.get('score', 0), reverse=True)
    else:
        raise LiteWebError('当前 Render lite 仅支持 Tavily、Brave、Bing、Serper 和 SearXNG 搜索。')
    return _normalized_results(items, count)


async def search_web_provider(
    engine: str,
    query: str,
    count: int,
    config: dict[str, Any],
    *,
    session: aiohttp.ClientSession,
    timeout_seconds: int = 15,
) -> list[LiteSearchResult]:
    engine = (engine or '').strip().lower()
    count = min(max(int(count or 5), 1), MAX_SEARCH_RESULTS)
    cache_key = _cache_key('search', engine, query.strip().casefold(), count)
    cached = SEARCH_CACHE.get(cache_key)
    if cached is not None:
        return cached
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    def cache_results(payload: dict[str, Any]) -> list[LiteSearchResult]:
        results = normalize_provider_payload(engine, payload, count)
        SEARCH_CACHE.set(cache_key, results)
        return results

    if engine == 'tavily':
        key = str(config.get('TAVILY_API_KEY') or '')
        if not key:
            raise LiteWebError('Tavily API Key 尚未配置。')
        async with session.post(
            'https://api.tavily.com/search',
            headers={'Authorization': f'Bearer {key}'},
            json={'query': query, 'max_results': count, 'include_answer': False},
            timeout=timeout,
            allow_redirects=False,
        ) as response:
            validate_response_peer(response)
            reject_provider_redirect(response)
            response.raise_for_status()
            payload = await response.json()
        return cache_results(payload)

    if engine == 'brave':
        key = str(config.get('BRAVE_SEARCH_API_KEY') or '')
        if not key:
            raise LiteWebError('Brave Search API Key 尚未配置。')
        async with session.get(
            'https://api.search.brave.com/res/v1/web/search',
            headers={'X-Subscription-Token': key, 'Accept': 'application/json'},
            params={'q': query, 'count': count},
            timeout=timeout,
            allow_redirects=False,
        ) as response:
            validate_response_peer(response)
            reject_provider_redirect(response)
            response.raise_for_status()
            payload = await response.json()
        return cache_results(payload)

    if engine == 'serper':
        key = str(config.get('SERPER_API_KEY') or '')
        if not key:
            raise LiteWebError('Serper API Key 尚未配置。')
        async with session.post(
            'https://google.serper.dev/search',
            headers={'X-API-KEY': key, 'Content-Type': 'application/json'},
            json={'q': query, 'num': count},
            timeout=timeout,
            allow_redirects=False,
        ) as response:
            validate_response_peer(response)
            reject_provider_redirect(response)
            response.raise_for_status()
            payload = await response.json()
        return cache_results(payload)

    if engine == 'bing':
        key = str(config.get('BING_SEARCH_V7_SUBSCRIPTION_KEY') or '')
        if not key:
            raise LiteWebError('Bing Search API Key 尚未配置。')
        endpoint = str(config.get('BING_SEARCH_V7_ENDPOINT') or 'https://api.bing.microsoft.com/v7.0/search')
        await validate_public_endpoint(endpoint)
        async with session.get(
            endpoint,
            headers={'Ocp-Apim-Subscription-Key': key},
            params={'q': query, 'count': count, 'mkt': 'zh-CN'},
            timeout=timeout,
            allow_redirects=False,
        ) as response:
            validate_response_peer(response)
            reject_provider_redirect(response)
            response.raise_for_status()
            payload = await response.json()
        return cache_results(payload)

    if engine == 'searxng':
        endpoint = str(config.get('SEARXNG_QUERY_URL') or '')
        if not endpoint:
            raise LiteWebError('SearXNG 地址尚未配置。')
        await validate_public_endpoint(endpoint)
        async with session.get(
            endpoint,
            params={
                'q': query,
                'format': 'json',
                'pageno': 1,
                'safesearch': 1,
                'language': str(config.get('SEARXNG_LANGUAGE') or 'all'),
            },
            timeout=timeout,
            allow_redirects=False,
        ) as response:
            validate_response_peer(response)
            reject_provider_redirect(response)
            response.raise_for_status()
            payload = await response.json()
        return cache_results(payload)

    raise LiteWebError('当前 Render lite 仅支持 Tavily、Brave、Bing、Serper 和 SearXNG 搜索。')


def dumps_search_fixture(results: list[LiteSearchResult]) -> str:
    return json.dumps([item.as_dict() for item in results], ensure_ascii=False, sort_keys=True)
