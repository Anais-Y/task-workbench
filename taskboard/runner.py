"""Task lifecycle and bounded background scheduling, independent of any agent."""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_plan(text):
    """Accept a JSON object inside plain prose or a fenced final answer."""
    decoder = json.JSONDecoder()
    candidates = [text.strip()]
    candidates += [text[m.start():] for m in re.finditer(r'\{', text)]
    obj = None
    for candidate in candidates:
        try:
            value, _ = decoder.raw_decode(candidate)
            if isinstance(value, dict) and isinstance(value.get('subtasks'), list):
                obj = value
                break
        except (ValueError, TypeError):
            pass
    if obj is None:
        raise ValueError('计划没有包含可执行的 subtasks 列表，请反馈让 Agent 补充结构化计划。')
    items = obj['subtasks']
    if not 1 <= len(items) <= 24:
        raise ValueError('计划需要包含 1–24 个子任务。')
    plan = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get('title'), str) or not item['title'].strip():
            raise ValueError('每个子任务都需要标题。')
        deps = item.get('deps', item.get('dependencies', []))
        if not isinstance(deps, list) or any(type(d) is not int or d < 0 or d >= len(items) or d == i for d in deps):
            raise ValueError('计划依赖必须是有效的子任务编号（从 0 开始）。')
        criteria = item.get('criteria', [])
        if not isinstance(criteria, list) or any(not isinstance(x, str) for x in criteria):
            raise ValueError('子任务验收标准需要是文字列表。')
        plan.append({'title': item['title'].strip()[:200], 'goal': str(item.get('goal', item['title']))[:12000],
                     'criteria': criteria, 'deps': sorted(set(deps))})
    visiting, visited = set(), set()
    def visit(i):
        if i in visiting:
            raise ValueError('计划存在循环依赖，请调整。')
        if i in visited:
            return
        visiting.add(i)
        for d in plan[i]['deps']:
            visit(d)
        visiting.remove(i)
        visited.add(i)
    for i in range(len(plan)):
        visit(i)
    return plan


