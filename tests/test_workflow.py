"""Cross-component lifecycle through the same HTTP and MCP surfaces as the UI."""
import json
import tempfile
import threading
import time
import unittest
from taskboard.cli import Client
from taskboard.mcp import Bridge
from taskboard.runner import Runner
from taskboard.server import make_server
from taskboard.store import Store


class WorkflowAdapter:
    def __init__(self): self.calls = []
    def list_adapters(self):
        return [{'id': 'test', 'name': 'Controlled test agent', 'available': True, 'enabled': True}]
    def run(self, engine, **kw):
        self.calls.append(kw)
        if kw['phase'] == 'plan':
            text = json.dumps({'subtasks': [{'title': 'A', 'criteria': ['A ok'], 'deps': []},
                                           {'title': 'B', 'criteria': ['B ok'], 'deps': [0]}]})
            return {'ok': True, 'text': text, 'sessionId': 'plan-id', 'artifacts': []}
        artifact = {'name': 'result.txt', 'type': 'text', 'body': '<safe> actual output'}
        kw['emit']({'type': 'artifact', 'artifact': artifact, 'message': '产物已生成'})
        return {'ok': True, 'text': 'result', 'sessionId': 'execute-id', 'artifacts': [artifact]}


class WorkflowTests(unittest.TestCase):
    def test_plan_dependency_review_feedback_and_artifact_via_api(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(':memory:')
            store.create_project({'id': 'p', 'name': 'test', 'path': directory})
            adapter = WorkflowAdapter()
            runner = Runner(store, adapter, interval=.02)
            server = make_server(store, adapter, runner, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            runner.start()
            client = Client('http://127.0.0.1:%d' % server.server_address[1])
            bridge = Bridge(client)
            bridge.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
            def action(task_id, action_name, **payload):
                return client.request('/api/tasks/' + task_id + '/actions', {'action': action_name, **payload})
            def wait_review(task_id):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    task = store.get_task(task_id)
                    if task['status'] == 'review': return task
                    time.sleep(.02)
                self.fail('task never reached review')
            try:
                response = bridge.dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                    'params': {'name': 'create_task', 'arguments': {'projectId': 'p', 'title': 'Workflow', 'engine': 'test', 'start': True}}})
                task = json.loads(response['result']['content'][0]['text'])
                planned = wait_review(task['id'])
                self.assertEqual(planned['kind'], 'plan')
                parent = action(task['id'], 'approve_plan')
                a, b = parent['children']
                first = wait_review(a)
                self.assertEqual(store.get_task(b)['status'], 'queued')
                # The streamed and final copies of the same artifact appear once.
                self.assertEqual(sum(x['name'] == 'result.txt' for x in first['artifacts']), 1)
                preview = client.request('/api/tasks/' + a + '/artifacts/0')
                self.assertEqual(preview['body'], '<safe> actual output')
                action(a, 'accept')
                wait_review(b)
                action(b, 'feedback', message='请再检查一下')
                revised = wait_review(b)
                self.assertEqual(len(revised['runs']), 2)
                self.assertEqual(adapter.calls[-1]['session_id'], 'execute-id')
                action(b, 'accept')
                state = client.request('/api/state')
                self.assertTrue(all(t['status'] == 'done' for t in state['tasks']))
                self.assertEqual(len(adapter.calls), 4)
            finally:
                runner.stop()
                server.shutdown()
                server.server_close()
                thread.join(2)
                store.close()


if __name__ == '__main__': unittest.main()
