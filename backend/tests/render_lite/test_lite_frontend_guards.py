from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class LiteFrontendGuardTests(unittest.TestCase):
    def test_message_input_has_scanned_pdf_cleanup_and_format_limits(self):
        source = (ROOT / 'src/lib/components/chat/MessageInput.svelte').read_text(encoding='utf-8')

        self.assertIn('isFullyScannedLitePdf(uploadedFile)', source)
        self.assertIn('await deleteFileById(localStorage.token, uploadedFile.id)', source)
        self.assertIn('visionCapableModels.length !== selectedModelIds.length', source)
        self.assertIn('text_max_bytes: 128 * 1024', source)
        self.assertIn('pptx_max_bytes: 15 * 1024 * 1024', source)
        self.assertIn('超过${limit.label}的', source)

    def test_authenticated_config_exposes_lite_limits(self):
        source = (ROOT / 'backend/open_webui/main.py').read_text(encoding='utf-8')

        self.assertIn("'lite_limits': {", source)
        self.assertIn("'pdf_max_pages': LITE_PDF_MAX_PAGES", source)
        self.assertIn("'open_webui_lite_mode': OPEN_WEBUI_LITE_MODE", source)

    def test_backend_size_errors_are_format_specific_and_chinese(self):
        source = (ROOT / 'backend/open_webui/utils/files_lite.py').read_text(encoding='utf-8')

        self.assertIn("label = f'文本型 PDF（最多读取前 {LITE_PDF_MAX_PAGES} 页）'", source)
        self.assertIn("label = '文本或代码文件'", source)
        self.assertIn("f'{label}超过当前 {_format_lite_byte_limit(limit)} 限制", source)

    def test_lite_code_execution_is_forced_to_pyodide(self):
        router = (ROOT / 'backend/open_webui/routers/configs.py').read_text(encoding='utf-8')
        settings = (ROOT / 'src/lib/components/admin/Settings/CodeExecution.svelte').read_text(
            encoding='utf-8'
        )

        self.assertGreaterEqual(router.count("updates['CODE_EXECUTION_ENGINE'] = 'pyodide'"), 1)
        self.assertIn("values['CODE_INTERPRETER_ENGINE'] = 'pyodide'", router)
        self.assertIn("liteMode ? ['pyodide'] : ['pyodide', 'jupyter']", settings)
        self.assertIn('不支持 Shell、子进程或任意软件包安装', settings)

    def test_startup_prefers_go_proxy_with_python_emergency_fallback(self):
        startup = (ROOT / 'backend/start.sh').read_text(encoding='utf-8')
        dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')

        self.assertIn('RENDER_BOOT_PROXY_IMPLEMENTATION:-go', startup)
        self.assertIn('exec "$BOOT_PROXY_BINARY"', startup)
        self.assertIn('render_boot_proxy.py', startup)
        self.assertIn('/out/render-boot-proxy /app/bin/render-boot-proxy', dockerfile)

    def test_memory_probe_reports_cgroup_cache_breakdown(self):
        source = (ROOT / 'backend/open_webui/routers/lite_debug.py').read_text(encoding='utf-8')

        self.assertIn("('anon', 'file', 'kernel', 'kernel_stack', 'pagetables', 'sock', 'shmem', 'slab')", source)
        self.assertIn("'breakdown': _read_memory_stat", source)


if __name__ == '__main__':
    unittest.main()
