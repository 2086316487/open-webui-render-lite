import asyncio
import base64
import mimetypes
import os
from pathlib import Path
from typing import Optional

from open_webui.env import ENABLE_IMAGE_CONTENT_TYPE_EXTENSION_FALLBACK, LITE_TEXT_FILE_CONTEXT_MAX_BYTES
from open_webui.models.files import Files
from open_webui.storage.provider import Storage


_TEXT_FILE_EXTENSIONS = {
    '.bat',
    '.c',
    '.cfg',
    '.conf',
    '.cpp',
    '.cs',
    '.css',
    '.csv',
    '.go',
    '.h',
    '.hpp',
    '.html',
    '.ini',
    '.java',
    '.js',
    '.json',
    '.jsonl',
    '.jsx',
    '.log',
    '.md',
    '.php',
    '.ps1',
    '.py',
    '.rb',
    '.rs',
    '.sh',
    '.sql',
    '.toml',
    '.ts',
    '.tsv',
    '.tsx',
    '.txt',
    '.xml',
    '.yaml',
    '.yml',
}

_IMAGE_MIME_FALLBACK = {
    '.webp': 'image/webp',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.svg': 'image/svg+xml',
    '.bmp': 'image/bmp',
    '.tiff': 'image/tiff',
    '.tif': 'image/tiff',
    '.ico': 'image/x-icon',
    '.heic': 'image/heic',
    '.heif': 'image/heif',
    '.avif': 'image/avif',
}


def is_lite_text_file(filename: str, content_type: str | None) -> bool:
    content_type = content_type or ''
    if content_type.startswith('text/'):
        return True
    if content_type in {
        'application/json',
        'application/jsonl',
        'application/javascript',
        'application/x-javascript',
        'application/xml',
        'application/yaml',
        'application/x-yaml',
    }:
        return True
    return os.path.splitext(filename or '')[1].lower() in _TEXT_FILE_EXTENSIONS


def extract_lite_text_content(contents: bytes, filename: str, content_type: str | None) -> tuple[str | None, str | None]:
    if not is_lite_text_file(filename, content_type):
        return None, None
    if len(contents) > LITE_TEXT_FILE_CONTEXT_MAX_BYTES:
        return None, 'too_large'
    if b'\x00' in contents:
        return None, 'binary'

    for encoding in ('utf-8', 'utf-8-sig', 'latin-1'):
        try:
            return contents.decode(encoding), None
        except UnicodeDecodeError:
            continue
    return None, 'decode_failed'


async def get_lite_file_sources(items: list[dict] | None, user=None) -> list[dict]:
    if not items or user is None:
        return []

    sources = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        file_id = item.get('id') or item.get('url')
        if not file_id or file_id in seen:
            continue
        seen.add(file_id)

        file = await Files.get_file_by_id(file_id)
        if not file or (file.user_id != user.id and user.role != 'admin'):
            continue

        data = file.data or {}
        content = data.get('content')
        if not isinstance(content, str) or not content:
            continue

        meta = file.meta or {}
        source_name = meta.get('name') or item.get('name') or file.filename or file.id
        sources.append(
            {
                'source': {'id': file.id, 'name': source_name, 'type': 'file'},
                'document': [content],
                'metadata': [
                    {
                        'file_id': file.id,
                        'name': source_name,
                        'source': source_name,
                        'content_type': meta.get('content_type'),
                    }
                ],
            }
        )

    return sources


async def get_image_base64_from_file_id(id: str, user=None) -> Optional[str]:
    file = await Files.get_file_by_id(id)
    if not file or user is None:
        return None

    if file.user_id != user.id and user.role != 'admin':
        return None

    try:
        meta = file.meta or {}
        content_type = meta.get('content_type') if isinstance(meta, dict) else None
        if content_type and not content_type.startswith('image/'):
            return None

        file_path = await asyncio.to_thread(Storage.get_file, file.path)
        file_path = Path(file_path)
        if not file_path.is_file():
            return None

        content_type = content_type or mimetypes.guess_type(file_path.name)[0]
        if not content_type and ENABLE_IMAGE_CONTENT_TYPE_EXTENSION_FALLBACK:
            content_type = _IMAGE_MIME_FALLBACK.get(file_path.suffix.lower())
        if not content_type or not content_type.startswith('image/'):
            return None

        with open(file_path, 'rb') as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
        return f'data:{content_type};base64,{encoded_string}'
    except Exception:
        return None


async def get_image_base64_from_url(url: str, user=None) -> Optional[str]:
    if url.startswith('data:image/'):
        return url
    if url.startswith('http'):
        return None
    return await get_image_base64_from_file_id(url, user=user)
