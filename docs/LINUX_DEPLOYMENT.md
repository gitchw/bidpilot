# 标擎 BidPilot Linux 部署与运维手册

版本：v0.8.0 跨平台终版

适用：Debian/Ubuntu 系 Linux 服务器、WSL2 systemd、Docker Engine

目标：让 Web/API 与长期任务 worker 在后台稳定运行，同时保留登录来源的真实权限边界。

## 1. 先选部署形态

| 形态 | 适用场景 | 生命周期管理 | 千里马免费登录 |
|---|---|---|---|
| systemd 原生部署 | 需要浏览器登录来源、便于运维审计 | `systemctl` | 支持；首次必须通过可见图形会话，随后可复用已验证持久配置做有界无头检索 |
| Docker Compose | 快速部署公开来源和标准 Web/worker | Compose `up/stop/down` | 默认镜像不含 Chromium；若必须使用登录来源，优先 systemd 原生部署 |
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
sudo /opt/bidpilot/.venv/bin/python -m playwright install --with-deps chromium
```

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
```

`auto` 的含义：桌面 Linux 使用可见浏览器；无 `DISPLAY/WAYLAND_DISPLAY` 的后台服务，仅在已经通过人工可见登录与真实验证后，用同一持久浏览器配置执行一次首屏无头检索。它不会导出 Cookie，也不会把 Cookie 交给 HTTP 客户端。

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

## 3. 千里马在 Linux 服务器上的安全登录

### 3.1 不变的边界

- 首次登录必须由账号本人在可见浏览器中完成；系统不代填密码、不处理验证码、不扫码。
- 不导出千里马会员 Cookie，不把 Cookie 重放到 HTTP 客户端。
- 会员增强仅用于用户主动的即时任务、单个主题、一次首屏、最多 20 条、10 秒冷却。
- 不进入每日/每周/月度任务，不自动翻页，不读取付费详情。
- 登录过期或页面无法证明会员状态时立即降级为 `auth_required/failed`，不会冒充成功。

### 3.2 有桌面 Linux

在图形会话中以服务用户执行：

```bash
sudo systemctl stop bidpilot-worker.service bidpilot-web.service
sudo -u bidpilot -H env DISPLAY="$DISPLAY" \
  /opt/bidpilot/.venv/bin/python -m bidpilot auth qianlima
sudo systemctl start bidpilot-web.service bidpilot-worker.service
```

完成授权后的真实测试必须看到站内搜索与免费会员首屏结果，状态才会进入 `authorized`。

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
