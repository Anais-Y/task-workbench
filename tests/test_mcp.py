import io
import json
import unittest
from taskboard.mcp import Bridge, serve_stdio


class FakeClient:
    def __init__(self):
        self.calls = []
    def request(self, path, body=None):
        self.calls.append((path, body))
        if path == '/api/state':
            return {'projects': [{'id': 'p'}], 'tasks': [{'id': 'T1', 'projectId': 'p', 'status': 'queued'}]}
        return {'id': 'T2', **(body or {})}


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.bridge = Bridge(self.client)
        self.rpc('initialize', {'protocolVersion': '2025-03-26'})
    def rpc(self, method, params=None):
        return self.bridge.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})
    def test_discovery_and_creation_share_api(self):
        self.assertEqual(len(self.rpc('tools/list')['result']['tools']), 6)
        result = self.rpc('tools/call', {'name': 'create_task', 'arguments': {'title': 'A', 'projectId': 'p'}})
        self.assertFalse(result['result']['isError'])
        self.assertEqual(self.client.calls[-1][0], '/api/tasks')
        self.assertEqual(self.client.calls[-1][1]['phase'], 'plan')
        self.assertNotIn('start', self.client.calls[-1][1])
    def test_invalid_input_does_not_call_api(self):
        result = self.rpc('tools/call', {'name': 'read_artifact', 'arguments': {'taskId': 'T1', 'index': -1}})
        self.assertEqual(result['error']['code'], -32602)
        self.assertFalse(self.client.calls)
    def test_stdio_is_json_only_and_notifications_have_no_reply(self):
        lines = [json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}),
                 json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}),
                 json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}), '{bad']
        output = io.StringIO()
        serve_stdio(self.client, io.StringIO('\n'.join(lines)), output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(replies), 3)
        self.assertEqual(replies[-1]['error']['code'], -32700)
    def test_task_filter(self):
        result = self.rpc('tools/call', {'name': 'list_tasks', 'arguments': {'projectId': 'other'}})
        self.assertEqual(json.loads(result['result']['content'][0]['text']), [])


if __name__ == '__main__':
    unittest.main()
