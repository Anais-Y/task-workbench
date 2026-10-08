"""Read-only environment diagnostics; executable discovery is not login proof."""
from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from .adapters import AdapterRegistry


def _configuration(config_path, root):
    if config_path:
        return {'source': 'argument', 'path': str(Path(config_path).expanduser().resolve())}
    if os.environ.get('TASKBOARD_ADAPTERS'):
        return {'source': 'environment', 'path': str(Path(os.environ['TASKBOARD_ADAPTERS']).expanduser().resolve())}
    default = root / 'adapters.json'
    return {'source': 'default' if default.exists() else 'none',
            'path': str(default) if default.exists() else None}


def _service(url):
    result = {'url': url, 'status': 'unreachable', 'message': '服务未启动或暂时无法连接；可先运行 start。'}
    try:
        with urlopen(url + '/api/health', timeout=2) as response:
            value = json.load(response)
        if isinstance(value, dict) and value.get('ok') is True and value.get('service') == 'task-workbench':
            result.update(status='running', message='本机任务工作台正在响应。', pid=value.get('pid'))
        else:
            result.update(status='unexpected', message='该地址有响应，但未识别为任务工作台。')
    except (OSError, URLError) as exc:
        result['detail'] = str(exc)
    except (ValueError, UnicodeError):
        result.update(status='unexpected', message='该地址返回了无法识别的内容。')
    return result


def diagnose(url='http://127.0.0.1:8766', config_path=None):
    root = Path(__file__).resolve().parent.parent
    config = _configuration(config_path, root)
    supported_python = sys.version_info >= (3, 9)
    supported_system = sys.platform in ('darwin', 'linux')
    report = {
        'ok': supported_python and supported_system,
        'python': {'version': platform.python_version(), 'executable': sys.executable,
                   'minimum': '3.9', 'supported': supported_python},
        'system': {'name': platform.system(), 'release': platform.release(),
                   'machine': platform.machine(), 'supported': supported_system,
                   'support': 'macOS 和 Linux；尚未验证 Windows 原生运行。'},
        'workspace': str(root), 'service': _service(url),
        'configuration': config, 'adapters': [],
        'authentication': 'not_checked',
        'authenticationNote': '这里只检查 CLI 文件是否存在且可执行；未检查登录、账户额度或远端可用性。请在对应 CLI 中完成登录。',
        'warnings': [],
    }
    try:
        registry = AdapterRegistry(config_path)
        report['adapters'] = [dict(item, source='builtin' if item['id'] in ('codex', 'claude') else 'custom',
                                    authentication='not_checked') for item in registry.list_adapters()]
        config['loaded'] = True
        config['customCount'] = sum(item['source'] == 'custom' for item in report['adapters'])
    except (OSError, ValueError, TypeError) as exc:
        config.update(loaded=False, error=str(exc), customCount=0)
        report['ok'] = False
        report['warnings'].append('自定义 Agent 配置无法载入；请修正配置后启动服务。')
    if config.get('loaded') and not any(item.get('available') and item.get('enabled') for item in report['adapters']):
        report['warnings'].append('未发现已启用的 CLI。工作台仍可打开；执行任务前需安装或配置一个 Agent。')
    if report['service']['status'] == 'unexpected':
        report['warnings'].append('检查端口是否被其他应用占用，或选择另一个端口。')
    if not supported_python:
        report['warnings'].append('需要 Python 3.9 或更新版本。')
    if not supported_system:
        report['warnings'].append('当前系统不在已支持范围内；请使用 macOS 或 Linux。')
    return report


def format_report(report):
    python = report['python']
    system = report['system']
    service = report['service']
    config = report['configuration']
    lines = ['任务工作台 · 本机检查',
             f"Python：{python['version']}（要求 {python['minimum']}+，{'符合' if python['supported'] else '不符合'}）",
             f"Python 路径：{python['executable']}",
             f"系统：{system['name']} {system['release']} / {system['machine']}",
             f"支持范围：{system['support']}", f"仓库位置：{report['workspace']}",
             f"本机服务：{service['url']} · {service['message']}",
             'Agent 配置：' + (config['path'] or '未配置，使用内置发现')]
    if config.get('error'):
        lines.append('配置错误：' + config['error'])
    for item in report['adapters']:
        status = '已找到 CLI' if item.get('available') else '未找到 CLI'
        enabled = '已启用' if item.get('enabled') else '未启用'
        kind = '内置' if item['source'] == 'builtin' else '自定义'
        lines.append(f"  {item['name']}（{kind}）：{status}，{enabled}" +
                     (f" · {item['executable']}" if item.get('executable') else ''))
    lines.append('登录状态：' + report['authenticationNote'])
    for warning in report['warnings']:
        lines.append('提示：' + warning)
    return '\n'.join(lines)
