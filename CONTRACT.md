# Task Workbench — integration contract

## v0.2 Project exploration contract (2026-10-09)

- User model: Project → exploration node → tasks/subtasks. Nodes are research directions, not task execution states. Each has `id, projectId, title, hypothesis, color, parentIds:[], outcome, conclusion, createdAt, updatedAt`. Colors: `blue, violet, teal, amber, rose, slate`. Outcomes: `exploring, adopted, discarded, inconclusive`; a non-exploring outcome requires a nonempty conclusion. Parent nodes are in the same project, form an acyclic graph, and allow branching plus multi-parent synthesis. These lineage edges do not automatically schedule tasks.
- Tasks gain nullable `nodeId`. Plans propagate their node to child tasks. `Store.assign_task_node(task_id,node_id)` moves the whole parent/subtask family atomically, rejecting running or scheduled members. Cross-project references are rejected. Old tasks remain unassigned; no fabricated experiments or dates.
- Projects have nullable `deletedAt`; delete means move to recoverable trash, never remove workspace files. `list_projects(include_deleted=False)` and `list_tasks()` hide deleted projects by default; `get_project/get_task` remain available internally. `delete_project(id)` rejects running or scheduled tasks, `restore_project(id)` restores all records. Initial-project bootstrap must consider deleted projects so deleting the final project stays deleted on restart.
- Store node API: `list_nodes(project_id=None)`, `get_node(id)`, `create_node(data)`, `update_node(id,patch)`. Node project/id/createdAt immutable. `list_trajectory(project_id=None)` returns persisted append-only milestone events `{id,projectId,nodeId,taskId,type,at,title,summary}` in time order. Record node creation/edits/conclusion revisions, task creation/status transitions/result revisions/node reassignment and project deletion/restoration. Do not record polling or streamed progress. Existing tasks get truthful creation events and an explicitly labeled migration snapshot, not invented past transitions. History survives reopening the database and the task log's 500-line cap.
- `GET /api/state` additionally returns `{nodes,trajectory,deletedProjects}`. Existing keys stay compatible. `POST /api/nodes` creates; `POST /api/nodes/{id}` updates. `POST /api/projects/{id}/actions {action:delete|restore}` calls `runner.handle_project_action` under the runner lock. `POST /api/tasks/{id}/node {nodeId:string|null}` calls `runner.assign_task_node` under the runner lock. All retain existing loopback/security validation.
- Stop failures persist `cancelFailed`, `processId`, `processState` and actionable `attention`. Until exit is confirmed, reject project deletion, family reassignment and family restart paths; reserve the worker slot. Probes only inspect process existence. `confirm_stopped` requires `confirmStopped:true` and explicit user verification, is available for unknown state, and cannot bypass a detected live process. CLI and MCP retain this confirmation requirement.
- UI: add project management/trash, node view and Trajectory view. Stable color with text labels across node cards and task cards, filter tasks by node, allow assigning existing tasks. Node detail shows hypothesis, lineage, tasks, conclusion editor and recorded history. Trajectory makes forks/parallel branches/merges visible and provides chronological, dated milestones plus conclusion snapshots. Preserve existing task review/feedback/worker behavior, draft handling, responsive layout. Empty states guide adding a first node; don't insert fake experiment data in the user's DB. Use `worker`/`Agent` instead of 执行者/执行器 in product-facing UI.
- Owners for this iteration: backend owns store.py/server.py and storage/API tests; frontend owns web/**; root owns runner/cli/mcp/bootstrap/docs/integration tests and release. Backward-compatible migration and existing data preservation are required.

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
