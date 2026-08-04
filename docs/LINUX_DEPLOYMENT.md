# 标擎 BidPilot Linux 部署与运维手册

版本：v0.8.0 跨平台终版

适用：Debian/Ubuntu 系 Linux 服务器、WSL2 systemd、Docker Engine

目标：让 Web/API 与长期任务 worker 在后台稳定运行，同时保留登录来源的真实权限边界。

## 1. 先选部署形态

| 形态 | 适用场景 | 生命周期管理 | 千里马免费登录 |
|---|---|---|---|
| systemd 原生部署 | 需要浏览器登录来源、便于运维审计 | `systemctl` | 支持；首次必须通过可见图形会话，随后可复用已验证持久配置做有界无头检索 |
| Docker Compose | 企业网关或标准 Web/worker | Compose `up/stop/down` | 镜像包含 Playwright Chromium；首次登录仍须受保护的可见图形会话 |
| 前台开发 | 本地调试与比赛演示 | CLI `serve/status/stop/restart` | 桌面环境可见授权 |

无论采用哪种形态，默认只监听 `127.0.0.1`。不要直接把 8000 端口暴露到公网。公网访问必须由反向代理或零信任网关补齐 HTTPS、身份认证、角色权限、限流与审计。

## 2. systemd 生产部署

### 2.1 系统准备

以下命令以 Debian/Ubuntu 为例，需由管理员执行：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv git
sudo useradd --system --home /opt/bidpilot --shell /usr/sbin/nologin bidpilot
sudo git clone https://github.com/gitchw/bidpilot.git /opt/bidpilot
sudo python3 -m venv /opt/bidpilot/.venv
sudo /opt/bidpilot/.venv/bin/python -m pip install --upgrade pip
sudo /opt/bidpilot/.venv/bin/python -m pip install /opt/bidpilot
```

若需要登录来源：

```bash
sudo /opt/bidpilot/.venv/bin/python -m pip install "/opt/bidpilot[auth]"
sudo env PLAYWRIGHT_BROWSERS_PATH=/opt/bidpilot/.playwright \
  /opt/bidpilot/.venv/bin/python -m playwright install-deps chromium
sudo install -d -o bidpilot -g bidpilot -m 0755 /opt/bidpilot/.playwright
sudo -u bidpilot -H env PLAYWRIGHT_BROWSERS_PATH=/opt/bidpilot/.playwright \
  /opt/bidpilot/.venv/bin/python -m playwright install chromium
```

系统依赖由 root 安装，Chromium 文件由 `bidpilot` 用户写入固定的 `/opt/bidpilot/.playwright`。不要使用 root 默认的 `~/.cache/ms-playwright`，否则 `ProtectHome=true` 的 systemd 服务通常无法读取。

### 2.2 目录与环境

```bash
sudo install -d -o bidpilot -g bidpilot -m 0700 \
  /opt/bidpilot/data /opt/bidpilot/outputs/reports
sudo install -d -o root -g bidpilot -m 0750 /etc/bidpilot
sudo install -o root -g bidpilot -m 0640 \
  /opt/bidpilot/deploy/systemd/bidpilot.env.example \
  /etc/bidpilot/bidpilot.env
```

编辑 `/etc/bidpilot/bidpilot.env`。推荐至少确认：

```dotenv
BIDPILOT_ENV=production
BIDPILOT_NETWORK_ACCESS_MODE=local
BIDPILOT_EMBEDDED_WORKER=false
BIDPILOT_DATA_DIR=/opt/bidpilot/data
BIDPILOT_CONTROL_DIR=/opt/bidpilot/data
BIDPILOT_DATABASE_PATH=/opt/bidpilot/data/bidpilot.db
BIDPILOT_REPORT_DIR=/opt/bidpilot/outputs/reports
BIDPILOT_QIANLIMA_BROWSER_MODE=auto
PLAYWRIGHT_BROWSERS_PATH=/opt/bidpilot/.playwright
BIDPILOT_QIANLIMA_MEMBER_MAX_PAGES=2
BIDPILOT_QIANLIMA_MEMBER_MAX_RESULTS=40
BIDPILOT_QIANLIMA_MEMBER_COOLDOWN_SECONDS=30
BIDPILOT_QIANLIMA_MEMBER_DAILY_QUERY_BUDGET=24
BIDPILOT_QIANLIMA_MEMBER_MONITORING_ENABLED=false
```

`auto` 的含义：桌面 Linux 使用可见浏览器；无 `DISPLAY/WAYLAND_DISPLAY` 的后台服务，仅在已经通过人工可见登录与真实验证后，用同一持久 profile 执行有界无头检索。它不会导出 Cookie，也不会把 Cookie 交给 HTTP 客户端。若已取得书面授权或官方 API 权利并需要会员监控，还须把开关改为 `true`，并设置 `BIDPILOT_QIANLIMA_MEMBER_MONITORING_AUTHORIZATION_REFERENCE` 为合同/API/变更记录编号；没有该记录时应用拒绝启动。

### 2.3 安装和启动服务

```bash
sudo install -o root -g root -m 0644 \
  /opt/bidpilot/deploy/systemd/bidpilot-web.service \
  /etc/systemd/system/bidpilot-web.service
