import tempfile
import json
import sqlite3
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from taskboard.store import MAX_LOG_ENTRIES, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path / "state.sqlite3")
        self.project = self.store.create_project({"id": "build", "name": "建立一个任务管理器", "path": str(self.path)})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def task(self, title="任务", **fields):
        return self.store.create_task({"projectId": self.project["id"], "title": title, **fields})

    def test_defaults_persistence_and_detached_values(self):
        item = self.task()
        self.assertEqual(item["id"], "T001")
        self.assertEqual((item["status"], item["phase"], item["scheduled"]), ("queued", "plan", False))
        item["criteria"].append("outside mutation")
        self.assertEqual(self.store.get_task(item["id"])["criteria"], [])
        version = self.store.version()
        self.store.close()
        self.store = Store(self.path / "state.sqlite3")
        self.assertEqual(self.store.version(), version)
        self.assertEqual(self.task()["id"], "T002")

    def test_invalid_update_leaves_record_and_revision_unchanged(self):
        item = self.task()
        version = self.store.version()
        for patch in ({"status": "invalid"}, {"status": []}, {"kind": {}}, {"id": "T999"}, {"done": 1}, {"scheduled": "yes"}):
            with self.assertRaises(ValueError):
                self.store.update_task(item["id"], patch)
        self.assertEqual(self.store.version(), version)
        self.assertEqual(self.store.get_task(item["id"]), item)
        with self.assertRaises(KeyError):
            self.store.update_task("absent", {})

    def test_dependency_and_parent_graph_validation(self):
        first = self.task()
        second = self.task(deps=[first["id"]], parentTaskId=first["id"])
        with self.assertRaisesRegex(ValueError, "循环"):
            self.store.update_task(first["id"], {"deps": [second["id"]]})
        with self.assertRaisesRegex(ValueError, "循环"):
            self.store.update_task(first["id"], {"parentTaskId": second["id"]})
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.task(deps=["missing"])
        other = self.store.create_project({"name": "另外一个项目", "path": str(self.path)})
        with self.assertRaisesRegex(ValueError, "同一个项目"):
            self.store.update_task(first["id"], {"projectId": other["id"]})
        with self.assertRaises(ValueError):
            self.store.create_task({"title": "跨项目依赖", "projectId": other["id"], "deps": [first["id"]]})

    def test_concurrent_events_are_not_lost(self):
        item = self.task()
        errors = []

        def write_events(worker):
            try:
                for count in range(25):
                    self.store.add_event(item["id"], f"{worker}:{count}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write_events, args=(worker,)) for worker in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        events = self.store.get_task(item["id"])["log"]
        self.assertEqual(len(events), 200)
        self.assertEqual(len({message for _, message in events}), 200)

    def test_event_updates_state_atomically_and_bounds_log(self):
        item = self.task(log=[["time", f"event {i}"] for i in range(MAX_LOG_ENTRIES)])
        updated = self.store.add_event(item["id"], "已经完成", status="done")
        self.assertEqual(updated["status"], "done")
        self.assertEqual(len(updated["log"]), MAX_LOG_ENTRIES)
        self.assertEqual(updated["log"][0][1], "event 1")
        self.assertEqual(updated["log"][-1][1], "已经完成")

    def test_manual_ids_advance_sequence(self):
        self.task(id="T008")
        self.assertEqual(self.task()["id"], "T009")

    def node(self, title="探索方向", **fields):
        return self.store.create_node({"projectId": self.project["id"], "title": title, **fields})

    def test_project_trash_restores_records_and_never_touches_files(self):
        file_path = self.path / "重要文件.txt"
        file_path.write_text("保留", encoding="utf-8")
        node = self.node()
        task = self.task(nodeId=node["id"], status="done")
        deleted = self.store.delete_project(self.project["id"])
        self.assertTrue(deleted["deletedAt"])
        self.assertEqual(self.store.list_projects(), [])
        self.assertEqual(self.store.list_tasks(), [])
        self.assertEqual(self.store.list_nodes(), [])
        self.assertEqual(self.store.list_trajectory(), [])
        self.assertEqual(self.store.get_task(task["id"]), task)
        self.assertEqual(self.store.get_node(node["id"]), node)
        self.assertEqual(self.store.list_projects(include_deleted=True), [deleted])
        self.assertEqual(self.store.list_trajectory(self.project["id"])[-1]["type"], "project_deleted")
        version = self.store.version()
        self.store.delete_project(self.project["id"])
        self.assertEqual(self.store.version(), version)
        with self.assertRaisesRegex(ValueError, "回收站"):
            self.task()
        with self.assertRaisesRegex(ValueError, "回收站"):
            self.node()
        with self.assertRaisesRegex(ValueError, "回收站"):
            self.store.update_task(task["id"], {"status": "queued"})
        self.store.restore_project(self.project["id"])
        self.assertEqual(self.store.list_tasks(), [task])
        self.assertEqual(self.store.list_nodes(), [node])
        self.assertEqual(self.store.list_trajectory()[-1]["type"], "project_restored")
        self.assertEqual(file_path.read_text(encoding="utf-8"), "保留")

    def test_project_delete_rejects_running_or_scheduled_without_changes(self):
        task = self.task(status="running")
        for state in ({"status": "running", "scheduled": False}, {"status": "queued", "scheduled": True}):
            self.store.update_task(task["id"], state)
            before = self.store.list_trajectory()
            revision = self.store.version()
            with self.assertRaisesRegex(ValueError, "运行或已排队"):
                self.store.delete_project(self.project["id"])
            self.assertEqual(self.store.list_projects(), [self.project])
            self.assertEqual(self.store.list_trajectory(), before)
            self.assertEqual(self.store.version(), revision)

    def test_stop_failure_blocks_project_delete_and_family_assignment_until_cleared(self):
        node = self.node()
        parent = self.task("父任务")
        child = self.task("停止失败的子任务", parentTaskId=parent["id"], status="failed",
                          cancelFailed=True, processId=123456, processState="unknown")
        revision = self.store.version()
        history = self.store.list_trajectory()
        with self.assertRaisesRegex(ValueError, "尚未确认 Agent 已停止"):
            self.store.delete_project(self.project["id"])
        with self.assertRaisesRegex(ValueError, "尚未确认 Agent 已停止"):
            self.store.assign_task_node(parent["id"], node["id"])
        self.assertEqual(self.store.version(), revision)
        self.assertEqual(self.store.list_trajectory(), history)
        self.assertIsNone(self.store.get_task(parent["id"])["nodeId"])
        self.assertIsNone(self.store.get_project(self.project["id"])["deletedAt"])
        self.store.update_task(child["id"], {"cancelFailed": False, "processState": "exited"})
        self.store.assign_task_node(child["id"], node["id"])
        self.assertEqual(self.store.get_task(parent["id"])["nodeId"], node["id"])
        self.assertTrue(self.store.delete_project(self.project["id"])["deletedAt"])

    def test_nodes_allow_forks_merges_but_reject_cycles_and_cross_project_edges(self):
        origin = self.node("起点")
        left = self.node("分支一", parentIds=[origin["id"]], color="violet")
        right = self.node("分支二", parentIds=[origin["id"]], color="teal")
        merged = self.node("合并", parentIds=[left["id"], right["id"]])
        revision = self.store.version()
        with self.assertRaisesRegex(ValueError, "循环"):
            self.store.update_node(origin["id"], {"parentIds": [merged["id"]]})
        self.assertEqual(self.store.version(), revision)
        other = self.store.create_project({"name": "其他", "path": str(self.path)})
        with self.assertRaisesRegex(ValueError, "同一个项目"):
            self.store.create_node({"title": "跨项目", "projectId": other["id"], "parentIds": [origin["id"]]})
        for change in ({"id": "other"}, {"projectId": other["id"]}, {"createdAt": "new time"}):
            with self.assertRaises(ValueError):
                self.store.update_node(origin["id"], change)
        self.assertEqual(self.store.get_node(origin["id"]), origin)

    def test_conclusion_versions_and_validation(self):
        node = self.node(hypothesis="原假设")
        for invalid in ({"outcome": "adopted"}, {"color": "pink"}, {"outcome": []}):
            with self.assertRaises(ValueError):
                self.store.update_node(node["id"], invalid)
        self.store.update_node(node["id"], {"outcome": "adopted", "conclusion": "第一版完整结论"})
        self.store.update_node(node["id"], {"outcome": "inconclusive", "conclusion": "第二版修正结论"})
        revisions = [e for e in self.store.list_trajectory() if e["type"] == "node_conclusion"]
        self.assertEqual(len(revisions), 2)
        self.assertIn("第一版完整结论", revisions[0]["summary"])
        self.assertIn("第二版修正结论", revisions[1]["summary"])
        self.assertEqual(self.store.get_node(node["id"])["conclusion"], "第二版修正结论")

    def test_node_history_saves_predecessor_names_and_puts_conclusion_first(self):
        parent = self.node("特征探索")
        node = self.node("组合实验", hypothesis="组合特征能改善结果", parentIds=[parent["id"]], color="blue")
        self.store.update_node(node["id"], {"outcome": "adopted", "conclusion": "完整结论\n第二行证据"})
        first = [event for event in self.store.list_trajectory() if event["type"] == "node_conclusion"][-1]
        self.assertTrue(first["summary"].startswith("结论状态：已采用\n结论：\n完整结论\n第二行证据"))
        self.assertIn("前序节点：特征探索", first["summary"])
        self.assertNotIn(parent["id"], first["summary"])
        self.assertNotIn("blue", first["summary"])
        self.store.update_node(parent["id"], {"title": "改名后的探索"})
        self.store.update_node(node["id"], {"conclusion": "后续修订结论"})
        revisions = [event for event in self.store.list_trajectory() if event["type"] == "node_conclusion"]
        self.assertEqual(revisions[0], first)
        self.assertIn("前序节点：改名后的探索", revisions[-1]["summary"])

    def test_family_assignment_inherits_and_moves_ancestors_siblings_descendants(self):
        first, second = self.node("方向一"), self.node("方向二")
        parent = self.task("父任务", nodeId=first["id"])
        child = self.task("子任务", parentTaskId=parent["id"])
        sibling = self.task("兄弟任务", parentTaskId=parent["id"])
        grandchild = self.task("孙任务", parentTaskId=child["id"])
        independent = self.task("独立任务", nodeId=first["id"])
        self.assertEqual(child["nodeId"], first["id"])
        selected = self.store.assign_task_node(child["id"], second["id"])
        self.assertEqual(selected["id"], child["id"])
        for member in (parent, child, sibling, grandchild):
            self.assertEqual(self.store.get_task(member["id"])["nodeId"], second["id"])
        self.assertEqual(self.store.get_task(independent["id"])["nodeId"], first["id"])
        with self.assertRaisesRegex(ValueError, "任务分组"):
            self.store.update_task(child["id"], {"nodeId": first["id"]})
        with self.assertRaisesRegex(ValueError, "同一个节点"):
            self.task("错配的子任务", parentTaskId=parent["id"], nodeId=first["id"])
        self.store.assign_task_node(parent["id"], None)
        self.assertIsNone(self.store.get_task(grandchild["id"])["nodeId"])

    def test_family_assignment_is_atomic_and_blocks_scheduled_members(self):
        node = self.node()
        parent = self.task("父")
        child = self.task("子", parentTaskId=parent["id"], scheduled=True)
        revision = self.store.version()
        history = self.store.list_trajectory()
        with self.assertRaisesRegex(ValueError, "运行或已排队"):
            self.store.assign_task_node(parent["id"], node["id"])
        self.assertEqual(self.store.version(), revision)
        self.assertEqual(self.store.list_trajectory(), history)
        self.assertIsNone(self.store.get_task(parent["id"])["nodeId"])
        self.store.update_task(child["id"], {"scheduled": False})
        original_write = self.store._write
        revision = self.store.version()
        history = self.store.list_trajectory()

        def fail_second_write(table, item, create=False):
            if item["id"] == child["id"]:
                raise sqlite3.OperationalError("injected failure")
            original_write(table, item, create)

        with patch.object(self.store, "_write", side_effect=fail_second_write):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.assign_task_node(parent["id"], node["id"])
        self.assertIsNone(self.store.get_task(parent["id"])["nodeId"])
        self.assertIsNone(self.store.get_task(child["id"])["nodeId"])
        self.assertEqual(self.store.version(), revision)
        self.assertEqual(self.store.list_trajectory(), history)

    def test_trajectory_survives_log_cap_reopen_and_cannot_be_rewritten(self):
        task = self.task(result="第一版结果", log=[["time", str(i)] for i in range(MAX_LOG_ENTRIES)])
        self.store.add_event(task["id"], "流式进度")
        self.assertEqual(len(self.store.list_trajectory()), 2)
        self.store.update_task(task["id"], {"status": "review", "result": "第二版结果"})
        history = self.store.list_trajectory()
        self.assertEqual([e["type"] for e in history], ["task_created", "task_result", "task_status", "task_result"])
        self.assertEqual([e["summary"] for e in history if e["type"] == "task_result"], ["第一版结果", "第二版结果"])
        self.assertEqual(len(self.store.get_task(task["id"])["log"]), MAX_LOG_ENTRIES)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store._conn.execute("UPDATE trajectory SET data='{}'")
        self.store._conn.rollback()
        self.store.close()
        self.store = Store(self.path / "state.sqlite3")
        self.assertEqual(self.store.list_trajectory(), history)

    def test_v1_migration_preserves_dates_and_only_records_known_history_once(self):
        task = self.task(status="done", result="升级前已保存的结果")
        task.pop("nodeId")
        task["createdAt"] = "2025-01-02T03:04:05.000+00:00"
        task["updatedAt"] = "2025-02-03T04:05:06.000+00:00"
        project = dict(self.project)
        project.pop("deletedAt")
        legacy = self.path / "legacy.sqlite3"
        with sqlite3.connect(legacy) as connection:
            connection.executescript("CREATE TABLE projects(id TEXT PRIMARY KEY,data TEXT);"
                "CREATE TABLE tasks(id TEXT PRIMARY KEY,data TEXT);"
                "CREATE TABLE metadata(key TEXT PRIMARY KEY,value INTEGER);"
                "INSERT INTO metadata VALUES ('version',42),('next_task',8);")
            connection.execute("INSERT INTO projects VALUES (?,?)", (project["id"], json.dumps(project)))
            connection.execute("INSERT INTO tasks VALUES (?,?)", (task["id"], json.dumps(task)))
        migrated = Store(legacy)
        try:
            restored = migrated.get_task(task["id"])
            self.assertEqual(restored, dict(task, nodeId=None))
            history = migrated.list_trajectory()
            self.assertEqual([event["type"] for event in history], ["task_created", "migration_snapshot"])
            self.assertEqual(history[0]["at"], task["createdAt"])
            self.assertIn("升级", history[1]["summary"])
            self.assertIn(task["result"], history[1]["summary"])
            version = migrated.version()
        finally:
            migrated.close()
        reopened = Store(legacy)
        try:
            self.assertEqual(reopened.list_trajectory(), history)
            self.assertEqual(reopened.version(), version)
            self.assertEqual(reopened.create_task({"projectId": project["id"], "title": "下一项"})["id"], "T008")
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
