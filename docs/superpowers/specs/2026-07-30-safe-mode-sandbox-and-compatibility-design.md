# OpenSquilla 安全模式、沙箱设置与升级兼容设计

## 1. 背景与目标

OpenSquilla Desktop 0.5.2 的 Windows 冻结包会在启动沙箱文件系统
worker 时执行：

```text
opensquilla-gateway.exe -m opensquilla...
```

冻结后的 Gateway 可执行文件并不是 Python 解释器，因此 Typer 在处理
`-m` 时直接退出。沙箱随后被判定为不可用，任务界面又没有及时把失败
收敛成明确状态，最终表现为操作被拦截或任务卡住。

本设计不只修复这一条命令，而是统一解决以下问题：

- 将用户可见的运行模式收敛为“安全模式”和“完全访问”。
- 从旧客户端直接覆盖更新时，兼容旧配置、数据库、任务、会话、Token、
  CLI 和 RPC 内容。
- 为安全模式提供可配置的文件、命令和网络策略。
- 随桌面包提供 Node.js、Python、Git 与 Bash。
- 在 source、venv/uv、wheel/CLI 和三平台桌面冻结包中使用同一套内部
  子进程启动协议。
- 在沙箱确实不可用时软着陆，同时不把未认证内网客户端升级到宿主机
  权限。

## 2. 已确认的产品边界

### 2.1 用户可见模式

新版业务模型只存在：

- `safe`：系统沙箱加文件、命令、网络策略。
- `full`：直接使用宿主机权限，不应用沙箱模块规则。

“托管模式”从 UI、业务枚举、新配置、新数据库字段、新日志和 canonical
sandbox API 中删除。旧名称不再代表可选择的运行模式。

### 2.2 明确不在本期实现

- 不为内网连接启用 TLS。
- 不做升级失败后的自动数据回滚。
- 不在 Full 模式下承诺文件、命令或域名策略仍然有效。
- 不限制已认证 Safe 模式读取宿主机文件；操作系统自身权限仍然有效。
- 不使用虚拟文件系统或特权文件系统驱动截获所有不透明原生程序的
  删除操作。
- 不让随包运行时脱离应用独立自动升级。

这些边界必须在设置和文档中如实描述，不能使用“完全防泄漏”或“所有
删除都可恢复”等无法兑现的措辞。

## 3. 总体架构

新增一个统一策略层，位于工具调用、代码执行和系统沙箱后端之间。

```text
客户端 / 历史内容
        |
        v
LegacyCompatibilityCodec ---> canonical safe/full
        |
        v
Principal + ModeResolver + SandboxCapability
        |
        +---------------- Full ----------------> Host
        |
        v
PolicySnapshot
  | FilePolicy
  | CommandPolicy
  | NetworkPolicy
  | RuntimePolicy
        |
        v
ApprovalService / FileMutationBroker / BackupVault / ScopedGrant
        |
        v
Windows / macOS / Linux SandboxBackend
```

核心组件职责如下：

- `LegacyCompatibilityCodec`：只在旧数据和 v1 协议边界解析旧名称。
- `ModeResolver`：根据 principal、用户选择和真实沙箱能力输出执行模式。
- `SandboxCapabilityService`：执行真实启动探测，不以“已安装”代替“可用”。
- `SandboxPolicyStore`：校验并持久化版本化策略。
- `PolicySnapshot`：任务启动时取得不可变策略快照。
- `FilePolicyEngine`：规范化路径并判断读取、修改、删除和备份要求。
- `CommandPolicyEngine`：解析命令 token、前缀和内置高风险动作。
- `NetworkPolicyEngine`：计算域名规则并保留基础 SSRF 防护。
- `ApprovalService`：生成一次性、精确作用域授权。
- `FileMutationBroker`：只执行获批的结构化文件动作，不运行整条 shell。
- `BackupVault`：保存递归删除备份并执行容量淘汰。
- `RuntimeLauncher`：解析 Gateway 内部 child role 和用户工具运行时。

审批和宿主机提权是两条独立决策链。用户批准 Safe 中的高风险动作后，
动作仍在 Safe 中执行；批准本身不能把整条命令改成 Host 执行。

## 4. 模式与旧内容兼容


### 4.1 新模型与协议版本

领域层只定义：

```text
RunMode.SAFE = "safe"
RunMode.FULL = "full"
```

所有新配置、数据库写入和前端领域类型只允许这两个值。全新安装默认
Safe；升级安装保留按旧字段上下文迁移后的偏好。没有客户端偏好时使用
Safe。有效 Token 授予选择 Full 的能力，但认证本身不会把一个正在运行
的 guest task 静默切到 Host。

“v2”只表示新的 sandbox schema，不冒充现有传输协议版本：

- 新 REST 资源为 `/api/v2/sandbox/*`，只收发 `safe/full`。
- 现有 `/api/v1` 保持旧 sandbox 载荷。
- WebSocket protocol 1–3 使用 legacy codec。
- 新 canonical sandbox 载荷通过 WebSocket protocol 4 发送。
- 协商后的 WS protocol 必须保存在 `WsConnection` 和 `RpcContext`。
- RPC 响应、事件和广播按每个连接的 protocol 编码，不能先生成一份
  全局载荷再发给所有连接。
- 载荷同时带 `sandboxSchemaVersion`，避免以后把传输协议和业务 schema
  再次耦合。

### 4.2 单向旧格式翻译

旧名称仅存在于隔离的兼容模块和迁移测试夹具中：

| 字符串输入 | 新模式 |
|---|---|
| `standard`, `standard-sandbox`, `on`, `off` | `safe` |
| `trusted`, `trusted-sandbox`, `trust`, `managed` | `safe` |
| `full`, `full-host-access`, `bypass` | `full` |

