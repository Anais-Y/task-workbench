import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from taskboard.server import MAX_ARTIFACT_BYTES, make_server
from taskboard.store import Store


class FakeRegistry:
    def list_adapters(self):
        return [{"id": "codex", "name": "Codex CLI", "available": True}]


class FakeRunner:
    def __init__(self, store):
        self.store = store

    def snapshot(self):
        return {"workers": [], "maxWorkers": 3}

    def handle_action(self, task_id, action, payload):
        if action != "start":
            raise ValueError("未知操作")
        return self.store.update_task(task_id, {"scheduled": True})


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.workspace = self.path / "project"
        self.workspace.mkdir()
        self.web = self.path / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<!doctype html><h1>任务工作台</h1>", encoding="utf-8")
        self.store = Store(self.path / "state.sqlite3")
        self.project = self.store.create_project({"name": "建立一个任务管理器", "path": str(self.workspace)})
        self.server = make_server(self.store, FakeRegistry(), FakeRunner(self.store), port=0, web_root=self.web)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        request_headers = dict(headers or {})
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")
            request_headers.setdefault("X-Taskboard-Client", "taskboard")
            body = json.dumps(body, ensure_ascii=False)
        client.request(method, path, body=body.encode() if body is not None else None, headers=request_headers)
        response = client.getresponse()
        content = response.read()
        result = (response.status, dict(response.getheaders()), content)
        client.close()
        return result

    def test_frontend_and_state(self):
        status, headers, content = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("任务工作台".encode(), content)
        status, headers, content = self.request("GET", "/api/state")
        state = json.loads(content)
        self.assertEqual(status, 200)
        self.assertEqual(state["projects"][0]["name"], "建立一个任务管理器")
        self.assertEqual(state["maxWorkers"], 3)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_create_task_and_dispatch_action(self):
        status, _, content = self.request("POST", "/api/tasks", {"title": "接通工作台", "projectId": self.project["id"]})
        self.assertEqual(status, 201)
        item = json.loads(content)
        self.assertFalse(item["scheduled"])
        status, _, content = self.request("POST", f"/api/tasks/{item['id']}/actions", {"action": "start"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(content)["scheduled"])
        status, _, content = self.request("POST", "/api/tasks", {"title": "直接开始", "projectId": self.project["id"], "start": True})
        self.assertEqual(status, 201)
        self.assertTrue(json.loads(content)["scheduled"])

    def test_security_headers_and_origin_checks(self):
        status, _, _ = self.request("POST", "/api/tasks", {}, {"X-Taskboard-Client": ""})
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/tasks", {}, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/state", headers={"Host": f"evil.example:{self.port}"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/state", headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", "/api/tasks", {}, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        with self.assertRaises(ValueError):
            make_server(self.store, FakeRegistry(), FakeRunner(self.store), host="0.0.0.0", port=0)

    def test_artifact_inline_file_and_escape_rejection(self):
        (self.workspace / "result.txt").write_text("实际产物", encoding="utf-8")
        outside = self.path / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        (self.workspace / "escape.txt").symlink_to(outside)
        item = self.store.create_task({"title": "产物", "projectId": self.project["id"], "artifacts": [
            {"name": "内联", "type": "text", "body": "内联内容"},
            {"name": "文件", "type": "text", "path": "result.txt"},
            {"name": "逃逸", "type": "text", "path": "../outside.txt"},
            {"name": "符号链接", "type": "text", "path": "escape.txt"},
            {"name": "过大", "type": "text", "body": "x" * (MAX_ARTIFACT_BYTES + 1)},
        ]})
        url = f"/api/tasks/{item['id']}/artifacts/"
        for index, expected in [(0, "内联内容"), (1, "实际产物")]:
            status, _, content = self.request("GET", url + str(index))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(content)["body"], expected)
        for index in (2, 3):
            status, _, _ = self.request("GET", url + str(index))
            self.assertEqual(status, 403)
        status, _, _ = self.request("GET", url + "4")
        self.assertEqual(status, 413)
        status, _, _ = self.request("GET", url + "9")
        self.assertEqual(status, 404)

    def test_unknown_routes_and_static_escape(self):
        for method, path in [("GET", "/api/missing"), ("POST", "/api/missing")]:
            status, _, content = self.request(method, path, {} if method == "POST" else None)
            self.assertEqual(status, 404)
            self.assertIn("error", json.loads(content))
        status, _, _ = self.request("GET", "/%2e%2e/state.sqlite3")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
