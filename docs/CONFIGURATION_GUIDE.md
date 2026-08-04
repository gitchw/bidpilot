# 标擎 BidPilot 配置中心指南

版本：v0.8.0

更新时间：2026-07-24

## 1. 推荐方式：网页配置

启动服务后打开 `http://127.0.0.1:8000`，进入“配置中心”。AI、检索、长期 worker、局域网和推送通道都可直接在网页填写，不需要编辑 `.env`。普通字段保存后立即生效；访问范围、LAN 策略、可信网段、管理员令牌和端口会清楚标记“等待重启”，避免保存瞬间中断当前远端连接。

配置优先级如下：

1. 网页保存到 SQLite 的运行时配置；
2. 启动进程时的 `BIDPILOT_*` 环境变量或 `.env`；
3. 代码安全默认值。

网页配置适合日常管理；环境变量保留给容器、CI 和集中密钥系统。

### 1.1 先判断你用哪种方式启动

“服务端口”不是所有部署方式共用的开关。先看启动命令，再按下面对应的一列操作：

| 你怎样启动 | 谁控制浏览器访问端口 | 容器/进程实际监听 | 网页“服务端口”是否生效 |
|---|---|---|---|
| `.venv/.../python -m bidpilot serve` | 网页保存的 `port`，默认 8000 | Python 进程监听该端口 | 是，保存后重启原生服务 |
| `docker compose up ...` | 项目根目录 `.env` 的 `BIDPILOT_PORT`，默认 8000 | 容器内固定监听 8000 | 否，网页不会修改 Compose 映射 |

#### 原生 Python：修改、重启、停止

1. 打开“配置中心 → 长期任务与局域网”，修改“服务端口”并点击“保存本卡修改”。
2. 页面出现“等待重启”是正常现象。例如改为 8012 后，先执行对应系统的重启命令：

```text
# Windows
.venv\Scripts\python.exe -m bidpilot restart

# macOS / Linux
.venv/bin/python -m bidpilot restart
```

3. 重启完成后打开 `http://127.0.0.1:8012`。停止服务时执行：

```text
# Windows
.venv\Scripts\python.exe -m bidpilot stop

# macOS / Linux
.venv/bin/python -m bidpilot stop
```

也可以在运行 `serve` 的终端按 `Ctrl+C`。生命周期命令必须使用启动服务时的同一个项目 `.venv`，否则可能找不到正确服务。

#### Docker Compose：修改、重启、停止

Compose 映射形如 `127.0.0.1:${BIDPILOT_PORT:-8000}:8000`：左侧的 `BIDPILOT_PORT` 是电脑上的访问端口，右侧 `8000` 是容器内固定端口。网页修改“服务端口”不会修改 `.env`、`compose.yaml` 或已创建容器。

例如要把电脑上的入口改为 8012：

1. 用文本编辑器打开项目根目录 `.env`，加入或修改一行 `BIDPILOT_PORT=8012`。
2. 在项目根目录执行 `docker compose up -d --force-recreate`；只有重新创建容器，新的端口映射才会生效。
3. 打开 `http://127.0.0.1:8012`。容器内部仍然监听 8000，这是正确状态。

常用生命周期命令：

```text
docker compose stop                         # 暂停全部容器，保留数据
docker compose start                        # 继续运行，端口映射不变
docker compose restart                      # 配置没变化时普通重启
docker compose up -d --force-recreate       # 修改 BIDPILOT_PORT 后重新创建
docker compose down                         # 停止并移除容器和项目网络
```

`docker compose down` 不会删除命名卷；不要附加 `-v`，否则会删除卷中的持久数据。修改 Compose 端口后，不要运行原生 Python 的 `bidpilot restart`，那只管理原生服务。

## 2. 安全模型

### 2.1 敏感值如何保存

以下字段视为敏感信息：

- 模型 API Key；
- 飞书机器人 Webhook、签名密钥、App Secret；
- SMTP 密码/授权码；
- 钉钉 Webhook 与加签密钥；
- 企业微信 Webhook；
- 通用 Webhook 与 Bearer Token；
- Telegram Bot Token；
- Slack Incoming Webhook；
- 局域网管理员令牌。

首次保存敏感值时，系统在 `data/secrets/runtime_config.key` 生成本机 Fernet 密钥，再把密文写入 SQLite。Windows 会移除 `Everyone`、`Authenticated Users` 和内置普通用户组的继承读取权限，只保留当前用户；macOS/Linux 使用目录 `0700`、密钥文件 `0600`。权限无法安全收紧时程序会明确失败，不会悄悄留下可被其他本机用户读取的密钥。API 和网页只返回“已配置/未配置”，不会回显原文。

