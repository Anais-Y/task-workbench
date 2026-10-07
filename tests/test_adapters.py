import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from taskboard.adapters import AdapterRegistry, _ProcessStopError, _stop_process_group, _discover_executable


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name)
        self.events = []

    def tearDown(self):
        self.temp.cleanup()

    def registry(self, source, protocol="taskboard-jsonl", **kwargs):
        script = self.cwd / "fake.py"
        script.write_text(source, encoding="utf-8")
        config = self.cwd / "adapters.json"
        config.write_text(json.dumps({"adapters": [{
            "id": "fixture", "name": "测试执行器", "enabled": True,
            "command": [sys.executable, str(script)], "protocol": protocol,
            "planArgs": ["--read-only"], **kwargs,
        }]}), encoding="utf-8")
        return AdapterRegistry(config)

    def run_agent(self, registry, cancel=None, **kwargs):
        return registry.run("fixture", prompt="测试任务", cwd=str(self.cwd),
                            phase="execute", session_id=None, emit=self.events.append,
                            cancel=cancel or threading.Event(), **kwargs)

    def test_custom_stdin_session_and_artifact(self):
        registry = self.registry("""
import json, sys
prompt = sys.stdin.read()
print(json.dumps({'type':'session','sessionId':'session-123'}))
print(json.dumps({'type':'progress','message':'已读取任务'}))
print(json.dumps({'type':'result','ok':True,'text':prompt,'artifacts':[{'name':'result.txt','type':'text','body':'done'}]}))
""")
        result = self.run_agent(registry)
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "测试任务")
        self.assertEqual(result["sessionId"], "session-123")
        self.assertEqual(result["artifacts"][0]["body"], "done")
        self.assertEqual({event["type"] for event in self.events}, {"progress", "session", "artifact"})

    def test_prompt_is_not_shell_or_command_line(self):
        registry = self.registry("""
import json, sys
print(json.dumps({'type':'result','ok':True,'text':sys.stdin.read(),'artifacts':[{'body':json.dumps(sys.argv),'name':'argv'}]}))
""")
        prompt = "$(touch should-not-exist) `touch should-not-exist` ; 中文"
        result = registry.run("fixture", prompt=prompt, cwd=str(self.cwd), phase="execute",
                              session_id=None, emit=self.events.append, cancel=threading.Event())
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], prompt)
        self.assertNotIn(prompt, result["artifacts"][0]["body"])
        self.assertFalse((self.cwd / "should-not-exist").exists())

    def test_nonzero_exit_overrules_success(self):
        registry = self.registry("""
import json, sys
print(json.dumps({'type':'result','ok':True,'text':'misleading'}))
print('authentication failed', file=sys.stderr)
sys.exit(7)
""")
        result = self.run_agent(registry)
        self.assertFalse(result["ok"])
        self.assertIn("7", result["error"])
        self.assertIn("authentication failed", result["error"])

    def test_error_event_overrules_zero_exit_and_success_result(self):
        registry = self.registry("""
import json
print(json.dumps({'type':'error','message':'not permitted'}))
print(json.dumps({'type':'result','ok':True,'text':'done'}))
""")
        self.assertFalse(self.run_agent(registry)["ok"])

    def test_missing_terminal_result_is_failure(self):
        registry = self.registry("print('unstructured banner')")
        result = self.run_agent(registry)
        self.assertFalse(result["ok"])
        self.assertIn("完整的结果事件", result["error"])

    def test_codex_session_final_text_file_and_completion(self):
        records = [
            {"type": "thread.started", "thread_id": "codex-123"},
            {"type": "item.started", "item": {"type": "command_execution", "command": "python -V"}},
            {"type": "item.completed", "item": {"type": "file_change", "status": "completed", "changes": [{"path": "report.md", "kind": "add"}]}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "完成"}},
            {"type": "turn.completed", "usage": {"input_tokens": 3}},
        ]
        registry = self.registry("import json\nfor item in " + repr(records) + ": print(json.dumps(item))", "codex-jsonl")
        result = self.run_agent(registry)
        self.assertTrue(result["ok"])
        self.assertEqual(result["sessionId"], "codex-123")
        self.assertEqual(result["text"], "完成")
        self.assertEqual(result["artifacts"][0]["path"], "report.md")

    def test_codex_failed_turn_even_exit_zero(self):
        registry = self.registry("print('{\"type\":\"turn.failed\",\"error\":{\"message\":\"quota exceeded\"}}')", "codex-jsonl")
        result = self.run_agent(registry)
        self.assertFalse(result["ok"])
        self.assertIn("quota exceeded", result["error"])

    def test_claude_session_and_result(self):
        records = [
            {"type": "system", "subtype": "init", "session_id": "claude-123"},
            {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "working"}}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "draft"}, {"type": "tool_use", "name": "Read"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "Final report", "session_id": "claude-123"},
        ]
        registry = self.registry("import json\nfor item in " + repr(records) + ": print(json.dumps(item))", "claude-jsonl")
        result = self.run_agent(registry)
        self.assertTrue(result["ok"])
        self.assertEqual(result["sessionId"], "claude-123")
        self.assertEqual(result["text"], "Final report")

    def test_claude_error_and_permission_denial(self):
        for result_event in [
            {"type": "result", "subtype": "error_during_execution", "is_error": True, "errors": ["API unavailable"]},
            {"type": "result", "subtype": "success", "result": "could not edit", "permission_denials": [{"tool_name": "Edit"}]},
        ]:
            with self.subTest(event=result_event):
                registry = self.registry("import json\nprint(json.dumps(" + repr(result_event) + "))", "claude-jsonl")
                self.assertFalse(self.run_agent(registry)["ok"])

    def test_text_protocol_and_final_line_without_newline(self):
        registry = self.registry("import sys\nsys.stdout.write('plain final output')", "text")
        result = self.run_agent(registry)
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"].strip(), "plain final output")

    def test_cancel_stops_parent_and_descendants(self):
        heartbeat = self.cwd / "heartbeat"
        child_source = "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); p=pathlib.Path(" + repr(str(heartbeat)) + "); " + "\nwhile True:\n p.write_text(str(time.time()))\n time.sleep(.03)"
        registry = self.registry("""
import json, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', %r])
print(json.dumps({'type':'progress','message':'started'}), flush=True)
while True: time.sleep(.1)
""" % child_source)
        cancel = threading.Event()
        result = {}
        thread = threading.Thread(target=lambda: result.update(self.run_agent(registry, cancel)))
        thread.start()
        deadline = time.monotonic() + 5
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(heartbeat.exists())
        cancel.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["ok"])
        previous = heartbeat.read_text()
        time.sleep(.15)
        self.assertEqual(heartbeat.read_text(), previous)

    def test_cancel_after_streams_closed(self):
        registry = self.registry("import os,time\nos.close(1)\nos.close(2)\ntime.sleep(30)")
        cancel = threading.Event()
        timer = threading.Timer(.2, cancel.set)
        timer.start()
        result = self.run_agent(registry, cancel)
        timer.join()
        self.assertTrue(result["cancelled"])

    def test_pre_cancel_does_not_start_process(self):
        registry = self.registry("from pathlib import Path\nPath('started').touch()")
        cancel = threading.Event()
        cancel.set()
        self.assertTrue(self.run_agent(registry, cancel)["cancelled"])
        self.assertFalse((self.cwd / "started").exists())

    def test_output_is_bounded(self):
        registry = self.registry("print('a' * 10000)", "text")
        with patch("taskboard.adapters.MAX_OUTPUT", 1000):
            result = self.run_agent(registry)
        self.assertFalse(result["ok"])
        self.assertIn("输出超过", result["error"])

    def test_exited_group_permission_race_is_reaped(self):
        process = Mock(pid=987654, returncode=-signal.SIGTERM)
        process.poll.return_value = process.returncode
        process.wait.return_value = process.returncode
        cases = [
            [PermissionError(1, "Operation not permitted")],
            [None, PermissionError(1, "Operation not permitted")],
        ]
        for effects in cases:
            with self.subTest(effects=len(effects)):
                process.wait.reset_mock()
                with patch("taskboard.adapters.os.killpg", side_effect=effects):
                    _stop_process_group(process)
                process.wait.assert_called()

    def test_exit_racing_permission_error_is_rechecked(self):
        process = Mock(pid=987654, returncode=-signal.SIGTERM)
        process.poll.side_effect = [None, -signal.SIGTERM]
        process.wait.return_value = -signal.SIGTERM
        with patch("taskboard.adapters.os.killpg", side_effect=PermissionError(1, "Operation not permitted")):
            _stop_process_group(process)
        process.wait.assert_called_once_with(timeout=0.15)

    def test_missing_group_still_reaps_direct_child(self):
        process = Mock(pid=987654, returncode=0)
        process.poll.return_value = 0
        with patch("taskboard.adapters.os.killpg", side_effect=ProcessLookupError()):
            _stop_process_group(process)
        process.wait.assert_called_once_with(timeout=2)

    def test_live_process_permission_error_is_not_success(self):
        process = Mock(pid=987654, returncode=None)
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("fake", 0.15)
        with patch("taskboard.adapters.os.killpg", side_effect=PermissionError(1, "Operation not permitted")):
            with self.assertRaisesRegex(_ProcessStopError, "无权限停止"):
                _stop_process_group(process)

    def test_cancel_stop_failure_returns_error_not_cancelled(self):
        registry = self.registry("import time\ntime.sleep(30)")
        cancel = threading.Event()
        timer = threading.Timer(.15, cancel.set)
        timer.start()
        captured = []

        def cannot_stop(process):
            captured.append(process)
            raise _ProcessStopError("无权限停止 Agent 进程")

        try:
            with patch("taskboard.adapters._stop_process_group", side_effect=cannot_stop):
                result = self.run_agent(registry, cancel)
            self.assertFalse(result["ok"])
            self.assertTrue(result["cancelFailed"])
            self.assertNotIn("cancelled", result)
            self.assertEqual(result["processId"], captured[0].pid)
            self.assertEqual(len(captured), 1)
        finally:
            timer.join()
            for process in captured:
                _stop_process_group(process)

    def test_output_failure_preserves_error_when_cleanup_fails(self):
        registry = self.registry("import time\nprint('a' * 10000, flush=True)\ntime.sleep(30)", "text")
        captured = []

        def cannot_stop(process):
            captured.append(process)
            raise _ProcessStopError("无权限停止 Agent 进程")

        try:
            with patch("taskboard.adapters.MAX_OUTPUT", 1000), patch("taskboard.adapters._stop_process_group", side_effect=cannot_stop):
                result = self.run_agent(registry)
            self.assertFalse(result["ok"])
            self.assertIn("输出超过", result["error"])
            self.assertIn("停止进程失败", result["error"])
            self.assertTrue(result["cancelFailed"])
        finally:
            for process in captured:
                _stop_process_group(process)

    def test_unsupported_resume_and_plan_fail_before_spawn(self):
        registry = self.registry("raise Exception('should not run')", planArgs=[])
        result = registry.run("fixture", prompt="plan", cwd=str(self.cwd), phase="plan", session_id=None,
                              emit=self.events.append, cancel=threading.Event())
        self.assertFalse(result["ok"])
        self.assertIn("未配置计划模式", result["error"])
        result = registry.run("fixture", prompt="resume", cwd=str(self.cwd), phase="execute", session_id="id-123",
                              emit=self.events.append, cancel=threading.Event())
        self.assertFalse(result["ok"])
        self.assertIn("会话恢复参数", result["error"])

    def test_custom_resume_passes_specific_id(self):
        registry = self.registry("import json,sys\nprint(json.dumps({'type':'result','ok':True,'text':json.dumps(sys.argv)}))",
                                 resumeArgs=["--session", "{session_id}"])
        result = registry.run("fixture", prompt="resume", cwd=str(self.cwd), phase="execute", session_id="id-123",
                              emit=self.events.append, cancel=threading.Event())
        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(result["text"])[-2:], ["--session", "id-123"])

    def test_builtin_commands_restrict_plan_and_preserve_model(self):
        registry = AdapterRegistry()
        for engine in ("codex", "claude"):
            item = registry._adapters[engine]
            for session in (None, "specific-session"):
                command = registry._command(item, str(self.cwd), "plan", session)
                self.assertNotIn("--model", command)
                self.assertFalse(any("dangerously" in str(arg) for arg in command))
                if engine == "codex":
                    self.assertIn("read-only", command)
                else:
                    self.assertIn("plan", command)
                    self.assertIn("Read,Glob,Grep", command)
                if session:
                    self.assertIn(session, command)

    def test_discovery_requires_real_executable_candidate(self):
        app_binary = self.cwd / "Codex.app" / "Contents" / "Resources" / "codex"
        app_binary.parent.mkdir(parents=True)
        app_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        with patch("taskboard.adapters.shutil.which", return_value=None):
            self.assertIsNone(_discover_executable("codex", [str(app_binary)]))
            app_binary.chmod(0o755)
            self.assertEqual(_discover_executable("codex", [str(self.cwd / "missing"), str(app_binary)]), str(app_binary.resolve()))

    def test_discovery_prefers_path_and_checks_codex_app(self):
        with patch("taskboard.adapters.shutil.which", side_effect=lambda name: sys.executable if name == "codex" else None):
            registry = AdapterRegistry()
            self.assertEqual(registry._adapters["codex"]["executable"], str(Path(sys.executable).resolve()))
        app_binary = "/Applications/Codex.app/Contents/Resources/codex"
        with patch("taskboard.adapters.shutil.which", return_value=None), patch("taskboard.adapters._executable", side_effect=lambda name, base=None: app_binary if name == app_binary else None):
            registry = AdapterRegistry()
            self.assertEqual(registry._adapters["codex"]["executable"], app_binary)

    def test_config_rejects_duplicate_and_prompt_argument(self):
        for kwargs in ({"id": "codex"}, {"command": [sys.executable, "{prompt}"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.registry("", **kwargs)


if __name__ == "__main__":
    unittest.main()
