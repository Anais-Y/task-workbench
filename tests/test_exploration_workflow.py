"""Exercise project lineage, execution, history and trash through the real API."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from taskboard.cli import Client, ensure_initial_project
from taskboard.runner import Runner
from taskboard.server import make_server
from taskboard.store import Store


class EvidenceAdapter:
    def __init__(self):
        self.prompts = []

    def list_adapters(self):
        return [{'id': 'test', 'available': True, 'enabled': True}]

    def run(self, engine, **kwargs):
        self.prompts.append(kwargs['prompt'])
        return {'ok': True, 'text': '可复核的测试结果', 'artifacts': []}


class ExplorationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='探索节点 ')
        self.marker = Path(self.temp.name) / 'keep.txt'
        self.marker.write_text('项目文件不得被删除')
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.store.create_project({'id': 'p', 'name': '模型探索', 'path': self.temp.name})
        self.adapter = EvidenceAdapter()
        self.runner = Runner(self.store, self.adapter, interval=.02)
        self.server = make_server(self.store, self.adapter, self.runner, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.runner.start()
        self.client = Client('http://127.0.0.1:%d' % self.server.server_address[1])

    def tearDown(self):
        self.runner.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.store.close()
        self.temp.cleanup()

    def node(self, title, **fields):
        return self.client.request('/api/nodes', {'projectId': 'p', 'title': title, **fields})

    def wait(self, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        self.fail('worker did not finish')

    def test_branch_merge_plan_inheritance_and_conclusion_revisions(self):
        a = self.node('特征 A', hypothesis='减少冷启动误差', color='blue')
        b = self.node('Loss B', hypothesis='提高排序质量', color='violet')
        merged = self.node('组合验证', parentIds=[a['id'], b['id']], color='teal')
        task = self.client.request('/api/tasks', {
            'projectId': 'p', 'title': '特征 A 消融', 'engine': 'test', 'nodeId': a['id'],
            'status': 'review', 'kind': 'plan', 'phase': 'plan',
            'plan': [{'title': '固定基线后比较', 'goal': '只在测试中运行', 'criteria': ['有结果'], 'deps': []}],
        })
        parent = self.client.request('/api/tasks/' + task['id'] + '/actions', {'action': 'approve_plan'})
        child_id = parent['children'][0]
        self.wait(lambda: self.store.get_task(child_id)['status'] == 'review' and not self.runner.snapshot()['workers'])
        self.assertEqual(self.store.get_task(child_id)['nodeId'], a['id'])
        self.assertIn('减少冷启动误差', self.adapter.prompts[0])
        self.assertEqual(self.store.get_node(a['id'])['outcome'], 'exploring')
        self.client.request('/api/tasks/' + child_id + '/actions', {'action': 'accept'})
        for conclusion in ('第一轮结论：有改善', '复核结论：仅在冷启动样本有效'):
            self.client.request('/api/nodes/' + a['id'], {'outcome': 'adopted', 'conclusion': conclusion})
        moved = self.client.request('/api/tasks/' + child_id + '/node', {'nodeId': merged['id']})
        self.assertEqual(moved['nodeId'], merged['id'])
        self.assertEqual(self.store.get_task(task['id'])['nodeId'], merged['id'])
        state = self.client.request('/api/state')
        summaries = '\n'.join(e['summary'] for e in state['trajectory'])
        self.assertIn('第一轮结论：有改善', summaries)
        self.assertIn('复核结论：仅在冷启动样本有效', summaries)
        with self.assertRaises(ValueError):
            self.client.request('/api/nodes/' + a['id'], {'parentIds': [merged['id']]})
        self.assertEqual(self.store.get_node(a['id'])['parentIds'], [])

    def test_trash_preserves_records_and_workspace_and_blocks_execution(self):
        node = self.node('保留节点')
        task = self.client.request('/api/tasks', {'projectId': 'p', 'title': '保留任务', 'nodeId': node['id'],
                                                'status': 'running', 'executionMode': 'external'})
        action_url = '/api/projects/p/actions'
        with self.assertRaises(ValueError):
            self.client.request(action_url, {'action': 'delete'})
        self.store.update_task(task['id'], {'status': 'done'})
        self.client.request(action_url, {'action': 'delete'})
        state = self.client.request('/api/state')
        self.assertEqual(state['projects'], [])
        self.assertEqual(state['tasks'], [])
        self.assertEqual(state['nodes'], [])
        self.assertEqual(state['deletedProjects'][0]['id'], 'p')
        self.assertIsNone(ensure_initial_project(self.store, self.temp.name))
        with self.assertRaises(ValueError):
            self.client.request('/api/tasks/' + task['id'] + '/actions', {'action': 'feedback', 'message': '继续'})
        self.client.request(action_url, {'action': 'restore'})
        restored = self.client.request('/api/state')
        self.assertEqual(restored['tasks'][0]['nodeId'], node['id'])
        self.assertEqual(restored['nodes'][0]['id'], node['id'])
        self.assertEqual(self.marker.read_text(), '项目文件不得被删除')
        types = {e['type'] for e in restored['trajectory']}
        self.assertTrue({'project_deleted', 'project_restored'} <= types)


if __name__ == '__main__':
    unittest.main()