class Runner:
    def __init__(self, store, registry, max_workers=3, interval=0.5):
        if not 1 <= max_workers <= 8:
            raise ValueError('并发数需要在 1–8 之间。')
        self.store, self.registry = store, registry
        self.max_workers, self.interval = max_workers, interval
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._active = {}
        self._thread = None

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            # An interrupted process cannot be silently counted as still running.
            for task in self.store.list_tasks():
                if task['status'] == 'running' and task.get('executionMode') == 'local':
                    runs = task.get('runs', [])
                    if runs:
                        runs[-1] = dict(runs[-1], status='interrupted', endedAt=now())
                    self.store.add_event(task['id'], '上次服务停止，执行已中断。可以重试。', status='failed',
                                         scheduled=False, worker=None, error='服务中断', runs=runs)
            self._thread = threading.Thread(target=self._loop, name='task-scheduler', daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            active = list(self._active.values())
            for item in active:
                item['cancel'].set()
        if self._thread:
            self._thread.join(timeout=2)
        for item in active:
            item['thread'].join(timeout=6)

    def snapshot(self):
        with self._lock:
            workers = [{'id': item['worker'], 'name': item['worker'], 'taskId': task_id,
                        'engine': item['engine'], 'status': 'running', 'source': 'local'}
                       for task_id, item in self._active.items()]
            seen = {w['id'] for w in workers}
            for task in self.store.list_tasks():
                name = task.get('worker')
                if task.get('executionMode') == 'external' and task['status'] == 'running' and name and name not in seen:
                    seen.add(name)
                    workers.append({'id': name, 'name': name, 'taskId': task['id'], 'engine': task['engine'],
                                    'status': 'running', 'source': 'external'})
            return {'workers': workers, 'maxWorkers': self.max_workers}

    def _available(self, engine):
        adapter = next((a for a in self.registry.list_adapters() if a['id'] == engine), None)
        if not adapter or not adapter.get('available') or not adapter.get('enabled', True):
            raise ValueError('这个执行器尚未安装或启用，请在执行器页面查看。')

    def handle_action(self, task_id, action, payload=None):
        payload = payload or {}
        with self._lock:
            task = self.store.get_task(task_id)
            if not task:
                raise KeyError('找不到任务')
            external = task.get('executionMode') == 'external'
            state = task['status']
            if action == 'accept':
                if state != 'review' or task.get('kind') != 'result':
                    raise ValueError('只有待验收的结果可以标记完成。')
                task = self.store.add_event(task_id, '结果已验收。', status='done', scheduled=False,
                                            done=len(task.get('steps', [])), worker=None)
                self._reconcile_parents()
                return task
            if action == 'approve_plan':
                if state != 'review' or task.get('kind') != 'plan':
                    raise ValueError('当前任务没有待审核的计划。')
                plan = parse_plan(json.dumps({'subtasks': task.get('plan')}, ensure_ascii=False))
                self._available(task['engine'])
                # Generate all nodes unscheduled before wiring and releasing them.
                children = []
                for item in plan:
                    child = self.store.create_task({'title': item['title'], 'goal': item['goal'], 'desc': item['goal'],
                        'criteria': item['criteria'], 'projectId': task['projectId'], 'engine': task['engine'],
                        'phase': 'execute', 'kind': 'result', 'parentTaskId': task_id,
                        'executionMode': 'local', 'scheduled': False, 'steps': item['criteria']})
                    children.append(child)
                for i, item in enumerate(plan):
                    self.store.update_task(children[i]['id'], {'deps': [children[d]['id'] for d in item['deps']],
                                                               'scheduled': True})
                return self.store.add_event(task_id, '计划已批准，已创建 %d 个子任务。' % len(children),
                    status='queued', executionMode='external', scheduled=False, kind='result', worker=None,
                    children=[c['id'] for c in children], steps=[p['title'] for p in plan], done=0)
            if action == 'feedback':
                message = payload.get('message', '')
                if not isinstance(message, str):
                    raise ValueError('反馈内容需要是文字。')
                message = message.strip()
                if not message:
                    raise ValueError('请先填写反馈。')
                if len(message) > 20000:
                    raise ValueError('反馈过长，请缩短到 20000 字以内。')
                if state not in ('review', 'failed', 'done'):
                    raise ValueError('任务待审核或结束后可以反馈。')
                if task.get('children'):
                    raise ValueError('计划已执行，请向具体子任务反馈。')
                if external:
                    return self.store.add_event(task_id, '收到反馈：' + message, status='review', kind='result',
                                                feedback=message, feedbackPending=True)
                engine = payload.get('engine', task['engine'])
                self._available(engine)
                return self.store.add_event(task_id, '反馈已加入下一轮：' + message, status='queued', error='',
                    scheduled=True, feedback=message, worker=None, engine=engine)
            if action in ('start', 'retry'):
                if task_id in self._active:
                    raise ValueError('上一次执行正在停止，请稍后重试。')
                allowed = ('queued',) if action == 'start' else ('failed', 'cancelled')
                if state not in allowed:
                    raise ValueError('当前状态不能执行这个操作。')
                if external:
                    raise ValueError('这是外部协作任务，由当前 Codex 对话协调；本地执行器不能重复启动它。')
                engine = payload.get('engine', task['engine'])
                self._available(engine)
                return self.store.add_event(task_id, '已加入执行队列。', scheduled=True, error='',
                                            status='queued', engine=engine, worker=None, cancelRequested=False)
            if action == 'cancel':
                if state not in ('queued', 'running', 'review', 'failed'):
                    raise ValueError('当前状态不能取消。')
                if external:
                    raise ValueError('外部协作 worker 需要在当前 Codex 对话中停止。')
                item = self._active.get(task_id)
                if item:
                    item['cancel'].set()
                    return self.store.add_event(task_id, '已请求停止任务，等待执行器退出。', scheduled=False, cancelRequested=True)
                return self.store.add_event(task_id, '任务已取消。', status='cancelled', scheduled=False, worker=None)
            raise ValueError('不支持的操作：' + str(action))

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                # Keep the dispatcher alive; runtime errors must still be visible to the owner.
                import sys
                print('scheduler: ' + str(exc), file=sys.stderr, flush=True)
            self._stop.wait(self.interval)

    def _reconcile_parents(self):
        tasks = {t['id']: t for t in self.store.list_tasks()}
        for parent in tasks.values():
            ids = parent.get('children', [])
            if not ids:
                continue
            children = [tasks.get(i) for i in ids]
            done = sum(bool(c and c['status'] == 'done') for c in children)
            if done == len(ids) and parent['status'] != 'done':
                self.store.add_event(parent['id'], '所有子任务均已验收。', status='done', done=done, worker=None)
            elif done != parent.get('done', 0) or (done < len(ids) and parent['status'] == 'done'):
                self.store.update_task(parent['id'], {'done': done, 'status': 'queued'})

    def tick(self):
        with self._lock:
            self._reconcile_parents()
            tasks = self.store.list_tasks()
            by_id = {t['id']: t for t in tasks}
            for task in tasks:
                if self._stop.is_set() or len(self._active) >= self.max_workers:
                    break
                if task['status'] != 'queued' or not task.get('scheduled') or task.get('executionMode') != 'local':
                    continue
                if task['id'] in self._active:
                    continue
                deps = task.get('deps', [])
                if any(by_id.get(d, {}).get('status') != 'done' for d in deps):
                    continue
                try:
                    self._available(task['engine'])
                    project = self.store.get_project(task['projectId'])
                    if not project or not Path(project['path']).is_dir():
                        raise ValueError('项目工作目录不存在。')
                except Exception as exc:
                    self.store.add_event(task['id'], str(exc), status='failed', scheduled=False, error=str(exc))
                    continue
                used = {item['worker'] for item in self._active.values()}
                slot = next('Worker %d' % i for i in range(1, self.max_workers + 1) if 'Worker %d' % i not in used)
                run_id = uuid.uuid4().hex[:12]
                runs = task.get('runs', []) + [{'id': run_id, 'engine': task['engine'], 'startedAt': now(),
                                               'status': 'running', 'phase': task.get('phase', 'plan')}]
                self.store.add_event(task['id'], slot + ' 开始执行。', status='running', worker=slot,
                                     error='', runs=runs[-100:])
                cancellation = threading.Event()
                thread = threading.Thread(target=self._execute, args=(task['id'], project['path'], cancellation),
                                          name='run-' + task['id'], daemon=True)
                self._active[task['id']] = {'worker': slot, 'engine': task['engine'], 'cancel': cancellation, 'thread': thread}
                thread.start()

    def _prompt(self, task):
        criteria = '\n'.join('- ' + str(x) for x in task.get('criteria', []))
        prompt = '任务：' + task['title'] + '\n目标：' + (task.get('goal') or task.get('desc') or task['title'])
        prompt += '\n验收标准：\n' + criteria
        if task.get('deps'):
            prompt += '\n已验收的前置任务：\n'
            for dep_id in task['deps']:
                dep = self.store.get_task(dep_id)
                if dep:
                    prompt += dep['title'] + '\n' + dep.get('result', '')[-6000:] + '\n'
        if task.get('feedback'):
            prompt += '\n用户本轮反馈：' + task['feedback']
        if task.get('phase') == 'plan':
            prompt += '''\n请只调查和制定计划，不要修改文件。将工作拆为 1–24 个可验收的子任务，明确每个任务负责的文件范围，避免并行任务编辑同一个文件。最后必须输出 JSON 对象（可用代码围栏），格式：{"summary":"概述","subtasks":[{"title":"标题","goal":"目标与负责的文件范围","criteria":["验收标准"],"deps":[]}]}。deps 为依赖任务在 subtasks 数组的从 0 开始的编号，不能形成循环。不要执行计划，等待用户审核。'''
        else:
            prompt += '\n请在当前项目目录完成这个任务。并行任务可能正在工作，只修改任务负责的文件，不覆盖别人的改动。完成后说明修改文件、验证结果和未完成项。需要更多信息时明确提出，不要声称已经完成。'
        return prompt

    def _execute(self, task_id, cwd, cancellation):
        try:
            task = self.store.get_task(task_id)
            def emit(event):
                with self._lock:
                    current = self.store.get_task(task_id)
                    if not current or current['status'] != 'running':
                        return
                    patch = {}
                    if event.get('sessionId'):
                        patch.update(sessionId=event['sessionId'], sessionEngine=task['engine'])
                    artifact = event.get('artifact')
                    if isinstance(artifact, dict):
                        patch['artifacts'] = current.get('artifacts', []) + [artifact]
                    message = str(event.get('message', '')).strip()
                    if message:
                        self.store.add_event(task_id, message[:12000], **patch)
                    elif patch:
                        self.store.update_task(task_id, patch)
            session = task.get('sessionId') if task.get('sessionEngine') == task['engine'] else None
            result = self.registry.run(task['engine'], prompt=self._prompt(task), cwd=cwd,
                phase=task.get('phase', 'plan'), session_id=session, emit=emit, cancel=cancellation)
            with self._lock:
                current = self.store.get_task(task_id)
                runs = current.get('runs', [])
                cancelled = not result.get('cancelFailed') and (
                    cancellation.is_set() or result.get('cancelled') or current['status'] == 'cancelled')
                ok = bool(result.get('ok')) and not cancelled
                patch = {'scheduled': False, 'worker': None, 'runs': runs, 'cancelRequested': False,
                         'result': str(result.get('text', ''))[-200000:]}
                if result.get('sessionId'):
                    patch.update(sessionId=result['sessionId'], sessionEngine=task['engine'])
                if runs:
                    runs[-1] = dict(runs[-1], endedAt=now(), status='cancelled' if cancelled else 'review' if ok else 'failed',
                                    sessionId=result.get('sessionId'))
                if cancelled:
                    patch.update(status='cancelled')
                    message = '任务已停止。'
                elif not ok:
                    patch.update(status='failed', error=str(result.get('error') or '执行未成功，请查看日志。'))
                    message = '执行失败：' + patch['error']
                else:
                    patch.update(status='review', kind='result', error='')
                    artifacts = list(current.get('artifacts', []))
                    for artifact in result.get('artifacts') or []:
                        if artifact not in artifacts:
                            artifacts.append(artifact)
                    artifacts.append({'name': '执行结果-' + str(len(runs)) + '.md', 'type': 'markdown', 'body': patch['result']})
                    # Keep references to actual existing changed files, when reported by a provider.
                    patch['artifacts'] = artifacts[-100:]
                    message = '执行器已返回结果，等待验收。'
                    if task.get('phase') == 'plan':
                        try:
                            patch.update(plan=parse_plan(patch['result']), kind='plan')
                            message = '计划已生成，等待审核。'
                        except ValueError as exc:
                            patch.update(kind='question', error=str(exc), plan=[])
                            message = str(exc)
                self.store.add_event(task_id, message, **patch)
        except Exception as exc:
            with self._lock:
                current = self.store.get_task(task_id)
                if current:
                    cancelled = cancellation.is_set() or current['status'] == 'cancelled'
                    runs = current.get('runs', [])
                    if runs:
                        runs[-1] = dict(runs[-1], endedAt=now(), status='cancelled' if cancelled else 'failed')
                    self.store.add_event(task_id, '执行异常：' + str(exc), status='cancelled' if cancelled else 'failed',
                        error=str(exc), worker=None, scheduled=False, runs=runs)
        finally:
            with self._lock:
                self._active.pop(task_id, None)
