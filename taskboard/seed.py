"""The real development work for this project, never demo executions."""
from pathlib import Path

PROJECT_ID = 'task-manager'

BUILD_TASKS = [
    ('T001', '保存项目、任务与依赖关系', '协作 worker · 后端', ['实现持久化存储', '提供任务接口', '验证依赖与文件预览边界'], 'running'),
    ('T002', '把工作台接上真实任务数据', '协作 worker · 界面', ['保留已确认的界面', '接入任务与操作接口', '保留轮询时的输入草稿'], 'running'),
    ('T003', '接入可插拔的 Agent 执行器', '协作 worker · 适配器', ['接入 Codex CLI 和 Claude Code', '统一日志、会话与取消操作', '支持自定义执行器'], 'running'),
    ('T004', '实现排队、并行与依赖调度', '统筹', ['限制同时运行的任务数', '等待前置任务验收', '恢复服务中断的任务'], 'running'),
    ('T005', '完成计划审核、反馈与重试流程', None, ['生成计划后等待审核', '批准计划并创建子任务', '反馈、重试和验收结果'], 'queued'),
    ('T006', '汇总执行日志和产物', None, ['记录真实执行事件', '显示结果和文件产物', '验证产物预览'], 'queued'),
    ('T007', '提供 CLI 和 MCP 入口', None, ['通过命令行操作任务', '通过 MCP 提供相同操作', '整理接入配置'], 'queued'),
    ('T008', '联调、检查交互与整理使用说明', None, ['验证完整任务生命周期', '检查桌面与窄屏交互', '提供启动和使用说明'], 'queued'),
]


def seed_build(store):
    """Idempotent initial import. Later updates are made by the actual coordinator."""
    if not store.get_project(PROJECT_ID):
        store.create_project({'id': PROJECT_ID, 'name': '建立一个任务管理器',
                              'path': str(Path(__file__).resolve().parent.parent)})
    for task_id, title, worker, steps, status in BUILD_TASKS:
        if store.get_task(task_id):
            continue
        store.create_task({'id': task_id, 'title': title, 'projectId': PROJECT_ID, 'engine': 'codex',
            'goal': title, 'desc': title, 'criteria': steps, 'steps': steps, 'worker': worker,
            'status': status, 'phase': 'execute', 'kind': 'result', 'executionMode': 'external',
            'scheduled': False})
        store.add_event(task_id, '已分配给当前 Codex 对话中的' + worker + '。' if worker else '已列入本轮开发 To-Do。')
