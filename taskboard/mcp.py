"""Minimal MCP stdio bridge, using the same local API as the browser and CLI.

Protocol reference: https://modelcontextprotocol.io/specification/2025-03-26
No background task execution is hidden inside tool calls: create and start are explicit.
"""
from __future__ import annotations
import json
import sys
from urllib.parse import quote
from . import __version__


def schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


S = {'type': 'string'}
NODE_FIELDS = {
    'title': S, 'hypothesis': S,
    'color': {'type': 'string', 'enum': ['blue', 'violet', 'teal', 'amber', 'rose', 'slate']},
    'parentIds': {'type': 'array', 'items': S},
    'outcome': {'type': 'string', 'enum': ['exploring', 'adopted', 'discarded', 'inconclusive']},
    'conclusion': S,
}
TOOLS = [
    {'name': 'list_projects', 'description': '查看项目；includeDeleted=true 时同时列出回收站中的项目。',
     'inputSchema': schema({'includeDeleted': {'type': 'boolean'}})},
    {'name': 'project_action', 'description': 'delete 将项目移入可恢复的回收站，restore 恢复项目；不会删除工作目录。应遵循用户授权。',
     'inputSchema': schema({'projectId': S, 'action': {'type': 'string', 'enum': ['delete', 'restore']}}, ['projectId', 'action'])},
    {'name': 'list_nodes', 'description': '列出探索节点及假设、父节点、结论；节点代表研究方向，不是任务执行状态。',
     'inputSchema': schema({'projectId': S})},
    {'name': 'create_node', 'description': '创建探索节点；parentIds 可指定多个父节点。节点脉络不会自动启动 Agent 或排队任务。',
     'inputSchema': schema({'projectId': S, **NODE_FIELDS}, ['projectId', 'title'])},
    {'name': 'update_node', 'description': '更新探索节点。parentIds 替换完整父节点列表，空数组可清除；非 exploring 状态需要非空 conclusion。',
     'inputSchema': schema({'nodeId': S, **NODE_FIELDS}, ['nodeId'])},
    {'name': 'list_trajectory', 'description': '按时间查看持久保存的探索轨迹、任务里程碑和结论修订。可按项目筛选。',
     'inputSchema': schema({'projectId': S})},
    {'name': 'list_tasks', 'description': '查看项目任务、状态、依赖和需要用户处理的事项。',
     'inputSchema': schema({'projectId': S, 'status': S,
                            'nodeId': {'type': 'string', 'description': '按节点筛选；空字符串只返回未归类任务'}})},
    {'name': 'get_task', 'description': '查看任务详情、日志、执行轮次和产物。', 'inputSchema': schema({'taskId': S}, ['taskId'])},
    {'name': 'create_task', 'description': '在现有项目创建任务；默认先规划，不自动开始。start=true 时立即排队。',
     'inputSchema': schema({'projectId': S, 'title': S, 'goal': S, 'engine': S, 'nodeId': S,
                           'criteria': {'type': 'array', 'items': S},
                           'phase': {'type': 'string', 'enum': ['plan', 'execute']},
                           'start': {'type': 'boolean'}}, ['projectId', 'title'])},
    {'name': 'assign_task_node', 'description': '将任务及其整个父子任务家族移动到节点；nodeId 空字符串表示移回未归类。正在运行或排队的任务不能移动。',
     'inputSchema': schema({'taskId': S, 'nodeId': S}, ['taskId', 'nodeId'])},
    {'name': 'task_action', 'description': '启动、批准计划、验收结果、反馈、重试或取消任务。批准计划或验收结果应遵循用户授权。confirm_stopped 仅用于用户已明确核实 Agent 退出且文件写入停止，需 confirmStopped=true；不能跳过仍存活的进程。',
     'inputSchema': schema({'taskId': S, 'action': {'type': 'string', 'enum': ['start', 'approve_plan', 'accept', 'feedback', 'retry', 'cancel', 'confirm_stopped']},
                           'message': S, 'confirmStopped': {'type': 'boolean'}}, ['taskId', 'action'])},
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
        if name in ('list_projects', 'list_tasks', 'get_task', 'list_nodes', 'list_trajectory'):
            state = self.client.request('/api/state')
            if name == 'list_projects':
                return state['projects'] + (state.get('deletedProjects', []) if args.get('includeDeleted') else [])
            if name in ('list_nodes', 'list_trajectory'):
                items = state.get('nodes' if name == 'list_nodes' else 'trajectory', [])
                return [item for item in items if not args.get('projectId') or item['projectId'] == args['projectId']]
            if name == 'get_task':
                task = next((t for t in state['tasks'] if t['id'] == args['taskId']), None)
                if task is None:
                    raise ValueError('任务不存在')
                return task
            return [{k: t.get(k) for k in ('id', 'title', 'projectId', 'nodeId', 'status', 'kind', 'phase', 'engine', 'worker',
                                           'deps', 'executionMode', 'scheduled', 'error', 'updatedAt')}
                    for t in state['tasks'] if (not args.get('projectId') or t['projectId'] == args['projectId'])
                    and (not args.get('status') or t['status'] == args['status'])
                    and ('nodeId' not in args or t.get('nodeId') == (args['nodeId'] or None))]
        if name == 'project_action':
            return self.client.request('/api/projects/' + quote(args['projectId'], safe='') + '/actions',
                                       {'action': args['action']})
        if name == 'create_node':
            return self.client.request('/api/nodes', dict(args))
        if name == 'update_node':
            patch = {key: value for key, value in args.items() if key != 'nodeId'}
            if not patch:
                raise ValueError('请至少提供一项要更新的节点内容。')
            return self.client.request('/api/nodes/' + quote(args['nodeId'], safe=''), patch)
        if name == 'create_task':
            data = dict(args)
            data.setdefault('engine', 'codex')
            data.setdefault('phase', 'plan')
            data['kind'] = 'plan' if data['phase'] == 'plan' else 'result'
            if 'nodeId' in data:
                data['nodeId'] = data['nodeId'] or None
            return self.client.request('/api/tasks', data)
        task_path = '/api/tasks/' + quote(args['taskId'], safe='')
        if name == 'assign_task_node':
            return self.client.request(task_path + '/node', {'nodeId': args['nodeId'] or None})
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
                      'capabilities': {'tools': {}}, 'serverInfo': {'name': 'task-workbench', 'version': __version__},
                      'instructions': '连接本机任务工作台。项目包含探索节点与任务，节点间脉络不代表任务依赖。任务结果需人工验收，计划需批准后 Agent 才会执行。'}
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
