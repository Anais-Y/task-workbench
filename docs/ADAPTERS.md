# 接入其他 coding agent

适配器把不同 CLI 的调用参数和输出转换成同一种任务事件。任务库、网页和调度器无需修改。

## 已有适配器

| ID | 启动方式 | 事件协议 | 会话 |
| --- | --- | --- | --- |
| `codex` | `codex exec` / `codex exec resume` | Codex JSONL | 记录 `thread_id` |
| `claude` | `claude --print` / `--resume` | Claude stream-json | 记录 `session_id` |
| 自定义 ID | 配置中的参数数组 | Taskboard JSONL、纯文本或已有 provider 协议 | 可选 |

默认沿用 CLI 的登录和模型配置。找到可执行文件不等于账号已登录。

## 注册一个执行器

复制根目录的 `adapters.example.json` 到 `adapters.json`，修改并启用条目。也可以单独保存文件，在启动时显式选择：

```sh
./task-workbench start --adapters /absolute/path/to/my-adapters.json
```

改配置后先停止再启动服务。正在运行的服务不会热重载配置。

```json
{
  "adapters": [{
    "id": "my-agent",
    "name": "我的 Agent",
    "enabled": true,
    "description": "本机 coding agent 包装程序",
    "command": ["/absolute/path/to/my-agent", "run"],
    "protocol": "taskboard-jsonl",
    "planArgs": ["--read-only"],
    "executeArgs": [],
    "resumeArgs": ["--resume", "{session_id}"]
  }]
}
```

- `command` 是参数数组，不是 shell 语句。程序从 stdin 读取完整提示词。
- 当前工作目录是任务所属项目目录。额外脚本路径宜使用绝对路径；不要依赖开发机的用户名或目录。
- `id` 必须唯一，不能覆盖内置的 `codex` 或 `claude`。
- `planArgs` 表示自定义程序具备只读计划模式，**限制由该程序实际保证**。没有配置时不会允许规划任务，可以直接创建执行任务。
- `resumeArgs` 可省略。提供时必须包含 `{session_id}`，并且该 CLI 确实支持恢复。
- 配置文件由本机用户管理，不能从网页填写任意可执行命令。

## 输出协议

`taskboard-jsonl` 要求 stdout 每行一个 JSON 对象，stderr 留给诊断。事件示例：

```json
{"type":"progress","message":"正在读取项目"}
{"type":"message","message":"发现两个需要修改的文件"}
{"type":"session","sessionId":"agent-session-123"}
{"type":"artifact","artifact":{"name":"report.md","type":"markdown","path":"report.md"}}
{"type":"result","ok":true,"text":"修改与验证说明","sessionId":"agent-session-123","artifacts":[]}
```

产物可以使用项目内相对路径或内联 `body` 文字。工作台拒绝预览项目目录外的文件。流式事件与最终结果重复报告同一产物时只展示一次。

最后必须返回一个 `result` 事件，并且退出码为 0，才会进入待验收状态。`ok:false`、错误事件、非零退出码、没有最终结果都算失败。报错形式：

```json
{"type":"error","message":"需要用户先登录"}
{"type":"result","ok":false,"text":"","error":"需要用户先登录"}
```

也支持 `protocol: "text"`，使用 stdout 作为结果并依据退出码判断。纯文本不能表达上述结构化事件和会话信息。若输出其他 Agent 的专有格式，应写一个包装程序转换，而不是假定这些格式兼容。

## 计划、反馈和取消

计划阶段最终 `text` 中需要有以下 JSON，任务才可在网页里批准：

```json
{"summary":"计划概述","subtasks":[
  {"title":"任务 A","goal":"目标与负责文件","criteria":["验收标准"],"deps":[]},
  {"title":"任务 B","goal":"使用 A 的结果","criteria":["验收标准"],"deps":[0]}
]}
```

依赖编号从 0 开始，禁止循环。工作台会创建真实子任务，在前置任务验收后放行后续任务。

反馈会保持同一任务 ID 和历史记录。若适配器返回会话 ID 并支持恢复，后续运行会使用该会话；不能把一种 provider 的会话 ID 传给另一种 provider。

取消会终止执行器进程组。CLI 包装程序应响应系统终止信号，并把子进程放在同一进程组，不要偷偷创建脱离工作台管理的后台进程。

## 在没有模型账号时验证接入

运行独立的 [协议示例](../examples/README.md)：

```sh
python3 examples/check_custom_agent.py
```

该示例是确定性的协议测试程序，不具备推理或编码能力。检查通过说明工作台可以接入一个独立实现的 CLI，不代表任意模型或工具权限都已验证。实际 coding agent 还需在目标设备完成登录、模型调用和工具权限验收。
