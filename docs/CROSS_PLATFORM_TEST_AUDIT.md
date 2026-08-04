# 标擎 BidPilot 跨平台测试与审计报告

报告日期：2026-08-02

审计对象：`gitchw/bidpilot` 主分支跨平台终版

结论：Windows、Linux、macOS 自动化矩阵全部通过；Debian systemd 与 Ubuntu 容器均完成真实运行验证。

## 1. 执行摘要

本轮修复前，Windows 3.11/3.13 通过，Ubuntu 与 macOS 均在同一项 CLI 测试失败。日志证明业务安全拒绝已经生效，失败来自 Rich/Typer 在 POSIX 终端插入 ANSI 样式码，测试对裸 `--host` 的字符串比较不具备跨平台稳定性。

修复后：

- Windows 本地 288 项 Pytest 全通过；
- Debian 13 / Python 3.13 原生目录 288 项 Pytest 全通过；
- Debian WSL2 `systemd 257` 真实启动 Web/worker，两个单元均为 `active`，API 与 CLI 均确认 worker 在线；
- GitHub Actions 7 个作业全部通过：Windows/Ubuntu/macOS × Python 3.11/3.13，以及 Ubuntu Docker Compose/镜像构建；
- Ruff lint、Ruff format、compileall、前端 JavaScript、JSON/YAML/TOML 与 Compose 模型均通过。

## 2. 失败根因与修复

### 2.1 原始失败

测试 `test_local_mode_rejects_non_loopback_host_override` 要求：当系统处于仅本机模式时，`serve --host 0.0.0.0` 必须拒绝启动，并提示使用 `BIDPILOT_NETWORK_ACCESS_MODE=lan`。

Linux/macOS 输出包含 ANSI 样式序列，视觉上仍是 `--host`，但程序捕获的字符串不再连续。Windows runner 未启用相同样式，因此只有 Windows 通过。

### 2.2 修复原则

- 不放宽安全规则；仍拒绝本机模式绑定非回环地址。
- 不把测试改成只看退出码；继续核验两条关键用户提示。
- 在断言前统一剥离 ANSI 终端样式，核验跨平台一致的可见语义。

### 2.3 加固项

- CI 新增格式检查、`compileall`、结构化配置校验；
- Ubuntu 独立运行 `docker compose config --quiet` 与 Linux 镜像构建；
- Actions 升级到官方 v7 主版本，消除旧 Node 运行时弃用告警；
- systemd 单元加入最小权限、专用用户、只读系统、私有临时目录和严格写目录；
- 千里马增加 `auto/visible/headless` 策略，区分首次可见登录与后台已验证配置复用。

## 3. 验证矩阵

| 平台 | Python | 范围 | 结果 |
|---|---:|---|---|
| Windows 本机 | 3.11 | 288 项测试 + 全部静态检查 | 通过 |
| Debian 13 WSL2 | 3.13.5 | 288 项测试 + lint/format/compile/config | 通过 |
| Debian 13 WSL2 | 3.13.5 | 真实 systemd Web/worker、健康与心跳 | 通过 |
| GitHub Windows | 3.11 / 3.13 | 完整 CI | 通过 |
| GitHub Ubuntu | 3.11 / 3.13 | 完整 CI | 通过 |
| GitHub macOS | 3.11 / 3.13 | 完整 CI | 通过 |
| GitHub Ubuntu | Docker | Compose 模型 + 镜像构建 | 通过 |

GitHub Actions 证据：`cross-platform-ci` run `30736770753`，结论 `success`，提交 `d4d5671`。

## 4. 登录来源审计

### 4.1 中国招标投标网

- 用户本人登录后，仅保存允许域名的加密会话；
- 搜索与至少一条免费会员详情同时真实解锁，才进入 `authorized`；
- 捕获任意 Cookie、零候选或无法判断均不能冒充授权成功；
- Cookie 仅发送到严格 HTTPS 与来源白名单，不随重定向泄露到其他域名。

### 4.2 千里马

- 真实用户登录页已验证“服务器”首屏可见 20 条免费会员列表结果；
- 普通即时任务保留 3 条真实 `auth_level=free_member` 记录；
- 不导出 Cookie，不把 Cookie 交给 HTTP 客户端；
- 仅即时、单主题、一次首屏、最多 20 条、10 秒冷却；
- 定时任务继续只用公开分类，不翻页、不访问付费详情；
- Linux 后台只允许复用已经通过可见登录与真实测试的持久浏览器配置。

## 5. systemd 真实运行记录

验证环境：WSL2 Debian，Linux 6.6.87.2，systemd 257，Python 3.13.5。

关键结果：

```text
Web 服务        在线 · v0.8.0 · http://127.0.0.1:8000
长期任务 worker 在线
数据库          /opt/bidpilot/data/bidpilot.db
报告目录        /opt/bidpilot/outputs/reports
User=bidpilot
Group=bidpilot
ActiveState=active
SubState=running
systemd smoke test passed
```

测试结束后脚本停止服务并删除临时单元、用户与测试目录，没有遗留后台进程。

## 6. 竞争交付核验映射

| 官方要求 | 证据 |
|---|---|
| 自然语言识别主题/地域/时间/频率 | 意图与混合意图测试、Web 确认快照 |
| 来源至少 2 个 | 10 个真实来源适配器，来源中心可见 |
| 至少 1 个免费登录来源 | 中国招标投标网与千里马均完成真实验证 |
| 内容清洗、去重与筛选 | 证据流水线、跨站聚类、硬过滤漏斗 |
| 标题/时间/链接/核心内容/附件 | 运行结果 Word 与结构化记录 |
| 定时与仅新增 | SQLite worker、版本账本、逐目标增量 Outbox |
| 完整代码与操作步骤 | 源码 ZIP、零基础手册、API/配置/用户文档 |
| 多问题完整 Demo | 3–5 分钟分镜覆盖正向、零结果、登录与订阅 |

## 7. 已知边界与风险披露

- 真实网站可能调整 DOM、WAF 或服务策略；每轮来源诊断必须保留，不能用缓存伪装在线成功。
- 普通 Webhook/SMTP 属于外部 `at-least-once`，外部已接收但本地回执前崩溃的极小窗口可能产生重复通知。
- 内置 LAN 模式不是公网多用户系统；公网必须增加独立身份、TLS、角色权限与审计。
- 千里马免费会员增强不能进入定时任务；这是一条主动遵守原站权益的硬边界。
- 本报告中的效率、覆盖率和转化指标是试点目标，不是既有客户业绩；需在超聚变真实基线中验证。

## 8. 审计结论

本轮跨平台失败已经从日志证据出发完成根因修复，并通过独立 Linux、官方云 runner 和真实 systemd/容器三层验证。系统已具备 Windows 演示、Linux 后台运行和 macOS 开发/验收能力；登录来源在不导出千里马 Cookie、不绕过付费权限的前提下保持可用。
