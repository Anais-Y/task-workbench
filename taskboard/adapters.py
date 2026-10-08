"""Local CLI adapters, with a small stdin / JSONL contract for other agents.

Custom configuration is a JSON object containing ``adapters``. Each entry has
``id``, ``name``, ``command`` (an argv array), and optional ``enabled``,
``protocol``, ``planArgs``, ``executeArgs``, and ``resumeArgs``. The only template
is ``{session_id}`` inside resumeArgs; prompts always arrive on stdin. Supported
protocols: taskboard-jsonl (default), text, codex-jsonl, claude-jsonl.

taskboard-jsonl accepts message/progress/session/artifact/error events in the
shape documented by CONTRACT.md, followed by one terminal result event:
{"type":"result","ok":true,"text":"...","sessionId":"...","artifacts":[]}.
An error event, a false result, missing terminal result, or a nonzero exit code
fails the run. Text mode uses stdout as its result and the exit code as status.
The custom executable is responsible for honoring planArgs as read-only mode.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Callable


MAX_OUTPUT = 16 * 1024 * 1024
MAX_LINE = 1024 * 1024
MAX_TEXT = 200_000
MAX_MESSAGE = 8_000
MAX_PROMPT = 2 * 1024 * 1024


def _text(value, limit=MAX_MESSAGE):
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return value if len(value) <= limit else value[:limit] + "\n[内容过长，已截断]"


def _executable(command, base=None):
    expanded = os.path.expanduser(command)
    if os.sep in expanded:
        candidate = Path(expanded)
        if not candidate.is_absolute() and base:
            candidate = Path(base) / candidate
        return str(candidate.resolve()) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    return shutil.which(expanded)


def _discover_executable(name, candidates):
    """PATH wins; application-bundle candidates count only when executable."""
    discovered = shutil.which(name)
    if discovered:
        return _executable(discovered)
    for candidate in candidates:
        discovered = _executable(candidate)
        if discovered:
            return discovered
    return None


def _argv(value, label):
    if not isinstance(value, list) or len(value) > 100 or any(
        not isinstance(item, str) or "\0" in item or len(item) > 8192 for item in value
    ):
        raise ValueError(f"{label} 必须是字符串数组")
    return list(value)


def _artifact(data):
    if not isinstance(data, dict):
        return None
    path = data.get("path")
    body = data.get("body")
    if not isinstance(path, str) and not isinstance(body, str):
        return None
    result = {"name": _text(data.get("name") or (Path(path).name if path else "产物"), 300),
              "type": _text(data.get("type") or "text", 100)}
    if isinstance(path, str):
        result["path"] = path[:4096]
    if isinstance(body, str):
        result["body"] = _text(body, MAX_TEXT)
    return result


class _Stream:
    def __init__(self, protocol, emit, session_id):
        self.protocol = protocol
        self.emit = emit
        self.session_id = session_id
        self.text = ""
        self.artifacts = []
        self.error = ""
        self.terminal = False
        self.success = False
        self.event_count = 0
        self.partial = ""
        self.last_partial = 0.0

    def event(self, kind, message="", **fields):
        self.event_count += 1
        if self.event_count <= 2000 or kind in {"session", "artifact", "error"}:
            self.emit({"type": kind, "message": _text(message), **fields})
        elif self.event_count == 2001:
            self.emit({"type": "progress", "message": "过程事件较多，后续仅保留会话、产物和最终结果。"})

    def session(self, value):
        if isinstance(value, str) and value and value != self.session_id:
            self.session_id = value[:1000]
            self.event("session", "已记录 Agent 会话", sessionId=self.session_id)

    def fail(self, message):
        self.error = _text(message or "Agent 返回执行错误")
        self.event("error", self.error)

    def add_artifact(self, data):
        item = _artifact(data)
        if item and item not in self.artifacts and len(self.artifacts) < 100:
            self.artifacts.append(item)
            self.event("artifact", f"产物：{item['name']}", artifact=item)

    def line(self, raw):
        if not raw.strip():
            return
        if self.protocol == "text":
            self.text = _text(self.text + raw + "\n", MAX_TEXT)
            self.event("message", raw)
            return
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            self.event("progress", raw)
            return
        if not isinstance(data, dict):
            return
        if self.protocol == "codex-jsonl":
            self.codex(data)
        elif self.protocol == "claude-jsonl":
            self.claude(data)
        else:
            self.custom(data)

    def codex(self, data):
        kind = data.get("type")
        if kind == "thread.started":
            self.session(data.get("thread_id"))
        elif kind in {"error", "turn.failed"}:
            error = data.get("error") or data.get("message")
            self.fail(error.get("message") if isinstance(error, dict) else error)
            if kind == "turn.failed":
                self.terminal = True
        elif kind == "turn.completed":
            self.terminal = True
            self.success = True
        elif kind in {"item.started", "item.updated", "item.completed"}:
            item = data.get("item") or {}
            if not isinstance(item, dict):
                return
            item_type = item.get("type")
            if item_type == "agent_message" and kind == "item.completed":
                self.text = _text(item.get("text"), MAX_TEXT)
                self.event("message", self.text)
            elif item_type == "command_execution":
                if kind in {"item.started", "item.completed"}:
                    state = "完成" if kind == "item.completed" else "开始"
                    self.event("progress", f"{state}命令：{_text(item.get('command'), 1000)}")
            elif item_type == "file_change" and kind == "item.completed":
                if item.get("status") not in {"failed", "declined"}:
                    for change in item.get("changes", [])[:100]:
                        if isinstance(change, dict) and isinstance(change.get("path"), str):
                            self.add_artifact({"path": change["path"], "type": "file"})
            elif item_type in {"plan", "todo_list"}:
                self.event("progress", "Agent 更新了执行步骤")
            elif item_type == "reasoning" and kind == "item.started":
                self.event("progress", "Agent 正在分析任务")
            elif item_type == "error":
                self.fail(item.get("message") or item.get("text"))

    def claude(self, data):
        self.session(data.get("session_id"))
        kind = data.get("type")
        if kind == "result":
            self.terminal = True
            subtype = data.get("subtype", "success")
            self.success = subtype == "success" and not data.get("is_error")
            self.text = _text(data.get("result") or data.get("structured_output") or self.text, MAX_TEXT)
            if not self.success:
                self.fail(data.get("errors") or self.text or f"Claude 返回 {subtype}")
            elif data.get("permission_denials"):
                self.fail("Claude 的工具权限请求未获允许；请检查 CLI 权限设置后重试。")
            elif self.text:
                self.event("message", self.text)
        elif kind == "assistant":
            message = data.get("message") or {}
            parts = message.get("content", []) if isinstance(message, dict) else []
            texts = [part.get("text", "") for part in parts if isinstance(part, dict) and part.get("type") == "text"]
            if texts:
                self.text = _text("\n".join(texts), MAX_TEXT)
                self.event("message", self.text)
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    self.event("progress", f"调用工具：{_text(part.get('name'), 200)}")
            if data.get("error"):
                self.fail(data["error"])
        elif kind == "stream_event":
            event = data.get("event") or {}
            delta = event.get("delta") or {} if isinstance(event, dict) else {}
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                self.partial = _text(self.partial + _text(delta.get("text")), MAX_TEXT)
                if time.monotonic() - self.last_partial > 1:
                    self.event("progress", self.partial[-1000:])
                    self.last_partial = time.monotonic()
        elif kind == "error":
            self.fail(data.get("error") or data.get("message"))

    def custom(self, data):
        kind = data.get("type")
        self.session(data.get("sessionId"))
        if kind == "result":
            self.terminal = True
            self.success = data.get("ok") is True
            self.text = _text(data.get("text"), MAX_TEXT)
            for item in data.get("artifacts", [])[:100]:
                self.add_artifact(item)
            if not self.success:
                self.fail(data.get("error") or self.text)
        elif kind == "error":
            self.fail(data.get("message") or data.get("error"))
        elif kind == "artifact":
            self.add_artifact(data.get("artifact"))
        elif kind in {"message", "progress"}:
            self.event(kind, data.get("message"))


class _ProcessStopError(RuntimeError):
    """The operating system did not confirm termination of our direct child."""


def _stop_process_group(process):
    """Terminate descendants and reap the child, tolerating macOS exit races.

    macOS can answer EPERM rather than ESRCH when an already reaped process's
    group disappears. Only treat that as an exit race after poll/wait confirms
    the direct child exited. A still-running child must report a stop failure.
    """
    def reap(timeout=2):
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise _ProcessStopError(f"无法确认 Agent 进程 {process.pid} 已停止") from error

    def signal_group(sig):
        process.poll()
        try:
            os.killpg(process.pid, sig)
            return True
        except ProcessLookupError:
            reap()
            return False
        except PermissionError as error:
            # A signal/exit can race with the poll above. A bounded wait both
            # resolves that race and reaps the child instead of leaking it.
            try:
                process.wait(timeout=0.15)
            except subprocess.TimeoutExpired:
                raise _ProcessStopError(
                    f"无权限停止 Agent 进程 {process.pid}，进程可能仍在运行"
                ) from error
            if process.poll() is None or process.returncode is None:
                raise _ProcessStopError(f"无法确认 Agent 进程 {process.pid} 已停止") from error
            return False

    if os.name == "posix":
        if not signal_group(signal.SIGTERM):
            return
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if not signal_group(0):
                return
            time.sleep(0.025)
        if not signal_group(signal.SIGKILL):
            return
    elif process.poll() is None:
        try:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
        except PermissionError as error:
            if process.poll() is None:
                raise _ProcessStopError(f"无权限停止 Agent 进程 {process.pid}") from error
    reap()


class AdapterRegistry:
    def __init__(self, config_path=None):
        codex = _discover_executable("codex", [
            "/Applications/Codex.app/Contents/Resources/codex",
            "~/Applications/Codex.app/Contents/Resources/codex",
            "/Applications/ChatGPT.app/Contents/Resources/codex",
            "~/Applications/ChatGPT.app/Contents/Resources/codex",
            "~/.local/bin/codex",
        ])
        claude = _discover_executable("claude", ["~/.local/bin/claude"])
        self._adapters = {
            "codex": {"id": "codex", "name": "Codex CLI", "enabled": True,
                      "executable": codex, "protocol": "codex-jsonl",
                      "description": "本机 Codex CLI；计划阶段只读，执行阶段可编辑项目工作区。登录状态由 CLI 检查。",
                      "capabilities": ["plan", "execute", "resume", "stream", "cancel"]},
            "claude": {"id": "claude", "name": "Claude Code", "enabled": True,
                       "executable": claude, "protocol": "claude-jsonl",
                       "description": "本机 Claude Code；计划阶段只读，执行阶段允许文件编辑。登录与工具权限由 CLI 检查。",
                       "capabilities": ["plan", "execute", "resume", "stream", "cancel"]},
        }
        selected = config_path or os.environ.get("TASKBOARD_ADAPTERS")
        if selected is None:
            default = Path(__file__).resolve().parent.parent / "adapters.json"
            selected = default if default.exists() else None
        if selected is not None:
            path = Path(selected).expanduser().resolve()
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = data.get("adapters") if isinstance(data, dict) else None
            if not isinstance(entries, list):
                raise ValueError("适配器配置需要 adapters 数组")
            for entry in entries:
                self._load_custom(entry, path.parent)

    def _load_custom(self, entry, base):
        if not isinstance(entry, dict):
            raise ValueError("每个适配器必须是对象")
        adapter_id = entry.get("id")
        if not isinstance(adapter_id, str) or not adapter_id or len(adapter_id) > 80 or not all(
            char.isascii() and (char.isalnum() or char in "-_") for char in adapter_id
        ):
            raise ValueError("适配器 id 只能包含英文字母、数字、连字符和下划线")
        if adapter_id in self._adapters:
            raise ValueError(f"适配器 id 重复：{adapter_id}")
        command = _argv(entry.get("command"), "command")
        if not command or not command[0]:
            raise ValueError("command 不能为空")
        protocol = entry.get("protocol", "taskboard-jsonl")
        if protocol not in {"taskboard-jsonl", "text", "codex-jsonl", "claude-jsonl"}:
            raise ValueError(f"不支持的适配协议：{protocol}")
        plan_args = _argv(entry.get("planArgs", []), "planArgs")
        execute_args = _argv(entry.get("executeArgs", []), "executeArgs")
        resume_args = _argv(entry.get("resumeArgs", []), "resumeArgs")
        if any("{prompt}" in arg for arg in command + plan_args + execute_args + resume_args):
            raise ValueError("提示词必须通过 stdin 传入，不能使用 {prompt}")
        if resume_args and not any("{session_id}" in arg for arg in resume_args):
            raise ValueError("resumeArgs 需要包含 {session_id}")
        self._adapters[adapter_id] = {
            "id": adapter_id, "name": _text(entry.get("name") or adapter_id, 120),
            "description": _text(entry.get("description") or "自定义本机 CLI；计划权限由适配器配置决定。", 1000),
            "enabled": entry.get("enabled", False) is True,
            "executable": _executable(command[0], base), "command": command,
            "protocol": protocol, "planArgs": plan_args, "executeArgs": execute_args,
            "resumeArgs": resume_args,
            "capabilities": ["execute", "stream", "cancel"] + (["plan"] if plan_args else []) + (["resume"] if resume_args else []),
        }

    def list_adapters(self):
        return [{"id": item["id"], "name": item["name"],
                 "available": bool(item["executable"]), "enabled": item["enabled"],
                 "description": item["description"], "capabilities": list(item["capabilities"]),
                 **({"executable": item["executable"]} if item["executable"] else {})}
                for item in self._adapters.values()]

    def _command(self, item, cwd, phase, session_id):
        if item["id"] == "codex":
            command = [item["executable"], "exec", "--sandbox", "read-only" if phase == "plan" else "workspace-write"]
            if session_id:
                return command + ["resume", "--json", "--skip-git-repo-check", session_id, "-"]
            return command + ["--json", "--color", "never", "--cd", cwd, "--skip-git-repo-check", "-"]
        if item["id"] == "claude":
            command = [item["executable"], "--print", "--output-format", "stream-json", "--verbose",
                       "--include-partial-messages", "--permission-mode", "plan" if phase == "plan" else "acceptEdits"]
            if phase == "plan":
                command += ["--tools", "Read,Glob,Grep"]
            if session_id:
                command += ["--resume", session_id]
            return command
        command = [item["executable"]] + item["command"][1:]
        command += item["planArgs"] if phase == "plan" else item["executeArgs"]
        if session_id:
            if not item["resumeArgs"]:
                raise ValueError("此适配器尚未配置会话恢复参数")
            command += [part.replace("{session_id}", session_id) for part in item["resumeArgs"]]
        return command

    def run(self, engine, *, prompt, cwd, phase, session_id, emit: Callable, cancel: threading.Event):
        state = _Stream("taskboard-jsonl", emit, session_id)
        process = None
        stderr_tail = ""
        try:
            item = self._adapters.get(engine)
            if item is None:
                raise ValueError(f"未注册 Agent：{engine}")
            if not item["enabled"]:
                raise ValueError("此 Agent 尚未启用")
            if not item["executable"]:
                raise ValueError(f"未找到 {item['name']} 可执行文件")
            if phase not in {"plan", "execute"}:
                raise ValueError("任务阶段必须是 plan 或 execute")
            if phase == "plan" and "plan" not in item["capabilities"]:
                raise ValueError("此自定义 Agent 未配置计划模式；请配置 planArgs 或直接创建执行任务。")
            cwd = str(Path(cwd).expanduser().resolve(strict=True))
            if not Path(cwd).is_dir():
                raise ValueError("项目工作区必须是文件夹")
            if not isinstance(prompt, str) or len(prompt.encode("utf-8")) > MAX_PROMPT:
                raise ValueError("任务提示词过长或格式错误")
            if session_id is not None and (not isinstance(session_id, str) or not session_id or session_id.startswith("-") or "\0" in session_id):
                raise ValueError("无效的会话 ID")
            if cancel.is_set():
                return {"ok": False, "text": "", "sessionId": session_id, "artifacts": [], "cancelled": True, "error": "任务已取消"}
            state.protocol = item["protocol"]
            command = self._command(item, cwd, phase, session_id)
            state.event("progress", f"正在启动 {item['name']}（{'计划' if phase == 'plan' else '执行'}）")
            with tempfile.TemporaryFile() as input_file, selectors.DefaultSelector() as selector:
                input_file.write(prompt.encode("utf-8"))
                input_file.seek(0)
                process = subprocess.Popen(command, stdin=input_file, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, cwd=cwd, start_new_session=True)
                buffers = {"stdout": b"", "stderr": b""}
                for label, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                    selector.register(pipe, selectors.EVENT_READ, label)
                output_size = 0
                ended_at = None
                while selector.get_map():
                    if cancel.is_set():
                        _stop_process_group(process)
                        return {"ok": False, "text": state.text, "sessionId": state.session_id,
                                "artifacts": state.artifacts, "cancelled": True, "error": "任务已取消"}
                    if process.poll() is not None:
                        ended_at = ended_at or time.monotonic()
                        if time.monotonic() - ended_at > 1:
                            _stop_process_group(process)
                            break
                    for key, _ in selector.select(timeout=0.1):
                        label = key.data
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output_size += len(chunk)
                        if output_size > MAX_OUTPUT:
                            raise ValueError("Agent 输出超过 16 MB，本次运行已停止")
                        if label == "stderr":
                            stderr_tail = (stderr_tail + chunk.decode("utf-8", errors="replace"))[-16000:]
                        buffers[label] += chunk
                        while b"\n" in buffers[label]:
                            line, buffers[label] = buffers[label].split(b"\n", 1)
                            if len(line) > MAX_LINE:
                                raise ValueError("Agent 单条输出过长，本次运行已停止")
                            if label == "stdout":
                                state.line(line.decode("utf-8", errors="replace"))
                        if len(buffers[label]) > MAX_LINE:
                            raise ValueError("Agent 单条输出过长，本次运行已停止")
                if buffers["stdout"]:
                    state.line(buffers["stdout"].decode("utf-8", errors="replace"))
                # A CLI may close its streams before exiting. Continue honoring cancellation.
                while process.poll() is None:
                    if cancel.wait(0.1):
                        _stop_process_group(process)
                        return {"ok": False, "text": state.text, "sessionId": state.session_id,
                                "artifacts": state.artifacts, "cancelled": True, "error": "任务已取消"}
                return_code = process.returncode
            if return_code != 0:
                state.fail(f"{item['name']} 退出码 {return_code}" + (f"：{stderr_tail.strip()}" if stderr_tail.strip() else ""))
            elif state.protocol != "text" and not state.terminal:
                state.fail("Agent 已退出，但未返回完整的结果事件" + (f"：{stderr_tail.strip()}" if stderr_tail.strip() else ""))
            ok = return_code == 0 and not state.error and (state.protocol == "text" or state.success)
            result = {"ok": ok, "text": state.text, "sessionId": state.session_id, "artifacts": state.artifacts}
            if not ok:
                result["error"] = state.error or "Agent 未成功完成任务"
            return result
        except Exception as error:
            stop_error = error if isinstance(error, _ProcessStopError) else None
            if process is not None and stop_error is None:
                try:
                    _stop_process_group(process)
                except Exception as cleanup_error:
                    # Cleanup must never replace the result with an uncaught
                    # exception, or report cancellation while a process lives.
                    stop_error = cleanup_error
            message = str(error) or type(error).__name__
            if stop_error is not None and stop_error is not error:
                message += f"；停止进程失败：{stop_error}"
            result = {"ok": False, "text": state.text, "sessionId": state.session_id,
                      "artifacts": state.artifacts, "error": _text(message)}
            if stop_error is not None:
                result.update(cancelFailed=True, processId=process.pid if process else None)
            return result
        finally:
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if stream:
                        stream.close()
