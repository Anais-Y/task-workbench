#!/usr/bin/env python3
"""Offline end-to-end protocol check using real child processes and HTTP.

No installed model CLI, account, API key, or existing workbench is needed.
Creates its own temporary workspace, SQLite database and loopback HTTP port.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

EXAMPLES = Path(__file__).resolve().parent
sys.path.insert(0, str(EXAMPLES.parent))

from setup_example import create_config
from taskboard.adapters import AdapterRegistry
from taskboard.cli import Client
from taskboard.runner import Runner
from taskboard.server import make_server
from taskboard.store import Store


def check():
    stages = []
    with tempfile.TemporaryDirectory(prefix="taskboard 协议自检 ") as temporary:
        workspace = Path(temporary) / "项目 空格与中文"
        workspace.mkdir()
        config = create_config(Path(temporary) / "执行器 配置.json")
        registry = AdapterRegistry(config)
        store = Store(str(Path(temporary) / "tasks.sqlite3"))
        runner = Runner(store, registry, interval=0.02)
        server = make_server(store, registry, runner, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        runner.start()
        client = Client("http://127.0.0.1:" + str(server.server_address[1]))

        def task_by_id(task_id):
            return next(task for task in client.request("/api/state")["tasks"] if task["id"] == task_id)

        def wait_for(task_id, predicate, label, timeout=8):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                task = task_by_id(task_id)
                if predicate(task):
                    return task
                if task["status"] == "failed":
                    raise AssertionError(f"{label}失败：{task.get('error')}")
                time.sleep(0.02)
            raise AssertionError(f"等待{label}超时：{task_by_id(task_id)}")

        def action(task_id, name, **payload):
            return client.request(f"/api/tasks/{task_id}/actions", {"action": name, **payload})

        try:
            project = client.request("/api/projects", {"name": "协议自检 · 非 AI", "path": str(workspace)})
            parent = client.request("/api/tasks", {"title": "验证真实子进程协议", "projectId": project["id"],
                "engine": "protocol-example", "phase": "plan", "start": True})
            planned = wait_for(parent["id"], lambda task: task["status"] == "review", "计划")
            assert planned["kind"] == "plan" and len(planned["plan"]) == 1, "计划未被解析"
            assert not (workspace / ".taskboard-example").exists(), "只读计划阶段写了文件"
            stages.append("HTTP 创建任务、真实子进程只读计划")
            approved = action(parent["id"], "approve_plan")
            child_id = approved["children"][0]
            first = wait_for(child_id, lambda task: task["status"] == "review", "执行")
            assert any("第 1 轮" in line[1] for line in first["log"]), "缺少子进程日志"
            artifact_index = next(i for i, item in enumerate(first["artifacts"]) if item.get("path"))
            first_body = client.request(f"/api/tasks/{child_id}/artifacts/{artifact_index}")["body"]
            assert "不是真正的 AI" in first_body, "示例没有标注能力边界"
            stages.append("批准计划、真实文件产物和 HTTP 预览")
            session_id = first["sessionId"]
            feedback = "请保留这条反馈：中文 spaces & literal $()。"
            action(child_id, "feedback", message=feedback)
            second = wait_for(child_id, lambda task: task["status"] == "review" and len(task["runs"]) == 2, "反馈恢复")
            assert second["sessionId"] == session_id, "反馈未恢复同一会话"
            session_file = workspace / ".taskboard-example" / "sessions" / (session_id + ".json")
            session = json.loads(session_file.read_text(encoding="utf-8"))
            assert session["round"] == 2 and feedback in session["lastPrompt"], "子进程没有收到恢复参数或反馈"
            revised_path = workspace / ".taskboard-example" / "results" / session_id / "run-2.md"
            assert feedback in revised_path.read_text(encoding="utf-8"), "反馈没有写入真实第二轮产物"
            action(child_id, "accept")
            assert task_by_id(parent["id"])["status"] == "done", "验收未完成父任务"
            stages.append("反馈经 stdin 送达、跨进程 resume、人工验收")
            slow = client.request("/api/tasks", {"title": "验证进程取消", "projectId": project["id"],
                "engine": "protocol-example-slow", "phase": "execute", "kind": "result", "start": True})
            running = wait_for(slow["id"], lambda task: any("等待期间写入心跳" in line[1] for line in task["log"]), "可取消执行")
            slow_file = workspace / ".taskboard-example" / "sessions" / (running["sessionId"] + ".json")
            action(slow["id"], "cancel")
            stopped = wait_for(slow["id"], lambda task: task["status"] == "cancelled", "取消")
            assert stopped["runs"][-1]["status"] == "cancelled", "取消未写入执行记录"
            after_cancel = slow_file.read_bytes()
            time.sleep(0.15)
            assert slow_file.read_bytes() == after_cancel, "取消后子进程仍在写心跳"
            assert all(worker["taskId"] != slow["id"] for worker in client.request("/api/state")["workers"]), "取消后仍占用 worker"
            assert not (workspace / ".taskboard-example" / "results" / running["sessionId"]).exists(), "取消任务竟然产生完成文件"
            stages.append("HTTP 取消真实子进程、心跳停止、worker 释放")
            return {"ok": True, "isAI": False, "checks": stages, "modelRequests": 0}
        finally:
            runner.stop()
            server.shutdown()
            server.server_close()
            thread.join(2)
            store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="无需模型登录的真实子进程协议自检")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)
    try:
        result = check()
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("协议自检通过。这个示例不是 AI，未调用任何模型。")
        for stage in result["checks"]:
            print("  ✓ " + stage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
