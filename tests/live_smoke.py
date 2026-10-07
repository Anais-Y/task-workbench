"""Opt-in live Codex smoke test. Uses a temporary workspace, no user files.
Run: python3 -m tests.live_smoke is not required; use python3 tests/live_smoke.py.
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from taskboard.store import Store
from taskboard.adapters import AdapterRegistry
from taskboard.runner import Runner


def main():
    report = {'provider': 'codex', 'checks': [], 'ok': False}
    with tempfile.TemporaryDirectory(prefix='taskboard-live-') as directory:
        store = Store(':memory:')
        store.create_project({'id': 'smoke', 'name': '临时联调', 'path': directory})
        runner = Runner(store, AdapterRegistry(), max_workers=1)
        task = store.create_task({'title': '验证任务工作台的本机执行', 'projectId': 'smoke',
            'goal': '这是任务工作台的隔离联调。请制定一个只有一个子任务的计划：在当前空目录创建 hello.txt，内容严格为 task-workbench-ok 后跟换行。不需要研究资料，不要访问当前目录外的文件，不要联网。',
            'criteria': ['hello.txt 的内容为 task-workbench-ok 后跟换行'], 'phase': 'plan', 'kind': 'plan'})
        runner.start()
        def wait(task_id, timeout=100):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                current = store.get_task(task_id)
                if current['status'] in ('review', 'failed', 'cancelled'):
                    return current
                time.sleep(.2)
            runner.handle_action(task_id, 'cancel')
            return store.get_task(task_id)
        try:
            runner.handle_action(task['id'], 'start')
            print('live: planning started', flush=True)
            planned = wait(task['id'])
            report['planStatus'] = planned['status']
            report['planError'] = planned.get('error')
            report['planResult'] = planned.get('result', '')
            if planned['status'] != 'review' or planned['kind'] != 'plan' or len(planned['plan']) != 1:
                raise RuntimeError('实际计划执行没有返回一个可审核的子任务')
            report['checks'].append('实际 Codex 计划已进入审核')
            approved = runner.handle_action(task['id'], 'approve_plan')
            child_id = approved['children'][0]
            print('live: plan approved, child dispatched', flush=True)
            child = wait(child_id)
            report['executeStatus'] = child['status']
            report['executeError'] = child.get('error')
            report['executeResult'] = child.get('result', '')
            if child['status'] != 'review':
                raise RuntimeError('实际子任务未返回待验收结果')
            actual = Path(directory, 'hello.txt').read_text()
            if actual != 'task-workbench-ok\n':
                raise RuntimeError('实际文件内容与目标不符')
            report['checks'].append('实际 Codex 写入的隔离文件内容正确')
            if not child.get('sessionId'):
                raise RuntimeError('没有捕获实际会话 ID')
            report['checks'].append('会话 ID、执行日志和结果已记录')
            runner.handle_action(child_id, 'accept')
            if store.get_task(task['id'])['status'] != 'done':
                raise RuntimeError('验收子任务后父任务没有完成')
            report['checks'].append('验收子任务后父任务自动完成')
            report['ok'] = True
        except Exception as exc:
            report['error'] = str(exc)
        finally:
            runner.stop()
            report['tasks'] = store.list_tasks()
            store.close()
    output = Path(__file__).resolve().parent.parent / '.data' / 'live-smoke.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != 'tasks'}, ensure_ascii=False, indent=2), flush=True)
    return 0 if report['ok'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
