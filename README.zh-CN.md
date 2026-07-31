# Codex Remote Runner

[English](README.md)

Codex Remote Runner 是一个命令行应用，用于将需要持久运行的任务提交到项目自有的远程机器池。它把队列和执行状态保存在控制器主机上，严格运行一个干净且确定的 Git revision，并允许客户端在原始 shell 退出后重新连接，继续监控、等待、停止或归档任务。

项目目前处于 1.0 之前的阶段。状态格式和部署流程已经过测试，但在活跃机器池上升级前，运维人员仍应审查版本变更。

## 功能

- 由控制器持久化队列与运行状态，任务不会依赖原始客户端进程。
- 根据已配置的容量、可用性和优先级自动选择执行服务器。
- 在远程 detached worktree 中准备并运行精确的 Git revision。
- 支持附着式 Codex 等待，并在等待工具完成后恢复发起它的 App 回合。
- 提供可交互的 Textual 和本地网页控制面板，并支持确认后停止任务。
- 提供由控制器持有、包含冻结 binding 与已验证结果的实验注册表。
- 提供明确的停止、清理、彻底删除、服务器排空/下线和输出归档流程。

```text
本地 CLI / Codex skill
          |
          | SSH
          v
      控制器主机  ------> 归档目标
          |
          | SSH
          v
      计算服务器池
```

## 环境要求

