from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from open_webui.env import LITE_MEMORY_PROBE_ENABLED
from open_webui.utils.auth import get_admin_user

router = APIRouter()

_KIB = 1024
_MIB = 1024 * 1024


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding='utf-8').strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    value = _read_text(path)
    if not value or value == 'max':
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _bytes_to_mb(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / _MIB, 2)


def _kb_to_mb(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value * _KIB / _MIB, 2)


def _read_proc_status() -> dict[str, Any]:
    status_text = _read_text(Path('/proc/self/status'))
    if not status_text:
        return {'available': False}

    values: dict[str, Any] = {'available': True}
    key_map = {
        'VmRSS': 'rss_mb',
        'VmHWM': 'rss_peak_mb',
        'VmSize': 'virtual_mb',
        'RssAnon': 'rss_anon_mb',
        'RssFile': 'rss_file_mb',
    }

    for line in status_text.splitlines():
        if ':' not in line:
            continue
        key, raw_value = line.split(':', 1)
        raw_value = raw_value.strip()

        if key in key_map:
            parts = raw_value.split()
            try:
                values[key_map[key]] = _kb_to_mb(int(parts[0]))
            except (IndexError, ValueError):
                values[key_map[key]] = None
        elif key == 'Threads':
            try:
                values['threads'] = int(raw_value)
            except ValueError:
                values['threads'] = None

    return values


def _cgroup_v2_candidates() -> list[Path]:
    candidates: list[Path] = []
    cgroup_text = _read_text(Path('/proc/self/cgroup')) or ''

    for line in cgroup_text.splitlines():
        parts = line.split(':', 2)
        if len(parts) == 3 and parts[1] == '':
            cgroup_path = parts[2].strip('/')
            candidates.append(Path('/sys/fs/cgroup') / cgroup_path)

    candidates.append(Path('/sys/fs/cgroup'))
    return candidates


def _cgroup_v1_memory_candidates() -> list[Path]:
    candidates: list[Path] = []
    cgroup_text = _read_text(Path('/proc/self/cgroup')) or ''

    for line in cgroup_text.splitlines():
        parts = line.split(':', 2)
        if len(parts) != 3:
            continue

        controllers = parts[1].split(',')
        if 'memory' not in controllers:
            continue

        cgroup_path = parts[2].strip('/')
        candidates.append(Path('/sys/fs/cgroup/memory') / cgroup_path)

    candidates.append(Path('/sys/fs/cgroup/memory'))
    return candidates


def _read_cgroup_memory() -> dict[str, Any]:
    for base in _cgroup_v2_candidates():
        current_path = base / 'memory.current'
        if current_path.exists():
            current = _read_int(current_path)
            limit = _read_int(base / 'memory.max')
            peak = _read_int(base / 'memory.peak')
            return {
                'available': True,
                'version': 'v2',
                'current_mb': _bytes_to_mb(current),
                'peak_mb': _bytes_to_mb(peak),
                'limit_mb': _bytes_to_mb(limit),
                'usage_ratio': round(current / limit, 4) if current is not None and limit else None,
            }

    for base in _cgroup_v1_memory_candidates():
        current_path = base / 'memory.usage_in_bytes'
        if current_path.exists():
            current = _read_int(current_path)
            limit = _read_int(base / 'memory.limit_in_bytes')
            peak = _read_int(base / 'memory.max_usage_in_bytes')
            return {
                'available': True,
                'version': 'v1',
                'current_mb': _bytes_to_mb(current),
                'peak_mb': _bytes_to_mb(peak),
                'limit_mb': _bytes_to_mb(limit),
                'usage_ratio': round(current / limit, 4) if current is not None and limit else None,
            }

    return {'available': False}


@router.get('/memory')
async def get_lite_memory_probe(user=Depends(get_admin_user)):
    if not LITE_MEMORY_PROBE_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')

    return {
        'ok': True,
        'timestamp_ms': int(time.time() * 1000),
        'probe': {
            'mode': 'manual',
            'admin_only': True,
            'no_new_dependencies': True,
        },
        'process': _read_proc_status(),
        'cgroup': _read_cgroup_memory(),
    }