字符串表只用于显式 run-mode 字段和旧命令参数，不能脱离字段上下文迁移
布尔配置。

兼容合同：

- canonical REST、WS protocol 4 和新配置永远不返回旧名称。
- REST v1 和 WS protocol 1–3 继续接收旧名称。
- legacy 输出把 Safe 的当前偏好编码为 `trusted`，把 Full 编码为 `full`。
- legacy allowed-modes 可继续包含 `standard/trusted/full`；其中两个旧
  沙箱选项都进入同一个 Safe。
- 旧 `standard` 和 `trusted` 行为不再分别模拟；升级后统一使用新版
  Safe 策略。
- 历史事件原始载荷保持不可变，读取和恢复时经过 codec。
- 业务代码不得导入旧模式枚举或根据 `trusted/standard` 分支。

旧 CLI 合同：

- `sandbox on`、`sandbox trust` 和旧 standard/trusted 拼写设置 Safe。
- `sandbox full`、`sandbox bypass` 设置 Full。
- `sandbox reset` 清除偏好，随后使用新版默认 Safe。
- 旧命令保留原 exit code，作为隐藏别名继续工作。
- 旧 `sandbox status --json` 保留旧字段和旧 mode 编码，并新增可忽略的
  `canonical_run_mode`。
- `sandbox status --json --schema-version 2` 只返回 canonical 字段。

旧配置迁移按 schema、字段路径和组合执行：

| 优先级 | 旧字段条件 | 新模式 |
|---|---|---|
| 1 | 显式 `sandbox.run_mode` 为 standard/trusted/on/off 族 | Safe |
| 1 | 显式 `sandbox.run_mode` 为 full/bypass 族 | Full |
| 2 | 显式 `permissions.default_mode` 为 `full` 或 `bypass` | Full |
| 3 | 显式布尔 `sandbox.sandbox=false` | Full |
| 4 | `sandbox.sandbox=true`，不论旧 grading 开关 | Safe |
| 5 | 旧 sandbox/grading 字段全部缺失 | 保留旧版本推导的 Full |
| 6 | 其他可识别的旧 off/on/restricted 组合 | Safe |
| — | 未知值或矛盾字段 | 迁移失败，不默认 Full |

显式 `run_mode` 高于推导布尔值。新版 fresh-install 配置由新 schema 直接
写 Safe，不经过“旧字段全部缺失”的迁移行。


### 4.3 直接更新的数据迁移

直接覆盖安装必须支持已有：

- Gateway TOML/JSON 配置。
- 桌面偏好。
- WebUI localStorage/IndexedDB 中的运行模式和连接偏好。
- session、scheduler、approval、user-grants 等 SQLite/JSON 存储。
- 会话、历史任务、定时任务和待恢复任务。
- 单一旧 Token、环境变量 Token、operator/admin token 和 node token。

迁移由 Gateway 启动前协调器负责，而不是等 Gateway 启动后再通过 RPC
补救：

1. Desktop 在启动 Gateway 前运行迁移协调器；source、venv、wheel/CLI
   在服务入口启动前运行同一个协调器。
2. 协调器盘点全部数据源和 schema，执行磁盘空间预检。
3. SQLite 使用 backup API；外部配置和 JSON 使用原始字节复制。
4. 快照先写 staging、校验，再原子发布并轮换旧快照。
5. 升级 journal 记录每个存储的 prepared/committed 状态，使中断后可
   重入；不能伪装成跨多个数据库的一次 SQL 事务。
6. 快照使用仅当前用户和迁移器可读的 OS ACL/file mode，因为其中可能
   包含可恢复的旧 Token。
7. 快照与递归删除 Backup Vault 分开，只保留最近一次升级快照。
8. 外部配置读取时兼容旧字段，不因一次读取而重写整个文件。
9. 用户保存相关设置后只写新名称并保留不相关、未知字段。
10. 迁移失败时停止 Gateway 和其他写入者，保留 journal、诊断和快照。
11. 独立于 Gateway 的 Desktop recovery window 和 CLI recovery 命令
    提供导出诊断与手动恢复。
12. 本期不自动恢复升级前快照。

升级成功不仅是应用能够打开，还包括旧任务可显示、旧定时任务可恢复、
旧角色和 scope 未意外丢失、旧 Token 可认证、旧 CLI 自动化和旧
REST/WS 客户端仍能完成原本获准的操作。

## 5. Principal、内网与 Token

### 5.1 来源范围

内网入口只接受实际 socket peer 位于：

- IPv4 loopback。
- `10.0.0.0/8`。
- `172.16.0.0/12`。
- `192.168.0.0/16`。
- IPv6 loopback。
- IPv6 ULA `fc00::/7`。

公网来源在认证前直接拒绝。Gateway 不信任 `X-Forwarded-For`、
`Forwarded` 或其他客户端可伪造的转发头。用户可在设置中进一步缩小
`allowedClientCidrs`，但默认覆盖上述内网范围。


### 5.2 Principal 与 capability

| Principal | 可用模式 | 宿主机读取 | 新增管理权限 |
|---|---|---|---|
| 本机 owner | Safe、Full | Safe 中允许 | owner 原有权限 |
| 新具名人类 Token | Safe、Full | Safe 中允许 | 无 |
| 旧 operator/admin Token | Safe、Full | Safe 中允许 | 保留旧 scopes |
| node/service Token | 由原 scopes 决定 | 默认无 | 不自动增加 |
| 内网访客 | `guest_safe` | 不允许 | 无 |

