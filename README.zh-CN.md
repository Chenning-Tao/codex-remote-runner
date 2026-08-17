# Codex Remote Runner

[English](README.md)

Codex Remote Runner 是一个命令行应用，用于将需要持久运行的任务提交到项目自有的远程机器池。它把队列和执行状态保存在控制器主机上，严格运行一个干净且确定的 Git revision，并允许客户端在原始 shell 退出后重新连接，继续监控、等待、停止或归档任务。
它还提供完全独立的前台 `dev` 命令，用于把经过过滤的 dirty working tree 直接放到一台可信计算服务器上做快速测试，不进入正式持久生命周期。

项目目前处于 1.0 之前的阶段。状态格式和部署流程已经过测试，但在活跃机器池上升级前，运维人员仍应审查版本变更。

## 功能

- 由控制器持久化队列与运行状态，任务不会依赖原始客户端进程。
- 根据已配置的容量、可用性和优先级自动选择执行服务器。
- 在远程 detached worktree 中准备并运行精确的 Git revision。
- 默认 detached 提交；仅在明确请求时使用附着式 Codex 等待。
- 提供本地网页控制面板，并支持确认后停止任务。
- 原样执行 opaque workload 命令，并通过 `RR_ASSIGNED_CORES` 暴露分配资源。
- 提供明确的停止、清理、彻底删除、服务器排空/下线和输出归档流程。
- 可从 dirty、未跟踪或非 Git 源码目录直接执行一次性前台开发测试。

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
- 使用 `dev` 时本地和目标计算服务器都需要 rsync；启用输出同步时也需要 rsync。
- 为所有已配置连接准备基于密钥、无需交互的 SSH alias。

目前不支持 Windows。本应用会在远程机器上执行运维人员提供的命令，适用于可信的项目基础设施，不应作为面向不可信租户的多租户调度器。

## 安装

使用 `uv` 直接从 GitHub tag 安装当前版本：

```bash
uv tool install 'codex-remote-runner[web] @ git+https://github.com/Chenning-Tao/codex-remote-runner.git@v0.9.6'
remote-runner --help
```

也可以从源码 checkout 安装：

```bash
git clone https://github.com/Chenning-Tao/codex-remote-runner.git
cd codex-remote-runner
uv tool install '.[web]'
remote-runner --help
```

开发环境：

```bash
uv sync --frozen --group dev
npm ci --prefix web
npm run build --prefix web
uv run pytest -q
```

`web` extra 是可选的。核心生命周期命令只依赖 PyYAML。

激活这次边界变更后的 controller release 时，会在阻止 dispatch lease 并停止
controller worker 后执行一次历史状态迁移。queue 记录与执行记录都已不存在的过期
dispatch lease 会先被释放——purge 只允许删除 terminal 执行记录，因此这类 lease
不可能还保护着仍在运行的授权 workload；其余 lease 仍然阻止激活。旧实验 registry
的字节会原子移出
活跃项目状态，保存到
`<controller-root>/retired-state/experiment-registry-v1/<project-id>`。旧 schema-1
pending output-sync intent 只有在 run ID、终态 execution record、revision、server、
路径和时间戳完全一致时才会升级为纯传输 schema。迁移可重复执行；遇到 symlink
或源/目标冲突时会拒绝覆盖并阻止激活。
活跃 registry 只留下一个私有 retirement marker，用于阻止尚未退出的旧二进制
重新创建已删除系统；正常 controller API 不读取该 marker。

## 配置

Remote Runner 使用两个 YAML 文件：

1. `~/.codex/remote-servers.yaml` 描述稳定的 `machine_id`、共享物理容量、SSH 端点和可选的服务器级 `dev_root`。
2. 项目自有的 `.remote-runner.yaml` 描述控制器、源码仓库、项目远程服务器、调度策略以及可选的输出归档。

可以从 [examples/remote-servers.yaml](examples/remote-servers.yaml) 和 [examples/project.remote-runner.yaml](examples/project.remote-runner.yaml) 开始。完整配置契约和环境准备要求见 [references/configuration.md](references/configuration.md)。

