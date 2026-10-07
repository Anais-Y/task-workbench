"""Minimal MCP stdio bridge, using the same local API as the browser and CLI.

Protocol reference: https://modelcontextprotocol.io/specification/2025-03-26
No background task execution is hidden inside tool calls: create and start are explicit.
"""
from __future__ import annotations
import json
import sys
from urllib.parse import quote


def schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


S = {'type': 'string'}
TOOLS = [
    {'name': 'list_projects', 'description': '查看本机任务工作台的所有项目。', 'inputSchema': schema({})},
    {'name': 'list_tasks', 'description': '查看项目任务、状态、依赖和需要用户处理的事项。',
     'inputSchema': schema({'projectId': S, 'status': S})},
    {'name': 'get_task', 'description': '查看任务详情、日志、执行轮次和产物。', 'inputSchema': schema({'taskId': S}, ['taskId'])},
    {'name': 'create_task', 'description': '在现有项目创建任务；默认先规划，不自动开始。start=true 时立即排队。',
     'inputSchema': schema({'projectId': S, 'title': S, 'goal': S, 'engine': S,
                           'criteria': {'type': 'array', 'items': S},
                           'phase': {'type': 'string', 'enum': ['plan', 'execute']},
                           'start': {'type': 'boolean'}}, ['projectId', 'title'])},
    {'name': 'task_action', 'description': '启动、批准计划、验收结果、反馈、重试或取消任务。批准计划或验收结果应遵循用户授权。',
     'inputSchema': schema({'taskId': S, 'action': {'type': 'string', 'enum': ['start', 'approve_plan', 'accept', 'feedback', 'retry', 'cancel']},
                           'message': S}, ['taskId', 'action'])},
    {'name': 'read_artifact', 'description': '读取一个任务的产物内容。index 从 0 开始。',
     'inputSchema': schema({'taskId': S, 'index': {'type': 'integer', 'minimum': 0}}, ['taskId', 'index'])},
]


def validate(arguments, spec):
    if not isinstance(arguments, dict):
        raise ValueError('arguments 必须是对象')
    for name in spec.get('required', []):
        if name not in arguments:
            raise ValueError('缺少参数：' + name)
    for name, value in arguments.items():
        prop = spec['properties'].get(name)
        if not prop:
            raise ValueError('不支持的参数：' + name)
        typ = prop['type']
        ok = ((typ == 'string' and isinstance(value, str)) or
              (typ == 'boolean' and type(value) is bool) or
              (typ == 'integer' and type(value) is int) or
              (typ == 'array' and isinstance(value, list) and all(isinstance(x, str) for x in value)))
        if not ok or ('enum' in prop and value not in prop['enum']) or (typ == 'integer' and value < prop.get('minimum', value)):
            raise ValueError('参数格式不正确：' + name)


class Bridge:
    def __init__(self, client):
        self.client = client
        self.initialized = False

    def call(self, name, args):
        if name in ('list_projects', 'list_tasks', 'get_task'):
            state = self.client.request('/api/state')
            if name == 'list_projects':
                return state['projects']
            if name == 'get_task':
                task = next((t for t in state['tasks'] if t['id'] == args['taskId']), None)
                if task is None:
                    raise ValueError('任务不存在')
                return task
            return [{k: t.get(k) for k in ('id', 'title', 'projectId', 'status', 'kind', 'phase', 'engine', 'worker',
                                           'deps', 'executionMode', 'scheduled', 'error', 'updatedAt')}
                    for t in state['tasks'] if (not args.get('projectId') or t['projectId'] == args['projectId'])
                    and (not args.get('status') or t['status'] == args['status'])]
        if name == 'create_task':
            data = dict(args)
            data.setdefault('engine', 'codex')
            data.setdefault('phase', 'plan')
            data['kind'] = 'plan' if data['phase'] == 'plan' else 'result'
            return self.client.request('/api/tasks', data)
        task_path = '/api/tasks/' + quote(args['taskId'], safe='')
        if name == 'task_action':
            return self.client.request(task_path + '/actions', {k: v for k, v in args.items() if k != 'taskId'})
        return self.client.request(task_path + '/artifacts/' + str(args['index']))

    def dispatch(self, message):
        if isinstance(message, list):
            if not message:
                return self.error(None, -32600, 'Empty batch')
            replies = [self.dispatch(item) for item in message]
            return [r for r in replies if r is not None] or None
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
            return self.error(message.get('id') if isinstance(message, dict) else None, -32600, 'Invalid request')
        if 'id' not in message:
            return None
        request_id = message['id']
        method, params = message['method'], message.get('params', {})
        if not isinstance(params, dict):
            return self.error(request_id, -32602, 'Invalid params')
        if method == 'initialize':
            self.initialized = True
            version = params.get('protocolVersion')
            result = {'protocolVersion': version if version in ('2024-11-05', '2025-03-26') else '2025-03-26',
                      'capabilities': {'tools': {}}, 'serverInfo': {'name': 'task-workbench', 'version': '0.1.0'},
                      'instructions': '连接本机任务工作台。任务结果需人工验收，计划需批准后才会执行。'}
        elif method == 'ping':
            result = {}
        elif not self.initialized:
            return self.error(request_id, -32002, 'Initialize first')
        elif method == 'tools/list':
            result = {'tools': TOOLS}
        elif method == 'tools/call':
            name = params.get('name')
            tool = next((t for t in TOOLS if t['name'] == name), None)
            if not tool:
                return self.error(request_id, -32602, 'Unknown tool')
            args = params.get('arguments', {})
            try:
                validate(args, tool['inputSchema'])
            except ValueError as exc:
                return self.error(request_id, -32602, str(exc))
            try:
                output = self.call(name, args)
                result = {'content': [{'type': 'text', 'text': json.dumps(output, ensure_ascii=False)}], 'isError': False}
            except Exception as exc:
                result = {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
        else:
            return self.error(request_id, -32601, 'Method not found')
        return {'jsonrpc': '2.0', 'id': request_id, 'result': result}

    @staticmethod
    def error(request_id, code, message):
        return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


def serve_stdio(client, input_stream=None, output_stream=None):
    bridge = Bridge(client)
    source, sink = input_stream or sys.stdin, output_stream or sys.stdout
    for line in source:
        try:
            message = json.loads(line)
        except ValueError:
            reply = bridge.error(None, -32700, 'Parse error')
        else:
            reply = bridge.dispatch(message)
        if reply is not None:
            sink.write(json.dumps(reply, ensure_ascii=False, separators=(',', ':')) + '\n')
            sink.flush()
