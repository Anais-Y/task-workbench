"""CLI and a small shared HTTP client. No provider coupling."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

DEFAULT_URL = 'http://127.0.0.1:8766'


class Client:
    def __init__(self, url=DEFAULT_URL):
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
                or parsed.username or parsed.password or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or not 1 <= (parsed.port if parsed.port is not None else 80) <= 65535):
            raise ValueError('工作台地址必须是本机 HTTP 地址。')
        self.url = url.rstrip('/')

    def request(self, path, body=None):
        headers = {'X-Taskboard-Client': 'taskboard', 'Content-Type': 'application/json'}
        data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
        try:
            with urlopen(Request(self.url + path, data=data, headers=headers), timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                message = json.load(exc).get('error', str(exc))
            except (ValueError, AttributeError):
                message = str(exc)
            raise ValueError(message) from exc
        except URLError as exc:
            raise ValueError('无法连接本机工作台。请先运行 task-workbench serve。') from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description='本机任务工作台')
    parser.add_argument('--url', default=DEFAULT_URL, help='已启动的工作台地址')
    sub = parser.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve', help='启动网页与任务服务')
    serve.add_argument('--port', type=int, default=8766)
    serve.add_argument('--db', default=str(Path(__file__).resolve().parent.parent / '.data' / 'tasks.sqlite3'))
    serve.add_argument('--adapters', help='自定义 Agent JSON 配置文件')
    serve.add_argument('--max-workers', type=int, default=3)
    serve.add_argument('--seed-build', action='store_true', help='首次导入当前开发项目的实际 To-Do')
    start = sub.add_parser('start', help='在后台启动本机工作台，关闭终端后仍可使用')
    start.add_argument('--port', type=int, default=8766)
    start.add_argument('--db', default=str(Path(__file__).resolve().parent.parent / '.data' / 'tasks.sqlite3'))
    start.add_argument('--adapters')
    start.add_argument('--max-workers', type=int, default=3)
    stop = sub.add_parser('stop', help='停止由 start 启动的本机服务')
    stop.add_argument('--port', type=int, default=8766)
    doctor = sub.add_parser('doctor', help='检查本机环境、服务与 Agent 发现结果，不运行 Agent')
    doctor.add_argument('--json', action='store_true', help='输出可供程序读取的诊断结果')
    doctor.add_argument('--port', type=int, help='检查指定本机端口，默认使用 --url 的地址')
    doctor.add_argument('--adapters', help='检查指定自定义 Agent JSON 配置')
    sub.add_parser('state', help='查看所有任务和 Agent')
    tasks = sub.add_parser('tasks', help='列出任务')
    tasks.add_argument('--project')
    task_nodes = tasks.add_mutually_exclusive_group()
    task_nodes.add_argument('--node', '--node-id', dest='node', help='按探索节点筛选；空字符串表示未归类')
    task_nodes.add_argument('--unassigned', action='store_true', help='只显示未归类任务')
    projects = sub.add_parser('projects', help='列出项目')
    projects.add_argument('--include-deleted', action='store_true', help='同时列出回收站中的项目')
    nodes = sub.add_parser('nodes', help='列出探索节点')
    nodes.add_argument('--project', help='只查看指定项目')
    node_create = sub.add_parser('node-create', help='创建探索节点；父节点表示研究脉络，不会自动排队任务')
    node_create.add_argument('--project', required=True)
    node_create.add_argument('--title', required=True)
    node_create.add_argument('--hypothesis', default='')
    node_create.add_argument('--color', choices=['blue', 'violet', 'teal', 'amber', 'rose', 'slate'], default='blue')
    node_create.add_argument('--parent', action='append', default=[], help='父节点 ID；可重复指定以合并研究方向')
    node_update = sub.add_parser('node-update', help='更新探索节点；非 exploring 状态要求保存非空结论')
    node_update.add_argument('node_id')
    node_update.add_argument('--title')
    node_update.add_argument('--hypothesis')
    node_update.add_argument('--color', choices=['blue', 'violet', 'teal', 'amber', 'rose', 'slate'])
    node_parents = node_update.add_mutually_exclusive_group()
    node_parents.add_argument('--parent', action='append', help='替换完整父节点列表；可重复指定')
    node_parents.add_argument('--clear-parents', action='store_true', help='清除父节点列表')
    node_update.add_argument('--outcome', choices=['exploring', 'adopted', 'discarded', 'inconclusive'])
    node_update.add_argument('--conclusion', help='结论内容；非 exploring 状态要求非空结论')
    trajectory = sub.add_parser('trajectory', help='查看持久保存的探索轨迹和里程碑')
    trajectory.add_argument('--project', help='只查看指定项目')
    assign_node = sub.add_parser('assign-node', help='将任务及其整个子任务家族移动到探索节点')
    assign_node.add_argument('task_id')
    assign_node.add_argument('node_id', nargs='?', help='目标节点 ID；空字符串表示移回未归类')
    assign_node.add_argument('--clear', action='store_true', help='移回未归类')
    project_action = sub.add_parser('project-action', help='将项目移入回收站或恢复；不会删除工作目录')
    project_action.add_argument('project_id')
    project_action.add_argument('action', choices=['delete', 'restore'])
    create = sub.add_parser('create', help='创建一个任务')
    create.add_argument('--title', required=True)
    create.add_argument('--goal', default='')
    create.add_argument('--project', default='task-manager')
    create.add_argument('--engine', default='codex')
    create.add_argument('--node', '--node-id', dest='node', help='任务所属的探索节点 ID；省略时未归类')
    create.add_argument('--execute', action='store_true', help='直接执行；默认先制定计划')
    create.add_argument('--start', action='store_true')
    create.add_argument('--criterion', action='append', default=[])
    action = sub.add_parser('action', help='启动、审核、反馈、重试或取消任务')
    action.add_argument('task_id')
    action.add_argument('action', choices=['start', 'approve_plan', 'accept', 'feedback', 'retry', 'cancel', 'confirm_stopped'])
    action.add_argument('--message', default='')
    action.add_argument('--confirm-stopped', action='store_true', help='仅在你已核实 Agent 退出且文件写入停止后，明确确认无法自动检测的退出状态')
    sub.add_parser('mcp', help='启动 MCP stdio 桥接（先启动工作台服务）')
    args = parser.parse_args(argv)
    try:
        if args.command == 'doctor':
            from .diagnostics import diagnose, format_report
            url = 'http://127.0.0.1:%d' % args.port if args.port is not None else args.url
            # Apply the same loopback URL validation as all other clients.
            report = diagnose(Client(url).url, args.adapters)
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_report(report))
            return 0 if report['ok'] else 1
        if args.command in ('start', 'stop'):
            from .service import control
            return control(args)
        if args.command == 'serve':
            return serve_app(args)
        if args.command == 'mcp':
            from .mcp import serve_stdio
            serve_stdio(Client(args.url))
            return 0
        client = Client(args.url)
        if args.command in ('state', 'tasks', 'projects', 'nodes', 'trajectory'):
            state = client.request('/api/state')
            result = state if args.command == 'state' else state.get(args.command, [])
            if args.command in ('tasks', 'nodes', 'trajectory') and args.project:
                result = [item for item in result if item['projectId'] == args.project]
            if args.command == 'tasks' and (args.node is not None or args.unassigned):
                node_id = None if args.unassigned else args.node or None
                result = [task for task in result if task.get('nodeId') == node_id]
            if args.command == 'projects' and args.include_deleted:
                result = result + state.get('deletedProjects', [])
        elif args.command == 'create':
            data = {'title': args.title, 'goal': args.goal or args.title,
                'projectId': args.project, 'engine': args.engine, 'phase': 'execute' if args.execute else 'plan',
                'kind': 'result' if args.execute else 'plan', 'criteria': args.criterion, 'start': args.start}
            if args.node is not None:
                data['nodeId'] = args.node or None
            result = client.request('/api/tasks', data)
        elif args.command == 'node-create':
            result = client.request('/api/nodes', {'projectId': args.project, 'title': args.title,
                'hypothesis': args.hypothesis, 'color': args.color, 'parentIds': args.parent})
        elif args.command == 'node-update':
            patch = {key: getattr(args, key) for key in ('title', 'hypothesis', 'color', 'outcome', 'conclusion')
                     if getattr(args, key) is not None}
            if args.parent is not None or args.clear_parents:
                patch['parentIds'] = [] if args.clear_parents else args.parent
            if not patch:
                raise ValueError('请至少提供一项要更新的节点内容。')
            result = client.request('/api/nodes/' + quote(args.node_id, safe=''), patch)
        elif args.command == 'assign-node':
            if args.clear and args.node_id is not None:
                raise ValueError('目标节点和 --clear 只能选择一个。')
            if args.node_id is None and not args.clear:
                raise ValueError('请提供目标节点，或使用 --clear 移回未归类。')
            result = client.request('/api/tasks/' + quote(args.task_id, safe='') + '/node',
                                    {'nodeId': None if args.clear else args.node_id or None})
        elif args.command == 'project-action':
            result = client.request('/api/projects/' + quote(args.project_id, safe='') + '/actions',
                                    {'action': args.action})
        else:
            payload = {'action': args.action, 'message': args.message}
            if args.confirm_stopped:
                payload['confirmStopped'] = True
            result = client.request('/api/tasks/' + quote(args.task_id, safe='') + '/actions',
                                    payload)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def ensure_initial_project(store, workspace=None):
    """Create the real empty workspace once; never import development history."""
    if store.list_projects(include_deleted=True):
        return None
    root = Path(workspace) if workspace is not None else Path(__file__).resolve().parent.parent
    return store.create_project({'id': 'task-manager', 'name': '建立一个任务管理器',
                                 'path': str(root.expanduser().resolve())})


def serve_app(args):
    from .adapters import AdapterRegistry
    from .store import Store
    from .runner import Runner
    from .server import make_server
    from .seed import seed_build
    import signal
    import threading
    db = Path(args.db).expanduser().resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    registry = AdapterRegistry(args.adapters)
    store = Store(str(db))
    if getattr(args, 'seed_build', False):
        seed_build(store)
    else:
        ensure_initial_project(store)
    runner = Runner(store, registry, args.max_workers)
    server = make_server(store, registry, runner, port=args.port,
                         web_root=str(Path(__file__).resolve().parent.parent / 'web'))
    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    runner.start()
    print('任务工作台已启动：http://127.0.0.1:' + str(server.server_address[1]), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        runner.stop()
        server.server_close()
        store.close()
    return 0