sudo install -o root -g root -m 0644 \
  /opt/bidpilot/deploy/systemd/bidpilot-worker.service \
  /etc/systemd/system/bidpilot-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now bidpilot-web.service bidpilot-worker.service
```

验收：

```bash
systemctl is-active bidpilot-web.service bidpilot-worker.service
curl -fsS http://127.0.0.1:8000/health
sudo -u bidpilot /opt/bidpilot/.venv/bin/python -m bidpilot status
journalctl -u bidpilot-web.service -u bidpilot-worker.service --since today
```

预期：两个单元均为 `active`；健康接口返回 `status=ok`；CLI 显示 Web 与长期任务 worker 在线。

### 2.4 停止、重启与升级

```bash
sudo systemctl restart bidpilot-web.service bidpilot-worker.service
sudo systemctl stop bidpilot-worker.service bidpilot-web.service
sudo systemctl start bidpilot-web.service bidpilot-worker.service
```

升级前先备份，随后在维护窗口执行：

```bash
sudo systemctl stop bidpilot-worker.service bidpilot-web.service
sudo -u bidpilot cp -a /opt/bidpilot/data /opt/bidpilot/data.backup-YYYYMMDD
sudo git -C /opt/bidpilot pull --ff-only origin main
sudo /opt/bidpilot/.venv/bin/python -m pip install --upgrade /opt/bidpilot
sudo systemctl start bidpilot-web.service bidpilot-worker.service
```

禁止对数据库和浏览器配置目录做跨版本“挑文件式”覆盖。恢复时应先停止两个服务，并保证数据库、`data/secrets/` 和 `data/browser_profiles/` 来自同一备份点。

### 2.5 企业内网 HTTPS 模式

正式公司服务器不要使用 LAN 的普通 HTTP 便捷模式。推荐让 BidPilot 只监听回环地址，由同机 Nginx、Traefik 或组织零信任网关终止 TLS：

```dotenv
BIDPILOT_ENV=production
BIDPILOT_NETWORK_ACCESS_MODE=enterprise
BIDPILOT_HOST=127.0.0.1
BIDPILOT_PORT=8000
BIDPILOT_LAN_ACCESS_POLICY=admin_token
BIDPILOT_LAN_TRUSTED_NETWORKS=10.20.0.0/16
BIDPILOT_LAN_ADMIN_TOKEN=<至少16位密码学随机值>
BIDPILOT_TRUSTED_PROXY_NETWORKS=127.0.0.1/32
BIDPILOT_ENTERPRISE_ALLOWED_ORIGINS=https://bidpilot.example.internal
```

企业模式按顺序执行四道校验：从明确受信代理解析客户端地址、限制客户端私有 CIDR、要求 HTTPS 且精确校验浏览器 Origin、要求所有 `/api/` 读写携带管理员令牌。代理头来自其他地址时会被忽略；`trusted_lan` 免令牌策略会在启动或保存时被拒绝。Uvicorn 自身的隐式代理头解析已关闭，避免代理信任边界被提前改写。

如果 `BIDPILOT_NETWORK_ACCESS_MODE=enterprise` 来自服务器环境，全部网络重启字段由环境强制锁定：旧 SQLite 中的 `lan/trusted_lan` 配置不会覆盖生产边界，网页保存也会被拒绝。变更这些字段必须走部署配置与重启流程。

部署时还必须做到：后端 8000 不对用户网段开放；TLS 私钥只允许 root/网关读取；DNS 名称与证书 SAN 匹配；管理员令牌进入密钥系统而非 Git；网关启用限流、访问日志和组织身份认证。应用管理员令牌是纵深防御，不替代企业 SSO、MFA 或角色授权。

仓库提供 `deploy/nginx/bidpilot-systemd.conf.example`。替换其中域名和证书路径后先运行 `sudo nginx -t`，再在维护窗口重载 Nginx；不要直接复制示例域名投入生产。

## 3. 千里马在 Linux 服务器上的安全登录

### 3.1 不变的边界

- 首次登录必须由账号本人在可见浏览器中完成；系统不代填密码、不处理验证码、不扫码。
- 不导出千里马会员 Cookie，不把 Cookie 重放到 HTTP 客户端。
- 会员增强默认仅用于用户主动的即时任务和单个主题；默认最多 2 页/40 条、30 秒冷却、每日 24 次，预算跨进程持久化。
- 不读取付费详情。会员监控默认关闭；只有已取得书面授权或官方 API 权利，并填写授权记录编号后才允许显式开启。
- 登录过期或页面无法证明会员状态时立即降级为 `auth_required/failed`，不会冒充成功。

### 3.2 有桌面 Linux

在图形会话中以服务用户执行：

```bash
sudo systemctl stop bidpilot-worker.service bidpilot-web.service
sudo -u bidpilot -H env DISPLAY="$DISPLAY" \
  /opt/bidpilot/.venv/bin/python -m bidpilot auth qianlima
