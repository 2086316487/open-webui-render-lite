"""S3 storage via the persistent Go file-extractor helper.

Keeps boto3/botocore out of the Render lite runtime. Mirrors the
S3StorageProvider behavior for the Put/Get/Delete/List surface Open WebUI
actually uses; unsupported S3 features (tagging, virtual-host addressing,
accelerate, IAM-role credentials) are guarded in provider.get_storage_provider
and routed to the boto3 provider instead.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import selectors
import subprocess
import threading
import uuid
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
        self._process = None
        self._process_lock = threading.Lock()
        atexit.register(self.close)

    def _start_helper_locked(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return

        self._stop_helper_locked()
        command = [
            LITE_GO_FILE_EXTRACTOR_PATH,
            's3-serve',
            '--endpoint',
            S3_ENDPOINT_URL,
            '--region',
            S3_REGION_NAME or 'us-east-1',
            '--bucket',
            self.bucket_name,
            '--timeout-seconds',
            str(LITE_GO_S3_TIMEOUT_SECONDS),
        ]

        # Credentials travel via the child environment, never argv.
        env = os.environ.copy()
        env['S3_ACCESS_KEY_ID'] = S3_ACCESS_KEY_ID
        env['S3_SECRET_ACCESS_KEY'] = S3_SECRET_ACCESS_KEY

        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding='utf-8',
                bufsize=1,
                env=env,
            )
        except OSError as e:
            self._process = None
            raise RuntimeError(f'failed to start persistent Go S3 helper: {e}')

    def _stop_helper_locked(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return

        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass

        if process.poll() is None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

        try:
            if process.stdout is not None:
                process.stdout.close()
        except OSError:
            pass

    def close(self) -> None:
        with self._process_lock:
            self._stop_helper_locked()

    def _read_response_locked(self, process) -> str:
        if process.stdout is None:
            raise RuntimeError('Go S3 helper stdout is unavailable')

        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            ready = selector.select(LITE_GO_S3_TIMEOUT_SECONDS + 10)
        finally:
            selector.close()

        if not ready:
            raise TimeoutError('Go S3 helper timed out')

        line = process.stdout.readline()
        if not line:
            raise BrokenPipeError('Go S3 helper exited before returning a response')
        return line

    def _request_locked(self, payload: dict) -> dict:
        self._start_helper_locked()
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError('Go S3 helper stdin is unavailable')

        process.stdin.write(json.dumps(payload, separators=(',', ':')) + '\n')
        process.stdin.flush()
        result = json.loads(self._read_response_locked(process))
        if result.get('request_id') != payload['request_id']:
            raise RuntimeError('Go S3 helper returned a mismatched request id')
        return result

    def _run(
        self,
        op: str,
        *,
        key: str | None = None,
        file_path: str | None = None,
        prefix: str | None = None,
        max_keys: int | None = None,
    ) -> dict:
        payload = {'request_id': uuid.uuid4().hex, 'op': op}
        if key is not None:
            payload['key'] = key
        if file_path is not None:
            payload['file'] = file_path
        if prefix is not None:
            payload['prefix'] = prefix
        if max_keys is not None:
            payload['max_keys'] = max_keys

        with self._process_lock:
            result = None
            last_error = None
            for attempt in range(2):
                try:
                    result = self._request_locked(payload)
                    break
                except (BrokenPipeError, OSError, TimeoutError, json.JSONDecodeError, RuntimeError) as e:
                    last_error = e
                    self._stop_helper_locked()
                    if attempt == 0:
                        log.warning('Restarting Go S3 helper after %s during %s', type(e).__name__, op)

            if result is None:
                raise RuntimeError(f'Error during S3 {op}: persistent Go S3 helper failed: {last_error}')

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
        self._run('put', key=s3_key, file_path=file_path)
        return contents, f's3://{self.bucket_name}/{s3_key}'

    def get_file(self, file_path: str) -> str:
        s3_key = self._extract_s3_key(file_path)
        local_file_path = self._get_local_file_path(s3_key)
        self._run('get', key=s3_key, file_path=local_file_path)
        return local_file_path

    def delete_file(self, file_path: str) -> None:
        from open_webui.storage.provider import LocalStorageProvider

        s3_key = self._extract_s3_key(file_path)
        self._run('delete', key=s3_key)

        # Always delete from local storage
        LocalStorageProvider.delete_file(file_path)

    def delete_all_files(self) -> None:
        from open_webui.storage.provider import LocalStorageProvider

        result = self._run('list', prefix=self.key_prefix)
        for key in result.get('keys', []):
            # Skip objects that were not uploaded from open-webui in the first place
            if not key.startswith(self.key_prefix):
                continue
            self._run('delete', key=key)

        # Always delete from local storage
        LocalStorageProvider.delete_all_files()

    # Same key/path semantics as S3StorageProvider.
    def _extract_s3_key(self, full_file_path: str) -> str:
        return '/'.join(full_file_path.split('//')[1].split('/')[1:])

    def _get_local_file_path(self, s3_key: str) -> str:
        return os.path.join(UPLOAD_DIR, s3_key.split('/')[-1])
