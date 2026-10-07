"""Thread-safe SQLite storage for local Task Workbench state."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


STATUSES = {"queued", "running", "review", "done", "failed", "cancelled"}
KINDS = {"plan", "result", "question"}
PHASES = {"plan", "execute"}
MAX_LOG_ENTRIES = 500
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _copy(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("数据必须是有效的 JSON") from exc


def _identifier(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label}格式不正确")


def _strings(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label}必须是文字列表")


class Store:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(Path(self.db_path).expanduser())
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, data TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL);"
            "INSERT OR IGNORE INTO metadata VALUES ('version', 0);"
            "INSERT OR IGNORE INTO metadata VALUES ('next_task', 1);"
        )
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    def version(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT value FROM metadata WHERE key='version'").fetchone()[0]

    def _bump(self):
        self._conn.execute("UPDATE metadata SET value=value+1 WHERE key='version'")

    def _get(self, table, item_id):
        row = self._conn.execute(f"SELECT data FROM {table} WHERE id=?", (item_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _list(self, table):
        return [json.loads(row[0]) for row in self._conn.execute(f"SELECT data FROM {table} ORDER BY rowid")]

    def _write(self, table, item, create=False):
        data = json.dumps(item, ensure_ascii=False, allow_nan=False)
        if create:
            self._conn.execute(f"INSERT INTO {table}(id,data) VALUES (?,?)", (item["id"], data))
        else:
            self._conn.execute(f"UPDATE {table} SET data=? WHERE id=?", (data, item["id"]))
        self._bump()

    def list_projects(self):
        with self._lock:
            return self._list("projects")

    def get_project(self, project_id):
        with self._lock:
            return self._get("projects", project_id)

    def create_project(self, data):
        if not isinstance(data, dict):
            raise ValueError("项目内容必须是对象")
        item = _copy(data)
        item.setdefault("id", "P-" + uuid.uuid4().hex[:12])
        _identifier(item["id"], "项目 ID")
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ValueError("请填写项目名称")
        item["name"] = item["name"].strip()
        if not isinstance(item.get("path"), str) or not item["path"].strip():
            raise ValueError("请填写项目的本机绝对路径")
        path = Path(item["path"]).expanduser()
        if not path.is_absolute():
            raise ValueError("项目路径必须是绝对路径")
        if not path.is_dir():
            raise ValueError("项目路径必须是已存在的文件夹")
        item["path"] = str(path.resolve())
        item["createdAt"] = now()
        with self._lock, self._conn:
            if self._get("projects", item["id"]):
                raise ValueError("项目 ID 已存在")
            self._write("projects", item, create=True)
        return _copy(item)

    def list_tasks(self):
        with self._lock:
            return self._list("tasks")

    def get_task(self, task_id):
        with self._lock:
            return self._get("tasks", task_id)

    def _validate_task(self, item):
        _identifier(item["id"], "任务 ID")
        if not isinstance(item.get("status"), str) or item["status"] not in STATUSES:
            raise ValueError("未知任务状态")
        if (not isinstance(item.get("kind"), str) or item["kind"] not in KINDS
                or not isinstance(item.get("phase"), str) or item["phase"] not in PHASES):
            raise ValueError("未知任务阶段")
        if not isinstance(item.get("executionMode"), str) or item["executionMode"] not in {"local", "external"}:
            raise ValueError("未知任务执行方式")
        if not isinstance(item.get("scheduled"), bool):
            raise ValueError("排队标记必须是布尔值")
        if not isinstance(item.get("title"), str) or not item["title"].strip():
            raise ValueError("请填写任务名称")
        item["title"] = item["title"].strip()
        for key in ("desc", "goal", "engine", "result"):
            if not isinstance(item.get(key), str):
                raise ValueError(f"{key} 必须是文字")
        if not item["engine"].strip():
            raise ValueError("请选择执行器")
        for key in ("worker", "sessionId", "sessionEngine", "error", "parentTaskId"):
            if item.get(key) is not None and not isinstance(item[key], str):
                raise ValueError(f"{key} 必须是文字或空值")
        for key in ("criteria", "steps", "deps"):
            _strings(item.get(key), key)
        if len(set(item["deps"])) != len(item["deps"]):
            raise ValueError("不能重复添加同一依赖任务")
        if type(item.get("done")) is not int or not 0 <= item["done"] <= len(item["steps"]):
            raise ValueError("已完成步骤数必须在任务步骤范围内")
        for key in ("artifacts", "runs", "plan"):
            if not isinstance(item.get(key), list) or any(not isinstance(x, dict) for x in item[key]):
                raise ValueError(f"{key} 必须是对象列表")
        for artifact in item["artifacts"]:
            if not isinstance(artifact.get("name"), str) or not isinstance(artifact.get("type"), str):
                raise ValueError("产物必须包含名称和类型")
            for key in ("body", "path"):
                if key in artifact and not isinstance(artifact[key], str):
                    raise ValueError("产物内容和路径必须是文字")
        log = item.get("log")
        if not isinstance(log, list) or any(
            not isinstance(entry, list) or len(entry) != 2 or any(not isinstance(x, str) for x in entry)
            for entry in log
        ):
            raise ValueError("任务日志格式不正确")
        item["log"] = log[-MAX_LOG_ENTRIES:]
        project_id = item.get("projectId")
        if not isinstance(project_id, str) or not self._get("projects", project_id):
            raise ValueError("任务所属项目不存在")

        # Validate the whole graph so moving a task cannot strand its dependents.
        tasks = {task["id"]: task for task in self._list("tasks")}
        tasks[item["id"]] = item
        for task in tasks.values():
            for dependency in task["deps"]:
                if dependency not in tasks:
                    raise ValueError(f"依赖任务不存在：{dependency}")
                if tasks[dependency]["projectId"] != task["projectId"]:
                    raise ValueError("依赖任务必须属于同一个项目")
            parent = task.get("parentTaskId")
            if parent and (parent not in tasks or tasks[parent]["projectId"] != task["projectId"]):
                raise ValueError("父任务必须存在且属于同一个项目")

        def check_cycles(edges, message):
            visited, active = set(), set()
            for task_id in tasks:
                stack = [(task_id, False)]
                while stack:
                    current, leaving = stack.pop()
                    if leaving:
                        active.remove(current)
                        visited.add(current)
                    elif current in active:
                        raise ValueError(message)
                    elif current not in visited:
                        active.add(current)
                        stack.append((current, True))
                        stack.extend((dependency, False) for dependency in edges(tasks[current]))

        check_cycles(lambda task: task["deps"], "任务依赖不能形成循环")
        check_cycles(lambda task: [task["parentTaskId"]] if task.get("parentTaskId") else [], "父子任务不能形成循环")

    def create_task(self, data):
        if not isinstance(data, dict):
            raise ValueError("任务内容必须是对象")
        supplied = _copy(data)
        stamp = now()
        item = {
            "title": "", "desc": "", "goal": "", "criteria": [], "engine": "codex",
            "status": "queued", "kind": "plan", "phase": "plan", "steps": [], "done": 0,
            "deps": [], "worker": None, "executionMode": "local", "scheduled": False,
            "artifacts": [], "log": [], "runs": [], "sessionId": None, "sessionEngine": None,
            "error": None, "result": "", "plan": [], "parentTaskId": None,
        }
        item.update(supplied)
        item["createdAt"] = item["updatedAt"] = stamp
        with self._lock, self._conn:
            next_id = self._conn.execute("SELECT value FROM metadata WHERE key='next_task'").fetchone()[0]
            item.setdefault("id", f"T{next_id:03d}")
            _identifier(item["id"], "任务 ID")
            while "id" not in supplied and self._get("tasks", item["id"]):
                next_id += 1
                item["id"] = f"T{next_id:03d}"
            if self._get("tasks", item["id"]):
                raise ValueError("任务 ID 已存在")
            self._validate_task(item)
            match = re.fullmatch(r"T(\d+)", item["id"])
            next_id = max(next_id, int(match.group(1)) if match else 0) + 1
            self._write("tasks", item, create=True)
            self._conn.execute("UPDATE metadata SET value=? WHERE key='next_task'", (next_id,))
        return _copy(item)

    def update_task(self, task_id, patch):
        if not isinstance(patch, dict):
            raise ValueError("任务更新必须是对象")
        patch = _copy(patch)
        with self._lock, self._conn:
            item = self._get("tasks", task_id)
            if item is None:
                raise KeyError(task_id)
            if "id" in patch and patch["id"] != task_id:
                raise ValueError("不能修改任务 ID")
            if "createdAt" in patch and patch["createdAt"] != item["createdAt"]:
                raise ValueError("不能修改任务创建时间")
            item.update(patch)
            item["updatedAt"] = now()
            self._validate_task(item)
            self._write("tasks", item)
            return _copy(item)

    def add_event(self, task_id, message, **fields):
        if not isinstance(message, str) or not message.strip():
            raise ValueError("日志内容不能为空")
        with self._lock:
            item = self._get("tasks", task_id)
            if item is None:
                raise KeyError(task_id)
            fields["log"] = (item["log"] + [[now(), message]])[-MAX_LOG_ENTRIES:]
            return self.update_task(task_id, fields)
