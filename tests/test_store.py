import tempfile
import threading
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
