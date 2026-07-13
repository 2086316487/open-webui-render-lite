from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from open_webui.env import LITE_MEMORY_PROBE_ENABLED
from open_webui.utils.auth import get_admin_user

router = APIRouter()

_KIB = 1024
_MIB = 1024 * 1024

_SMAPS_HEADER_RE = re.compile(r'^[0-9a-f]+-[0-9a-f]+\s')
_SMAPS_METRICS = ('Rss', 'Pss', 'Private_Clean', 'Private_Dirty', 'Swap')


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


def _read_memory_stat(path: Path, keys: tuple[str, ...]) -> dict[str, float | None]:
    stat_text = _read_text(path)
    values: dict[str, int] = {}
    if stat_text:
        for line in stat_text.splitlines():
            parts = line.split()
            if len(parts) != 2 or parts[0] not in keys:
                continue
            try:
                values[parts[0]] = int(parts[1])
            except ValueError:
                continue
    return {f'{key}_mb': _bytes_to_mb(values.get(key)) for key in keys}


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
                'breakdown': _read_memory_stat(
                    base / 'memory.stat',
                    ('anon', 'file', 'kernel', 'kernel_stack', 'pagetables', 'sock', 'shmem', 'slab'),
                ),
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
                'breakdown': _read_memory_stat(
                    base / 'memory.stat',
                    ('cache', 'rss', 'rss_huge', 'mapped_file', 'swap'),
                ),
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


def _read_smaps_by_mapping(limit: int = 25) -> dict[str, Any]:
    smaps_text = _read_text(Path('/proc/self/smaps'))
    if not smaps_text:
        return {'available': False}

    groups: dict[str, dict[str, int]] = {}
    current: dict[str, int] | None = None

    for line in smaps_text.splitlines():
        if _SMAPS_HEADER_RE.match(line):
            fields = line.split(None, 5)
            name = fields[5].strip() if len(fields) > 5 else '[anon]'
            current = groups.setdefault(name, {metric: 0 for metric in _SMAPS_METRICS})
            continue
        if current is None or ':' not in line:
            continue
        key, raw_value = line.split(':', 1)
        if key not in _SMAPS_METRICS:
            continue
        parts = raw_value.split()
        try:
            current[key] += int(parts[0])
        except (IndexError, ValueError):
            continue

    def _entry(name: str, metrics: dict[str, int]) -> dict[str, Any]:
        return {
            'name': name.rsplit('/', 1)[-1] if name.startswith('/') else name,
            'path': name if name.startswith('/') else None,
            'pss_mb': _kb_to_mb(metrics['Pss']),
            'rss_mb': _kb_to_mb(metrics['Rss']),
            'private_mb': _kb_to_mb(metrics['Private_Clean'] + metrics['Private_Dirty']),
            'swap_mb': _kb_to_mb(metrics['Swap']),
        }

    ranked = sorted(groups.items(), key=lambda item: item[1]['Pss'], reverse=True)
    total_pss_kb = sum(metrics['Pss'] for metrics in groups.values())
    anon_pss_kb = sum(metrics['Pss'] for name, metrics in groups.items() if not name.startswith('/'))

    return {
        'available': True,
        'total_pss_mb': _kb_to_mb(total_pss_kb),
        'anon_pss_mb': _kb_to_mb(anon_pss_kb),
        'mapping_count': len(groups),
        'top': [_entry(name, metrics) for name, metrics in ranked[:limit]],
    }


def _python_module_histogram(limit: int = 40) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for name in list(sys.modules):
        top_level = name.split('.', 1)[0]
        counts[top_level] = counts.get(top_level, 0) + 1

    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    return {
        'total_modules': len(sys.modules),
        'total_packages': len(counts),
        'top_level_packages': [{'package': name, 'modules': count} for name, count in ranked[:limit]],
    }


@router.get('/memory/breakdown')
async def get_lite_memory_breakdown(user=Depends(get_admin_user)):
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
        'mappings': _read_smaps_by_mapping(),
        'python_modules': _python_module_histogram(),
    }
