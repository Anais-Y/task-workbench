import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch
from taskboard.cli import ensure_initial_project, main
from taskboard.mcp import Bridge, serve_stdio


class FakeClient:
    def __init__(self):
        self.calls = []
        self.state = {
            'projects': [{'id': 'p', 'deletedAt': None}],
            'deletedProjects': [{'id': 'deleted', 'deletedAt': '2026-10-09T08:00:00Z'}],
            'tasks': [{'id': 'T1', 'projectId': 'p', 'status': 'queued', 'nodeId': None},
                      {'id': 'T2', 'projectId': 'p', 'status': 'review', 'nodeId': 'N1'},
                      {'id': 'T3', 'projectId': 'other', 'status': 'queued', 'nodeId': 'N2'}],
            'nodes': [{'id': 'N1', 'projectId': 'p', 'title': '方向 A', 'parentIds': []},
                      {'id': 'N2', 'projectId': 'other', 'title': '方向 B', 'parentIds': []}],
            'trajectory': [{'id': 'E1', 'projectId': 'p', 'type': 'node_created'},
                           {'id': 'E2', 'projectId': 'other', 'type': 'task_created'}],
        }
    def request(self, path, body=None):
        self.calls.append((path, body))
        if path == '/api/state':
            return self.state
        return {'id': 'T2', **(body or {})}


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.bridge = Bridge(self.client)
        self.rpc('initialize', {'protocolVersion': '2025-03-26'})
    def rpc(self, method, params=None):
        return self.bridge.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})
    def test_discovery_and_creation_share_api(self):
        discovered = {tool['name']: tool for tool in self.rpc('tools/list')['result']['tools']}
        self.assertTrue({'create_task', 'list_tasks', 'read_artifact', 'create_node', 'update_node',
                         'assign_task_node', 'project_action', 'list_trajectory'}.issubset(discovered))
        self.assertEqual(discovered['assign_task_node']['inputSchema']['required'], ['taskId', 'nodeId'])
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
        result = self.rpc('tools/call', {'name': 'list_tasks', 'arguments': {'projectId': 'missing'}})
        self.assertEqual(json.loads(result['result']['content'][0]['text']), [])

    def call(self, name, **arguments):
        reply = self.rpc('tools/call', {'name': name, 'arguments': arguments})
        self.assertNotIn('error', reply, reply)
        self.assertFalse(reply['result']['isError'], reply)
        return json.loads(reply['result']['content'][0]['text'])

    def test_lists_filter_by_project_node_and_unassigned(self):
        self.assertEqual([item['id'] for item in self.call('list_tasks', projectId='p', nodeId='N1')], ['T2'])
        self.assertEqual([item['id'] for item in self.call('list_tasks', nodeId='')], ['T1'])
        self.assertEqual([item['id'] for item in self.call('list_tasks', projectId='p', status='queued')], ['T1'])
        self.assertEqual([item['id'] for item in self.call('list_nodes', projectId='p')], ['N1'])
        self.assertEqual([item['id'] for item in self.call('list_trajectory', projectId='p')], ['E1'])
        self.assertEqual(len(self.call('list_nodes')), 2)

    def test_deleted_projects_are_opt_in_and_actions_share_api(self):
        self.assertEqual([item['id'] for item in self.call('list_projects')], ['p'])
        self.assertEqual([item['id'] for item in self.call('list_projects', includeDeleted=True)], ['p', 'deleted'])
        for action in ('delete', 'restore'):
            self.call('project_action', projectId='project/name', action=action)
            self.assertEqual(self.client.calls[-1], ('/api/projects/project%2Fname/actions', {'action': action}))

    def test_node_creation_and_conclusion_update_preserve_fields(self):
        self.call('create_node', projectId='p', title='合并方向', hypothesis='共同假设',
                  color='violet', parentIds=['N1', 'N2'])
        path, data = self.client.calls[-1]
        self.assertEqual(path, '/api/nodes')
        self.assertEqual(data['parentIds'], ['N1', 'N2'])
        self.assertEqual(data['hypothesis'], '共同假设')
        self.call('update_node', nodeId='N1', outcome='adopted', conclusion='通过比较决定采用')
        self.assertEqual(self.client.calls[-1], ('/api/nodes/N1', {'outcome': 'adopted', 'conclusion': '通过比较决定采用'}))
        self.call('update_node', nodeId='N1', parentIds=[])
        self.assertEqual(self.client.calls[-1][1], {'parentIds': []})

    def test_create_and_reassign_tasks_normalize_unassigned(self):
        self.call('create_task', projectId='p', title='节点任务', nodeId='N1', phase='execute')
        self.assertEqual(self.client.calls[-1][1]['nodeId'], 'N1')
        self.assertEqual(self.client.calls[-1][1]['kind'], 'result')
        self.call('create_task', projectId='p', title='未归类任务', nodeId='')
        self.assertIsNone(self.client.calls[-1][1]['nodeId'])
        self.call('assign_task_node', taskId='T/name', nodeId='N1')
        self.assertEqual(self.client.calls[-1], ('/api/tasks/T%2Fname/node', {'nodeId': 'N1'}))
        self.call('assign_task_node', taskId='T1', nodeId='')
        self.assertEqual(self.client.calls[-1], ('/api/tasks/T1/node', {'nodeId': None}))

    def test_explicit_stop_confirmation_is_forwarded(self):
        self.call('task_action', taskId='T1', action='confirm_stopped', confirmStopped=True)
        self.assertEqual(self.client.calls[-1], ('/api/tasks/T1/actions', {'action': 'confirm_stopped', 'confirmStopped': True}))

    def test_invalid_new_tool_parameters_do_not_reach_api(self):
        cases = [
            ('create_node', {'projectId': 'p', 'title': 'A', 'color': 'green'}),
            ('update_node', {'nodeId': 'N1', 'outcome': 'success'}),
            ('create_node', {'projectId': 'p', 'title': 'A', 'parentIds': [1]}),
            ('project_action', {'projectId': 'p', 'action': 'erase'}),
            ('assign_task_node', {'taskId': 'T1', 'nodeId': None}),
            ('list_projects', {'includeDeleted': 'true'}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                reply = self.rpc('tools/call', {'name': name, 'arguments': arguments})
                self.assertEqual(reply['error']['code'], -32602)
        self.assertFalse(self.client.calls)
        empty_update = self.rpc('tools/call', {'name': 'update_node', 'arguments': {'nodeId': 'N1'}})
        self.assertTrue(empty_update['result']['isError'])
        self.assertFalse(self.client.calls)


class ExplorationCLITests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()

    def invoke(self, *arguments, expected=0):
        output, errors = io.StringIO(), io.StringIO()
        with patch('taskboard.cli.Client', return_value=self.client), redirect_stdout(output), redirect_stderr(errors):
            code = main(list(arguments))
        self.assertEqual(code, expected, errors.getvalue())
        return json.loads(output.getvalue()) if code == 0 else errors.getvalue()

    def test_node_and_trajectory_lists(self):
        self.assertEqual([item['id'] for item in self.invoke('nodes', '--project', 'p')], ['N1'])
        self.assertEqual([item['id'] for item in self.invoke('trajectory', '--project', 'p')], ['E1'])
        self.assertEqual([item['id'] for item in self.invoke('projects')], ['p'])
        self.assertEqual([item['id'] for item in self.invoke('projects', '--include-deleted')], ['p', 'deleted'])

    def test_task_node_filter_and_creation_are_backward_compatible(self):
        self.assertEqual([item['id'] for item in self.invoke('tasks', '--project', 'p', '--node', 'N1')], ['T2'])
        self.assertEqual([item['id'] for item in self.invoke('tasks', '--unassigned')], ['T1'])
        self.assertEqual([item['id'] for item in self.invoke('tasks', '--node', '')], ['T1'])
        self.invoke('create', '--title', 'Existing usage')
        old_body = self.client.calls[-1][1]
        self.assertNotIn('nodeId', old_body)
        self.assertEqual(old_body['projectId'], 'task-manager')
        self.assertEqual(old_body['phase'], 'plan')
        self.invoke('create', '--title', 'With node', '--project', 'p', '--node-id', 'N1', '--execute')
        self.assertEqual(self.client.calls[-1][1]['nodeId'], 'N1')
        self.assertEqual(self.client.calls[-1][1]['phase'], 'execute')

    def test_node_create_multi_parent_and_update_only_explicit_fields(self):
        self.invoke('node-create', '--project', 'p', '--title', '探索方向', '--hypothesis', '假设',
                    '--color', 'teal', '--parent', 'N1', '--parent', 'N2')
        self.assertEqual(self.client.calls[-1], ('/api/nodes', {'projectId': 'p', 'title': '探索方向',
                         'hypothesis': '假设', 'color': 'teal', 'parentIds': ['N1', 'N2']}))
        self.invoke('node-update', 'N1', '--outcome', 'discarded', '--conclusion', '实验结果不支持')
        self.assertEqual(self.client.calls[-1][1], {'outcome': 'discarded', 'conclusion': '实验结果不支持'})
        self.invoke('node-update', 'N1', '--clear-parents')
        self.assertEqual(self.client.calls[-1], ('/api/nodes/N1', {'parentIds': []}))
        self.invoke('node-update', 'N1', '--hypothesis', '')
        self.assertEqual(self.client.calls[-1][1], {'hypothesis': ''})

    def test_assign_clear_and_project_actions(self):
        self.invoke('assign-node', 'T1', 'N1')
        self.assertEqual(self.client.calls[-1], ('/api/tasks/T1/node', {'nodeId': 'N1'}))
        for args in (('assign-node', 'T1', '--clear'), ('assign-node', 'T1', '')):
            self.invoke(*args)
            self.assertEqual(self.client.calls[-1], ('/api/tasks/T1/node', {'nodeId': None}))
        self.invoke('project-action', 'p', 'delete')
        self.assertEqual(self.client.calls[-1], ('/api/projects/p/actions', {'action': 'delete'}))
        self.invoke('project-action', 'p', 'restore')
        self.assertEqual(self.client.calls[-1], ('/api/projects/p/actions', {'action': 'restore'}))

    def test_empty_or_conflicting_mutation_does_not_call_api(self):
        self.invoke('node-update', 'N1', expected=1)
        self.invoke('assign-node', 'T1', expected=1)
        self.invoke('assign-node', 'T1', 'N1', '--clear', expected=1)
        self.assertFalse(self.client.calls)

    def test_bootstrap_respects_deleted_projects(self):
        store = Mock()
        store.list_projects.return_value = [{'id': 'deleted', 'deletedAt': '2026-10-09'}]
        self.assertIsNone(ensure_initial_project(store))
        store.list_projects.assert_called_once_with(include_deleted=True)
        store.create_project.assert_not_called()

    def test_explicit_stop_confirmation_flag_is_forwarded(self):
        self.invoke('action', 'T1', 'confirm_stopped', '--confirm-stopped')
        self.assertEqual(self.client.calls[-1][1]['confirmStopped'], True)
        self.invoke('action', 'T1', 'confirm_stopped')
        self.assertNotIn('confirmStopped', self.client.calls[-1][1])


if __name__ == '__main__':
    unittest.main()