例如，为一台服务器设置 `dev_root: /srv/remote-runner-dev` 即可启用直接开发测试。项目可选配置 `dev.include`、`dev.exclude` 和 `dev.stale_after_seconds`；最小 schema 见上述示例。

## 网页控制台

为一个已经配置的项目打开网页控制台：

```bash
remote-runner web \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --source-repo /absolute/path/to/clean/worktree
```

控制台绑定在 `127.0.0.1`，可在服务器详情中直接设置控制器全局共享的
Standard/Test 并发任务数，也可以在排队任务详情中切换任务类型、优先级和
候选服务器。槽位与跨 Standard/Test 共享的物理核心预算共同限制任务准入；
内存仅用于库存和监控，不参与 admission。未指定 `--cores` 时保持兼容的整机
独占分配，`--cores N` 才显式共享恰好 `N` 核。调度不会修改 worker 数，也不会
停止已经运行的任务；将槽位设为 `0` 会暂停该类型的新任务。

服务器详情还可以先评估、再经二次确认永久下线机器。评估会覆盖同一控制器下
的所有项目、服务器实际任务进程、冻结队列候选和结果归档状态。下线会先在
控制器范围暂停新任务准入，再删除项目、全局、本机 SSH 和归档端专用同步凭据；
共享登录密钥、历史记录、运行目录和输出仍会保留。

该命令只监听 `127.0.0.1`，自动打开系统浏览器，并持续展示 controller snapshot。服务器列表会在远端主机提供数据时显示实时负载和物理内存使用量。使用 `--no-open` 可以只启动服务而不打开浏览器，使用 `--port PORT` 可以选择其他本地端口。浏览器不会收到 SSH 配置。详情栏可以停止一个精确的排队中或运行中任务，也可以修改排队任务的类型、优先级和可用服务器；队列表格还可以跨页勾选多个任务，批量修改任务类型、优先级，并可选地统一为同一组兼容服务器。未选择的设置保持不变。如果新选择的兼容服务器尚未准备，Web 进程会先为任务的精确 revision 完成准备，再启用该服务器。队列写操作使用各任务自己的 controller revision 和有时限的准备租约，旧快照修改或已经进入调度的任务会被拒绝；批量操作会明确报告部分失败并保留失败项。

当队列操作可能需要准备新服务器时，建议用 `--source-repo` 明确指定干净的本地
worktree；后端只会从中推送每个 queued historical revision 的精确 commit。若省略
该参数且配置的 `source.local_repo` dirty，历史队列扩容可以选择同一 Git common
directory 已注册的 clean linked worktree。后端会逐个验证所需 commit 对象，并在
结构化准备结果中报告所选路径和选择方式。整个过程不会 stash、commit、reset，
也不会提交未提交文件；找不到可信 clean source 时准备失败，原队列设置保持不变。

## 运行

Remote Runner 只接受干净且已经提交的源码 revision。默认提交在 controller 返回精确 run ID 和 queue record 后结束：

```bash
remote-runner run \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --source-repo /absolute/path/to/clean/worktree \
  --label "smoke test" \
  --task-id "validation/smoke" \
  --command '"$RR_PROJECT_PYTHON" -m pytest -q'
```

workload 命令不会被追加 `--num-workers` 或做其他改写；程序自行读取
`RR_ASSIGNED_CORES` 并决定如何使用。只有明确要求前台等待时才添加
`--wait --until reportable`。

输出同步只证明路径、传输字节、checksum 和 receipt。failed/stopped run 也可以
同步 checkpoint；同步完成不会改写 execution 状态，也不会判断科学有效性。

## 开发测试

用 `dev` 对当前 working tree 做一次性前台测试：

```bash
remote-runner dev \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --server compute-a \
  --command 'python3 -m pytest -q'
```

除非用 `--source-root` 指定另一个绝对目录，`dev` 会使用
`source.local_repo`。Git 源码根会传输 tracked 文件的当前磁盘字节和未被 ignore
的 untracked 文件；被 ignore 的文件必须通过 `dev.include` 明确加入。非 Git 根
使用过滤后的文件系统遍历。默认排除 VCS/工具状态、虚拟环境、依赖目录、构建/
结果目录和常见凭据文件，结构性排除项不能被覆盖。