备份时先停服，再整体复制 `data/` 目录。这样数据库、运行时配置密钥、来源授权密钥、意图快照签名密钥和可选 Cookie 文件不会漏掉：

```text
data/
├── bidpilot.db
└── secrets/
    ├── runtime_config.key
    ├── source_auth.key
    └── intent_snapshot.key
```

不要把 `data/` 提交到 Git；项目 `.gitignore` 已排除数据库和 `data/secrets/`。只备份数据库而漏掉密钥，会导致已保存的模型、渠道或来源授权无法解密。

### 2.2 留空、替换与清除

- 敏感输入框留空：保持原值。
- 输入新内容：替换原值。
- 勾选“清除”并保存：显式清除。
- 普通文本字段可以保存为空；数字或枚举字段需要点击字段来源行的“恢复来源值”。

该规则避免浏览器加载配置时拿到密钥，也避免用户只想改模型名却意外清空 API Key。

### 2.3 编辑令牌

网页保存和测试前会自动调用 `/api/v1/config/edit-token`，获得默认有效 10 分钟的短期令牌，并通过 `X-BidPilot-Config-Token` 请求头提交。令牌只存在当前进程内、禁止缓存，服务重启后失效。

短期编辑令牌不是用户账号密码：它用于防止另一个网页静默提交配置或读取授权浏览器 Cookie。默认应用仅监听 `127.0.0.1`；LAN 管理由下一节的策略控制。如需公网使用，必须在反向代理增加：

- HTTPS/TLS；
- 登录与角色权限；
- CSRF/来源校验；
- 请求限流；
- 访问与变更审计；
- 独立密钥管理系统。

## 3. 局域网访问

### 3.1 默认只允许本机

全新安装固定使用：

```text
访问范围：local
监听地址：127.0.0.1
端口：8000
```

这时手机和其他电脑无法连接。不要用 `--host 0.0.0.0` 绕开页面设置；CLI 会拒绝“页面显示仅本机、实际监听所有网卡”的不一致启动。

### 3.2 开放给同一局域网

在“长期任务与局域网”卡片按以下步骤操作：

1. 把“访问范围”改为“局域网设备”。
2. 选择管理方式。
3. 保持服务端口 `8000`，除非它确实被别的程序占用。
4. 点击“保存本卡修改”。页面会同时显示“当前生效”和“重启后生效”。
5. 原生 Python 用户：Windows 运行 `.venv\Scripts\python.exe -m bidpilot restart`；macOS/Linux 运行 `.venv/bin/python -m bidpilot restart`。Docker Compose 用户不要执行这一步，应按 1.1 节修改 `.env` 的 `BIDPILOT_PORT` 并重新创建容器。
6. 在其他设备打开 `http://服务电脑的局域网IP:端口`；端口是原生服务的网页保存值，或 Compose 的 `BIDPILOT_PORT`。

不知道服务电脑 IP 时，可查看路由器设备列表，或使用操作系统的网络设置。不要使用搜索引擎显示的公网 IP，也不要在路由器配置端口映射。

### 3.3 两种 LAN 管理方式

`admin_token`（推荐）：

- 远端读取页面不需要令牌；首次执行查询、保存、删除、真实测试或来源授权等写操作时，网页要求管理员令牌。
- 点击“生成安全令牌”会在当前浏览器本地生成 32 字符随机值；保存后不会回显原文。
- 令牌只保存在远端浏览器当前标签页的 `sessionStorage`，关闭标签页后需重新输入。

`trusted_lan`（便捷模式）：

- 常见私有网段、链路本地和组网地址内的设备无需输入管理员令牌，查询、订阅、删除、配置和来源授权功能不再做额外设备限制。
- `auto` 是推荐可信网段值；也可填写逗号分隔的私有 CIDR 来缩小范围，例如 `192.168.1.0/24`。
- 公网 CIDR、`0.0.0.0/0`、`::/0` 和伪造的 `X-Forwarded-For` 不会被信任。
- 只有当家中、宿舍或办公室同网设备都可信时才选择。任何能连接的可信设备都能修改或删除本地业务数据。

两种模式都使用普通 HTTP，局域网流量不会自动加密；它们不是公网部署方案。

### 3.4 其他设备如何管理平台登录

局域网设备可以点击来源中心的“打开浏览器授权”“完成授权”“测试授权”和“清除”。可见 Chromium 会在运行 BidPilot 服务的电脑上弹出，而不是在手机上弹出；你需要在服务电脑亲自登录，然后回到任意管理页面点击完成。中国招标投标网完成后验证搜索与免费会员详情；千里马保留独立持久浏览器配置并验证一次首屏免费会员查询。等待验证、无法判定、失败或过期都不会显示为已增强。只有平台官方提供 OAuth、扫码确认或设备码时，远端设备才可能直接完成。

