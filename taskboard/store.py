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
NODE_COLORS = {"blue", "violet", "teal", "amber", "rose", "slate"}
NODE_OUTCOMES = {"exploring", "adopted", "discarded", "inconclusive"}
STATUS_LABELS = {"queued": "待开始", "running": "进行中", "review": "待审核", "done": "已完成", "failed": "失败", "cancelled": "已取消"}
OUTCOME_LABELS = {"exploring": "探索中", "adopted": "已采用", "discarded": "已舍弃", "inconclusive": "尚无定论"}
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
            "CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, data TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS trajectory (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "id TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL, at TEXT NOT NULL, data TEXT NOT NULL);"
            "CREATE INDEX IF NOT EXISTS trajectory_project_time ON trajectory(project_id, at, seq);"
            "CREATE TRIGGER IF NOT EXISTS trajectory_no_update BEFORE UPDATE ON trajectory "
            "BEGIN SELECT RAISE(ABORT, 'trajectory is append-only'); END;"
            "CREATE TRIGGER IF NOT EXISTS trajectory_no_delete BEFORE DELETE ON trajectory "
            "BEGIN SELECT RAISE(ABORT, 'trajectory is append-only'); END;"
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL);"
            "INSERT OR IGNORE INTO metadata VALUES ('version', 0);"
            "INSERT OR IGNORE INTO metadata VALUES ('next_task', 1);"
        )
        self._conn.commit()
        self._migrate_v2()

    def _record_trajectory(self, project_id, event_type, title, summary, *, node_id=None, task_id=None, at=None):
        event = {"id": "E-" + uuid.uuid4().hex, "projectId": project_id, "nodeId": node_id,
                 "taskId": task_id, "type": event_type, "at": at or now(), "title": title,
                 "summary": summary}
        self._conn.execute("INSERT INTO trajectory(id,project_id,at,data) VALUES (?,?,?,?)",
                           (event["id"], project_id, event["at"], json.dumps(event, ensure_ascii=False)))

    def _migrate_v2(self):
        """Backfill only facts present in v0.1; do not infer past transitions."""
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if row and row[0] >= 2:
                return
            projects, tasks = self._list("projects"), self._list("tasks")
            for project in projects:
                project.setdefault("deletedAt", None)
                self._conn.execute("UPDATE projects SET data=? WHERE id=?",
                    (json.dumps(project, ensure_ascii=False), project["id"]))
            migrated_at = now()
            for task in tasks:
                task.setdefault("nodeId", None)
                self._conn.execute("UPDATE tasks SET data=? WHERE id=?",
                    (json.dumps(task, ensure_ascii=False), task["id"]))
                known_creation = None
                try:
                    original = datetime.fromisoformat(task.get("createdAt", "").replace("Z", "+00:00"))
                    if original.tzinfo is not None:
                        known_creation = original.astimezone(timezone.utc).isoformat(timespec="milliseconds")
                except (AttributeError, TypeError, ValueError):
                    pass
                if known_creation:
                    self._record_trajectory(task["projectId"], "task_created", "创建任务 · " + task["title"],
                        "依据原记录的 createdAt 补录任务创建时间；未推断此前的执行过程。",
                        task_id=task["id"], node_id=task.get("nodeId"), at=known_creation)
                summary = "这是升级时的已知状态快照，不代表过去的状态变更时间。\n当前状态：" + STATUS_LABELS.get(task.get("status"), str(task.get("status", "未知")))
                if task.get("result"):
                    summary += "\n升级时保存的结果：\n" + task["result"]
                self._record_trajectory(task["projectId"], "migration_snapshot", "旧任务迁移快照 · " + task["title"],
                    summary, task_id=task["id"], node_id=task.get("nodeId"), at=migrated_at)
            self._conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('schema_version',2)")
            if projects or tasks:
                self._bump()

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

    def list_projects(self, include_deleted=False):
        with self._lock:
            return [item for item in self._list("projects") if include_deleted or not item.get("deletedAt")]

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
        item["deletedAt"] = None
        with self._lock, self._conn:
            if self._get("projects", item["id"]):
                raise ValueError("项目 ID 已存在")
            self._write("projects", item, create=True)
        return _copy(item)

    def list_tasks(self):
        with self._lock:
            active = {project["id"] for project in self._list("projects") if not project.get("deletedAt")}
            return [item for item in self._list("tasks") if item["projectId"] in active]

    def get_task(self, task_id):
        with self._lock:
            return self._get("tasks", task_id)

    def _active_project(self, project_id):
        project = self._get("projects", project_id) if isinstance(project_id, str) else None
        if project is None:
            raise ValueError("所属项目不存在")
        if project.get("deletedAt"):
            raise ValueError("项目已在回收站，请先恢复项目")
        return project

    def delete_project(self, project_id):
        with self._lock, self._conn:
            project = self._get("projects", project_id)
            if project is None:
                raise KeyError(project_id)
            if project.get("deletedAt"):
                return project
            members = [task for task in self._list("tasks") if task["projectId"] == project_id]
            if any(task.get("cancelFailed") for task in members):
                raise ValueError("项目中有任务尚未确认 Agent 已停止，请先检查对应进程")
            if any(task["status"] == "running" or task.get("scheduled") for task in members):
                raise ValueError("项目中还有运行或已排队的任务，请先停止任务再移入回收站")
            project["deletedAt"] = now()
            self._write("projects", project)
            self._record_trajectory(project_id, "project_deleted", "项目移入回收站 · " + project["name"],
                "项目、节点、任务与历史保留，可随时恢复；工作区文件未删除。", at=project["deletedAt"])
            return _copy(project)

    def restore_project(self, project_id):
        with self._lock, self._conn:
            project = self._get("projects", project_id)
            if project is None:
                raise KeyError(project_id)
            if not project.get("deletedAt"):
                return project
            project["deletedAt"] = None
            self._write("projects", project)
            self._record_trajectory(project_id, "project_restored", "恢复项目 · " + project["name"],
                "项目、节点、任务与历史已恢复。")
            return _copy(project)

    def list_nodes(self, project_id=None):
        with self._lock:
            active = {project["id"] for project in self._list("projects") if not project.get("deletedAt")}
            return [node for node in self._list("nodes") if node["projectId"] in active
                    and (project_id is None or node["projectId"] == project_id)]

    def get_node(self, node_id):
        with self._lock:
            return self._get("nodes", node_id)

    def list_trajectory(self, project_id=None):
        with self._lock:
            if project_id is not None:
                rows = self._conn.execute("SELECT data FROM trajectory WHERE project_id=? ORDER BY at,seq", (project_id,))
                return [json.loads(row[0]) for row in rows]
            active = {project["id"] for project in self._list("projects") if not project.get("deletedAt")}
            return [event for event in (json.loads(row[0]) for row in self._conn.execute("SELECT data FROM trajectory ORDER BY at,seq"))
                    if event["projectId"] in active]

    def _validate_node(self, item):
        _identifier(item["id"], "节点 ID")
        self._active_project(item.get("projectId"))
        if not isinstance(item.get("title"), str) or not item["title"].strip():
            raise ValueError("请填写节点名称")
        item["title"] = item["title"].strip()
        for field in ("hypothesis", "conclusion"):
            if not isinstance(item.get(field), str):
                raise ValueError("假设和结论必须是文字")
        if not isinstance(item.get("color"), str) or item["color"] not in NODE_COLORS:
            raise ValueError("请选择有效的节点颜色")
        if not isinstance(item.get("outcome"), str) or item["outcome"] not in NODE_OUTCOMES:
            raise ValueError("请选择有效的探索结论状态")
        if item["outcome"] != "exploring" and not item["conclusion"].strip():
            raise ValueError("结束探索前，请填写结论")
        _strings(item.get("parentIds"), "父节点")
        if len(set(item["parentIds"])) != len(item["parentIds"]):
            raise ValueError("不能重复选择同一个父节点")
        nodes = {node["id"]: node for node in self._list("nodes")}
        nodes[item["id"]] = item
        for node in nodes.values():
            for parent_id in node["parentIds"]:
                if parent_id not in nodes:
                    raise ValueError("父节点不存在")
                if nodes[parent_id]["projectId"] != node["projectId"]:
                    raise ValueError("父节点必须属于同一个项目")
        self._check_cycles(nodes, lambda node: node["parentIds"], "节点关系不能形成循环")

    def _node_summary(self, item):
        # Save names now, so later renaming a predecessor cannot rewrite what
        # an earlier conclusion said. Display details use the saved text only.
        parents = [self._get("nodes", parent_id)["title"] for parent_id in item["parentIds"]]
        return "结论状态：" + OUTCOME_LABELS[item["outcome"]] + \
            "\n结论：\n" + (item["conclusion"] or "尚未记录") + \
            "\n\n假设：\n" + (item["hypothesis"] or "尚未记录") + \
            "\n\n前序节点：" + ("、".join(parents) or "无")

    def create_node(self, data):
        if not isinstance(data, dict):
            raise ValueError("节点内容必须是对象")
        item = {"id": "N-" + uuid.uuid4().hex[:12], "title": "", "hypothesis": "", "color": "blue",
                "parentIds": [], "outcome": "exploring", "conclusion": ""}
        item.update(_copy(data))
        item["createdAt"] = item["updatedAt"] = now()
        with self._lock, self._conn:
            _identifier(item["id"], "节点 ID")
            if self._get("nodes", item["id"]):
                raise ValueError("节点 ID 已存在")
            self._validate_node(item)
            self._write("nodes", item, create=True)
            self._record_trajectory(item["projectId"], "node_created", "创建节点 · " + item["title"],
                self._node_summary(item), node_id=item["id"], at=item["createdAt"])
            return _copy(item)

    def update_node(self, node_id, patch):
        if not isinstance(patch, dict):
            raise ValueError("节点更新必须是对象")
        patch = _copy(patch)
        with self._lock, self._conn:
            original = self._get("nodes", node_id)
            if original is None:
                raise KeyError(node_id)
            for field in ("id", "projectId", "createdAt"):
                if field in patch and patch[field] != original[field]:
                    raise ValueError("不能修改节点的 ID、所属项目或创建时间")
            item = dict(original, **patch)
            item["updatedAt"] = now()
            self._validate_node(item)
            changed = {field for field in item if field != "updatedAt" and item.get(field) != original.get(field)}
            if not changed:
                return original
            self._write("nodes", item)
            event_type = "node_conclusion" if changed.intersection({"conclusion", "outcome"}) else "node_updated"
            title = ("更新结论 · " if event_type == "node_conclusion" else "更新节点 · ") + item["title"]
            self._record_trajectory(item["projectId"], event_type, title, self._node_summary(item),
                node_id=node_id, at=item["updatedAt"])
            return _copy(item)

    @staticmethod
    def _check_cycles(records, edges, message):
        visited, active = set(), set()
        for record_id in records:
            stack = [(record_id, False)]
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
                    stack.extend((dependency, False) for dependency in edges(records[current]))

    def _validate_task(self, item, task_map=None):
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
            raise ValueError("请选择 Agent")
        for key in ("worker", "sessionId", "sessionEngine", "error", "parentTaskId", "nodeId"):
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
        self._active_project(project_id)
        node_id = item.get("nodeId")
        if node_id is not None:
            node = self._get("nodes", node_id)
            if node is None or node["projectId"] != project_id:
                raise ValueError("任务节点必须存在且属于同一个项目")

        # Validate the whole graph so moving a task cannot strand its dependents.
        tasks = dict(task_map) if task_map is not None else {task["id"]: task for task in self._list("tasks")}
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
            if parent and tasks[parent].get("nodeId") != task.get("nodeId"):
                raise ValueError("父任务与子任务必须属于同一个节点，请使用任务分组操作")

        self._check_cycles(tasks, lambda task: task["deps"], "任务依赖不能形成循环")
        self._check_cycles(tasks, lambda task: [task["parentTaskId"]] if task.get("parentTaskId") else [], "父子任务不能形成循环")

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
            "error": None, "result": "", "plan": [], "parentTaskId": None, "nodeId": None,
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
            if isinstance(item.get("parentTaskId"), str) and item["parentTaskId"] and "nodeId" not in supplied:
                parent = self._get("tasks", item["parentTaskId"])
                if parent:
                    item["nodeId"] = parent.get("nodeId")
            self._validate_task(item)
            match = re.fullmatch(r"T(\d+)", item["id"])
            next_id = max(next_id, int(match.group(1)) if match else 0) + 1
            self._write("tasks", item, create=True)
            self._conn.execute("UPDATE metadata SET value=? WHERE key='next_task'", (next_id,))
            self._record_trajectory(item["projectId"], "task_created", "创建任务 · " + item["title"],
                item["goal"] or item["desc"] or "任务已创建。", node_id=item.get("nodeId"), task_id=item["id"], at=stamp)
            if item["result"]:
                self._record_trajectory(item["projectId"], "task_result", "任务结果版本 · " + item["title"],
                    item["result"], node_id=item.get("nodeId"), task_id=item["id"], at=stamp)
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
            if "nodeId" in patch and patch["nodeId"] != item.get("nodeId"):
                raise ValueError("请使用任务分组操作，同时调整父任务和子任务的节点")
            original = _copy(item)
            item.update(patch)
            item["updatedAt"] = now()
            self._validate_task(item)
            self._write("tasks", item)
            if item["status"] != original["status"]:
                self._record_trajectory(item["projectId"], "task_status", "任务状态变化 · " + item["title"],
                    STATUS_LABELS[original["status"]] + " → " + STATUS_LABELS[item["status"]],
                    node_id=item.get("nodeId"), task_id=task_id, at=item["updatedAt"])
            if item["result"] != original["result"]:
                self._record_trajectory(item["projectId"], "task_result", "任务结果版本 · " + item["title"],
                    item["result"] or "结果已清空。", node_id=item.get("nodeId"), task_id=task_id, at=item["updatedAt"])
            return _copy(item)

    def assign_task_node(self, task_id, node_id):
        if node_id is not None and not isinstance(node_id, str):
            raise ValueError("节点 ID 必须是文字或空值")
        with self._lock, self._conn:
            tasks = {task["id"]: task for task in self._list("tasks")}
            if task_id not in tasks:
                raise KeyError(task_id)
            selected = tasks[task_id]
            self._active_project(selected["projectId"])
            if node_id is not None:
                node = self._get("nodes", node_id)
                if node is None or node["projectId"] != selected["projectId"]:
                    raise ValueError("目标节点必须存在且属于同一个项目")
            ancestor = task_id
            seen = set()
            while tasks[ancestor].get("parentTaskId"):
                if ancestor in seen:
                    raise ValueError("父子任务关系异常")
                seen.add(ancestor)
                ancestor = tasks[ancestor]["parentTaskId"]
                if ancestor not in tasks:
                    raise ValueError("父任务不存在")
            family = {ancestor}
            while True:
                more = {item["id"] for item in tasks.values() if item.get("parentTaskId") in family}
                if more.issubset(family):
                    break
                family.update(more)
            if any(tasks[item_id].get("cancelFailed") for item_id in family):
                raise ValueError("这组任务中有任务尚未确认 Agent 已停止，请先检查对应进程")
            if any(tasks[item_id]["status"] == "running" or tasks[item_id].get("scheduled") for item_id in family):
                raise ValueError("这组任务中还有运行或已排队的任务，请先停止再调整节点")
            changed = [item_id for item_id in tasks if item_id in family and tasks[item_id].get("nodeId") != node_id]
            original = {item_id: tasks[item_id] for item_id in changed}
            stamp = now()
            for item_id in changed:
                tasks[item_id] = dict(tasks[item_id], nodeId=node_id, updatedAt=stamp)
            for item_id in changed:
                self._validate_task(tasks[item_id], task_map=tasks)
            for item_id in changed:
                item = tasks[item_id]
                self._write("tasks", item)
                old_node = self._get("nodes", original[item_id]["nodeId"]) if original[item_id].get("nodeId") else None
                new_node = self._get("nodes", node_id) if node_id else None
                summary = (old_node["title"] if old_node else "未分组") + " → " + (new_node["title"] if new_node else "未分组")
                summary += "\n本次同时调整整组父任务与子任务。"
                self._record_trajectory(item["projectId"], "task_node_changed", "调整任务节点 · " + item["title"],
                    summary, node_id=node_id, task_id=item_id, at=stamp)
            return _copy(tasks[task_id])

    def add_event(self, task_id, message, **fields):
        if not isinstance(message, str) or not message.strip():
            raise ValueError("日志内容不能为空")
        with self._lock:
            item = self._get("tasks", task_id)
            if item is None:
                raise KeyError(task_id)
            fields["log"] = (item["log"] + [[now(), message]])[-MAX_LOG_ENTRIES:]
            return self.update_task(task_id, fields)