Full 由独立 `host.execute` capability 控制，不通过伪造 `is_owner` 实现。
新创建的人类 Token 默认得到 `task.submit`、`task.read` 和
`host.execute`，但没有 `settings.write` 或 `token.manage`。旧
operator/admin Token 保留已有 role/scopes，并为原本可提交人类任务的
Token 增加 `host.execute`；node/service token 不因升级自动获得 Host。

`guest_safe` 不是用户可见的第三种模式。它是认证前的受限 Safe profile：

- 只挂载任务临时工作区和启用的随包运行时。
- 不挂载宿主机 HOME、项目外路径或宿主机环境机密。
- 不注入 Gateway Token、云凭据或宿主机敏感环境变量。
- 应用 Safe 的命令和网络策略。
- 沙箱不可用时禁止执行，不得回退 Host。

### 5.3 无 Token 与错误 Token

无 Token 和错误 Token 得到相同的 `guest_safe` 执行权限。两者只在
认证状态和交互上不同：

- 无 Token：状态为正常访客。
- 错误 Token：状态为认证失败，并参与失败限速。
- 错误 Token 不会获得更少或更多的 guest 执行权限。
- 若请求明确要求 Full 而认证失败，该请求不静默改在 guest workspace
  中执行；客户端必须明确改用访客 Safe 后重试。

这是一项明确的产品决定，不代表“认证成功”。审计日志仍记录失败，
Token 猜测仍限速，Host capability 始终为 false。

### 5.4 具名 Token 与旧 Token

新 Token 格式为：

```text
osq_<public-id>_<secret>
```

服务端记录：

- token version、名称和公开 ID。
- secret 摘要。
- roles、scopes 和 capabilities。
- source kind：named、legacy-config、legacy-env、node/service。
- 创建、最近使用、最近来源和撤销状态。

完整新 Token 只显示一次。摘要使用恒定时间比较。失败认证按 socket
peer 和公开 ID 双重限速：每个维度每分钟允许 5 次突发失败，随后使用
1、2、4、8、16、30 秒递增延迟，最大 30 秒。响应不透露 ID 是否存在。

升级前的 opaque Token 自动显示为“旧版兼容”：

- 配置文件 Token 在快照完成后迁入摘要存储，旧明文配置字段安全移除。
- 环境变量 Token 不复制到磁盘；运行时为它建立 legacy adapter。
- v1 按完整 opaque secret 校验，并使用固定 legacy rate-limit ID。
- 旧 roles/scopes 原样迁移；旧 admin 权限不会悄悄撤销。
- 用户可在本机设置中查看旧 Token 的权限并主动换成最小权限具名 Token。

### 5.5 新旧传输方式

- canonical REST 使用 `Authorization: Bearer`。
- WS protocol 4 使用连接后的首个认证帧，不把 Token 放进 URL。
- REST v1 和 WS protocol 1–3 继续接受旧传递方式，但仅限真实内网。
- WS 第一条 task 消息前必须确定 principal；尚未认证就按 guest。
- 所有访问日志、错误和诊断移除 query/header 中的 Token。

本期不启用 TLS。设置页持续说明 Token 可能被局域网监听、重放或中间人
获取，但不阻止用户启用内网访问。


## 6. 沙箱能力探测与软着陆

`SandboxCapabilityService` 使用同一生命周期覆盖所有环境：

- Desktop 在 Gateway 和 scheduler 接收任务前探测。
- source、venv/uv、wheel/CLI 在第一次 Safe 任务前探测。
- 探测默认 5 秒超时，并清理所有 child、临时 mount、账户和代理。
- 结果按 backend、应用版本、OS build、策略能力指纹在当前进程缓存。
- 用户“重新检测”、后端设置变化或相关环境指纹变化会使缓存失效。

真实探测至少验证：

- 后端能创建并退出最小沙箱进程。
- Gateway 内部 filesystem worker 能通过正确 child role 启动。
- 一次允许读取和一次受控写入符合预期。
- 网络代理或完全断网模式可按策略建立。
- OS 账户、ACL、mount、seatbelt、bwrap/WFP 等资源真实可用。
- 必需的 `denyWriteCarveout` 和 `authorityDenyRead` 可用。
- `FileMutationBroker` 可取得 `stableObjectIdentity` 并执行精确动作。
- 可选的 `scopedNativeGrant` 能力是否存在被如实报告。

响应至少包含：

```text
available
backend
platform
code
reason
setupSupported
restartRequired
probeVersion
capabilities[]
```

不得用“setup 状态为 Ready”推断运行时一定可用。不能强制黑名单
deny-write 或不能强制内部 authority deny-read 的后端必须报告 Safe
不可用，不能退化成字符串扫描或同 UID 普通权限。缺少
`scopedNativeGrant` 不会禁用整个 Safe，只会使 native/shell 的黑名单
写入保持拒绝；获批结构化动作由 broker 完成。

ModeResolver 同时保留 `desiredMode` 和 `effectiveMode`。

### 6.1 可用

- Safe 正常可选。
- 用户上次模式保持不变。
- 不显示额外状态。

### 6.2 不可用

- 普通模式选择器只把 Safe 置灰，不显示红色、原因、横幅或角标。
- 本机 owner 和具有 `host.execute` 的 Token 可按已确认产品策略临时
  使用 Full；持久化 `desiredMode` 仍是 Safe。
- `guest_safe` 返回 `sandbox_unavailable_for_guest`，绝不回退 Host。
- 每个任务状态和协议响应都记录 `desiredMode=Safe`、
  `effectiveMode=Full` 和失败指纹，不能伪装成 Safe。

