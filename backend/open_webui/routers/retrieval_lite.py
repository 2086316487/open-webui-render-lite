from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from open_webui.models.config import Config
from open_webui.utils.access_control import has_permission
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.lite_web import (
    LiteWebError,
    fetch_web_document,
    search_web_provider,
)


log = logging.getLogger(__name__)
router = APIRouter()

SUPPORTED_ENGINES = {'tavily', 'brave', 'bing', 'serper', 'searxng'}
WEB_FIELDS = {
    'ENABLE_WEB_SEARCH': ('web.search.enable', False),
    'ENABLE_WEB_SEARCH_CONFIRMATION': ('web.search.confirmation.enable', False),
    'WEB_SEARCH_CONFIRMATION_CONTENT': (
        'web.search.confirmation.content',
        '您的查询将发送给已配置的联网搜索服务。',
    ),
    'WEB_SEARCH_ENGINE': ('web.search.engine', ''),
    'WEB_SEARCH_RESULT_COUNT': ('web.search.result_count', 5),
    'WEB_SEARCH_CONCURRENT_REQUESTS': ('web.search.concurrent_requests', 2),
    'WEB_FETCH_MAX_CONTENT_LENGTH': ('web.fetch.max_content_length', 12000),
    'WEB_LOADER_TIMEOUT': ('web.loader.timeout', '15'),
    'WEB_SEARCH_DOMAIN_FILTER_LIST': ('web.search.domain.filter_list', []),
    'BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL': ('web.search.bypass_embedding_and_retrieval', True),
    'BYPASS_WEB_SEARCH_WEB_LOADER': ('web.search.bypass_web_loader', False),
    'TAVILY_API_KEY': ('web.search.tavily_api_key', ''),
    'BRAVE_SEARCH_API_KEY': ('web.search.brave_search_api_key', ''),
    'SERPER_API_KEY': ('web.search.serper_api_key', ''),
    'BING_SEARCH_V7_ENDPOINT': (
        'web.search.bing_search_v7_endpoint',
        'https://api.bing.microsoft.com/v7.0/search',
    ),
    'BING_SEARCH_V7_SUBSCRIPTION_KEY': ('web.search.bing_search_v7_subscription_key', ''),
    'SEARXNG_QUERY_URL': ('web.search.searxng_query_url', ''),
    'SEARXNG_LANGUAGE': ('web.search.searxng_language', 'all'),
}


class WebConfig(BaseModel):
    model_config = ConfigDict(extra='ignore')

    ENABLE_WEB_SEARCH: bool | None = None
    ENABLE_WEB_SEARCH_CONFIRMATION: bool | None = None
    WEB_SEARCH_CONFIRMATION_CONTENT: str | None = None
    WEB_SEARCH_ENGINE: str | None = None
    WEB_SEARCH_RESULT_COUNT: int | None = Field(default=None, ge=1, le=10)
    WEB_SEARCH_CONCURRENT_REQUESTS: int | None = Field(default=None, ge=1, le=3)
    WEB_FETCH_MAX_CONTENT_LENGTH: int | None = Field(default=None, ge=1000, le=20000)
    WEB_LOADER_TIMEOUT: str | None = None
    WEB_SEARCH_DOMAIN_FILTER_LIST: list[str | None] | None = None
    BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL: bool | None = None
    BYPASS_WEB_SEARCH_WEB_LOADER: bool | None = None
    TAVILY_API_KEY: str | None = None
    BRAVE_SEARCH_API_KEY: str | None = None
    SERPER_API_KEY: str | None = None
    BING_SEARCH_V7_ENDPOINT: str | None = None
    BING_SEARCH_V7_SUBSCRIPTION_KEY: str | None = None
    SEARXNG_QUERY_URL: str | None = None
    SEARXNG_LANGUAGE: str | None = None


class ConfigForm(BaseModel):
    model_config = ConfigDict(extra='ignore')
    web: WebConfig | None = None


class ProcessUrlForm(BaseModel):
    url: str
    collection_name: str | None = None


