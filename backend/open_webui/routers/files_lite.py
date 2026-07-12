import asyncio
import errno
import hashlib
import io
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from open_webui.config import BYPASS_ADMIN_ACCESS_CONTROL
from open_webui.constants import ERROR_MESSAGES
from open_webui.internal.db import get_async_session
from open_webui.models.config import Config
from open_webui.models.files import (
    FileForm,
    FileListResponse,
    FileModel,
    FileModelResponse,
    Files,
)
from open_webui.storage.provider import Storage
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.files_lite import validate_lite_file_upload
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

router = APIRouter()
PAGE_SIZE = 50


def _has_direct_file_access(file: FileModel, user) -> bool:
    return file.user_id == user.id or user.role == 'admin'


def _safe_content_type(content_type) -> str | None:
    return content_type if isinstance(content_type, str) else None


def _build_process_status_event(data: dict) -> dict:
    event = {'status': data.get('status', 'completed')}
    if event['status'] == 'failed' and data.get('error'):
        event['error'] = data.get('error')
    if isinstance(data.get('lite_extraction'), dict):
        event['lite_extraction'] = data.get('lite_extraction')
    if data.get('processing_skipped') is not None:
        event['processing_skipped'] = bool(data.get('processing_skipped'))
    return event


async def _get_max_upload_size_bytes() -> int | None:
    max_size = await Config.get('rag.file.max_size')
    if not max_size:
        return None
    return int(max_size) * 1024 * 1024


async def _check_upload_size(contents: bytes) -> None:
    max_size_bytes = await _get_max_upload_size_bytes()
    if max_size_bytes and len(contents) > max_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                'code': 'file_too_large',
                'message': f'文件超过当前上传大小限制（{max_size_bytes // 1024 // 1024} MB）。请缩小文件，或使用 Hugging Face full。',
            },
        )


async def _upload_to_storage(
    contents: bytes, storage_filename: str, fallback_filename: str, tags: dict[str, str]
) -> tuple[bytes, str]:
    try:
        return await asyncio.to_thread(Storage.upload_file, io.BytesIO(contents), storage_filename, tags)
    except OSError as e:
        if e.errno != errno.ENAMETOOLONG:
            raise

        return await asyncio.to_thread(Storage.upload_file, io.BytesIO(contents), fallback_filename, tags)


@router.post('/', response_model=FileModelResponse)
async def upload_file(
    file: UploadFile = File(...),
    metadata: Optional[dict | str] = Form(None),
    process: bool = Query(True),
    process_in_background: bool = Query(True),
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    del process_in_background

    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={'code': 'invalid_metadata', 'message': '文件元数据格式无效，请重新上传。'},
            )
    file_metadata = metadata if isinstance(metadata, dict) else {}

    try:
        original_filename = os.path.basename(file.filename or 'upload')
        file_id = str(uuid.uuid4())
        storage_filename = f'{file_id}_{original_filename}'
        extension = os.path.splitext(original_filename)[1][:16]
        fallback_filename = f'{file_id}{extension}' if extension else file_id
        tags = {
            'OpenWebUI-User-Email': user.email,
            'OpenWebUI-User-Id': user.id,
            'OpenWebUI-User-Name': user.name,
            'OpenWebUI-File-Id': file_id,
        }

        contents = await file.read()
        await _check_upload_size(contents)

        file_hash = file_metadata.get('file_hash') or await asyncio.to_thread(
            lambda: hashlib.sha256(contents).hexdigest()
        )
        data = {'status': 'completed'}
        if process:
            data['processing_skipped'] = True

        content_type = _safe_content_type(file.content_type)
        allowed, extracted_data, validation_error = await asyncio.to_thread(
            validate_lite_file_upload, contents, original_filename, content_type
        )
        if not allowed:
            raise HTTPException(
                status_code=validation_error.get('status_code', status.HTTP_415_UNSUPPORTED_MEDIA_TYPE),
                detail={
                    'code': validation_error.get('code', 'unsupported_file_type'),
                    'message': validation_error.get('message', '当前 Render lite 不支持上传这种文件格式。'),
                },
            )

        data.update(extracted_data)

        _, file_path = await _upload_to_storage(contents, storage_filename, fallback_filename, tags)

        file_item = await Files.insert_new_file(
            user.id,
            FileForm(
                id=file_id,
                filename=original_filename,
                path=file_path,
                hash=file_hash,
                data=data,
                meta={
                    'name': original_filename,
                    'content_type': content_type,
                    'size': len(contents),
                    'file_hash': file_hash,
                    'data': file_metadata,
                },
            ),
            db=db,
        )
        if file_item:
            return file_item

        await asyncio.to_thread(Storage.delete_file, file_path)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={'code': 'file_upload_failed', 'message': '文件上传失败，请稍后重试。'},
        )
    except HTTPException:
        raise
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={'code': 'file_upload_failed', 'message': '文件上传失败，请稍后重试。'},
        )


@router.get('/', response_model=FileListResponse)
async def list_files(
    user=Depends(get_verified_user),
    page: int = Query(1, ge=1),
    content: bool = Query(True),
    db: AsyncSession = Depends(get_async_session),
):
    user_id = None if (user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL) else user.id
    result = await Files.get_file_list(user_id=user_id, skip=(page - 1) * PAGE_SIZE, limit=PAGE_SIZE, db=db)

    if not content:
        for file in result.items:
            if file.data and 'content' in file.data:
                del file.data['content']

    return result


