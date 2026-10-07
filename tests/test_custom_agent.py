"""Public adapter example is verified as a real executable, never a fake run()."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from taskboard.adapters import AdapterRegistry


ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


class CustomAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="taskboard 测试目录 ")
        self.directory = Path(self.temporary.name)
        self.workspace = self.directory / "我的项目 with spaces"
        self.workspace.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, *args, input=None, cwd=None):
        return subprocess.run([sys.executable, *map(str, args)], input=input, cwd=cwd or self.directory,
                              capture_output=True, text=True, encoding="utf-8", timeout=15)

    def test_full_public_self_check_from_unrelated_directory(self):
        result = self.command(EXAMPLES / "check_custom_agent.py", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertFalse(report["isAI"])
        self.assertEqual(report["modelRequests"], 0)
        self.assertEqual(len(report["checks"]), 4)

    def test_generated_config_works_from_clone_path_with_spaces_and_chinese(self):
        # Only the independent example is copied. It cannot import taskboard.
        clone = self.directory / "另一台机器的 clone with spaces" / "examples"
        clone.mkdir(parents=True)
        for name in ("example_agent.py", "setup_example.py"):
            shutil.copy2(EXAMPLES / name, clone / name)
        config = self.directory / "新配置目录 有空格" / "协议配置.json"
        generated = self.command(clone / "setup_example.py", "--output", config)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        self.assertEqual(Path(generated.stdout.strip()), config.resolve())
        definition = json.loads(config.read_text(encoding="utf-8"))["adapters"][0]
        self.assertEqual(Path(definition["command"][1]), (clone / "example_agent.py").resolve())
        registry = AdapterRegistry(config)
        prompt = "真实标准输入；空格 中文 $() `literal`"
        initial = registry.run("protocol-example", prompt=prompt, cwd=str(self.workspace), phase="execute",
                               session_id=None, emit=lambda event: None, cancel=threading.Event())
        self.assertTrue(initial["ok"], initial.get("error"))
        first_file = self.workspace / initial["artifacts"][0]["path"]
        self.assertIn(prompt, first_file.read_text(encoding="utf-8"))
        resumed = registry.run("protocol-example", prompt="第二轮真实反馈", cwd=str(self.workspace), phase="execute",
                               session_id=initial["sessionId"], emit=lambda event: None, cancel=threading.Event())
        self.assertTrue(resumed["ok"], resumed.get("error"))
        self.assertEqual(resumed["sessionId"], initial["sessionId"])
        second_file = self.workspace / resumed["artifacts"][0]["path"]
        self.assertIn("第二轮真实反馈", second_file.read_text(encoding="utf-8"))
        self.assertNotEqual(first_file, second_file)
        self.assertTrue(first_file.is_file(), "恢复会话不能覆盖前一轮产物")

    def test_setup_never_overwrites_existing_or_user_config(self):
        target = self.directory / "existing.json"
        target.write_text("keep this", encoding="utf-8")
        result = self.command(EXAMPLES / "setup_example.py", "--output", target)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep this")
        official = self.directory / "adapters.json"
        official.write_text("user settings", encoding="utf-8")
        result = self.command(EXAMPLES / "setup_example.py", "--output", official)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(official.read_text(encoding="utf-8"), "user settings")
        fresh_official = self.directory / "fresh" / "adapters.json"
        result = self.command(EXAMPLES / "setup_example.py", "--output", fresh_official)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(fresh_official.exists())

    def test_read_only_plan_does_not_write_workspace(self):
        result = self.command(EXAMPLES / "example_agent.py", "--read-only", input="plan only", cwd=self.workspace)
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(events[-1]["type"], "result")
        self.assertTrue(events[-1]["ok"])
        self.assertIn("subtasks", json.loads(events[-1]["text"]))
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_unknown_resume_is_failed_and_paths_cannot_escape(self):
        for session in ("00000000-0000-0000-0000-000000000000", "../../escape"):
            with self.subTest(session=session):
                result = self.command(EXAMPLES / "example_agent.py", "--resume", session, input="resume", cwd=self.workspace)
                self.assertNotEqual(result.returncode, 0)
                events = [json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual(events[-1]["type"], "result")
                self.assertFalse(events[-1]["ok"])
                self.assertEqual(list(self.workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
