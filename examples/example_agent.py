#!/usr/bin/env python3
"""A real subprocess exercising Task Workbench's protocol; this is NOT an AI.

Only Python's standard library is required. Read a prompt from stdin, emit one
JSON object per stdout line, and finish with a result event. Demonstrates
read-only planning, sessions, file artifacts, progress, and cancellation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid


def emit(kind, **fields):
    print(json.dumps({"type": kind, **fields}, ensure_ascii=False), flush=True)


def write_state(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="任务工作台协议演示：这是确定性示例，不是真正的 AI")
    parser.add_argument("--read-only", action="store_true", help="仅返回示例计划，不写任何文件")
    parser.add_argument("--resume", help="恢复本示例创建的会话 ID")
    parser.add_argument("--delay", type=float, default=0, help="执行前等待秒数，用于取消验证，范围 0–300")
    args = parser.parse_args(argv)
    if not math.isfinite(args.delay) or not 0 <= args.delay <= 300:
        parser.error("--delay 需要在 0–300 秒之间")
    try:
        prompt = sys.stdin.read(2 * 1024 * 1024 + 1)
        if len(prompt) > 2 * 1024 * 1024:
            raise ValueError("示例输入不能超过 2 MB")
        session = str(uuid.UUID(args.resume)) if args.resume else str(uuid.uuid4())
        emit("session", sessionId=session)
        emit("progress", message="协议示例进程已启动；这是确定性程序，不是真正的 AI。")
        if args.read_only:
            plan = {"summary": "验证自定义 CLI 协议，仅生成示例文件。", "subtasks": [{
                "title": "生成协议示例产物", "goal": "仅在 .taskboard-example/ 下生成演示文件，验证执行、日志和产物。",
                "criteria": ["真实子进程生成文件", "明确标注这是协议演示，非 AI 输出"], "deps": []}]}
            emit("result", ok=True, text=json.dumps(plan, ensure_ascii=False), sessionId=session, artifacts=[])
            return 0
        directory = Path.cwd() / ".taskboard-example"
        state_path = directory / "sessions" / (session + ".json")
        if args.resume:
            if not state_path.is_file():
                raise ValueError("找不到要恢复的示例会话；请使用本示例之前返回的 sessionId")
            previous = json.loads(state_path.read_text(encoding="utf-8"))
            iteration = int(previous["round"]) + 1
        else:
            iteration = 1
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {"sessionId": session, "round": iteration, "pid": os.getpid(),
                 "isProtocolExample": True, "lastPrompt": prompt, "status": "running"}
        write_state(state_path, state)
        emit("progress", message=f"{'恢复已有' if args.resume else '创建新的'}示例会话，第 {iteration} 轮。")
        deadline = time.monotonic() + args.delay
        if args.delay:
            emit("progress", message="示例等待中；等待期间写入心跳，可以在工作台取消。")
        while time.monotonic() < deadline:
            state["heartbeat"] = datetime.now(timezone.utc).isoformat()
            write_state(state_path, state)
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        relative = Path(".taskboard-example") / "results" / session / f"run-{iteration}.md"
        artifact_path = Path.cwd() / relative
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        body = f"# 协议演示产物\n\n这是确定性示例程序的输出，不是真正的 AI。\n\n会话：{session}\n\n轮次：{iteration}\n\n## 本轮收到的任务及反馈\n\n{prompt}\n"
        with artifact_path.open("x", encoding="utf-8") as file:
            file.write(body)
        artifact = {"name": f"协议演示-第{iteration}轮.md", "type": "markdown", "path": str(relative)}
        emit("artifact", message="示例文件已写入项目目录。", artifact=artifact)
        state["status"] = "complete"
        write_state(state_path, state)
        emit("result", ok=True, text=f"协议演示第 {iteration} 轮已完成，生成 {relative}。这只证明协议与真实进程链路可用，不代表 AI 能力。",
             sessionId=session, artifacts=[artifact])
        return 0
    except Exception as error:
        emit("error", message=str(error))
        emit("result", ok=False, text="协议演示未完成。", error=str(error), artifacts=[])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