@router.get('/search', response_model=list[FileModelResponse])
async def search_files(
    filename: str = Query(...),
    content: bool = Query(True),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    user_id = None if (user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL) else user.id
    files = await Files.search_files(user_id=user_id, filename=filename, skip=skip, limit=limit, db=db)

    if not content:
        for file in files:
            if file.data and 'content' in file.data:
                del file.data['content']

    return files


@router.get('/count', response_model=int)
async def count_files(user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    user_id = None if (user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL) else user.id
    return await Files.count_files_by_user_id(user_id=user_id, db=db)


@router.delete('/all')
async def delete_all_files(user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)):
    result = await Files.delete_all_files(db=db)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT('Error deleting files'),
        )

    try:
        await asyncio.to_thread(Storage.delete_all_files)
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT('Error deleting files'),
        )
    return {'message': 'All files deleted successfully'}


@router.get('/{id}', response_model=Optional[FileModel])
async def get_file_by_id(id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    file = await Files.get_file_by_id(id, db=db)
    if not file or not _has_direct_file_access(file, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return file


@router.get('/{id}/process/status')
async def get_file_process_status(
    id: str,
    stream: bool = Query(False),
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    file = await Files.get_file_by_id(id, db=db)
    if not file or not _has_direct_file_access(file, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    data = file.data or {}
    event = _build_process_status_event(data)

    if stream:
        async def event_stream():
            yield f'data: {json.dumps(event)}\n\n'

        return StreamingResponse(event_stream(), media_type='text/event-stream')

    return event


@router.get('/{id}/data/content')
async def get_file_data_content_by_id(
    id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    file = await Files.get_file_by_id(id, db=db)
    if not file or not _has_direct_file_access(file, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return {'content': (file.data or {}).get('content', '')}


class ContentForm(BaseModel):
    content: str


@router.post('/{id}/data/content/update')
async def update_file_data_content_by_id(
    id: str,
    form_data: ContentForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    file = await Files.get_file_by_id(id, db=db)
    if not file or not _has_direct_file_access(file, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    max_size_bytes = await _get_max_upload_size_bytes()
    if max_size_bytes and len(form_data.content.encode('utf-8')) > max_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=ERROR_MESSAGES.FILE_TOO_LARGE(size=f'{max_size_bytes // 1024 // 1024} MB'),
        )

    result = await Files.update_file_data_by_id(
        id,
        {'content': form_data.content, 'status': 'completed', 'processing_skipped': True},
        db=db,
    )
    if not result:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT('Error updating file content'),
        )
    return {'content': (result.data or {}).get('content', '')}


async def _get_file_response(file: FileModel, attachment: bool = False):
    try:
        meta = file.meta or {}
        filename = meta.get('name', file.filename)
        content_type = meta.get('content_type')
        encoded_filename = quote(filename)
        headers = {}

        if not file.path:
            file_content = (file.data or {}).get('content', '')
            headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"

            async def content_stream():
                yield file_content.encode('utf-8')

            return StreamingResponse(content_stream(), media_type='text/plain', headers=headers)

        file_path = await asyncio.to_thread(Storage.get_file, file.path)
        file_path = Path(file_path)
        if not file_path.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

        if attachment:
            headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"
        elif content_type == 'application/pdf' or filename.lower().endswith('.pdf'):
            headers['Content-Disposition'] = f"inline; filename*=UTF-8''{encoded_filename}"
            content_type = 'application/pdf'
        elif content_type != 'text/plain':
            headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"

        return FileResponse(file_path, headers=headers, media_type=content_type)
    except HTTPException:
        raise
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT('Error getting file content'),
        )


@router.get('/{id}/content')
async def get_file_content_by_id(
    id: str,
    user=Depends(get_verified_user),
    attachment: bool = Query(False),
    db: AsyncSession = Depends(get_async_session),
):
    file = await Files.get_file_by_id(id, db=db)
    if not file or not _has_direct_file_access(file, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return await _get_file_response(file, attachment=attachment)


@router.get('/{id}/content/html')
async def get_html_file_content_by_id(
    id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    file = await Files.get_file_by_id(id, db=db)
    if not file or not _has_direct_file_access(file, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return await _get_file_response(file)


@router.get('/{id}/content/{file_name}')
async def get_file_content_by_id_with_name(
    id: str, file_name: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    del file_name

    file = await Files.get_file_by_id(id, db=db)
    if not file or not _has_direct_file_access(file, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return await _get_file_response(file, attachment=True)


class FileRenameForm(BaseModel):
    filename: str


@router.post('/{id}/rename')
async def rename_file_by_id(
    id: str,
    form_data: FileRenameForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    file = await Files.get_file_by_id(id, db=db)
    if not file or not _has_direct_file_access(file, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    result = await Files.update_file_name_by_id(id, form_data.filename, db=db)
    if result:
        return result
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ERROR_MESSAGES.DEFAULT('Error renaming file'),
    )


@router.delete('/{id}')
async def delete_file_by_id(id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    file = await Files.get_file_by_id(id, db=db)
    if not file or not _has_direct_file_access(file, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    result = await Files.delete_file_by_id(id, db=db)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT('Error deleting file'),
        )

    try:
        await asyncio.to_thread(Storage.delete_file, file.path)
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT('Error deleting files'),
        )
    return {'message': 'File deleted successfully'}
