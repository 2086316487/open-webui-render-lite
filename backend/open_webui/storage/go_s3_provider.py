"""S3 storage via the Go file-extractor `s3` subcommand.

Keeps boto3/botocore out of the Render lite runtime. Mirrors the
S3StorageProvider behavior for the Put/Get/Delete/List surface Open WebUI
actually uses; unsupported S3 features (tagging, virtual-host addressing,
accelerate, IAM-role credentials) are guarded in provider.get_storage_provider
and routed to the boto3 provider instead.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import BinaryIO, Dict, Tuple

from open_webui.config import (
    S3_ACCESS_KEY_ID,
    S3_BUCKET_NAME,
    S3_ENDPOINT_URL,
    S3_KEY_PREFIX,
    S3_REGION_NAME,
    S3_SECRET_ACCESS_KEY,
    UPLOAD_DIR,
)
from open_webui.env import (
    LITE_GO_FILE_EXTRACTOR_PATH,
    LITE_GO_S3_TIMEOUT_SECONDS,
)

log = logging.getLogger(__name__)


class GoS3StorageProvider:
    def __init__(self):
        if not S3_BUCKET_NAME:
            raise RuntimeError('Go S3 client requires S3_BUCKET_NAME')
        if not S3_ENDPOINT_URL:
            raise RuntimeError('Go S3 client requires S3_ENDPOINT_URL')
        if not (S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY):
            raise RuntimeError('Go S3 client requires static S3 credentials')
        if not os.path.isfile(LITE_GO_FILE_EXTRACTOR_PATH):
            raise RuntimeError(f'Go S3 client binary not found at {LITE_GO_FILE_EXTRACTOR_PATH}')

        self.bucket_name = S3_BUCKET_NAME
        self.key_prefix = S3_KEY_PREFIX if S3_KEY_PREFIX else ''

    def _run(self, op: str, extra_args: list[str]) -> dict:
        command = [
            LITE_GO_FILE_EXTRACTOR_PATH,
            's3',
            '--op',
            op,
            '--endpoint',
            S3_ENDPOINT_URL,
            '--region',
            S3_REGION_NAME or 'us-east-1',
            '--bucket',
            self.bucket_name,
            '--timeout-seconds',
            str(LITE_GO_S3_TIMEOUT_SECONDS),
            *extra_args,
        ]

        # Credentials travel via the child environment, never argv.
        env = os.environ.copy()
        env['S3_ACCESS_KEY_ID'] = S3_ACCESS_KEY_ID
        env['S3_SECRET_ACCESS_KEY'] = S3_SECRET_ACCESS_KEY

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=LITE_GO_S3_TIMEOUT_SECONDS + 10,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f'Error during S3 {op}: Go S3 client timed out')
        except OSError as e:
            raise RuntimeError(f'Error during S3 {op}: failed to start Go S3 client: {e}')

        if completed.returncode != 0:
            stderr_excerpt = completed.stderr.decode('utf-8', errors='replace')[:300]
            raise RuntimeError(f'Error during S3 {op}: Go S3 client exited with {completed.returncode} {stderr_excerpt}')

        try:
            result = json.loads(completed.stdout.decode('utf-8', errors='replace').strip() or '{}')
        except json.JSONDecodeError:
            raise RuntimeError(f'Error during S3 {op}: Go S3 client returned invalid JSON')

        if not result.get('ok'):
            error_code = result.get('error_code', 'unknown')
            message = result.get('message', '')
            status = result.get('status')
            raise RuntimeError(f'Error during S3 {op}: {error_code} (status={status}) {message}'.strip())

        return result

    def upload_file(self, file: BinaryIO, filename: str, tags: Dict[str, str]) -> Tuple[bytes, str]:
        from open_webui.storage.provider import LocalStorageProvider

        contents, file_path = LocalStorageProvider.upload_file(file, filename, tags)
        s3_key = os.path.join(self.key_prefix, filename)
        self._run('put', ['--key', s3_key, '--file', file_path])
        return contents, f's3://{self.bucket_name}/{s3_key}'

    def get_file(self, file_path: str) -> str:
        s3_key = self._extract_s3_key(file_path)
        local_file_path = self._get_local_file_path(s3_key)
        self._run('get', ['--key', s3_key, '--file', local_file_path])
        return local_file_path

    def delete_file(self, file_path: str) -> None:
        from open_webui.storage.provider import LocalStorageProvider

        s3_key = self._extract_s3_key(file_path)
        self._run('delete', ['--key', s3_key])

        # Always delete from local storage
        LocalStorageProvider.delete_file(file_path)

    def delete_all_files(self) -> None:
        from open_webui.storage.provider import LocalStorageProvider

        result = self._run('list', [])
        for key in result.get('keys', []):
            # Skip objects that were not uploaded from open-webui in the first place
            if not key.startswith(self.key_prefix):
                continue
            self._run('delete', ['--key', key])

        # Always delete from local storage
        LocalStorageProvider.delete_all_files()

    # Same key/path semantics as S3StorageProvider.
    def _extract_s3_key(self, full_file_path: str) -> str:
        return '/'.join(full_file_path.split('//')[1].split('/')[1:])

    def _get_local_file_path(self, s3_key: str) -> str:
        return os.path.join(UPLOAD_DIR, s3_key.split('/')[-1])
