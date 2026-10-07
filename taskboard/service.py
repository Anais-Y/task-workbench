"""Explicit start/stop for a user-owned local service; no login startup changes."""
from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

ROOT = Path(__file__).resolve().parent.parent


def health(port):
    try:
        with urlopen('http://127.0.0.1:%d/api/health' % port, timeout=1) as response:
            value = json.load(response)
            if isinstance(value, dict) and value.get('ok') is True and value.get('service') == 'task-workbench':
                return value
    except (OSError, ValueError, URLError):
        pass
    return None


def _process_exists(pid):
    if type(pid) is not int or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def control(args):
    if sys.platform not in ('darwin', 'linux'):
        raise ValueError('后台服务当前支持 macOS 和 Linux；尚未验证 Windows 原生运行。')
    if not 1 <= args.port <= 65535:
        raise ValueError('后台服务端口必须在 1–65535 之间。')
    data = ROOT / '.data'
    data.mkdir(exist_ok=True)
    record = data / ('service-%d.json' % args.port)
    current = health(args.port)
    if args.command == 'stop':
        if not record.exists():
            raise ValueError('没有找到由 start 启动的服务记录。前台服务可在其终端中停止。')
        previous = json.loads(record.read_text(encoding='utf-8'))
        if not isinstance(previous, dict) or type(previous.get('pid')) is not int or previous['pid'] <= 1:
            raise ValueError('服务启动记录不完整，请检查 ' + str(record))
        if current is None:
            if _process_exists(previous['pid']):
                raise ValueError('记录中的进程仍在运行，但工作台未响应；未停止身份未确认的进程，请查看服务日志。')
            record.unlink()
            print('服务已停止。')
            return 0
        if current.get('pid') != previous.get('pid'):
            raise ValueError('这个端口上的服务与启动记录不同，未停止其他进程。')
        os.kill(previous['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 10
        while health(args.port) and time.monotonic() < deadline:
            time.sleep(.1)
        if health(args.port):
            raise ValueError('服务尚在退出，请稍后查看。')
        record.unlink(missing_ok=True)
        print('任务工作台已停止。')
        return 0
    if current:
        previous = json.loads(record.read_text(encoding='utf-8')) if record.exists() else {}
        if not isinstance(previous, dict) or previous.get('pid') != current.get('pid'):
            raise ValueError('该端口已有另一个工作台实例。请使用它原来的终端停止，或用 --port 选择其他端口。')
        print('任务工作台正在运行：http://127.0.0.1:%d' % args.port)
        return 0
    command = [sys.executable, '-m', 'taskboard', 'serve', '--port', str(args.port),
               '--db', str(Path(args.db).expanduser().resolve()), '--max-workers', str(args.max_workers)]
    if args.adapters:
        command += ['--adapters', str(Path(args.adapters).expanduser().resolve())]
    log_path = data / ('service-%d.log' % args.port)
    with log_path.open('ab') as log:
        process = subprocess.Popen(command, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = health(args.port)
        if current and current.get('pid') == process.pid:
            temporary = record.with_suffix('.tmp')
            temporary.write_text(json.dumps({'pid': process.pid, 'port': args.port,
                'root': str(ROOT), 'db': str(Path(args.db).expanduser().resolve())}, ensure_ascii=False), encoding='utf-8')
            temporary.replace(record)
            print('任务工作台已启动：http://127.0.0.1:%d' % args.port)
            return 0
        if process.poll() is not None:
            raise ValueError('启动失败，请查看 ' + str(log_path))
        time.sleep(.1)
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    raise ValueError('启动未完成，请查看 ' + str(log_path))
