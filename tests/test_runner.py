import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from taskboard.store import Store
from taskboard.runner import Runner, parse_plan, process_state


class ControlledAdapter:
    def __init__(self):
        self.calls = []
        self.release = threading.Event()
        self.result = {'ok': True, 'text': '已完成并验证。', 'sessionId': 'session-1', 'artifacts': []}
    def list_adapters(self):
        return [{'id': 'codex', 'available': True, 'enabled': True}, {'id': 'claude', 'available': True, 'enabled': True}]
    def run(self, engine, **kw):
        self.calls.append((engine, kw))
        kw['emit']({'type': 'message', 'message': '开始读取项目'})
        while not self.release.wait(.01):
            if kw['cancel'].is_set():
                return {'ok': False, 'cancelled': True, 'text': ''}
        return dict(self.result)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(':memory:')
        self.store.create_project({'id': 'p', 'name': 'Project', 'path': self.temp.name})
        self.adapter = ControlledAdapter()
        self.runner = Runner(self.store, self.adapter, max_workers=2)
    def tearDown(self):
        self.runner.stop()
        self.store.close()
        self.temp.cleanup()
    def create(self, **patch):
        data = {'projectId': 'p', 'title': 'Task', 'phase': 'execute', 'kind': 'result'}
        data.update(patch)
        return self.store.create_task(data)
    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail('condition did not complete')
    def test_concurrency_and_dependencies_wait_for_acceptance(self):
        first = self.create(scheduled=True)
        second = self.create(scheduled=True)
        dependent = self.create(deps=[first['id']], scheduled=True)
        extra = self.create(scheduled=True)
        self.runner.tick()
        self.wait_for(lambda: len(self.adapter.calls) == 2)
        self.assertEqual(self.store.get_task(dependent['id'])['status'], 'queued')
        self.assertEqual(self.store.get_task(extra['id'])['status'], 'queued')
        self.adapter.release.set()
        self.wait_for(lambda: not self.runner.snapshot()['workers'])
        self.assertEqual(self.store.get_task(first['id'])['status'], 'review')
        self.runner.tick()
        self.wait_for(lambda: self.store.get_task(extra['id'])['status'] == 'review')
        self.assertEqual(self.store.get_task(dependent['id'])['status'], 'queued')
        self.runner.handle_action(first['id'], 'accept')
        self.runner.tick()
        self.wait_for(lambda: self.store.get_task(dependent['id'])['status'] == 'review')
        self.assertEqual(len(self.adapter.calls), 4)
    def test_plan_approval_creates_graph_once_and_completes_parent(self):
        plan = [{'title': 'First', 'goal': 'a', 'criteria': ['ok'], 'deps': []},
                {'title': 'Second', 'goal': 'b', 'criteria': ['ok'], 'deps': [0]}]
        parent = self.create(phase='plan', kind='plan', status='review', plan=plan)
        updated = self.runner.handle_action(parent['id'], 'approve_plan')
        ids = updated['children']
        self.assertEqual(self.store.get_task(ids[1])['deps'], [ids[0]])
        with self.assertRaises(ValueError):
            self.runner.handle_action(parent['id'], 'approve_plan')
        for child_id in ids:
            self.store.update_task(child_id, {'status': 'review'})
            self.runner.handle_action(child_id, 'accept')
        self.assertEqual(self.store.get_task(parent['id'])['status'], 'done')
    def test_cancel_stops_run_and_retry_preserves_history(self):
        task = self.create(scheduled=True)
        self.runner.tick()
        self.wait_for(lambda: len(self.adapter.calls) == 1)
        self.runner.handle_action(task['id'], 'cancel')
        self.wait_for(lambda: not self.runner.snapshot()['workers'])
        self.assertEqual(self.store.get_task(task['id'])['status'], 'cancelled')
        self.runner.handle_action(task['id'], 'retry')
        self.adapter.release.set()
        self.runner.tick()
        self.wait_for(lambda: self.store.get_task(task['id'])['status'] == 'review')
        self.assertEqual(len(self.store.get_task(task['id'])['runs']), 2)
    def test_feedback_only_resumes_matching_provider(self):
        task = self.create(status='review', sessionId='codex-session', sessionEngine='codex')
        self.runner.handle_action(task['id'], 'feedback', {'message': '换个执行器', 'engine': 'claude'})
        self.adapter.release.set()
        self.runner.tick()
        self.wait_for(lambda: self.store.get_task(task['id'])['status'] == 'review')
        engine, args = self.adapter.calls[0]
        self.assertEqual(engine, 'claude')
        self.assertIsNone(args['session_id'])
        self.assertIn('换个执行器', args['prompt'])

    def test_retry_waits_for_previous_process_to_finish(self):
        task = self.create(scheduled=True)
        self.runner.tick()
        self.wait_for(lambda: len(self.adapter.calls) == 1)
        with self.runner._lock:
            self.runner.handle_action(task['id'], 'cancel')
            with self.assertRaisesRegex(ValueError, '正在停止'):
                self.runner.handle_action(task['id'], 'retry')
        self.wait_for(lambda: not self.runner.snapshot()['workers'])
        updated = self.runner.handle_action(task['id'], 'retry')
        self.assertEqual(updated['status'], 'queued')
        self.assertTrue(updated['scheduled'])

    def test_feedback_type_is_validated(self):
        task = self.create(status='review')
        with self.assertRaises(ValueError):
            self.runner.handle_action(task['id'], 'feedback', {'message': 123})

    def test_failure_to_stop_process_is_not_reported_as_cancelled(self):
        def fail_stop(engine, **kwargs):
            kwargs['cancel'].set()
            return {'ok': False, 'text': '', 'cancelFailed': True, 'error': '进程仍在运行'}
        self.adapter.run = fail_stop
        task = self.create(scheduled=True)
        self.runner.tick()
        self.wait_for(lambda: self.store.get_task(task['id'])['status'] == 'failed')
        self.assertIn('仍在运行', self.store.get_task(task['id'])['error'])

    def test_unknown_stop_requires_explicit_confirmation_and_blocks_all_restart_paths(self):
        task = self.create(status='failed', cancelFailed=True, processId=None, processState='unknown')
        for action, payload in (('retry', {}), ('feedback', {'message': 'continue'}), ('cancel', {})):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, '尚未确认'):
                self.runner.handle_action(task['id'], action, payload)
        with self.assertRaisesRegex(ValueError, '明确确认'):
            self.runner.handle_action(task['id'], 'confirm_stopped')
        with self.assertRaises(ValueError):
            self.runner.handle_action(task['id'], 'confirm_stopped', {'confirmStopped': 'true'})
        current = self.runner.handle_action(task['id'], 'confirm_stopped', {'confirmStopped': True})
        self.assertFalse(current['cancelFailed'])
        self.assertEqual(current['processState'], 'user_confirmed')
        self.assertIn('文件写入已经停止', current['log'][-1][1])
        self.assertTrue(self.runner.handle_action(task['id'], 'retry')['scheduled'])

    def test_stop_failure_persists_and_blocks_family_until_real_process_exits(self):
        process = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                                   stdin=subprocess.PIPE, start_new_session=True)
        path = str(Path(self.temp.name) / 'stop-failure.sqlite3')
        store = Store(path)
        adapter = ControlledAdapter()
        adapter.run = lambda engine, **kwargs: {'ok': False, 'cancelFailed': True,
            'processId': process.pid, 'error': 'controlled permission failure', 'text': ''}
        runner = Runner(store, adapter, interval=.01)
        try:
            store.create_project({'id': 'p', 'name': 'Process audit', 'path': self.temp.name})
            for node_id in ('A', 'B'):
                store.create_node({'id': node_id, 'projectId': 'p', 'title': node_id})
            parent = store.create_task({'projectId': 'p', 'title': 'Parent', 'nodeId': 'A', 'executionMode': 'external'})
            child = store.create_task({'projectId': 'p', 'title': 'Child', 'parentTaskId': parent['id'],
                                       'phase': 'execute', 'kind': 'result', 'scheduled': True})
            sibling = store.create_task({'projectId': 'p', 'title': 'Sibling', 'parentTaskId': parent['id']})
            runner.tick()
            self.wait_for(lambda: store.get_task(child['id'])['status'] == 'failed' and not runner._active)
            failed = store.get_task(child['id'])
            self.assertTrue(failed['cancelFailed'])
            self.assertEqual(failed['processId'], process.pid)
            self.assertEqual(failed['processState'], 'alive')
            self.assertIn('活动监视器', failed['attention']['description'])
            self.assertIn('写入', failed['error'])
            self.assertEqual(runner.snapshot()['workers'][0]['status'], 'unconfirmed')
            operations = [lambda: runner.handle_project_action('p', 'delete'),
                          lambda: runner.assign_task_node(parent['id'], 'B'),
                          lambda: runner.handle_action(child['id'], 'retry'),
                          lambda: runner.handle_action(child['id'], 'feedback', {'message': 'continue'}),
                          lambda: runner.handle_action(sibling['id'], 'start')]
            for operation in operations:
                with self.assertRaisesRegex(ValueError, '尚未确认'):
                    operation()
            with self.assertRaisesRegex(ValueError, '仍检测到'):
                runner.handle_action(child['id'], 'confirm_stopped', {'confirmStopped': True})
            # A service restart retains the stop barrier and original PID.
            runner.stop()
            store.close()
            store = Store(path)
            runner = Runner(store, adapter)
            self.assertTrue(store.get_task(child['id'])['cancelFailed'])
            with self.assertRaises(ValueError):
                runner.handle_project_action('p', 'delete')
            process.stdin.close()
            process.wait(timeout=3)
            # A new operation performs a fresh probe without waiting for the
            # background probe interval; only confirmed exit permits movement.
            runner.assign_task_node(child['id'], 'B')
            cleared = store.get_task(child['id'])
            self.assertFalse(cleared['cancelFailed'])
            self.assertEqual(cleared['processState'], 'exited')
            self.assertFalse(runner.snapshot()['workers'])
            self.assertTrue(runner.handle_action(child['id'], 'retry')['scheduled'])
            runner.handle_action(child['id'], 'cancel')
            self.assertTrue(all(store.get_task(task_id)['nodeId'] == 'B' for task_id in (parent['id'], child['id'], sibling['id'])))
            self.assertTrue(runner.handle_project_action('p', 'delete')['deletedAt'])
        finally:
            runner.stop()
            store.close()
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)
            if not process.stdin.closed:
                process.stdin.close()

    def test_unknown_process_reserves_capacity_and_prevents_scheduled_family_run(self):
        parent = self.create(executionMode='external')
        self.create(parentTaskId=parent['id'], status='failed', cancelFailed=True,
                    processState='unknown', processId=None, unconfirmedWorker='Worker 1')
        sibling = self.create(parentTaskId=parent['id'], scheduled=True)
        independent = self.create(scheduled=True)
        extra = self.create(scheduled=True)
        self.runner.tick()
        self.wait_for(lambda: len(self.adapter.calls) == 1)
        self.assertEqual(self.store.get_task(sibling['id'])['status'], 'queued')
        self.assertEqual(self.store.get_task(independent['id'])['status'], 'running')
        self.assertEqual(self.store.get_task(extra['id'])['status'], 'queued')
        self.assertEqual(len(self.runner.snapshot()['workers']), 2)

    def test_process_probe_only_checks_and_never_treats_unknown_as_exited(self):
        self.assertEqual(process_state(None), 'unknown')
        self.assertEqual(process_state(-1), 'unknown')
        with patch('taskboard.runner.os.kill', side_effect=PermissionError()) as kill:
            self.assertEqual(process_state(12345), 'unknown')
            kill.assert_called_once_with(12345, 0)
        with patch('taskboard.runner.os.kill', side_effect=ProcessLookupError()), patch('taskboard.runner.os.killpg', return_value=None) as group:
            self.assertEqual(process_state(12345), 'alive')
            group.assert_called_once_with(12345, 0)
        with patch('taskboard.runner.os.kill', side_effect=ProcessLookupError()), patch('taskboard.runner.os.killpg', side_effect=ProcessLookupError()):
            self.assertEqual(process_state(12345), 'exited')
        with patch('taskboard.runner.os.kill', side_effect=ProcessLookupError()), patch('taskboard.runner.os.killpg', side_effect=PermissionError()), patch('taskboard.runner.subprocess.run', side_effect=OSError('unavailable')):
            self.assertEqual(process_state(12345), 'unknown')
    def test_malformed_plan_needs_feedback_instead_of_claiming_success(self):
        self.adapter.result['text'] = '先读代码，然后实现。'
        task = self.create(phase='plan', kind='plan', scheduled=True)
        self.adapter.release.set()
        self.runner.tick()
        self.wait_for(lambda: self.store.get_task(task['id'])['status'] == 'review')
        current = self.store.get_task(task['id'])
        self.assertEqual(current['kind'], 'question')
        self.assertTrue(current['error'])
        with self.assertRaises(ValueError):
            self.runner.handle_action(task['id'], 'approve_plan')
    def test_restart_marks_interrupted_local_run_only(self):
        local = self.create(status='running', runs=[{'id': 'old', 'status': 'running'}])
        external = self.create(status='running', executionMode='external', worker='协作 worker')
        self.runner.start()
        self.assertEqual(self.store.get_task(local['id'])['status'], 'failed')
        self.assertEqual(self.store.get_task(external['id'])['status'], 'running')
    def test_provider_failure_and_unavailable_adapter(self):
        self.adapter.result.update(ok=False, error='测试失败')
        task = self.create(scheduled=True)
        self.adapter.release.set()
        self.runner.tick()
        self.wait_for(lambda: self.store.get_task(task['id'])['status'] == 'failed')
        self.assertIn('测试失败', self.store.get_task(task['id'])['error'])
        unavailable = self.create(engine='missing')
        with self.assertRaises(ValueError):
            self.runner.handle_action(unavailable['id'], 'start')
    def test_external_feedback_is_recorded_without_launch(self):
        task = self.create(status='review', executionMode='external')
        result = self.runner.handle_action(task['id'], 'feedback', {'message': '请修改标题'})
        self.assertTrue(result['feedbackPending'])
        self.assertFalse(result['scheduled'])
        self.runner.tick()
        self.assertFalse(self.adapter.calls)
    def test_parse_plan_rejects_cycles_and_bool_dependency(self):
        valid = {'subtasks': [{'title': 'A', 'deps': []}]}
        self.assertEqual(parse_plan('计划\n```json\n' + json.dumps(valid) + '\n```')[0]['title'], 'A')
        for items in ([{'title': 'A', 'deps': [1]}, {'title': 'B', 'deps': [0]}],
                      [{'title': 'A', 'deps': [False]}]):
            with self.assertRaises(ValueError):
                parse_plan(json.dumps({'subtasks': items}))


if __name__ == '__main__':
    unittest.main()
