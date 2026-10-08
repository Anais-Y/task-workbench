"""Loopback-only HTTP API and static frontend for Task Workbench."""

from __future__ import annotations

import ipaddress
import json
import logging
import mimetypes
import os
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import unquote, urlsplit


MAX_BODY_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 1024 * 1024
LOG = logging.getLogger(__name__)


class HTTPError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_server(store, registry, runner, host="127.0.0.1", port=8766, web_root=None):
    """Return an unstarted server; the caller owns serve_forever/shutdown."""
    if not _loopback(host):
        raise ValueError("任务工作台只能监听本机地址")
    # This is a loopback service. Binding the numeric address avoids a forward
    # lookup when callers use localhost, including machines with slow resolvers.
    if host == "localhost":
        host = "127.0.0.1"
    root = Path(web_root or Path(__file__).resolve().parent.parent / "web").resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "TaskWorkbench/1.0"
        sys_version = ""

        def log_message(self, format, *args):
            LOG.debug("%s - %s", self.address_string(), format % args)

        def _headers(self, status, content_type, length):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.end_headers()

        def _json(self, status, value):
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _check_request(self, mutation=False):
            # No CORS: another website cannot read state or dispatch CLI commands.
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                raise HTTPError(403, "请求必须来自本机工作台")
            try:
                target = urlsplit("//" + hosts[0])
                target_port = target.port or 80
                if (not target.hostname or not _loopback(target.hostname) or target.path or target.query or target.fragment
                        or target.username is not None or target.password is not None
                        or target_port != self.server.server_address[1]):
                    raise ValueError("invalid host")
            except ValueError:
                raise HTTPError(403, "请求地址不是本机工作台") from None
            origins = self.headers.get_all("Origin", [])
            if origins:
                try:
                    origin = urlsplit(origins[0])
                    if (len(origins) != 1 or origin.scheme != "http"
                            or origin.hostname != target.hostname
                            or (origin.port or 80) != target_port
                            or origin.path not in ("", "/") or origin.query or origin.fragment
                            or origin.username is not None or origin.password is not None):
                        raise ValueError("invalid origin")
                except ValueError:
                    raise HTTPError(403, "不接受来自其他页面的操作") from None
            if mutation:
                if self.headers.get("X-Taskboard-Client") != "taskboard":
                    raise HTTPError(403, "缺少工作台客户端标记")
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    raise HTTPError(415, "请求内容必须使用 JSON")

        def _body(self):
            if self.headers.get("Transfer-Encoding"):
                raise HTTPError(400, "不支持分块请求内容")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise HTTPError(400, "缺少或重复的请求长度")
            try:
                length = int(lengths[0])
            except ValueError:
                raise HTTPError(400, "请求长度不正确") from None
            if length < 0:
                raise HTTPError(400, "请求长度不正确")
            if length > MAX_BODY_BYTES:
                raise HTTPError(413, "请求内容不能超过 1 MB")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise HTTPError(400, "请求内容不完整")
            try:
                value = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (UnicodeDecodeError, ValueError):
                raise HTTPError(400, "请求内容不是有效的 JSON") from None
            if not isinstance(value, dict):
                raise HTTPError(400, "请求内容必须是 JSON 对象")
            return value

        def _artifact(self, task_id, index):
            task = store.get_task(task_id)
            if task is None:
                raise HTTPError(404, "任务不存在")
            artifacts = task.get("artifacts", [])
            if not 0 <= index < len(artifacts):
                raise HTTPError(404, "产物不存在")
            artifact = artifacts[index]
            if "body" in artifact:
                body = artifact["body"]
                if len(body.encode("utf-8")) > MAX_ARTIFACT_BYTES:
                    raise HTTPError(413, "产物过大，请在项目文件夹中查看")
            elif artifact.get("path"):
                project = store.get_project(task["projectId"])
                workspace = Path(project["path"]).resolve()
                file_path = Path(artifact["path"])
                if not file_path.is_absolute():
                    file_path = workspace / file_path
                try:
                    file_path = file_path.resolve(strict=True)
                    file_path.relative_to(workspace)
                except FileNotFoundError:
                    raise HTTPError(404, "产物文件不存在") from None
                except (ValueError, RuntimeError):
                    raise HTTPError(403, "只能预览当前项目文件夹内的产物") from None
                if not file_path.is_file():
                    raise HTTPError(400, "该产物不是可预览的文件")
                with file_path.open("rb") as stream:
                    raw = stream.read(MAX_ARTIFACT_BYTES + 1)
                if len(raw) > MAX_ARTIFACT_BYTES:
                    raise HTTPError(413, "产物过大，请在项目文件夹中查看")
                try:
                    body = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise HTTPError(415, "此产物不是 UTF-8 文本，请在项目文件夹中打开") from None
            else:
                raise HTTPError(404, "产物尚未提供可预览的内容")
            self._json(200, {"name": artifact["name"], "type": artifact["type"], "body": body})

        def _get(self):
            path = unquote(urlsplit(self.path).path)
            if path == "/api/health":
                self._json(200, {"ok": True, "service": "task-workbench", "pid": os.getpid()})
                return
            if path == "/api/state":
                # A version brackets the reads so a poll never advertises a new
                # revision while returning state from before that revision.
                for _ in range(4):
                    version = store.version()
                    projects, tasks = store.list_projects(), store.list_tasks()
                    nodes, trajectory = store.list_nodes(), store.list_trajectory()
                    deleted_projects = [project for project in store.list_projects(include_deleted=True) if project.get("deletedAt")]
                    if version == store.version():
                        break
                snapshot = runner.snapshot() or {}
                self._json(200, {
                    "projects": projects, "tasks": tasks,
                    "nodes": nodes, "trajectory": trajectory, "deletedProjects": deleted_projects,
                    "adapters": registry.list_adapters(),
                    "workers": snapshot.get("workers", []),
                    "maxWorkers": snapshot.get("maxWorkers", 3), "version": version,
                })
                return
            match = re.fullmatch(r"/api/tasks/([A-Za-z0-9_.-]+)/artifacts/(\d+)", path)
            if match:
                self._artifact(match.group(1), int(match.group(2)))
                return
            if path == "/api" or path.startswith("/api/"):
                raise HTTPError(404, "接口不存在")
            relative = path.lstrip("/") or "index.html"
            try:
                candidate = (root / relative).resolve()
                candidate.relative_to(root)
            except (ValueError, RuntimeError):
                raise HTTPError(403, "不能访问工作台目录以外的文件") from None
            if not candidate.is_file():
                raise HTTPError(404, "页面不存在")
            body = candidate.read_bytes()
            content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                content_type += "; charset=utf-8"
            self._headers(200, content_type, len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _post(self):
            path = unquote(urlsplit(self.path).path)
            data = self._body()
            if path == "/api/projects":
                self._json(201, store.create_project(data))
                return
            match = re.fullmatch(r"/api/projects/([A-Za-z0-9_.-]+)/actions", path)
            if match:
                action = data.get("action")
                if action not in ("delete", "restore"):
                    raise ValueError("项目操作必须是 delete 或 restore")
                self._json(200, runner.handle_project_action(match.group(1), action))
                return
            if path == "/api/nodes":
                self._json(201, store.create_node(data))
                return
            match = re.fullmatch(r"/api/nodes/([A-Za-z0-9_.-]+)", path)
            if match:
                self._json(200, store.update_node(match.group(1), data))
                return
            if path == "/api/tasks":
                start = data.pop("start", False)
                if not isinstance(start, bool):
                    raise ValueError("start 必须是布尔值")
                task = store.create_task(data)
                if start:
                    task = runner.handle_action(task["id"], "start", {})
                self._json(201, task)
                return
            match = re.fullmatch(r"/api/tasks/([A-Za-z0-9_.-]+)/node", path)
            if match:
                if "nodeId" not in data:
                    raise ValueError("请提供 nodeId，取消分组时使用空值")
                self._json(200, runner.assign_task_node(match.group(1), data["nodeId"]))
                return
            match = re.fullmatch(r"/api/tasks/([A-Za-z0-9_.-]+)/actions", path)
            if match:
                action = data.pop("action", None)
                if not isinstance(action, str) or not action.strip():
                    raise ValueError("请提供操作名称")
                self._json(200, runner.handle_action(match.group(1), action, data))
                return
            raise HTTPError(404, "接口不存在")

        def _dispatch(self, mutation=False):
            try:
                self._check_request(mutation)
                self._post() if mutation else self._get()
            except HTTPError as exc:
                self._json(exc.status, {"error": exc.message})
            except KeyError:
                self._json(404, {"error": "任务、项目或节点不存在"})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except PermissionError:
                self._json(403, {"error": "没有读取此文件的权限"})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                LOG.exception("HTTP request failed")
                self._json(500, {"error": "工作台处理请求失败，请查看本机服务日志"})

        def do_GET(self):
            self._dispatch()

        def do_HEAD(self):
            self._dispatch()

        def do_POST(self):
            self._dispatch(mutation=True)

        def _unsupported(self):
            self._json(405, {"error": "不支持此请求方法"})

        do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _unsupported

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def server_bind(self):
            # HTTPServer.server_bind calls getfqdn(), which performs a blocking
            # reverse DNS lookup even for 127.0.0.1. Nothing in this local app
            # needs that hostname; set the required metadata from the socket.
            TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(15)
            return connection, address

    if ":" in host:
        Server.address_family = socket.AF_INET6
    return Server((host, port), Handler)
