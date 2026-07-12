from __future__ import annotations

import html
import re
from pathlib import Path


_PAGE_WARNING_RE = re.compile(r'^page_(\d+)_(no_content|no_text|extract_failed)$')
_PAGE_LIMIT_RE = re.compile(r'^pages_(\d+)_to_(\d+)_skipped_by_limit$')
_PAGE_TRUNCATED_RE = re.compile(r'^content_truncated_at_page_(\d+)$')
_SLIDE_NOTES_WARNING_RE = re.compile(r'^slide_(\d+)_notes_extract_failed$')
_SOURCE_TAG_RE = re.compile(r'<\s*(/?)\s*source(?=[\s>])', re.IGNORECASE)
_BOUNDARY_PREFIX = '<<<LITE_FILE_'


def escape_source_attribute(value) -> str:
    if value is None:
        return ''
    normalized = _single_line(value, limit=512)
    return html.escape(normalized, quote=True)


def get_source_identity(metadata: dict | None, source: dict | None) -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    source = source if isinstance(source, dict) else {}
    value = metadata.get('file_id') or metadata.get('source') or source.get('id')
    return str(value) if value not in (None, '') else 'N/A'


def _single_line(value, *, limit: int) -> str:
    text = str(value).replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
    text = ''.join(character for character in text if character.isprintable())
    text = ' '.join(text.split()).strip()
    return text[:limit] or '未命名文件'


def _positive_int(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _context_id(file_id) -> str:
    safe_id = re.sub(r'[^A-Za-z0-9]', '', str(file_id or ''))
    return (safe_id[:12] or 'unknown-file').lower()


def _format_label(extraction: dict, source_name: str) -> str:
    value = extraction.get('format')
    if isinstance(value, str) and value.strip():
        return _single_line(value, limit=24).upper()
    suffix = Path(source_name).suffix.lstrip('.')
    return suffix.upper() if suffix else 'TEXT'


def _format_extraction_summary(extraction: dict, source_name: str) -> str:
    details = [f'格式={_format_label(extraction, source_name)}']
    for key, label in (('pages', '页数'), ('sheets', '工作表数'), ('slides', '幻灯片数')):
        count = _positive_int(extraction.get(key))
        if count:
            details.append(f'{label}={count}')
    if extraction.get('truncated'):
        details.append('内容已截断')
    return f"[解析信息] {'；'.join(details)}。"


def _format_warning_summary(extraction: dict) -> str | None:
    warning_codes = extraction.get('warnings')
    if not isinstance(warning_codes, list):
        warning_codes = []

    no_text_pages = set()
    failed_pages = set()
    failed_note_slides = set()
    skipped_ranges = []
    truncated_pages = set()
    unknown_warning = False

    for value in warning_codes[:21]:
        if not isinstance(value, str):
            continue
        page_match = _PAGE_WARNING_RE.fullmatch(value)
        if page_match:
            page_number = int(page_match.group(1))
            if page_match.group(2) == 'extract_failed':
                failed_pages.add(page_number)
            else:
                no_text_pages.add(page_number)
            continue
        limit_match = _PAGE_LIMIT_RE.fullmatch(value)
        if limit_match:
            skipped_ranges.append((int(limit_match.group(1)), int(limit_match.group(2))))
            continue
        truncated_match = _PAGE_TRUNCATED_RE.fullmatch(value)
        if truncated_match:
            truncated_pages.add(int(truncated_match.group(1)))
            continue
        slide_match = _SLIDE_NOTES_WARNING_RE.fullmatch(value)
        if slide_match:
            failed_note_slides.add(int(slide_match.group(1)))
            continue
        if value == 'additional_warnings_omitted':
            unknown_warning = True
        elif value:
            unknown_warning = True

    messages = []
    if no_text_pages:
        pages = '、'.join(str(value) for value in sorted(no_text_pages))
        messages.append(f'PDF 第 {pages} 页没有可提取文字')
    if failed_pages:
        pages = '、'.join(str(value) for value in sorted(failed_pages))
        messages.append(f'PDF 第 {pages} 页文字提取失败')
    if failed_note_slides:
        slides = '、'.join(str(value) for value in sorted(failed_note_slides))
        messages.append(f'PowerPoint 第 {slides} 张幻灯片的备注读取失败')
    for start, end in skipped_ranges:
        messages.append(f'PDF 第 {start}-{end} 页因页数上限未读取')
    if truncated_pages:
        pages = '、'.join(str(value) for value in sorted(truncated_pages))
        messages.append(f'PDF 在第 {pages} 页达到文字上限并截断')
    elif extraction.get('truncated') and not skipped_ranges:
        messages.append('超出轻量处理范围的内容已截断')
    if unknown_warning:
        messages.append('解析器还报告了其他未展开警告')

    return f"[解析警告] {'；'.join(messages)}。" if messages else None


def _neutralize_document_boundaries(document: str) -> str:
    document = document.replace(_BOUNDARY_PREFIX, '&lt;&lt;&lt;LITE_FILE_')
    return _SOURCE_TAG_RE.sub(lambda match: f'&lt;{match.group(1)}source', document)


def build_lite_file_context(document: str, metadata: dict | None, source: dict | None) -> str:
    if not isinstance(metadata, dict) or not metadata.get('lite_file_context'):
        return document

    source = source if isinstance(source, dict) else {}
    source_name = _single_line(
        metadata.get('name') or source.get('name') or '未命名文件',
        limit=240,
    )
    file_id = metadata.get('file_id') or source.get('id')
    context_id = _context_id(file_id)
    extraction = metadata.get('lite_extraction')
    extraction = extraction if isinstance(extraction, dict) else {}

    lines = [
        f'<<<LITE_FILE_CONTEXT_BEGIN id={context_id}>>>',
        f'[文件名] {source_name}',
        _format_extraction_summary(extraction, source_name),
    ]
    warning_summary = _format_warning_summary(extraction)
    if warning_summary:
        lines.append(warning_summary)
    lines.extend(
        [
            f'<<<LITE_FILE_CONTENT_BEGIN id={context_id}>>>',
            _neutralize_document_boundaries(document),
            f'<<<LITE_FILE_CONTENT_END id={context_id}>>>',
            f'<<<LITE_FILE_CONTEXT_END id={context_id}>>>',
        ]
    )
    return '\n'.join(lines)