class SearchForm(BaseModel):
    queries: list[str] = Field(default_factory=list)
    query: str | None = None
    collection_name: str | None = None


async def _web_config() -> dict[str, Any]:
    keys = [key for key, _ in WEB_FIELDS.values()]
    values = await Config.get_many(*keys)
    return {name: values.get(key, default) for name, (key, default) in WEB_FIELDS.items()}


def _timeout(config: dict[str, Any]) -> int:
    try:
        value = int(config.get('WEB_LOADER_TIMEOUT') or 15)
    except (TypeError, ValueError):
        value = 15
    return min(max(value, 5), 20)


@router.get('/config')
async def get_rag_config(user=Depends(get_admin_user)):
    return {
        'status': True,
        'lite_web': True,
        'web': {
            **await _web_config(),
            'WEB_SEARCH_TRUST_ENV': False,
            'WEB_LOADER_CONCURRENT_REQUESTS': 2,
            'ENABLE_WEB_LOADER_SSL_VERIFICATION': True,
            'WEB_LOADER_ENGINE': 'safe_web',
            'LITE_SUPPORTED_ENGINES': sorted(SUPPORTED_ENGINES),
        },
    }


@router.post('/config/update')
async def update_rag_config(form_data: ConfigForm, user=Depends(get_admin_user)):
    if form_data.web is None:
        raise HTTPException(status_code=400, detail='Render lite 仅允许更新联网搜索配置。')

    data = form_data.web.model_dump(exclude_none=True)
    engine = str(data.get('WEB_SEARCH_ENGINE') or '').strip().lower()
    if engine and engine not in SUPPORTED_ENGINES:
        raise HTTPException(
            status_code=400,
            detail='当前 Render lite 仅支持 Tavily、Brave、Bing、Serper 和 SearXNG 搜索。',
        )
    if 'WEB_LOADER_TIMEOUT' in data:
        try:
            data['WEB_LOADER_TIMEOUT'] = str(min(max(int(data['WEB_LOADER_TIMEOUT']), 5), 20))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail='联网读取超时必须为 5-20 秒。') from exc

    data['BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL'] = True
    updates = {WEB_FIELDS[name][0]: value for name, value in data.items() if name in WEB_FIELDS}
    await Config.upsert(updates)
    return await get_rag_config(user)