- 本地和控制器主机使用 macOS 或 Linux。
- Python 3.12 或更高版本，以及 [uv](https://docs.astral.sh/uv/)。
- 控制器和计算主机安装 Git、OpenSSH 与 tmux。
- 启用输出同步时需要 rsync。
- 为所有已配置连接准备基于密钥、无需交互的 SSH alias。

目前不支持 Windows。本应用会在远程机器上执行运维人员提供的命令，适用于可信的项目基础设施，不应作为面向不可信租户的多租户调度器。

## 安装

使用 `uv` 直接从 GitHub tag 安装当前版本：

```bash
uv tool install 'codex-remote-runner[tui,web] @ git+https://github.com/Chenning-Tao/codex-remote-runner.git@v0.8.0'
remote-runner --help
```

也可以从源码 checkout 安装：

```bash
git clone https://github.com/Chenning-Tao/codex-remote-runner.git
cd codex-remote-runner
uv tool install '.[tui,web]'
remote-runner --help
```

开发环境：

```bash
uv sync --frozen --group dev
npm ci --prefix web
npm run build --prefix web
uv run pytest -q
```

`tui` 和 `web` extra 都是可选的。核心生命周期命令只依赖 PyYAML。

## 配置

Remote Runner 使用两个 YAML 文件：

1. `~/.codex/remote-servers.yaml` 描述共享物理容量和 SSH 端点。
2. 项目自有的 `.remote-runner.yaml` 描述控制器、源码仓库、项目远程服务器、调度策略以及可选的输出归档。

可以从 [examples/remote-servers.yaml](examples/remote-servers.yaml) 和 [examples/project.remote-runner.yaml](examples/project.remote-runner.yaml) 开始。完整配置契约和环境准备要求见 [references/configuration.md](references/configuration.md)。

## 网页控制台

为一个已经配置的项目打开网页控制台：

```bash
remote-runner web --project-config /absolute/path/to/.remote-runner.yaml
```

控制台绑定在 `127.0.0.1`，可在服务器详情中直接设置控制器全局共享的
Standard/Test 并发任务数，也可以在排队任务详情中切换任务类型、优先级和
候选服务器。槽位只限制任务准入，不修改 worker 数，也不会停止已经运行的
任务；将槽位设为 `0` 会暂停该类型的新任务。

服务器详情还可以先评估、再经二次确认永久下线机器。评估会覆盖同一控制器下
的所有项目、服务器实际任务进程、冻结队列候选和结果归档状态。下线会先在
控制器范围暂停新任务准入，再删除项目、全局、本机 SSH 和归档端专用同步凭据；
共享登录密钥、历史记录、运行目录和输出仍会保留。

该命令只监听 `127.0.0.1`，自动打开系统浏览器，并持续展示与 TUI 相同的 controller snapshot。服务器列表会在远端主机提供数据时显示实时负载和物理内存使用量。使用 `--no-open` 可以只启动服务而不打开浏览器，使用 `--port PORT` 可以选择其他本地端口。浏览器不会收到 SSH 配置。详情栏可以停止一个精确的排队中或运行中任务，也可以修改排队任务的类型、优先级和可用服务器；队列表格还可以跨页勾选多个任务，批量修改任务类型、优先级，并可选地统一为同一组兼容服务器。未选择的设置保持不变。如果新选择的兼容服务器尚未准备，Web 进程会先为任务的精确 revision 完成准备，再启用该服务器。队列写操作使用各任务自己的 controller revision 和有时限的准备租约，旧快照修改或已经进入调度的任务会被拒绝；批量操作会明确报告部分失败并保留失败项。

## 实验注册表

网页中的“实验”分区展示由控制器持有的已发布通用实验设计、精确 point
revision、冻结的 run binding、经过输出同步验证的结构化结果，以及显式
结果决策。网页通过有界查询读取控制器；操作者核对候选指标、观测数和来源运行
后，可以显式接受或拒绝候选结果。结果不会按时间戳选择，真实空注册表也不会
回退到合成 Demo。

打开 `?demo=experiments` 可以在没有 Controller 的情况下查看内置的
`decoder_atomloss` 项目快照。它只是用于检查面板的静态测试数据，不会写入
Controller；正常“实验”视图仍只读取当前项目配置的 Controller 注册表。

```bash
remote-runner experiment plan preview \
  --project-config /path/to/.remote-runner.yaml \
  --file experiment-plan.json

remote-runner experiment query \
  --project-config /path/to/.remote-runner.yaml \
  --file experiment-query.json
```

使用 `remote-runner run --experiment-binding binding.json` 可以为精确 run ID 和
Git revision 生成并冻结 binding。新 producer 在同步输出中写入
`experiment_result`。带 binding 的 workload 会通过
`RR_EXPERIMENT_BINDING_PATH` 收到 canonical finalized binding 的只读文件路径；
对应文件摘要由 `RR_EXPERIMENT_BINDING_SHA256` 提供。没有 binding 的 workload
不会收到这两个变量。结果满足资格条件后仍需显式
acceptance 才会成为当前正式结果。契约、权威边界和后续加固项见
[实现计划](docs/plans/experiment-registry-results-dashboard.md)。

## 运行

Remote Runner 只接受干净且已经提交的源码 revision。下面是一个最小的前台等待任务：

```bash
remote-runner run \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --source-repo /absolute/path/to/clean/worktree \
  --label "smoke test" \
  --task-id "validation/smoke" \
  --result-intent supporting \
  --wait \
  --until reportable \
  --command '"$RR_PROJECT_PYTHON" -m pytest -q'
```

命令会以 JSON 返回权威的队列和执行状态。任务失败或被停止时，等待操作本身仍可能成功完成，因此应检查返回的 outcome，而不能只依赖 CLI 退出状态。

常用的后续命令：

```bash
remote-runner monitor --project-config /path/to/.remote-runner.yaml
remote-runner wait --project-config /path/to/.remote-runner.yaml --run-id rr-... --until reportable
remote-runner tui --project-config /path/to/.remote-runner.yaml
remote-runner web --project-config /path/to/.remote-runner.yaml
remote-runner stop --project-config /path/to/.remote-runner.yaml --run-id rr-...
remote-runner retire-server --project-config /path/to/.remote-runner.yaml --server compute-a
remote-runner retire-server --project-config /path/to/.remote-runner.yaml --server compute-a --apply
```

在 TUI 中选中运行中或排队中的任务后按 `x`，即可检查并确认停止请求。控制器始终是状态权威；如果传输结果不明确，TUI 会报告停止尚未得到确认并重新刷新，而不会假定任务已经停止。

修改放置策略、优先级、隐私设置或输出标识前，请阅读 [references/submission.md](references/submission.md)。执行破坏性的生命周期操作前，请阅读 [references/lifecycle.md](references/lifecycle.md)。

## Codex 集成

[SKILL.md](SKILL.md) 和 [agents/openai.yaml](agents/openai.yaml) 提供 Codex skill 的元数据与运行契约。它们用于补充 CLI；Python wheel 不会安装任何用户专属的 Codex 配置。

当前 Codex App 任务需要自动回报时，发起任务的这一轮必须让 `run --wait` 或
`remote-runner wait --until reportable` 保持为尚未完成的工具调用。这是一条
附着式完成链路，不是后台回调：

1. CLI 首先读取精确 run 的权威聚合状态。
2. run 尚不可回报时，CLI 使用状态 etag 在 controller 上发起有界的
   `wait-run` 长等待。状态一旦变化，controller 会立即返回；状态未变化时的
   超时只会让 CLI 在内部续接传输，不会结束工具调用、启动模型回合，也不会
   增加一套对计算服务器的探测循环。
3. 达到所选条件后，CLI 只向 stdout 写入一份最终权威 JSON，然后退出。
   状态变化和未变化的长等待超时只属于 stderr 状态信息。
4. 正常的工具完成事件随后恢复发起等待的 Codex 回合。Codex 此时才能检查
   已有日志或同步产物并生成最终回复。是否显示未读标记或系统通知，由 Codex
   App 根据任务焦点和通知设置自行决定；Remote Runner 不写入 App 状态，也不
   保证一定出现这两种界面提示。

没有显式传入 `--max-wait` 或 `--connection-grace` 时，本地等待既没有总时限，
也没有 controller 连续失联时限。这两个选项只是显式退出机制：它们只结束
本地等待，不会停止持久化的远程 run。如果发起等待的 App 回合或工具会话结束，
远程 run 仍会继续，但系统不会自动生成 detached 回报；之后必须使用同一个
精确 run ID 重新附着。Remote Runner 不再提供 detached Codex callback、独立
App Server 回报、模型 heartbeat 或定时模型/工具轮询路径。

## 安全与支持

部署控制器或受限的输出同步密钥前，请阅读 [SECURITY.md](SECURITY.md)。可复现的缺陷和功能请求请提交到 GitHub Issues；安全漏洞应通过私有漏洞报告流程提交。

## 参与贡献

贡献指南见 [CONTRIBUTING.md](CONTRIBUTING.md)。本项目使用 Apache License 2.0。