sudo systemctl start bidpilot-web.service bidpilot-worker.service
```

完成授权后的真实测试必须看到站内搜索与免费会员列表结果，状态才会进入 `authorized`。profile 不使用人为 7 天过期；每次查询都会重新检查原站搜索框、会员中心和个人中心，会话失效即停止。

### 3.3 无桌面 Linux

可使用 Xvfb + VNC/noVNC 作为临时运维图形会话，但必须满足：

1. VNC/noVNC 仅监听 `127.0.0.1`；
2. 管理员通过 SSH 本地端口转发访问；
3. 登录完成并测试通过后关闭临时远程桌面；
4. 不把 VNC/noVNC 端口映射到公网；
5. 浏览器配置目录权限保持 0700，仅 `bidpilot` 用户可读写。

示意隧道：

```bash
ssh -L 6080:127.0.0.1:6080 operator@server
```

随后仅在本机打开 `http://127.0.0.1:6080`。本手册不提供默认公网 VNC 配置，避免把登录会话暴露给第三方。

## 4. Docker Compose

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8000/health
```

Compose 将宿主机端口固定发布到 `127.0.0.1`，容器内 Web 使用独立 worker，命名卷保存数据库与报告。管理容器应使用：

```bash
docker compose restart
docker compose stop
docker compose down
```

`docker compose down` 默认不删除命名卷；除非已经确认备份并明确要清空数据，不要执行 `down -v`。

企业内网使用 `deploy/compose/compose.enterprise.yaml`。该文件增加 TLS Nginx、固定内部代理子网，且不发布后端 8000。Web/worker 同时接入不发布端口的出站 bridge 网络，用于访问招投标来源；内部代理网络保持 `internal`：

```bash
export BIDPILOT_SERVER_NAME=bidpilot.example.internal
export BIDPILOT_TRUSTED_CLIENT_NETWORKS=10.20.0.0/16
export BIDPILOT_LAN_ADMIN_TOKEN="$(openssl rand -hex 24)"
export BIDPILOT_TLS_CERT_DIR=/etc/bidpilot/tls
docker compose -f deploy/compose/compose.enterprise.yaml config
docker compose -f deploy/compose/compose.enterprise.yaml up -d --build
curl --cacert /path/to/corporate-ca.pem https://bidpilot.example.internal/health
```

证书目录必须存在 `fullchain.pem` 与 `privkey.pem`。示例不会伪造或自动签发证书；请使用企业 CA 或受信 ACME 流程。若前面再增加负载均衡器，必须把其固定私有 CIDR 加入代理信任链，并确认每一跳覆盖而非复制客户端提交的转发头。

## 5. 监控与故障处理

| 现象 | 检查 | 处理 |
|---|---|---|
| Web 离线 | `systemctl status`、`journalctl`、8000 端口 | 修复配置后重启 Web；不要按 PID 强杀未知进程 |
| worker 离线 | `/api/v1/system/status`、worker 日志 | 核对共享数据库路径和文件权限，重启 worker |
| 千里马登录失败 | `DISPLAY/WAYLAND_DISPLAY`、Playwright、授权状态 | 在受保护可见图形会话重新登录；不要切换到 Cookie 重放 |
| 报告无法写入 | `ls -ld outputs/reports` | 确保目录属于 `bidpilot:bidpilot` 且 0700/0750 |
| 外部渠道重试 | Web Outbox 控制塔 | 修复目标配置，只重试失败目标，避免重新抓取 |
| 端口冲突 | `ss -ltnp | grep :8000` | 修改环境文件端口或停止已确认的冲突服务 |

## 6. 备份与安全基线

必须备份：

- `data/bidpilot.db`
- `data/secrets/`
- `data/browser_profiles/`（若使用登录来源）
- `outputs/reports/`
- `/etc/bidpilot/bidpilot.env`

不得进入 Git、交付压缩包、截图或工单：`.env`、数据库、Cookie、浏览器配置、密钥、完整 Webhook、API Key、SMTP 密码、控制令牌。

systemd 单元使用 `NoNewPrivileges`、`ProtectSystem=strict`、`ProtectHome=true`、`PrivateTmp=true`、`UMask=0077`，只允许写入数据与报告目录。若企业有集中日志、密钥管理和备份平台，应在不扩大上述权限边界的前提下接入。

## 7. 可重复的 systemd 验证

仓库提供 `deploy/systemd/smoke-test.sh`。它只在确认 `/opt/bidpilot`、`/etc/bidpilot` 与两个测试单元不存在时运行，创建临时服务用户，完成安装、后台启动、worker 心跳、CLI 状态与优雅停止，然后安全清理测试环境。

```bash
sudo bash deploy/systemd/smoke-test.sh "$PWD" HEAD
```

脚本通过不等于公网生产就绪；生产仍需单独的 TLS、身份与权限方案。
