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
        self.calls = []

    def snapshot(self):
        return {"workers": [], "maxWorkers": 3}

    def handle_action(self, task_id, action, payload):
        if action != "start":
            raise ValueError("未知操作")
        return self.store.update_task(task_id, {"scheduled": True})

    def handle_project_action(self, project_id, action):
        self.calls.append(("project", project_id, action))
        return self.store.delete_project(project_id) if action == "delete" else self.store.restore_project(project_id)

    def assign_task_node(self, task_id, node_id):
        self.calls.append(("node", task_id, node_id))
        return self.store.assign_task_node(task_id, node_id)


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
        self.runner = FakeRunner(self.store)
        self.server = make_server(self.store, FakeRegistry(), self.runner, port=0, web_root=self.web)
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

    def test_nodes_family_assignment_and_trajectory_via_api(self):
        status, _, content = self.request("POST", "/api/nodes", {"title": "语义检索方向",
            "projectId": self.project["id"], "hypothesis": "待验证假设", "color": "violet"})
        self.assertEqual(status, 201)
        node = json.loads(content)
        parent = self.store.create_task({"title": "父任务", "projectId": self.project["id"]})
        child = self.store.create_task({"title": "子任务", "projectId": self.project["id"], "parentTaskId": parent["id"]})
        status, _, content = self.request("POST", f"/api/tasks/{child['id']}/node", {"nodeId": node["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["nodeId"], node["id"])
        self.assertEqual(self.store.get_task(parent["id"])["nodeId"], node["id"])
        self.assertEqual(self.runner.calls[-1], ("node", child["id"], node["id"]))
        status, _, content = self.request("POST", f"/api/nodes/{node['id']}",
            {"outcome": "adopted", "conclusion": "实验结论第一版"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["conclusion"], "实验结论第一版")
        status, _, content = self.request("GET", "/api/state")
        state = json.loads(content)
        self.assertEqual(status, 200)
        self.assertEqual(len(state["nodes"]), 1)
        self.assertEqual(state["deletedProjects"], [])
        self.assertIn("node_conclusion", [event["type"] for event in state["trajectory"]])
        status, _, content = self.request("POST", f"/api/tasks/{parent['id']}/node", {"nodeId": None})
        self.assertEqual(status, 200)
        self.assertIsNone(self.store.get_task(child["id"])["nodeId"])

    def test_project_delete_and_restore_via_runner_hide_state(self):
        task = self.store.create_task({"title": "保留任务", "projectId": self.project["id"]})
        route = f"/api/projects/{self.project['id']}/actions"
        status, _, content = self.request("POST", route, {"action": "delete"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(content)["deletedAt"])
        self.assertEqual(self.runner.calls[-1], ("project", self.project["id"], "delete"))
        _, _, content = self.request("GET", "/api/state")
        state = json.loads(content)
        self.assertEqual(state["projects"], [])
        self.assertEqual(state["tasks"], [])
        self.assertEqual(state["trajectory"], [])
        self.assertEqual(len(state["deletedProjects"]), 1)
        status, _, _ = self.request("POST", route, {"action": "restore"})
        self.assertEqual(status, 200)
        _, _, content = self.request("GET", "/api/state")
        state = json.loads(content)
        self.assertEqual(state["tasks"], [task])
        self.assertEqual(state["deletedProjects"], [])
        self.assertEqual(state["trajectory"][-1]["type"], "project_restored")

    def test_exploration_routes_reject_invalid_or_unauthorized_changes(self):
        task = self.store.create_task({"title": "保留任务", "projectId": self.project["id"], "scheduled": True})
        status, _, content = self.request("POST", f"/api/projects/{self.project['id']}/actions", {"action": "delete"})
        self.assertEqual(status, 400)
        self.assertIn("已排队", json.loads(content)["error"])
        status, _, _ = self.request("POST", "/api/nodes", {"title": "节点", "projectId": self.project["id"]},
            {"X-Taskboard-Client": ""})
        self.assertEqual(status, 403)
        for payload in ({}, {"nodeId": 123}, {"nodeId": "missing"}):
            status, _, content = self.request("POST", f"/api/tasks/{task['id']}/node", payload)
            self.assertEqual(status, 400)
            self.assertIn("error", json.loads(content))
        status, _, _ = self.request("POST", "/api/nodes/missing", {"title": "不存在"})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