Desktop 在用户处理启动提示前暂停 scheduler 和新任务分发；已选择
“不再提醒”的同一失败指纹可直接继续。交互式 CLI 显示同等提示；
非交互 CLI 必须带 `--allow-host-fallback` 或已持久化的同指纹授权。
远程 Token 客户端从 hello/status 得到 effective mode，并在第一次
fallback task 前确认；确认可按 Token 和失败指纹保存。

REST v1 和 WS protocol 1–3 不认识该确认协议，因此绝不为旧连接静默
Full。旧连接在 Safe 可用时完全兼容；Safe 不可用时，只有 owner 已通过
新版设置或 CLI 按 Token+失败指纹预授权 fallback 才能继续，否则返回旧
客户端可显示的现有 approval/error 形态。直接更新后的新版客户端使用
canonical 确认流程。

启动提示按钮为“我知道了”和“不再提醒”：

- “我知道了”只抑制当前启动或连接。
- “不再提醒”按应用主版本、平台、后端和规范化失败指纹保存。
- 不保存原始路径、完整堆栈或敏感错误文本。
- 沙箱恢复成功后 suppression 自动失效。
- 设置中提供“恢复沙箱提醒”。

### 6.3 运行中失败

软着陆只发生在任务开始前已经确认 Safe 不可用时。若执行中失败：

- 停止当前动作。
- 不自动切换 Host。
- 不自动重放可能已有副作用的命令。
- 返回可诊断错误，由用户决定是否在 Full 中重新执行。

## 7. 文件策略

### 7.1 读取

已认证 Safe 中，OpenSquilla 不对普通宿主机文件读取增加用户可配置
白名单或审批；操作系统 DAC/ACL 自身仍然有效。唯一固定例外是不能向
Safe 任务暴露可把 Safe 升级为 Full 或破坏恢复链的 OpenSquilla 内部
authority/recovery 数据：

- Token、ownership 和本地 session authority store。
- 升级快照和 migration journal。
- Backup Vault 数据与索引。
- 一次性授权和策略签名材料。

这些内部 deny-read/deny-write 路径不出现在用户黑名单里，也不能关闭。
Linux 使用 mount namespace 隐藏/替换这些路径，macOS 使用 Seatbelt
file-read deny，Windows 使用独立 sandbox identity 与 ACL。能力探测会
实际尝试读取每类 authority canary；任一成功都使 Safe 不可用。
UI 必须明确说明：

> 除 OpenSquilla 自身的权限和恢复资料外，安全模式允许读取当前用户
> 本来可以读取的文件；敏感路径黑名单只保护修改和删除，不提供一般
> 凭据防泄漏保证。

`guest_safe` 仍只能读取临时工作区和随包运行时。

### 7.2 修改判定

以下动作都属于 mutation：

- 新建、覆盖、追加、截断。
- 删除文件或目录。
- 递归删除。
- 移动、重命名或原子替换。
- 创建硬链接或符号链接。
- 修改 ACL、所有者或可能改变保护边界的权限位。

源路径和目标路径都必须检查。匹配前执行物理路径规范化：

