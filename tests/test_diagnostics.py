import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from taskboard.cli import Client, main
from taskboard.diagnostics import diagnose, format_report
from taskboard.service import health


class FakeRegistry:
    def __init__(self, config_path=None):
        self.config_path = config_path

    def list_adapters(self):
        return [
            {'id': 'codex', 'name': 'Codex CLI', 'available': True, 'enabled': True,
             'executable': '/Applications/Codex.app/Contents/Resources/codex'},
            {'id': 'claude', 'name': 'Claude Code', 'available': False, 'enabled': True},
            {'id': 'custom-agent', 'name': '本机自定义 Agent', 'available': True, 'enabled': False,
             'executable': '/a path/agent'},
        ]


class DiagnosticsTests(unittest.TestCase):
    def test_reports_discovery_separately_from_authentication(self):
        with patch('taskboard.diagnostics.AdapterRegistry', FakeRegistry), \
                patch('taskboard.diagnostics._service', return_value={'url': 'http://127.0.0.1:8766',
                    'status': 'unreachable', 'message': '服务未启动。'}):
            report = diagnose()
        self.assertTrue(report['ok'])
        self.assertEqual(report['authentication'], 'not_checked')
        self.assertEqual(report['configuration']['customCount'], 1)
        self.assertTrue(report['adapters'][0]['available'])
        self.assertEqual(report['adapters'][0]['authentication'], 'not_checked')
        self.assertEqual(report['adapters'][2]['source'], 'custom')
        self.assertIn('未检查登录', format_report(report))
        self.assertIn('Windows 原生', format_report(report))

    def test_invalid_configuration_is_reported_without_traceback(self):
        with tempfile.TemporaryDirectory(prefix='诊断 空格 ') as directory:
            config = Path(directory) / 'adapters.json'
            config.write_text('{broken', encoding='utf-8')
            with patch('taskboard.diagnostics._service', return_value={'url': 'http://127.0.0.1:8766',
                    'status': 'unreachable', 'message': '未启动'}):
                report = diagnose(config_path=str(config))
        self.assertFalse(report['ok'])
        self.assertFalse(report['configuration']['loaded'])
        self.assertEqual(report['configuration']['source'], 'argument')
        self.assertIn('error', report['configuration'])

    def test_doctor_json_is_parseable(self):
        output = io.StringIO()
        with patch('taskboard.diagnostics.AdapterRegistry', FakeRegistry), \
                patch('taskboard.diagnostics._service', return_value={'url': 'http://127.0.0.1:8769',
                    'status': 'running', 'message': '正在响应', 'pid': 42}), contextlib.redirect_stdout(output):
            code = main(['doctor', '--json', '--port', '8769'])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['service']['pid'], 42)
        self.assertIn('python', report)

    def test_unrelated_health_json_is_not_a_service(self):
        for response in ([], {'ok': False, 'service': 'task-workbench'}, {'ok': True, 'service': 'other'}):
            with patch('taskboard.service.urlopen', return_value=io.BytesIO(json.dumps(response).encode())):
                self.assertIsNone(health(8769))

    def test_client_rejects_non_root_or_non_loopback_urls(self):
        for url in ('https://127.0.0.1:8766', 'http://example.com', 'http://localhost:0',
                    'http://localhost:8766/wrong', 'http://localhost:8766/?q=1', 'http://localhost:8766/#fragment'):
            with self.assertRaises(ValueError, msg=url):
                Client(url)


if __name__ == '__main__':
    unittest.main()