async def process_web_search(request: Request, form_data: SearchForm, user=Depends(get_verified_user)):
    config = await _web_config()
    if not config.get('ENABLE_WEB_SEARCH'):
        raise HTTPException(status_code=403, detail='管理员尚未启用联网搜索。')
    if user.role != 'admin' and not await has_permission(
        user.id, 'features.web_search', await Config.get('user.permissions', {})
    ):
        raise HTTPException(status_code=403, detail='当前账户没有使用联网搜索的权限。')

    raw_queries = [*form_data.queries, *([form_data.query] if form_data.query else [])]
    queries = [str(query).strip() for query in raw_queries if str(query).strip()][:3]
    if not queries:
        raise HTTPException(status_code=400, detail='搜索关键词不能为空。')

    engine = str(config.get('WEB_SEARCH_ENGINE') or '').strip().lower()
    if engine not in SUPPORTED_ENGINES:
        raise HTTPException(status_code=400, detail='管理员尚未配置受支持的联网搜索服务。')

    timeout_seconds = _timeout(config)
    result_count = min(max(int(config.get('WEB_SEARCH_RESULT_COUNT') or 5), 1), 10)
    max_chars = min(max(int(config.get('WEB_FETCH_MAX_CONTENT_LENGTH') or 12000), 1000), 20000)
    concurrency = min(max(int(config.get('WEB_SEARCH_CONCURRENT_REQUESTS') or 2), 1), 3)
    semaphore = asyncio.Semaphore(concurrency)

    async with aiohttp.ClientSession(
        trust_env=False,
        headers={'User-Agent': 'Open-WebUI-Render-Lite/1.0'},
    ) as session:
        try:
            search_groups = [
                await search_web_provider(
                    engine,
                    query,
                    result_count,
                    config,
                    session=session,
                    timeout_seconds=timeout_seconds,
                )
                for query in queries
            ]
        except LiteWebError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except aiohttp.ClientResponseError as exc:
            raise HTTPException(status_code=400, detail=f'联网搜索服务返回 HTTP {exc.status}。') from exc
        except aiohttp.ClientError as exc:
            raise HTTPException(status_code=400, detail='联网搜索服务连接失败。') from exc

        results = []
        seen = set()
        for group in search_groups:
            for item in group:
                if item.link not in seen:
                    seen.add(item.link)
                    results.append(item)
                if len(results) >= result_count:
                    break
            if len(results) >= result_count:
                break
        if not results:
            raise HTTPException(status_code=404, detail='联网搜索没有返回可用结果。')

        async def load_result(item):
            if config.get('BYPASS_WEB_SEARCH_WEB_LOADER'):
                return {
                    'content': item.snippet or item.title,
                    'metadata': {
                        'source': item.link,
                        'title': item.title or item.link,
                        'link': item.link,
                        'snippet': item.snippet,
                        'lite_web_context': True,
                    },
                }
            async with semaphore:
                try:
                    document = await fetch_web_document(
                        item.link,
                        session=session,
                        timeout_seconds=timeout_seconds,
                        max_chars=max_chars,
                    )
                    return document.as_doc()
                except LiteWebError:
                    return {
                        'content': item.snippet or item.title,
                        'metadata': {
                            'source': item.link,
                            'title': item.title or item.link,
                            'link': item.link,
                            'snippet': item.snippet,
                            'lite_web_context': True,
                            'fetch_warning': 'page_fetch_failed',
                        },
                    }

        docs = await asyncio.gather(*(load_result(item) for item in results[:5]))

    total_chars = 0
    limited_docs = []
    for doc in docs:
        remaining = 40000 - total_chars
        if remaining <= 0:
            break
        content = str(doc.get('content') or '')[:remaining]
        if content:
            doc['content'] = content
            limited_docs.append(doc)
            total_chars += len(content)

    return {
        'status': True,
        'collection_name': None,
        'filenames': [item.link for item in results],
        'items': [item.as_dict() for item in results],
        'docs': limited_docs,
        'loaded_count': len(limited_docs),
    }


@router.post('/process/web/search')
async def process_web_search_route(
    request: Request,
    form_data: SearchForm,
    user=Depends(get_verified_user),
):
    return await process_web_search(request, form_data, user)


@router.post('/process/web')
async def process_web(
    form_data: ProcessUrlForm,
    user=Depends(get_verified_user),
):
    if user.role != 'admin' and not await has_permission(
        user.id, 'chat.web_upload', await Config.get('user.permissions', {})
    ):
        raise HTTPException(status_code=403, detail='当前账户没有读取网页的权限。')
    config = await _web_config()
    async with aiohttp.ClientSession(
        trust_env=False,
        headers={'User-Agent': 'Open-WebUI-Render-Lite/1.0'},
    ) as session:
        try:
            document = await fetch_web_document(
                form_data.url,
                session=session,
                timeout_seconds=_timeout(config),
                max_chars=min(max(int(config.get('WEB_FETCH_MAX_CONTENT_LENGTH') or 12000), 1000), 20000),
            )
        except LiteWebError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {
        'status': True,
        'collection_name': None,
        'filename': document.url,
        'filenames': [document.url],
        'docs': [document.as_doc()],
        'loaded_count': 1,
    }


async def get_lite_web_sources(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sources = []
    for item in items or []:
        if item.get('type') not in {'web_search', 'web'}:
            continue
        for doc in item.get('docs') or []:
            content = str(doc.get('content') or '').strip()
            metadata = doc.get('metadata') or {}
            if not content:
                continue
            url = str(metadata.get('source') or metadata.get('link') or item.get('url') or '').strip()
            title = str(metadata.get('title') or item.get('name') or url or '网页内容').strip()
            sources.append(
                {
                    'source': {'name': title, 'id': url or title},
                    'document': [content],
                    'metadata': [{**metadata, 'source': url, 'name': title, 'lite_web_context': True}],
                }
            )
    return sources