BidPilot 不自动填写账号密码，不破解验证码，不绕过 WAF、付费墙、角色权限或频率限制。中国招标投标网只向允许域名发送加密保存的会话；千里马不导出或向 HTTP 客户端重放 Cookie，首次登录必须可见，已验证配置可在 Linux 后台由持久浏览器内部复用；会员检索仍限即时、单次、首屏，不进入定时任务。

千里马浏览器策略由环境变量 `BIDPILOT_QIANLIMA_BROWSER_MODE` 控制：`auto` 为推荐值，桌面环境可见、Linux 无显示服务无头；`visible` 强制要求 `DISPLAY/WAYLAND_DISPLAY`，缺失时失败关闭；`headless` 只适合已经在可见会话完成登录与真实测试的持久配置，不能用于首次登录。

### 3.5 使用 `.env` 预先配置

无人值守部署可在 `.env` 使用：

```text
BIDPILOT_PORT=8000
BIDPILOT_NETWORK_ACCESS_MODE=lan
BIDPILOT_LAN_ACCESS_POLICY=trusted_lan
BIDPILOT_LAN_TRUSTED_NETWORKS=auto
BIDPILOT_LAN_ADMIN_TOKEN=
```

若改用 `BIDPILOT_LAN_ACCESS_POLICY=admin_token`，必须给 `BIDPILOT_LAN_ADMIN_TOKEN` 设置至少 16 个随机字符。网页保存值优先于 `.env`；可在字段下点击“恢复来源值”删除网页覆盖，回到环境变量或默认值。

## 4. AI 模型配置

### 必填项

- API 基础地址：OpenAI-compatible 服务的版本根，例如 `http://127.0.0.1:8045/v1`。
- 模型名称：服务实际支持的模型 ID。

### 可选项

- API 密钥：本机无鉴权服务可以留空；云服务通常必须填写。
- 超时：3～120 秒，默认 30 秒。
- 意图辅助模式：`auto`（推荐，只在低置信/缺失/冲突时调用）、`off`（完全关闭意图模型）、`always`（每次复核，但高置信规则字段仍锁定）。
- 意图置信阈值：0.50～0.99，默认 0.85。阈值越高越容易触发模型，调用次数与费用也可能增加；阈值不会放宽本地校验。
- AI 检索规划：`auto` 允许模型建议受控扩词和来源优先级；`off` 只用本地词典。模型不能生成公告。
- 最多检索轮次：1～2；推荐 2，即首轮后仅在存在可测覆盖缺口时补搜一轮。
- 每来源查询预算：1～5，推荐 2；控制支持关键词来源最多执行几个查询变体，避免无界调用。
- AI 边界复核：只复核已经通过日期、地域、公告类型和排除词硬过滤的主题边界候选；关闭后字面未命中项保守拒绝。
- 语义接受阈值：0.50～0.99，默认 0.82；只影响边界主题判定，不能放宽硬过滤。
- 语义候选上限：1～30，默认 12；只把最需要判断的边界候选送给模型，超出预算的候选保守处理。
- 逐公告摘要：`auto/off`；`auto` 只摘要受预算约束的真实公告，失败时回退本地证据摘要。
- 逐公告摘要数量、并发和字符上限：分别控制模型调用笔数、同时请求数和每条正文输入长度，是费用、速度和限流的三道硬边界。
- AI 情报简报：`auto/off`；关闭或失败时仍生成确定性摘要和建议。
- 简报证据上限：3～25，默认 12；只决定送给模型的最高优先证据数量，不删除其余结果。
- 企业适配 AI 复核：`auto/off`；本地画像计分和推荐始终执行。
- 适配复核证据上限：3～25，默认 15；超出窗口的结果继续使用本地规则逐条判断。

