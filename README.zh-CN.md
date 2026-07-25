# Codex Remote Runner

[English](README.md)

Codex Remote Runner 是一个命令行应用，用于将需要持久运行的任务提交到项目自有的远程机器池。它把队列和执行状态保存在控制器主机上，严格运行一个干净且确定的 Git revision，并允许客户端在原始 shell 退出后重新连接，继续监控、等待、停止或归档任务。

项目目前处于 1.0 之前的阶段。状态格式和部署流程已经过测试，但在活跃机器池上升级前，运维人员仍应审查版本变更。

## 功能

- 由控制器持久化队列与运行状态，任务不会依赖原始客户端进程。
- 根据已配置的容量、可用性和优先级自动选择执行服务器。
- 在远程 detached worktree 中准备并运行精确的 Git revision。
- 支持前台等待和事件驱动的 Codex 任务唤醒。
- 提供可交互的 Textual 和本地网页控制面板，并支持确认后停止任务。
- 提供明确的停止、清理、彻底删除、服务器排空和输出归档流程。

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
uv tool install 'codex-remote-runner[tui,web] @ git+https://github.com/Chenning-Tao/codex-remote-runner.git@v0.3.1'
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

该命令只监听 `127.0.0.1`，自动打开系统浏览器，并持续展示与 TUI 相同的 controller snapshot。使用 `--no-open` 可以只启动服务而不打开浏览器，使用 `--port PORT` 可以选择其他本地端口。浏览器不会收到 SSH 配置。详情栏可以停止一个精确的排队中或运行中任务，也可以修改排队任务的优先级和可用服务器；如果新选择的兼容服务器尚未准备，Web 进程会先为任务的精确 revision 完成准备，再启用该服务器。队列写操作使用 controller revision 和有时限的准备租约，旧快照修改或已经进入调度的任务会被拒绝。

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
  --command '"$RR_PROJECT_PYTHON" -m pytest -q'
```

命令会以 JSON 返回权威的队列和执行状态。任务失败或被停止时，等待操作本身仍可能成功完成，因此应检查返回的 outcome，而不能只依赖 CLI 退出状态。

常用的后续命令：

```bash
remote-runner monitor --project-config /path/to/.remote-runner.yaml
remote-runner wait --project-config /path/to/.remote-runner.yaml --run-id rr-...
remote-runner tui --project-config /path/to/.remote-runner.yaml
remote-runner web --project-config /path/to/.remote-runner.yaml
remote-runner stop --project-config /path/to/.remote-runner.yaml --run-id rr-...
```

在 TUI 中选中运行中或排队中的任务后按 `x`，即可检查并确认停止请求。控制器始终是状态权威；如果传输结果不明确，TUI 会报告停止尚未得到确认并重新刷新，而不会假定任务已经停止。

修改放置策略、优先级、隐私设置或输出标识前，请阅读 [references/submission.md](references/submission.md)。执行破坏性的生命周期操作前，请阅读 [references/lifecycle.md](references/lifecycle.md)。

## Codex 集成

[SKILL.md](SKILL.md) 和 [agents/openai.yaml](agents/openai.yaml) 提供 Codex skill 的元数据与运行契约。它们用于补充 CLI；Python wheel 不会安装任何用户专属的 Codex 配置。

## 安全与支持

部署控制器或受限的输出同步密钥前，请阅读 [SECURITY.md](SECURITY.md)。可复现的缺陷和功能请求请提交到 GitHub Issues；安全漏洞应通过私有漏洞报告流程提交。

## 参与贡献

贡献指南见 [CONTRIBUTING.md](CONTRIBUTING.md)。本项目使用 Apache License 2.0。