- 展开受支持的环境变量和 HOME。
- 解析 `.`、`..`、符号链接和 junction。
- Windows 处理大小写、UNC、`\\?\`、8.3 短路径和 ADS。
- POSIX 处理 bind mount、mount point 和符号链接循环。
- 规则匹配最终物理目标，同时保留用户输入路径用于展示。

Safe 中：

- 读取：自动允许。
- 黑名单外 mutation：自动允许。
- 黑名单内 mutation：每次强制询问。

黑名单审批不提供“始终允许”。命令自动放行前缀不能绕过文件审批。

### 7.3 内置黑名单

内置规则不能编辑、删除或禁用。用户可以新增、编辑和删除自定义规则。
`**` 表示目录子树。

Windows 初始规则：

```text
%USERPROFILE%\.ssh\**
%USERPROFILE%\.aws\**
%USERPROFILE%\.kube\config
%USERPROFILE%\.docker\config.json
%USERPROFILE%\.docker\daemon.json
%USERPROFILE%\.netrc
%USERPROFILE%\.npmrc
%USERPROFILE%\.pypirc
%USERPROFILE%\.gem\credentials
%USERPROFILE%\.config\gh\hosts.yml
%USERPROFILE%\.git-credentials
%USERPROFILE%\.config\gcloud\**
%USERPROFILE%\.azure\**
%USERPROFILE%\.terraform.d\credentials.tfrc.json
%APPDATA%\Microsoft\Protect\**
%APPDATA%\Microsoft\Credentials\**
%LOCALAPPDATA%\Microsoft\Credentials\**
```

macOS 初始规则：

```text
$HOME/.ssh/**
$HOME/.aws/**
$HOME/.kube/config
$HOME/.docker/config.json
/etc/docker/daemon.json
$HOME/.netrc
$HOME/.npmrc
$HOME/.pypirc
$HOME/.gem/credentials
$HOME/.config/gh/hosts.yml
$HOME/.git-credentials
$HOME/.config/gcloud/**
$HOME/.azure/**
$HOME/.terraform.d/credentials.tfrc.json
$HOME/.gnupg/**
$HOME/.password-store/**
$HOME/Library/Keychains/**
/Library/Keychains/**
/etc/sudoers
/etc/sudoers.d/**
/etc/ssh/**
/etc/pam.d/**
/Library/LaunchDaemons/**
```

Linux 初始规则：

```text
$HOME/.ssh/**
$HOME/.aws/**
$HOME/.kube/config
$HOME/.docker/config.json
/etc/docker/daemon.json
$HOME/.netrc
$HOME/.npmrc
$HOME/.pypirc
$HOME/.gem/credentials
$HOME/.config/gh/hosts.yml
$HOME/.git-credentials
$HOME/.config/gcloud/**
$HOME/.azure/**
$HOME/.terraform.d/credentials.tfrc.json
$HOME/.gnupg/**
$HOME/.password-store/**
$HOME/.local/share/keyrings/**
$HOME/.config/containers/auth.json
/etc/shadow
/etc/gshadow
/etc/sudoers
/etc/sudoers.d/**
/etc/ssh/**
/etc/pam.d/**
/root/**
```

OpenSquilla authority store、Backup Vault、升级快照、迁移 journal 和
策略签名材料属于内部不可移除的 deny-read/deny-write 路径，防止 Safe
任务读取宿主机授权秘密或篡改恢复链。

规则语义中 `dir/**` 同时匹配 `dir` 自身和全部后代。删除一个黑名单
祖先目录时，只要其下包含受保护对象，就视为命中并列出受保护目标。


### 7.4 后端强制执行与结构化 broker

文件黑名单不能只依赖命令扫描。每个平台后端必须强制：

- 黑名单外默认写入。
- 内置、自定义黑名单和 authority/recovery 路径不可被 sandbox process
  写入。
- authority/recovery 路径不可被 sandbox process 读取。

用户批准黑名单 mutation 后，不要求任意 native 进程获得临时 syscall
例外。首版执行模型为：

- OpenSquilla 结构化文件工具把确切动作交给 `FileMutationBroker`。
- 可静态识别的独立 shell 文件动作先转换为结构化动作，不再运行原始
  shell 删除/覆盖部分。
- broker 只执行获批的 open/write/rename/unlink，不接受任意命令文本。
- 复合 shell、动态脚本或不透明 native 程序仍由 OS deny-write 拦截。
- 后端若额外支持可证明安全的 `scopedNativeGrant`，可以启用；它不是
  Safe 首版的可用性前提。

一次性 broker grant 绑定：

- principal、session、任务 ID 和策略版本。
- 规范化物理路径。
- Windows file ID / POSIX device+inode 等稳定对象身份。
- 父目录 handle/identity。
- mutation 类型、工具调用摘要和 cwd。
- 过期时间和单次消费状态。

broker 在实际 open/rename/unlink 前通过 handle 重新解析和比对对象及
父目录，拒绝 reparse point/symlink/junction 在审批后的 TOCTOU。

若后端不能强制 deny-write/authority-deny-read，则 Safe 不可用。若
broker 不能安全表达获批动作，返回
`backend_cannot_scope_file_grant`；不得运行整条 Host 命令。运行时才被
native deny-write 拦截的复合动作不自动重放，用户可改用结构化工具或
显式 Full。

## 8. 递归删除和备份

本节只适用于 Safe；Full 不应用沙箱文件规则或 Backup Vault。Safe
不可用而软着陆 Full 时，状态必须明确指出递归删除备份也不会生效。

### 8.1 审批提示

Safe 中所有已识别的递归删除，无论是否命中黑名单，都必须审批。提示
显示路径、数量、大小、黑名单命中、备份状态、容量和恢复能力。

- 备份成功：强调“原位置将被递归删除；可从这份备份恢复”。
- 备份关闭或失败后选择继续：强调“永久删除且不可撤回”。
- 默认按钮始终是“取消”。

### 8.2 Backup Vault

- 默认开启，默认上限 3 GB，可在沙箱设置中修改。
- 容量按备份实际磁盘占用计算。
- 每份记录原始路径、时间、任务、大小、校验和和操作摘要。
- 设置提供打开备份目录和按记录恢复；覆盖现有目标必须确认。

备份先写 staging 并校验完整性。系统在删除任何旧备份前完成空间预检，
计算需要淘汰的完整记录；只有新 staging 可提交时才原子发布新备份并
淘汰最旧记录。新备份最终失败时不得先清空旧备份。

同卷、独立的结构化删除优先使用原子 quarantine move。跨卷或复合命令
使用 copy、校验、再删除；不跟随目标外符号链接，不越过未明确指定的
mount。

### 8.3 超出容量

若单个目标超过上限，或无法在安全预检后完整保存：

1. 不声称备份成功。
2. 显示原因和缺少容量。
3. 默认取消。
4. 用户可经过第二次确认后永久删除。

### 8.4 首版保证范围

首版保证 OpenSquilla 结构化工具、目标可确定的 shell 递归删除和执行前
可确定目标的随包 Python/Node 操作。不透明原生程序或动态目标不承诺
删除前备份；黑名单写保护仍由系统沙箱强制。文档不得描述为文件系统
级快照。

## 9. 命令策略

### 9.1 默认行为

Safe 中普通命令默认自动执行，不拦截、不询问。内置高风险目录至少覆盖：

- 所有 `git push`，包括 force push 和远端分支、tag 删除。
- package publish/unpublish。
- 生产部署、发布和回滚触发。
- 远端数据库不可逆变更。
- 云资源 destroy/delete。
- 其他可识别的远端、不可撤销写入。

批准后命令仍在 Safe 中运行。


### 9.2 自定义前缀

设置提供 `requireApprovalPrefixes` 和 `autoAllowPrefixes`。

先执行独立的结构性规则：

1. `systemTools=disabled` 是不可被前缀覆盖的禁用。
2. 文件黑名单和递归删除审批独立判定。
3. 其余命令按用户已确认的优先级：
   - 用户自动放行前缀。
   - 用户审批前缀。
   - 内置高风险审批。
   - 默认自动执行。

每张列表内部选择 token 数最多的最具体匹配；若 auto-allow 与 approval
列表都命中，即使 approval 更具体，仍按产品约定由 auto-allow 优先。
UI 在保存宽泛 auto-allow 时展示它将覆盖哪些审批规则。同一个规范化
前缀不能同时存入两张列表。

### 9.3 匹配语义

规则匹配命令 token 前缀，而不是任意子串或宽泛 glob：

- 规范化 executable 路径。
- Windows 比较忽略大小写并规范 `.exe`。
- 识别 `env`、`sudo`、`cmd /c`、PowerShell 和 shell `-c` 等 wrapper。
- 复合命令按 shell 控制边界拆分后逐段判断。
- 前缀规则本身拒绝管道、重定向、命令替换和不明确的 shell 元字符。
- 前台和后台执行必须走同一策略路径。

### 9.4 系统级工具

设置提供“自动执行 / 需要审批 / 禁用”三态，默认“需要审批”。

Windows 初始目录包括 WSL、wmic、sc、reg、schtasks 等；macOS/Linux
使用对应的 launchctl、systemctl、crontab、sudo 等平台目录。该控制只
适用于 Safe。

## 10. 网络策略

网络策略只适用于 Safe 和 `guest_safe`。Full 使用宿主机网络。

### 10.1 设置

- `blockAllNetwork`，默认关闭。
- `allowDomains`。
- `denyDomains`。

默认关闭阻断时，合法公网目标自动允许；打开后默认拒绝，仅允许列表
作为例外。

### 10.2 决策顺序

1. 先执行不可覆盖的基础安全检查。
2. 分别寻找最具体的 allow 和 deny 规则。
3. 精确域名比通配域名具体。
4. 后缀更长的规则更具体。
5. 同等具体时 deny 优先。
6. 没有命中规则时，`blockAllNetwork` 决定默认允许或拒绝。

`*.example.com` 只匹配子域名，不自动匹配 apex `example.com`。用户需要
时分别添加。

### 10.3 基础防护

Safe 始终保留现有和必要的：

- 云元数据地址阻断。
- 回环、link-local 和私网探测阻断。
- DNS rebinding 防护。
- DNS 解析后地址重新校验。
- 每次 HTTP 重定向重新执行安全和域名规则。
- 安全的 FQDN/通配域边界验证。

### 10.4 传输覆盖

Safe 默认开放表示“不额外限制合法公网目标”，不是让进程绕过域名策略
直接使用宿主机 raw network。系统沙箱阻断直接出站，受管代理提供：

- HTTP/HTTPS 和任意公网 TCP 的 CONNECT。
- 使用域名形式请求的 SOCKS5 TCP。
- 为随包 npm、pip、Git HTTPS 和 Git SSH 注入代理环境或 ProxyCommand。
- 仅由受管 resolver 使用的 DNS。

首版不提供通用 UDP 转发。忽略代理设置的原生程序不能绕过域名规则；
它会得到结构化的 network-denied 错误，而不是静默直连。

这部分是 Safe 的出站策略，不影响内网客户端连接 Gateway。沙箱网络
不能直接访问 Gateway 的监听端口；Gateway ingress 和 sandbox egress
保持独立。


## 11. 随包运行时与发行矩阵

### 11.1 首期必须交付的桌面矩阵

本功能不能通过“不把 Linux 标成正式平台”规避三平台目标。首期完成
矩阵明确为：

- Windows x64。
- macOS arm64 和 x64。
- Linux x64，glibc 基线不高于 2.28。

Windows arm64 和 Linux arm64 不属于首期完成门槛，但 source、venv/uv、
wheel/CLI 的能力探测和启动协议保持架构无关。

### 11.2 发行内容

每个矩阵制品必须包含：

- Node.js LTS，含 npm、npx/corepack，来源为 Node 官方固定版本资产。
- 完整 standalone Python，含标准库、pip、venv，使用可复现的固定版本
  standalone distribution。
- Git 与 Bash。
  - Windows 使用 PortableGit，不使用 MinGit。
  - macOS/Linux 使用固定源码和工具链构建的 Git + Bash 资产。

每项资产记录平台、架构、来源、SHA-256、许可证和 SBOM。禁止首次使用
时才联网下载。Windows 所有嵌套可执行文件进入 Authenticode 签名和
恶意软件扫描；macOS 嵌套 binary/dylib 先签名再随应用 notarize；Linux
制品验证 glibc 基线和依赖闭包。

开关只控制任务环境是否暴露工具，不删除安装文件。

### 11.3 PATH 优先级

Safe 和 `guest_safe`：

```text
随包运行时 -> 明确挂载的项目工具 -> 允许的系统工具
```

Full：

```text
宿主机 PATH -> 随包运行时作为 fallback
```

随包目录只读。pip/npm 安装写入项目 venv 或任务缓存。

### 11.4 更新兼容与安全维护

- 运行时使用稳定安装路径。
- 应用补丁版本只更新同一 Python 主次或 Node LTS 主版本的安全补丁。
- 跨 Python 主次版本前扫描 venv，并提供检测和重建命令。
- 跨 Node ABI 前检测原生模块并提示重建。
- 更新不删除项目 venv、npm 缓存或用户 Git 配置。
- Full 的宿主机优先级不覆盖现有本地开发环境。
- 已公开利用的运行时关键漏洞目标 72 小时内发布修复；其他 critical
  漏洞目标 7 天内发布。

包体积预算按“相对不捆绑运行时的增量”计算：

| 平台 | 压缩下载目标 | 压缩硬上限 | 安装后硬上限 |
|---|---:|---:|---:|
| Windows | 90–140 MB | 160 MB | 450 MB |
| macOS | 50–100 MB | 130 MB | 400 MB |
| Linux | 50–100 MB | 130 MB | 400 MB |

运行时补丁造成的 blockmap/增量更新超过 80 MB 时需要发布负责人显式批准
并在 release note 说明。

### 11.5 制品门禁

每个矩阵制品在最终安装布局中执行：

- node、npm/npx。
- python、pip、venv 创建。
- git、Bash 和本地仓库操作。
- Safe 最小读、写、审批和网络操作。
- Gateway 内部 child role。
- Windows 签名、macOS notarization 或 Linux 依赖基线检查。

任何制品只会软着陆 Full 而不能真实启动 Safe，或缺少随包 runtime，
都必须阻止发布。

## 12. Gateway 内部子进程启动

新增显式 `ChildRole` 协议，例如：

- `filesystem-worker`。
- `network-proxy`。
- `capability-probe`。
- 后续经过审核的新内部角色。

所有后端只向 `RuntimeLauncher` 请求角色，不直接拼接
`sys.executable -m`。

解析规则：

| 环境 | 启动形式 |
|---|---|
| source、venv/uv、wheel/CLI | `python -m <role-module> ...` |
| PyInstaller/frozen Desktop | `gateway --internal-child <role> ...` |

桌面入口在导入正常 Typer CLI 前识别内部 child 参数，校验固定参数形状，
调用对应角色并原样返回退出码。未知角色、重复角色或额外参数失败关闭。

随包的用户 Python 与 Gateway 内部 child 启动完全分离。更换或关闭用户
Python 不得影响沙箱 worker。


## 13. 设置 UI 与保存边界

设置新增“沙箱”模块，只展示产品概念，不暴露旧底层布尔项。

模块结构：

1. 默认执行模式：Safe、Full。
2. 文件安全：读取说明、内置/自定义黑名单、递归备份和 3 GB 上限。
3. 命令安全：审批前缀、自动放行前缀、系统级工具三态。
4. 网络安全：阻断全部、允许域名、拒绝域名。
5. 内置运行时：总开关和 Python、Node.js、Git + Bash 单项开关/版本。
6. 内网监听和 CIDR。
7. 具名 Token。

运行时总开关关闭时保留三个单项值但全部不暴露；重新开启后恢复原单项
选择，因此仍满足三项独立启用。

页面不伪装成跨多存储的一次原子保存。事务边界明确为：

- 默认模式：通过现有 run-mode preference RPC 即时保存。
- 文件、命令、网络和 runtime exposure：单个版本化
  `SandboxPolicy`，使用 `basePolicyVersion` 乐观并发和一次原子写。
- LAN bind/CIDR：独立 Gateway 配置保存，明确要求受控重启。
- Token 创建、撤销和 scope 修改：凭据库中的即时安全事务。
- 启动提醒 suppression：Electron desktop preference 即时保存。

UI 分别显示每个边界的 dirty/saving/error/restart 状态。某一边界失败
不能显示成其他边界也失败或成功。策略保存后，新任务使用新快照，运行
中的任务继续使用旧快照。

Safe 不可用时，普通模式选择器只显示不可选中的 Safe，不使用红色，
不显示原因、常驻错误、横幅或角标。原因和重新检测入口只在沙箱设置的
诊断折叠区。


## 14. 配置与存储模型

```text
SandboxPolicy {
  schemaVersion
  policyVersion
  files {
    customDenyWritePaths[]
    recursiveDeleteBackupEnabled: true
    backupQuotaBytes: 3221225472
  }
  commands {
    requireApprovalPrefixes[]
    autoAllowPrefixes[]
    systemTools: auto | prompt | disabled
  }
  network {
    blockAllNetwork: false
    allowDomains[]
    denyDomains[]
  }
  runtimes {
    enabled
    python
    node
    gitBash
  }
}

ExecutionPreference {
  clientId
  desiredMode: safe | full
}

GatewayLanConfig {
  enabled
  bindInterfaces[]
  allowedClientCidrs[]
}

TokenRecord {
  tokenVersion
  publicId
  name
  secretDigest
  roles[]
  scopes[]
  capabilities[]
  sourceKind
  createdAt
  lastUsedAt
  revokedAt
}
```

更新 `SandboxPolicy` 必须提交 `basePolicyVersion`；版本不一致返回 conflict
并要求 UI 合并或刷新，防止两个客户端相互覆盖。

内置黑名单和内置高风险命令随应用发布，不复制到用户配置。Token store、
提醒 suppression、升级 journal/快照和 Backup Vault 各自独立。默认
mode 的唯一事实来源是 `ExecutionPreference`，不再让旧 sandbox/grading/
permissions 布尔项互相推导。

## 15. 错误处理

错误必须结构化并按阶段区分：

- `sandbox_capability_unavailable`：任务开始前 Safe 不可用。
- `host_fallback_confirmation_required`：需确认 desired/effective mode 差异。
- `sandbox_unavailable_for_guest`：访客不能软着陆。
- `sandbox_authority_read_denied`：任务试图读取内部 authority/recovery。
- `backend_cannot_scope_file_grant`：后端无法安全表达精确文件授权。
- `sensitive_file_mutation_requires_approval`。
- `recursive_delete_requires_approval`。
- `recursive_backup_failed`。
- `recursive_backup_too_large`。
- `command_requires_approval`。
- `network_target_denied`。
- `runtime_unavailable`。
- `internal_child_launch_failed`。
- `auth_invalid_guest_only`。
- `migration_failed_manual_recovery_required`。
- `policy_version_conflict`。
- `legacy_protocol_encoding_failed`。

默认行为：

- 配置校验失败：不保存任何部分，并把错误定位到字段。
- 审批取消或超时：不执行动作。
- 备份失败：默认不删除；只有容量不足路径允许二次确认永久删除。
- 沙箱内已开始执行后失败：不自动重放、不自动 Host fallback。
- 旧值无法识别：保留原始数据并停止相关恢复，不静默映射为 Full。
- 内部 child 失败：记录角色、退出码和脱敏原因，不记录 Token 或敏感路径
  内容。

## 16. 测试与验收

### 16.1 单元测试

- 所有旧 run-mode alias 和按字段上下文的旧布尔真值表。
- 业务模块不再依赖旧模式枚举。
- REST v1、WS protocol 1–3 和 protocol 4 的逐连接 codec/广播。
- principal × requested/effective mode × capability 矩阵。
- 旧 operator/admin/node roles/scopes 与 `host.execute` 迁移。
- 无 Token/错误 Token 权限相同、状态不同且 Full 请求不静默降级。
- 恒定时间 Token 摘要比较和失败限速。
- 路径规范化、symlink/junction、UNC、8.3、ADS 和 mount 边界。
- object/parent identity 重校验及 symlink/junction TOCTOU 竞争测试。
- 三平台 `authorityDenyRead` canary 与 deny-write capability probe。
- authority/recovery 路径 deny-read/deny-write。
- 无 `scopedNativeGrant` 时结构化 broker 可用、native 写保持拒绝。
- 文件规则、命令规则和递归删除规则的独立优先级。
- tokenized 命令前缀与 wrapper/复合命令解析。
- 域名 exact/wildcard、具体度、deny tie-break 和 redirect。
- 3 GB 默认值、quota、staging 失败不淘汰旧备份和超大目标。
- `basePolicyVersion` 并发冲突与各保存边界的独立失败。
- Desktop/source/venv/wheel capability cache、超时、失效和清理。
- source/frozen `RuntimeLauncher` 启动参数。

### 16.2 集成测试

- Safe 中黑名单外写入自动通过。
- 黑名单内修改每次审批且批准后仍在沙箱。
- 命令 auto-allow 不绕过文件或递归删除审批。
- `git push` 默认审批，用户前缀按固定优先级覆盖。
- guest workspace 无法读取宿主机 HOME 和敏感环境变量。
- Safe 无法读取 OpenSquilla authority、Vault 和升级快照。
- Safe 网络默认开放公网，同时拦截 metadata、loopback、私网和 DNS
  rebinding；Git HTTPS/SSH 通过受管 TCP 代理工作。
- Full 绕过沙箱设置。
- 沙箱启动前不可用时，本机/有效 Token 软着陆，guest 拒绝。
- 执行中失败不自动重放。


### 16.3 直接更新矩阵

为每个已发布应用/数据 schema 保存由真实旧二进制生成的完整 fixture，
不能用单表合成数据代替：

```text
旧版创建 config/session/scheduler/approval/user-grants/Token
  -> 直接覆盖安装新版
  -> pre-Gateway migration coordinator
  -> 打开历史内容和恢复任务
  -> 验证旧 roles/scopes 与 opaque Token
  -> REST v1、WS protocol 1–3、旧 CLI 自动化
  -> canonical 设置保存
```

更新 manifest 增加：

```text
minSourceVersion
dataSchema
migrationSet
runtimeSet
sandboxSchemaVersion
```

覆盖 standard/trusted/full、旧布尔组合、字段全缺失、未知字段、环境变量
Token、admin/operator/node Token、迁移中断重试和独立 recovery UI/CLI。

### 16.4 Packaged end-to-end

Windows x64、macOS arm64/x64 和 Linux x64 必须在安装后的真实资源
布局中完成：

- capability probe。
- filesystem worker 启动。
- 真实 Safe 读写和黑名单拒绝。
- 审批后的精确授权。
- 网络开放、拒绝和 block-all。
- Node/Python/Git/Bash smoke。
- runtime manifest、包体积预算、Windows 签名、macOS notarization 和
  Linux glibc 基线。
- 桌面 UI 两态显示、Safe 置灰和启动提醒 suppression。

测试不得把“自动回退 Full”当作 Safe 测试通过。只要期望 Safe 的用例进入
Host，发布门禁即失败。

## 17. 实施工作流与门禁

本设计是跨模块的共同安全合同，实施拆成五个有依赖的工作流：

1. 兼容基础：pre-Gateway migrator、protocol 4/per-connection codec、
   canonical mode 和 `RuntimeLauncher`。
2. 身份基础：principal capability、guest_safe、具名/legacy Token。
3. 文件基础：三平台 deny-write/authority-deny-read、
   FileMutationBroker、可选 native grant 和 Backup Vault。
4. 策略扩展：命令前缀、系统工具、域名策略和受管 TCP 代理。
5. 产品交付：设置 UI、随包 runtime、三平台签名和 packaged CI。

后续工作流不能通过临时 Host fallback 伪造前置工作流完成。每个工作流
完成自己的 migration 和 packaged tests 后才能进入下一交付门禁；最终
仍作为一个用户可见功能发布，避免出现半套 Safe 设置。

## 18. 完成标准

本设计完成的可观察结果是：

- UI 中不存在托管模式或旧名称。
- 新配置、canonical REST 和 WS protocol 4 只产生 `safe/full`。
- 旧内容直接更新后仍可读取和继续使用。
- 赛车游戏等桌面任务不再因 `gateway.exe -m` 启动错误卡死。
- Safe 正常时文件、命令和网络策略按本设计执行。
- Safe 异常时模式置灰且按 principal 正确软着陆或拒绝。
- 未认证内网用户不能读取宿主机，但仍可使用访客沙箱。
- 有效具名 Token 可使用宿主机权限但不能管理 owner 设置。
- 递归删除明确警告，默认先备份，并在 3 GB 上限内淘汰最旧内容。
- 每个正式平台制品随包提供并验证 Node.js、Python、Git 与 Bash。
