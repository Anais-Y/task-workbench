# 不需要模型账号的适配器示例

这里提供一个确定性 Python 程序，验证**真实子进程、标准输入、JSONL 事件、会话恢复、文件产物和取消**。它不是 AI，不分析任务，也不证明某个真实 coding agent 已登录或有足够权限。所有代码仅使用 Python 标准库，可在 macOS 和 Linux 运行。

## 一条命令自检

在 clone 后的仓库中运行：

```sh
python3 examples/check_custom_agent.py
```

也可从其他目录运行这个文件的完整路径；路径有空格或中文时加引号。自检会独立创建临时数据库、带空格和中文的项目目录及随机本机端口，通过 HTTP 完成以下流程，并在结束后关闭服务、清理临时文件：

1. 创建计划任务，运行真实示例子进程，检查计划阶段没有写文件。
2. 批准计划，运行子任务，从工作台接口读取真实文件产物。
3. 发送反馈，在新的子进程中恢复相同会话，并检查第二轮产物确实包含反馈。
4. 验收结果，确认父任务完成。
5. 启动带等待的真实进程，取消它，确认心跳停止且 worker 已释放。

它不会连接现有工作台、调用模型、读取认证资料或改动已有任务。CI 可使用 `--json` 获取结果和非零失败退出码。

## 在工作台里体验示例

先生成独立配置：

```sh
python3 examples/setup_example.py
```

默认写入当前 clone 的 `.data/example-adapters.json`，文件中会使用本次运行发现的 Python 和脚本路径，没有写死开发者机器路径。把仓库移动到其他位置后请生成一份新配置。配置包含“协议示例 · 非 AI”和用于取消验证的“协议示例 · 取消测试”两个执行器。

要选择其他输出位置：

```sh
python3 examples/setup_example.py --output "/某个目录/我的 示例配置.json"
```

生成器拒绝覆盖任何已有文件，也拒绝以 `adapters.json` 为文件名，以保护你的正式配置。重复运行时可换一个新的输出文件名。

启动独立体验服务：

```sh
python3 -m taskboard serve --port 8767 --db .data/example.sqlite3 --adapters .data/example-adapters.json
```

打开 `http://127.0.0.1:8767`，使用自动创建的初始项目或添加一个可写项目目录，然后选择“协议示例 · 非 AI”创建任务。示例只会在该项目的 `.taskboard-example/` 下写文件。使用“协议示例 · 取消测试”直接创建执行任务，可在等待的 30 秒中点击取消。

## 接入你自己的 CLI

从 `example_agent.py` 复制协议部分，再把确定性的文件生成过程替换为你的 Agent 调用。脚本本身可单独运行，不依赖 `taskboard` 包。提示词始终通过 stdin 传入，stdout 每行输出一个 JSON 对象；请把非协议诊断写到 stderr。

```json
{"type":"session","sessionId":"稳定且可恢复的会话 ID"}
{"type":"progress","message":"正在处理"}
{"type":"artifact","artifact":{"name":"report.md","type":"markdown","path":"results/report.md"}}
{"type":"result","ok":true,"text":"完成说明","sessionId":"同一会话 ID","artifacts":[]}
```

最后必须发送 `result`。`error` 事件、`ok:false`、非零退出码或缺少结果都会判为失败。产物路径必须在当前项目目录内才能通过 HTTP 预览；也可通过 `body` 提供文本产物。文件产物应在文件实际写成后上报。

配置使用 argv 数组，工作台不会经过 shell；提示词不能出现在 command 参数中。`resumeArgs` 的 `{session_id}` 会被替换为前一次执行返回的 ID。`planArgs` 必须真正启用该 CLI 的只读模式，工作台不会为自定义进程提供额外沙箱。未配置 `planArgs` 的执行器只能直接执行任务。给 CLI 加哪些参数，应以该 CLI 的本机帮助和官方文档为准。

Codex 与 Claude Code 已有内置适配器。是否能实际运行还取决于安装、登录、工作目录和权限；此示例自检通过不替代它们各自的验证。
