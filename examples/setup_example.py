#!/usr/bin/env python3
"""Generate a fresh local adapter config without overwriting existing files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def create_config(output=None):
    source = Path(__file__).resolve().parent
    destination = Path(output).expanduser().resolve() if output else source.parent / ".data" / "example-adapters.json"
    if destination.name == "adapters.json":
        raise ValueError("请使用独立示例配置文件名，保留用户的 adapters.json。")
    executable = str(Path(sys.executable).resolve())
    command = [executable, str(source / "example_agent.py")]
    adapters = []
    for slow in (False, True):
        adapters.append({
            "id": "protocol-example-slow" if slow else "protocol-example",
            "name": "协议示例 · 取消测试" if slow else "协议示例 · 非 AI",
            "enabled": True,
            "description": "确定性 Python 示例，验证真实子进程和可插拔协议，不调用模型。",
            "command": command + (["--delay", "30"] if slow else []),
            "protocol": "taskboard-jsonl", "planArgs": ["--read-only"],
            "executeArgs": [], "resumeArgs": ["--resume", "{session_id}"],
        })
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also protects symlinks and repeated setup invocations.
    with destination.open("x", encoding="utf-8") as file:
        json.dump({"adapters": adapters}, file, ensure_ascii=False, indent=2)
        file.write("\n")
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description="为当前 clone 和 Python 生成协议示例配置，不覆盖已有文件")
    parser.add_argument("--output", help="新配置文件位置；默认是仓库 .data/example-adapters.json")
    args = parser.parse_args(argv)
    try:
        destination = create_config(args.output)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
