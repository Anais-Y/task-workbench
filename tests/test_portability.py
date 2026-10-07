import json
import http.client
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from unittest.mock import patch

from taskboard.cli import Client, ensure_initial_project
from taskboard.store import Store
from taskboard.server import make_server


ROOT = Path(__file__).resolve().parent.parent


def unused_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class ResolverIndependentServerTests(unittest.TestCase):
    def test_loopback_start_and_health_do_not_require_dns(self):
        # Exercise the real server constructor, bind/listen and HTTP exchange;
        # only the external DNS functions are forbidden, not our server code.
        for host in ('127.0.0.1', 'localhost'):
            with self.subTest(host=host), \
                    patch('socket.getfqdn', side_effect=AssertionError('Unexpected reverse DNS lookup')) as fqdn, \
                    patch('socket.gethostbyaddr', side_effect=AssertionError('Unexpected reverse DNS lookup')) as reverse:
                server = make_server(None, None, None, host=host, port=0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=1)
                try:
                    self.assertEqual(server.server_name, '127.0.0.1')
                    connection.request('GET', '/api/health')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertTrue(json.loads(response.read())['ok'])
                    fqdn.assert_not_called()
                    reverse.assert_not_called()
                finally:
                    connection.close()
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)


class InitialProjectTests(unittest.TestCase):
    def test_empty_store_gets_one_empty_real_project(self):
        with tempfile.TemporaryDirectory(prefix='任务管理器 空格 ') as directory:
            store = Store(Path(directory) / 'empty.sqlite3')
            try:
                result = ensure_initial_project(store, directory)
                self.assertEqual(result['name'], '建立一个任务管理器')
                self.assertEqual(result['path'], str(Path(directory).resolve()))
                self.assertEqual(len(store.list_projects()), 1)
                self.assertEqual(store.list_tasks(), [])
                revision = store.version()
                self.assertIsNone(ensure_initial_project(store, directory))
                self.assertEqual(store.version(), revision)
            finally:
                store.close()

    def test_existing_projects_and_tasks_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'existing.sqlite3')
            try:
                project = store.create_project({'id': 'my-project', 'name': '我的项目', 'path': directory})
                task = store.create_task({'title': '保留任务', 'projectId': project['id']})
                revision = store.version()
                self.assertIsNone(ensure_initial_project(store, directory))
                self.assertEqual(store.list_projects(), [project])
                self.assertEqual(store.list_tasks(), [task])
                self.assertEqual(store.version(), revision)
            finally:
                store.close()


@unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'Local services are tested on macOS/Linux')
class CloneStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='新设备 仓库 ')
        self.root = Path(self.temp.name) / '任务 工作台'
        self.root.mkdir()
        shutil.copytree(ROOT / 'taskboard', self.root / 'taskboard', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'web', self.root / 'web')
        self.db = self.root / '.data' / '全新 数据.sqlite3'
        self.port = unused_port()
        self.url = f'http://127.0.0.1:{self.port}'
        self.process = None
        self.env = dict(os.environ)
        self.env.pop('TASKBOARD_ADAPTERS', None)

    def tearDown(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.temp.cleanup()

    def wait_ready(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process and self.process.poll() is not None:
                self.fail('Fresh clone server exited before becoming ready')
            try:
                with urlopen(self.url + '/api/health', timeout=.5) as response:
                    if json.load(response).get('ok'):
                        return
            except (OSError, URLError):
                time.sleep(.05)
        self.fail('Fresh clone server did not become ready')

    def launch_foreground(self):
        self.process = subprocess.Popen([sys.executable, '-m', 'taskboard', 'serve', '--port', str(self.port),
            '--db', str(self.db)], cwd=self.root, env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait_ready()

    def test_fresh_serve_and_restart_preserve_task_in_unicode_clone(self):
        self.launch_foreground()
        client = Client(self.url)
        state = client.request('/api/state')
        self.assertEqual(len(state['projects']), 1)
        self.assertEqual(state['projects'][0]['path'], str(self.root.resolve()))
        self.assertEqual(state['tasks'], [])
        task = client.request('/api/tasks', {'projectId': 'task-manager', 'title': '另机保留任务'})
        self.process.terminate()
        self.process.wait(timeout=10)
        self.launch_foreground()
        after = client.request('/api/state')
        self.assertEqual(after['projects'], state['projects'])
        self.assertEqual(after['tasks'], [task])

    def test_background_start_and_stop_in_unicode_clone(self):
        command = [sys.executable, '-m', 'taskboard']
        try:
            result = subprocess.run(command + ['start', '--port', str(self.port), '--db', str(self.db)],
                cwd=self.root, env=self.env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            state = Client(self.url).request('/api/state')
            self.assertEqual(state['tasks'], [])
            self.assertEqual(state['projects'][0]['path'], str(self.root.resolve()))
            diagnosis = subprocess.run(command + ['doctor', '--json', '--port', str(self.port)],
                cwd=self.root, env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(diagnosis.returncode, 0, diagnosis.stderr)
            self.assertEqual(json.loads(diagnosis.stdout)['service']['status'], 'running')
        finally:
            stopped = subprocess.run(command + ['stop', '--port', str(self.port)], cwd=self.root,
                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(stopped.returncode, 0, stopped.stderr + stopped.stdout)


if __name__ == '__main__':
    unittest.main()
