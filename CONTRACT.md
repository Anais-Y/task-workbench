# Task Workbench — integration contract

Local-first task manager. Python 3 standard library server + SQLite + vanilla web UI. All paths are resolved from the checkout; no developer-specific paths. Preserve the approved prototype visual language. All user-facing text Chinese. One initial project: 建立一个任务管理器; multiple-project support retained. No simulation or invented progress.

## Ownership
- backend worker: taskboard/store.py, taskboard/server.py, tests/test_store.py, tests/test_server.py
- frontend worker: web/** only
- adapters worker: taskboard/adapters.py, tests/test_adapters.py, adapters.example.json
- root: all other files, especially runner.py, cli.py, mcp.py, seed.py, __main__.py, README.md, integration tests.
Do not modify others' files; send a message for changes. No dependency installation needed.

## Task JSON
`id` string (T001...), `projectId` string, `title`, `desc`, `goal`, `criteria` (string array), `engine` (codex|claude|custom ID), `status` (queued|running|review|done|failed|cancelled), `kind` (plan|result|question), `phase` (plan|execute), `steps` (string array), `done` (integer completed steps; never invented percentage), `deps` (task IDs), `worker` (string or null), `executionMode` (local|external), `scheduled` (bool; only true runs automatically), `artifacts` (array of {name,type,body?,path?}), `log` (array of [ISO time,message]), `runs` (array of run summaries), `sessionId`, `sessionEngine`, `error`, `result` (string), `plan` (array of {title,goal,criteria,deps:[zero-based plan index]}), `parentTaskId`, `createdAt`, `updatedAt`. Defaults filled in Store.create_task. Additional harmless JSON fields allowed for future adapters. New UI tasks default phase=plan, kind=plan, status=queued, executionMode=local, scheduled=false. UI clearly labels unstarted tasks.
Project: {id,name,path,createdAt}. Only one seeded project; path is absolute local workspace. Task statuses and counts from real data. No fake ticks.

## Store Python API (thread safe)
`Store(db_path)` opens DB and creates schema; connections usable from multiple threads under lock. `close()`.
`list_projects() -> list[dict]`; `create_project(data:dict)->dict`; `get_project(id)->dict|None`.
`list_tasks()->list[dict]`; `get_task(id)->dict|None`; `create_task(data:dict)->dict`; `update_task(id,patch:dict)->dict` raises KeyError when absent.
`add_event(id,message:str,**fields)->dict`: append log and merge fields atomically. Bound retained log (e.g. 500 rows).
`version()->int` monotonic persisted integer; any mutation increments. Validate task status, project existence, dependencies project membership and no cycles. Store does not schedule or execute. Field id immutable. Lists/dicts copies, not mutable shared references.

## Adapters Python API
`AdapterRegistry(config_path=None)` custom config optional; built-ins detect PATH plus known macOS Codex application / Claude ~/.local/bin paths.
`list_adapters()->list[dict]` each {id,name,available,enabled,description,capabilities:[str],executable?:str}; no auth claims from merely present executable.
`run(engine:str, *, prompt:str, cwd:str, phase:str, session_id:str|None, emit:callable, cancel:threading.Event)->dict` blocks in worker thread. Returns {ok:bool,text:str,sessionId:str|None,artifacts:list,error?:str,cancelled?:bool}. `emit(event:dict)` with keys type (message|progress|artifact|session|error), message, sessionId?, artifact?. Never shell=True; prompt through stdin. Stream stdout/stderr safely, bounded output; terminate process group on cancel. No dangerous permission bypass flags, no model overrides. phase plan read-only where supported. Nonzero exit -> ok false; error results even exit0 detected. Codex JSONL and Claude stream-json normalized; preserve provider session IDs. Custom config executable argv list with {prompt} avoided, use stdin. Document custom protocol.

## HTTP / frontend contract
Server function `make_server(store, registry, runner, host='127.0.0.1', port=8766, web_root=None)` returns ThreadingHTTPServer. runner exposes `handle_action(task_id, action, payload)->dict` and `snapshot()->dict`. Static web served at /.
- GET /api/state -> {projects,tasks,adapters,workers,maxWorkers,version}. `workers` from runner.snapshot(), maxWorkers default3. Response sets Cache-Control:no-store.
- POST /api/projects {name,path} -> project (201).
- POST /api/tasks task fields -> task (201), just queued; user then starts via action. Optional `start:true` invokes runner.handle_action(id,'start',{}).
- POST /api/tasks/{id}/actions {action, message?, engine?} -> updated task.
- GET /api/tasks/{id}/artifacts/{index} -> {name,type,body}; inline body or safe UTF8 file under project's workspace only (limit1MB). No path traversal or symlink escape.
- GET /api/health -> {ok:true}.
All mutations require Content-Type:application/json and X-Taskboard-Client:taskboard. Server validates Host/Origin loopback, no CORS. Unknown routes clear JSON error. Error shape {error:string} with suitable 4xx/5xx. Frontend fetch helper supplies header, displays network/action errors. Preserve drafts and focus during 2s polling; render on version changes only, pause/reconcile editor fields. Show connection state and last sync. Escaped text only.

## Actions handled by root runner
start: queued local task -> scheduled=true; queue scheduler claims it. If phase plan, generate proposed tasks -> review kind plan. Execution succeeds -> review kind result (human accepts). CLI failure -> failed.
approve_plan: review kind plan -> create subtasks from task.plan with dependencies, scheduled=true; parent becomes queued orchestration record (executionMode=external), automatically done after all child tasks accepted. Empty/invalid plan rejected with useful error.
accept: review result -> done, unblocks dependencies. External actual build tasks can also be accepted. No pretend re-execution of external tasks.
feedback: message required, review/failed/done -> append event; local resumes matching provider session in same phase, scheduled=true; external stores feedback and keeps review (this build’s collaboration agents are coordinated by root, not subprocess runner).
retry: failed/cancelled local -> queued scheduled=true.
cancel: queued/running/review/failed -> cancel event and cancel local process. External cannot terminate live Codex collaboration agent through HTTP; return explicit message when unsupported.

## UI requirements
Question tasks may include `attention: {badge, title, description, actionLabel, actionUrl}`. Show the concrete request on the card and at the top of the detail page; fall back to `result` or the latest log. Preserve instruction line breaks. Optional action links must be HTTP(S) URLs without embedded credentials and open only when clicked. A link or recorded feedback never implies authorization or completes the external action. Ignore these hints after leaving review/question.

Preserve the existing approved visual language in web/. Project selector, state counts, board/list, attention/artifacts filters, task detail, logs, artifacts preview, feedback/review/retry, new task and adapter availability. Remove demo play, mock datasets, widget state and simulated percentages. Show one actual project and actual externally tracked build tasks from backend. Top badge 本机工作台 / 已连接. External workers labeled 协作 worker; local engine distinction. Show project in task creation; no browser editor for custom executable commands (config file instead). Details show actions appropriate to state. Pending plan has 查看计划/批准计划 when parsed; failed plan without structured steps cannot approve. Preserve feedback textarea on updates.

## Root build tasks (seed IDs)
T001 storage/API — worker backend
T002 live interface — worker frontend
T003 Agent adapters — worker adapters
T004 queue/dependencies — root
T005 review/feedback — root
T006 logs/artifacts — root
T007 CLI/MCP entrypoints — root
T008 integration/checks/docs — root
All executionMode=external, scheduled=false. Record real events and meaningful completed milestones only. Keep project only 建立一个任务管理器. Root creates seed tasks shortly. Workers report progress and file paths; do not manipulate build state directly.