每次调用都会新建私有的
`<dev_root>/<project_id>/tmp/dev-.../source`，并只 rsync 最终文件清单。因此
`node_modules`、results 等已排除目录不会反复上传，但这仍是一次完整的过滤快照，
不是长期源码目录上的增量更新。成功、失败或可处理的中断后会删除本次 session；
`<dev_root>/<project_id>/cache` 会长期保留，也可能包含源码衍生信息，不承诺安全擦除。

命令原样继承 workload 的 stdout/stderr 并返回其 exit code，不创建正式 run ID、
队列记录、Web 条目、output sync 或科学 provenance。它也不申请 controller lease：
`RR_ASSIGNED_CORES` 与 `RR_SERVER_CORES` 都使用注册表中的整机核心数，因此选中忙碌
服务器可能与正式任务争用资源。若本地未显式设置，`MAKEFLAGS`、
`CMAKE_BUILD_PARALLEL_LEVEL` 与 `CARGO_BUILD_JOBS` 默认使用全部注册核心；opaque
命令本身不会被改写。

常用的后续命令：

```bash
remote-runner monitor --project-config /path/to/.remote-runner.yaml
remote-runner wait --project-config /path/to/.remote-runner.yaml --run-id rr-... --until reportable
remote-runner wait-cohort --project-config /path/to/.remote-runner.yaml --run-id rr-... --run-id rr-...
remote-runner web --project-config /path/to/.remote-runner.yaml
remote-runner stop --project-config /path/to/.remote-runner.yaml --run-id rr-...
remote-runner close-decommissioned-run --project-config /path/to/.remote-runner.yaml --run-id rr-... --server compute-a --reason "云厂商已销毁该实例"
remote-runner close-decommissioned-run --project-config /path/to/.remote-runner.yaml --run-id rr-... --server compute-a --reason "云厂商已销毁该实例" --apply
remote-runner retire-server --project-config /path/to/.remote-runner.yaml --server compute-a
remote-runner retire-server --project-config /path/to/.remote-runner.yaml --server compute-a --apply
```

修改放置策略、优先级、隐私设置或输出标识前，请阅读 [references/submission.md](references/submission.md)。执行破坏性的生命周期操作前，请阅读 [references/lifecycle.md](references/lifecycle.md)。

## Codex 集成

[SKILL.md](SKILL.md) 和 [agents/openai.yaml](agents/openai.yaml) 提供 Codex skill 的元数据与运行契约。它们用于补充 CLI；Python wheel 不会安装任何用户专属的 Codex 配置。

明确要求当前 Codex App 任务自动回报时，发起任务的这一轮必须让 `run --wait`、
`remote-runner wait --until reportable` 或一个 `remote-runner wait-cohort` 保持为
尚未完成的工具调用。精确的多 run cohort 必须使用 `wait-cohort`，不得为每个 run
分别启动 wait 进程。这是一条附着式完成链路，不是后台回调：

1. CLI 首先读取精确 run 或按顺序排列的 exact cohort 权威聚合状态。
2. 尚不可回报时，CLI 使用状态 etag 在 controller 上发起有界的 `wait-run` 或
   批量 `wait-runs` 长等待。相关状态一旦变化，controller 会立即返回；状态未变化时的
   超时只会让 CLI 在内部续接传输，不会结束工具调用、启动模型回合，也不会增加一套
   对计算服务器的探测循环。
3. 达到所选条件后，CLI 只向 stdout 写入一份最终权威 JSON，然后退出。
   `wait-cohort` 在任一成员失败、停止、缺失或同步状态无效时立即以 attention required
   退出；只有所有成员都达到 reportable 时才成功退出。
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

需要跨会话引用时，Trellis 只保存精确 run ID 和决定，不复制 Remote Runner 的
queue/execution record；后续使用 run ID 查询权威状态。

## 安全与支持

部署控制器或受限的输出同步密钥前，请阅读 [SECURITY.md](SECURITY.md)。可复现的缺陷和功能请求请提交到 GitHub Issues；安全漏洞应通过私有漏洞报告流程提交。

## 参与贡献

贡献指南见 [CONTRIBUTING.md](CONTRIBUTING.md)。本项目使用 Apache License 2.0。