系统会在基础地址后调用 `/chat/completions`；如果用户直接填了以 `/chat/completions` 结尾的完整端点，则不会重复拼接。请求主体使用标准 `model`、`messages`、`temperature` 和 `max_tokens` 字段，响应需至少包含 `choices[0].message.content`。可参考 [OpenAI Chat Completions API](https://developers.openai.com/api/reference/resources/chat)。

点击“测试已保存的模型”只测试已经保存的配置，不会顺便保存其他草稿。AI 卡片有未保存修改时，页面会要求先点击“保存本卡修改”。测试只发送固定测试句，不发送任何招投标内容；成功后显示延迟和短回复，失败只显示脱敏诊断，不返回密钥或完整认证 URL。

### 混合意图引擎如何使用模型

1. `rules-v2` 先生成完整、可独立执行的规则基线。
2. `auto` 模式只检查低于阈值、缺失或冲突的字段；`always` 模式会请求复核，但不自动解锁高置信字段。
3. 模型必须返回严格 JSON；Markdown 代码围栏、解释性前缀、额外字段和非法枚举都会让整份提议失效。
4. 本地逐字段验证主题是否来自原句、地域是否存在于行政区词表、日期是否合法有界、计划字段是否完整、渠道/公告类型是否属于允许枚举。
5. `region_code` 永远由本地词表派生，模型没有输出该字段的权限。
6. 超时、网络错误、非法 JSON 或提议被拒绝时，任务继续使用规则结果。

意图模型只收到自然语言原句、规则基线、当前时间、允许修复字段和 schema；不会收到抓取到的标讯正文、机会备注、订阅历史、API Key 或 Webhook。若模型服务位于云端，原句会离开本机，请先确认其隐私政策；敏感查询可把模式设为 `off`。

### 检索规划、二轮补搜与语义复核

1. 本地规划器先生成主题、同义词、来源能力和查询预算；模型只能提交 schema 允许的查询词与来源优先级。
2. 首轮结束后，系统根据来源是否支持关键词、是否授权、扫描/候选/保留数量和区域能力计算缺口；只有缺口存在且最多轮次为 2 才补搜。
3. 两轮共享已访问 URL 集合，避免重复抓取；网页展示每轮查询词、来源调用和数量变化。
4. 日期、地域、公告类型与排除词永远先由本地硬过滤。模型只能对主题相关性边界作接受/拒绝建议，并必须引用当前候选证据。
5. 模型超时、批量 JSON 不完整、未知候选或解释不合规时，边界候选保守回退，本轮其他来源继续。

### 情报简报、企业适配与证据追问

- 情报简报只使用本轮可信 E 编号，未知引用会让整份模型简报失效；标题、采购人、日期、地域、阶段与链接由本地回填。
- 企业适配只允许模型识别画像语义命中与逐字证据；本地计算基础分、反馈调整、最终建议和硬风险。
- 证据追问仅在用户点击回答后发送问题与本轮有界固定证据；结构化字段由本地快照回填，只有正文片段允许模型逐字选择。
- 不发送订阅历史、机会负责人/备注、渠道地址、API Key、Cookie 或模型原始响应。

### 模型失败时的行为

模型是增强项，不是单点故障：

- 无模型配置：使用本地证据抽取摘要；
- 模型超时、HTTP 错误或格式异常：自动回退到证据抽取；
- 模型摘要出现证据中不存在的数字：拒绝该摘要并回退；
- 模型配置保存后：后续运行立即使用，无需重启。
- 检索规划失败：继续使用本地规划；二轮补搜仍受既定预算控制。
- 语义复核失败：边界候选保守拒绝，已通过确定性过滤的结果不受影响。
- 情报简报或适配复核失败：返回明确回退状态和本地结果，不把失败伪装成模型结论。

## 5. 报告发布与下载链接

`public_base_url` 已从飞书卡移到独立的“报告发布与下载链接”卡，因为它由飞书、钉钉、企业微信、Slack 和超大 Telegram 文件共同使用。

- 只有你已经部署 HTTPS、登录/令牌校验、访问控制和审计的反向代理时才填写；
- 填写根地址，例如 `https://bidpilot.example.com`，系统在其后拼接 `/api/v1/reports/{filename}`；
- 留空时，链接型渠道会明确说明报告仅保存在 BidPilot 主机，不会虚构公网地址；
- BidPilot 不自动建立公网隧道、不绕过访问限制，也不会替你签发证书；
- 在 Telegram 或 Slack 卡点击“设置报告链接”可直接定位到此卡。

## 6. 飞书配置

### 6.1 群机器人

填写：

- 机器人 Webhook；
- 签名密钥（如果在飞书安全设置中启用了“签名校验”）；
- 报告链接来自独立“报告发布与下载链接”卡（可选）。

群机器人发送交互卡片。若未配置公网报告地址，卡片只提示报告已保存在部署主机；若配置，则附带 `/api/v1/reports/{filename}` 下载链接。公网地址必须由带 TLS 和身份认证的反向代理提供，不应直接暴露当前本机服务。平台创建和安全设置以 [飞书自定义机器人官方指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot) 为准。

### 6.2 飞书应用

填写：

- App ID；
- App Secret；
- 接收 ID；
- 接收 ID 类型：`chat_id`、`open_id`、`user_id`、`union_id` 或 `email`。

应用通道会申请 tenant access token，有报告时上传并发送 Word 文件，无新增时发送文本回执。应用必须具备发送消息和文件相关权限，并已安装到目标租户。

## 7. SMTP 配置

字段：

- 主机、端口；
- 加密方式：SSL、STARTTLS、明文；
- 用户名、密码/应用授权码；
- 发件人；
- 收件人，多个地址使用逗号或分号分隔；
- 超时，默认 30 秒。

就绪条件：主机、发件人、至少一个收件人存在；填写用户名时必须同时有密码。建议优先使用 SSL 或 STARTTLS，并使用服务商生成的应用专用密码。

测试会真实发送一封标题包含“配置中心连通性测试”的邮件。发送前网页会二次确认。

## 8. 钉钉配置

填写机器人 Webhook；如果机器人启用了加签，填写加签密钥。系统按照钉钉签名规则附加毫秒时间戳和 HMAC-SHA256 签名，并发送 Markdown 消息。机器人创建、安全设置、关键词和 IP 白名单以 [钉钉自定义机器人官方文档](https://open.dingtalk.com/document/dingstart/custom-bot-creation-and-installation) 为准。

若平台返回非零 `errcode`，任务会记录失败且不会写成功投递账本。

## 9. 企业微信配置

填写群机器人 Webhook。系统发送 `msgtype=markdown` 的标准 JSON；平台返回非零 `errcode` 时视为失败。机器人创建步骤以 [企业微信开发者中心群机器人文档](https://developer.work.weixin.qq.com/document/path/91770) 为准。

Webhook URL 通常包含访问密钥，因此读取接口不会返回原文。

## 10. 通用 Webhook 配置

填写：

- HTTP(S) Webhook 地址；
- 可选 Bearer Token；
- Webhook 超时，3～120 秒。

报告生成事件示例：

```json
{
  "event": "bidpilot.report.ready",
  "occurred_at": "2026-07-18T08:30:00+00:00",
  "subscription": {"name": "深圳充电桩日报"},
  "result": {
    "new_count": 3,
    "report_filename": "深圳充电桩日报_202607181630.docx",
    "report_url": "https://example.com/api/v1/reports/..."
  }
}
```

无新增事件使用 `bidpilot.run.no_change`，报告字段为 null。若配置 Bearer Token，请求带 `Authorization: Bearer ...`。

接收端应做到：

- 快速返回 2xx；
- 按事件类型处理；
- 自己实现幂等；
- 不在响应中返回敏感信息；
- 对公网端点启用 TLS、认证、限流和日志脱敏。

## 11. Telegram Bot 配置

网页卡片包含五个字段：

1. `Bot Token`：BotFather 签发的 `{数字}:{秘密}`，只加密保存且不回显；
2. `Chat ID`：整数会话 ID，或公开频道的 `@username`；
3. `Thread ID`：论坛群话题可选，留空表示主会话；
4. `静默发送`：消息到达但不触发声音；
5. `保护消息内容`：请求平台限制转发和保存。

就绪条件是 Token 与 Chat ID 同时存在并通过格式校验。机器人还必须已经加入目标群或频道，并具有发送消息/文件权限；“已就绪”不代表平台权限一定正确，所以必须完成真实测试。

普通 Word 通过官方 `sendDocument` 发送；超过 50 MB 时改用“报告发布与下载链接”卡中的安全地址。没有 `public_base_url` 时，超限报告会进入死信而不是伪装成功。429 会严格遵守平台 `retry_after`；Token、Chat ID、权限或文件过大导致的 400/401/403/404/413 会直接进入死信，修复后在结果页或订阅日志只重试该目标。官方字段解释见 [Telegram Bot API](https://core.telegram.org/bots/api/)。

## 12. Slack Incoming Webhook 配置

只接受 Slack 官方 HTTPS 地址，格式必须严格为：

```text
https://hooks.slack.com/services/{team}/{channel}/{secret}
```

政府云域名 `hooks.slack-gov.com` 同样支持。Webhook 与创建时选择的频道绑定且本身就是秘密。Incoming Webhook 只能发送消息和报告链接，不能上传 Word；需要链接时在独立“报告发布与下载链接”卡填写已有 TLS 和访问控制的安全入口。平台说明见 [Slack Incoming Webhooks](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks)。

如果以后要上传 Slack 文件，必须另行配置 Bot Token、`files:write` 和外部上传流程；当前卡片不会把 Webhook 伪装成文件接口。

## 13. 保存、测试与草稿规则

- 每张卡独立保存，只提交该卡真正修改的字段；
- 其他卡片未保存草稿不会被顺带覆盖；
- 测试按钮不会自动保存当前草稿；
- 卡片有未保存修改时测试按钮不可用，先保存再测试；
- 测试会真实外发，网页在发送前二次确认；
- 敏感字段的“清除”只影响当前保存卡片，不会误清其他卡秘密；
- 多进程部署时，独立 worker 在检索和 Outbox 外发前都会重新加载网页配置；远端恢复默认值也会让 worker 放弃旧凭据。

### 13.1 全部网页/API 配置字段字典

下面 62 个字段与 Swagger 中的 `RuntimeConfigUpdate` 一一对应。表里的“立即”表示保存成功后的下一次相关操作就使用新值；“重启”表示页面只保存目标值，当前监听进程不改变，重启后才生效。

#### AI 与意图字段

| 字段 | 给零基础用户的解释 | 范围/默认值 | 生效与风险 |
|---|---|---|---|
| `revision` | 当前配置版本号；网页自动携带，防止两个页面互相覆盖 | 非负整数 | 每次成功保存递增；旧值返回 409 |
| `llm_base_url` | OpenAI-compatible 服务根地址 | 完整 HTTP(S) URL；默认空 | 立即；云地址会接收允许发送的文本 |
| `llm_api_key` | 模型服务密钥 | 可空 | 立即；加密保存、永不回显 |
| `llm_model` | 服务实际开放的模型 ID | 最长 200 字；默认空 | 立即；名称错误会在测试中明确失败 |
| `llm_timeout` | 每次模型调用最多等待多久 | 3～120 秒；默认 30 | 立即；增大会延长页面等待 |
| `intent_llm_mode` | 是否让模型辅助修复意图 | `off/auto/always`；默认 `auto` | 立即；`always` 调用更多、费用更高 |
| `intent_llm_confidence_threshold` | `auto` 模式低于多少置信度才调用模型 | 0.50～0.99；默认 0.85 | 立即；越高越容易触发模型 |
| `retrieval_llm_mode` | 是否让模型建议发现词和来源优先级 | `off/auto`；默认 `auto` | 立即；模型词不会直接成为可信命中词 |
| `retrieval_max_rounds` | 一次任务最多检索几轮 | 1～2；默认 2 | 立即；第二轮只在覆盖有缺口时触发 |
| `retrieval_query_budget_per_source` | 每来源每轮最多几个查询变体 | 1～5；默认 2 | 立即；越大请求越多、耗时越长 |
| `retrieval_semantic_review` | 是否让模型复核主题边界候选 | 布尔值；默认开 | 立即；地域/日期等硬条件仍不可放宽 |
| `retrieval_semantic_threshold` | 语义复核最低接受分 | 0.50～0.99；默认 0.82 | 立即；仍要求正文逐字证据引句 |
| `retrieval_semantic_candidate_limit` | 单轮最多送模型复核多少条边界候选 | 1～30；默认 12 | 立即；是明确的调用预算上限 |
| `record_summary_mode` | 是否调用模型生成逐公告摘要 | `off/auto`；默认 `auto` | 立即；失败自动回退本地摘要 |
| `record_summary_max_records` | 单轮最多摘要多少条公告 | 0～30；默认 8 | 立即；0 可完全关闭这类费用 |
| `record_summary_concurrency` | 同时进行多少个摘要请求 | 1～8；默认 3 | 立即；过高可能限流或占满本地模型 |
| `record_summary_max_chars` | 每条公告最多送多少正文字符 | 500～12000；默认 5000 | 立即；截断会在结果中披露 |
| `intelligence_brief_mode` | 是否让模型生成本轮情报简报 | `off/auto`；默认 `auto` | 立即；只使用固定 E 编号证据 |
| `intelligence_brief_max_records` | 简报最多使用多少条高优先证据 | 3～25；默认 12 | 立即；不删除其他检索结果 |
| `decision_assessment_mode` | 是否让模型复核企业画像适配 | `off/auto`；默认 `auto` | 立即；本地计分始终执行 |
| `decision_assessment_max_records` | 单轮最多复核多少条适配证据 | 3～25；默认 15 | 立即；超出部分继续本地判断 |

#### 检索与 worker 字段

| 字段 | 解释 | 范围/默认值 | 生效与风险 |
|---|---|---|---|
| `request_timeout` | 单个来源 HTTP 请求最长等待 | 3～120 秒；默认 20 | 立即；超时只隔离该来源 |
| `request_interval` | 同一来源两次请求之间至少等待 | 0.1～10 秒；默认 0.8 | 立即；过小更易被限流 |
| `max_results_per_source` | 每来源进入统一过滤前的候选上限 | 1～100；默认 20 | 立即；不是最终结果数 |
| `ccgp_max_pages` | 中国政府采购网最多读取页数 | 1～20；默认 2 | 立即；越大越慢、请求越多 |
| `worker_poll_interval` | worker 空闲时多久检查一次任务 | 0.2～300 秒；默认 3 | 立即；过小只会增加空轮询 |
| `worker_lease_seconds` | worker 领取订阅后一次租约多长 | 30～7200 秒；默认 900 | 立即；运行中自动续租 |
| `worker_heartbeat_ttl` | 多久没心跳就显示 worker 离线 | 5～600 秒；默认 30 | 立即；只影响状态判断 |

#### 网络与报告发布字段

| 字段 | 解释 | 范围/默认值 | 生效与风险 |
|---|---|---|---|
| `network_access_mode` | 只允许本机，还是允许局域网设备连接 | `local/lan`；默认 `local` | 重启；不是公网部署开关 |
| `lan_access_policy` | LAN 写操作使用管理员令牌，还是可信私网免令牌 | `admin_token/trusted_lan` | 重启；便捷模式只适合可信网络 |
| `lan_trusted_networks` | 可信私网 CIDR 列表 | 默认 `auto` | 重启；拒绝公网段和全网段 |
| `port` | 原生 Python Web 服务端口 | 1～65535；默认 8000 | 重启；页面会同时显示当前与保存值；不修改 Compose 映射 |
| `lan_admin_token` | 远端写操作管理员令牌 | LAN 令牌模式至少 16 字 | 重启；加密保存且不回显 |
| `public_base_url` | 链接型渠道共用的安全报告根地址 | 完整 HTTP(S) URL；默认空 | 立即；必须先有 TLS、认证和访问控制 |

#### 飞书、邮件和机器人字段

| 字段 | 解释 | 范围/默认值 | 生效与风险 |
|---|---|---|---|
| `feishu_webhook_url` | 飞书群机器人地址 | 可空 | 立即；敏感、加密保存 |
| `feishu_webhook_secret` | 飞书机器人可选签名密钥 | 可空 | 立即；敏感、加密保存 |
| `feishu_app_id` | 飞书自建应用 App ID | 最长 200 字 | 立即；与 Secret/接收 ID 配套 |
| `feishu_app_secret` | 飞书应用 App Secret | 可空 | 立即；敏感、加密保存 |
| `feishu_receive_id` | 飞书应用消息接收者 ID | 最长 300 字 | 立即；必须匹配 ID 类型 |
| `feishu_receive_id_type` | 接收 ID 类型 | `chat_id/open_id/user_id/union_id/email` | 立即；默认 `chat_id` |
| `smtp_host` | 邮件服务器主机名 | 最长 500 字 | 立即 |
| `smtp_port` | 邮件服务器端口 | 1～65535；默认 465 | 立即；需匹配加密方式 |
| `smtp_security` | 邮件连接加密方式 | `ssl/starttls/plain`；默认 `ssl` | 立即；公网不建议 `plain` |
| `smtp_username` | 邮件登录账号 | 最长 500 字 | 立即 |
| `smtp_password` | 邮件密码或应用授权码 | 可空 | 立即；敏感、加密保存 |
| `smtp_from` | From 发件地址 | 最长 500 字 | 立即；需获服务商授权 |
| `smtp_to` | 一个或多个收件人 | 最长 2000 字 | 立即；逗号/分号/换行分隔 |
| `smtp_timeout` | 邮件连接与发送最长等待 | 3～120 秒；默认 30 | 立即；失败进入目标级重试 |
| `dingtalk_webhook_url` | 钉钉群机器人地址 | 可空 | 立即；敏感、加密保存 |
| `dingtalk_webhook_secret` | 钉钉可选加签密钥 | 可空 | 立即；敏感、加密保存 |
| `wecom_webhook_url` | 企业微信群机器人地址 | 可空 | 立即；敏感、加密保存 |
| `generic_webhook_url` | 用户自有自动化平台地址 | 完整 HTTP(S) URL | 立即；确认接收方可信 |
| `generic_webhook_bearer_token` | 通用 Webhook 可选 Bearer Token | 可空 | 立即；只进请求头、加密保存 |
| `delivery_webhook_timeout` | 所有 HTTP 消息通道的请求超时 | 3～120 秒；默认 20 | 立即；临时失败有限重试 |

#### Telegram、Slack 与控制字段

| 字段 | 解释 | 范围/默认值 | 生效与风险 |
|---|---|---|---|
| `telegram_bot_token` | BotFather 签发的 Token | `{数字}:{秘密}` | 立即；敏感、加密保存 |
| `telegram_chat_id` | 私聊/群/频道 ID 或公开 `@username` | 整数或合法用户名 | 立即；机器人必须有发送权限 |
| `telegram_message_thread_id` | 论坛群可选话题 ID | 正整数或空 | 立即；空表示主会话 |
| `telegram_disable_notification` | 是否静默发送 | 布尔值；默认关 | 立即 |
| `telegram_protect_content` | 是否请求限制转发和保存 | 布尔值；默认关 | 立即；最终能力由平台决定 |
| `slack_webhook_url` | Slack 官方 Incoming Webhook | 官方 HTTPS `/services/...` | 立即；敏感、加密保存 |
| `clear_secrets` | 本次明确要删除的敏感字段名列表 | 只能使用白名单名称 | 保存即不可恢复；留空不是清除 |
| `reset_fields` | 本次恢复环境变量/程序默认值的普通字段名列表 | 只能使用普通字段白名单 | 敏感字段不能从这里恢复 |

## 14. 环境变量兼容方式

网页是推荐入口，以下方式用于自动部署。复制 `.env.example` 为 `.env`，但绝不提交真实 `.env`：

```text
BIDPILOT_LLM_BASE_URL=
BIDPILOT_LLM_API_KEY=
BIDPILOT_LLM_MODEL=
BIDPILOT_INTENT_LLM_MODE=auto
BIDPILOT_INTENT_LLM_CONFIDENCE_THRESHOLD=0.85
BIDPILOT_RETRIEVAL_LLM_MODE=auto
BIDPILOT_RETRIEVAL_MAX_ROUNDS=2
BIDPILOT_RETRIEVAL_QUERY_BUDGET_PER_SOURCE=2
BIDPILOT_RETRIEVAL_SEMANTIC_REVIEW=true
BIDPILOT_RETRIEVAL_SEMANTIC_THRESHOLD=0.82
BIDPILOT_RETRIEVAL_SEMANTIC_CANDIDATE_LIMIT=12
BIDPILOT_INTELLIGENCE_BRIEF_MODE=auto
BIDPILOT_INTELLIGENCE_BRIEF_MAX_RECORDS=12
BIDPILOT_DECISION_ASSESSMENT_MODE=auto
BIDPILOT_DECISION_ASSESSMENT_MAX_RECORDS=15
BIDPILOT_PORT=8000
BIDPILOT_NETWORK_ACCESS_MODE=local
BIDPILOT_LAN_ACCESS_POLICY=admin_token
BIDPILOT_LAN_TRUSTED_NETWORKS=auto
BIDPILOT_LAN_ADMIN_TOKEN=
BIDPILOT_CONTROL_DIR=data
BIDPILOT_FEISHU_WEBHOOK_URL=
BIDPILOT_SMTP_HOST=
BIDPILOT_TELEGRAM_BOT_TOKEN=
BIDPILOT_TELEGRAM_CHAT_ID=
BIDPILOT_TELEGRAM_MESSAGE_THREAD_ID=
BIDPILOT_TELEGRAM_DISABLE_NOTIFICATION=false
BIDPILOT_TELEGRAM_PROTECT_CONTENT=false
BIDPILOT_SLACK_WEBHOOK_URL=
...
```

使用纯 Python 启动；未激活虚拟环境时必须使用项目 `.venv` 中的解释器。`BIDPILOT_CONTROL_DIR` 默认保持为项目 `data`，即使业务数据库迁移到别处，普通 `status/stop/restart` 也能发现服务：

```bash
python bootstrap.py
.venv\Scripts\python.exe -m bidpilot serve        # Windows
.venv/bin/python -m bidpilot serve                 # macOS / Linux
```

## 15. 故障排查

### 保存返回 403

本机通常表示短期编辑令牌过期；刷新配置中心后网页会自动获取并重试一次。LAN 的 `admin_token` 模式也可能表示管理员令牌错误；关闭当前标签页后重新打开并输入正确令牌。`trusted_lan` 模式若显示 403，说明请求的直连地址不属于已配置私网；伪造转发头不会改变判断。

### 保存返回 422

检查 URL 是否包含 `http://` 或 `https://`、端口是否在 1～65535、超时是否在 3～120、枚举值是否拼写正确。未知字段会被白名单拒绝。

### 显示“已配置”但测试失败

“已配置”只代表必填字段存在，不代表平台接受凭据。点击对应测试，检查：

- 本机能否访问目标地址；
- 平台权限、关键词、IP 白名单和签名是否一致；
- 接收 ID 类型是否匹配；
- SMTP 是否要求应用授权码；
- Telegram 机器人是否已加入目标会话、Chat ID/Thread ID 是否正确；
- Slack Webhook 是否仍有效且绑定了预期频道；
- 模型名称是否存在。

测试只验证当前已保存配置，不会发送历史招标数据。通道测试的 502 会返回脱敏后的具体原因；错误中不会出现 Telegram Token、Slack 完整 Webhook 或 SMTP 密码。

### 一个渠道成功、另一个失败

这是允许的“部分完成”，不是整轮丢失。打开订阅中心的“日志”，逐目标查看：

1. `succeeded` 不需要操作，也不会随失败目标重发；
2. `retrying` 等待页面显示的下次时间；
3. `dead_letter` 先修复对应配置并测试，再点“只重试此渠道”；
4. 不要重新运行整个订阅来代替死信恢复，否则会产生新一轮抓取和报告。

### 重启后敏感配置无法解密

确认 `data/secrets/runtime_config.key` 与数据库来自同一备份。若密钥丢失，系统不会尝试绕过加密；需在配置中心显式清除相关字段并重新填写。
