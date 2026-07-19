# 标擎 BidPilot 配置中心指南

版本：v0.7.0

更新时间：2026-07-19

## 1. 推荐方式：网页配置

启动服务后打开 `http://127.0.0.1:8000`，进入“配置中心”。AI、检索、长期 worker、局域网和推送通道都可直接在网页填写，不需要编辑 `.env`。普通字段保存后立即生效；访问范围、LAN 策略、可信网段、管理员令牌和端口会清楚标记“等待重启”，避免保存瞬间中断当前远端连接。

配置优先级如下：

1. 网页保存到 SQLite 的运行时配置；
2. 启动进程时的 `BIDPILOT_*` 环境变量或 `.env`；
3. 代码安全默认值。

网页配置适合日常管理；环境变量保留给容器、CI 和集中密钥系统。

## 2. 安全模型

### 2.1 敏感值如何保存

以下字段视为敏感信息：

- 模型 API Key；
- 飞书机器人 Webhook、签名密钥、App Secret；
- SMTP 密码/授权码；
- 钉钉 Webhook 与加签密钥；
- 企业微信 Webhook；
- 通用 Webhook 与 Bearer Token。
- 局域网管理员令牌。

首次保存敏感值时，系统在 `data/secrets/runtime_config.key` 生成本机 Fernet 密钥，再把密文写入 SQLite。Windows 会移除 `Everyone`、`Authenticated Users` 和内置普通用户组的继承读取权限，只保留当前用户；macOS/Linux 使用目录 `0700`、密钥文件 `0600`。权限无法安全收紧时程序会明确失败，不会悄悄留下可被其他本机用户读取的密钥。API 和网页只返回“已配置/未配置”，不会回显原文。

备份时必须同时备份数据库和密钥文件，否则恢复后无法解密已有凭据：

```text
data/bidpilot.db
data/secrets/runtime_config.key
```

不要把这两个文件提交到 Git；项目 `.gitignore` 已排除数据库和 `data/secrets/`。

### 2.2 留空、替换与清除

- 敏感输入框留空：保持原值。
- 输入新内容：替换原值。
- 勾选“清除”并保存：显式清除。
- 普通字段可以直接删除文本并保存为空。

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
5. 在终端运行 `python -m bidpilot restart`，或先 `stop` 再 `serve`。
6. 在其他设备打开 `http://服务电脑的局域网IP:8000`。

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

局域网设备可以点击来源中心的“打开浏览器授权”“完成授权”“测试授权”和“清除”。当前 Cookie 型授权的可见 Chromium 会在运行 BidPilot 服务的电脑上弹出，而不是在手机上弹出；你需要在服务电脑亲自登录，然后回到任意管理页面点击完成。只有平台官方提供 OAuth、扫码确认或设备码时，远端设备才可能直接完成。

BidPilot 不自动填写账号密码，不破解验证码，不绕过 WAF、付费墙、角色权限或频率限制。当前受管会话只用于中国招标投标网允许域名；千里马使用公开分类列表并提供原站工作台入口，不后台重放会员 Cookie。

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
- AI 情报简报：`auto/off`；关闭或失败时仍生成确定性摘要和建议。
- 简报证据上限：3～25，默认 12；只决定送给模型的最高优先证据数量，不删除其余结果。
- 企业适配 AI 复核：`auto/off`；本地画像计分和推荐始终执行。
- 适配复核证据上限：3～25，默认 15；超出窗口的结果继续使用本地规则逐条判断。

系统会在基础地址后调用 `/chat/completions`；如果用户直接填了以 `/chat/completions` 结尾的完整端点，则不会重复拼接。请求主体使用标准 `model`、`messages`、`temperature` 和 `max_tokens` 字段，响应需至少包含 `choices[0].message.content`。可参考 [OpenAI Chat Completions API](https://developers.openai.com/api/reference/resources/chat)。

点击“测试模型连接”只测试已经保存的配置，不会顺便保存其他草稿。AI 卡片有未保存修改时，页面会要求先保存本卡。测试只发送固定测试句，不发送任何招投标内容；成功后显示延迟和短回复，失败只显示脱敏诊断，不返回密钥或完整认证 URL。

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

## 5. 飞书配置

### 5.1 群机器人

填写：

- 机器人 Webhook；
- 签名密钥（如果在飞书安全设置中启用了“签名校验”）；
- 公网报告地址（可选）。

群机器人发送交互卡片。若未配置公网报告地址，卡片只提示报告已保存在部署主机；若配置，则附带 `/api/v1/reports/{filename}` 下载链接。公网地址必须由带 TLS 和身份认证的反向代理提供，不应直接暴露当前本机服务。平台创建和安全设置以 [飞书自定义机器人官方指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot) 为准。

### 5.2 飞书应用

填写：

- App ID；
- App Secret；
- 接收 ID；
- 接收 ID 类型：`chat_id`、`open_id`、`user_id`、`union_id` 或 `email`。

应用通道会申请 tenant access token，有报告时上传并发送 Word 文件，无新增时发送文本回执。应用必须具备发送消息和文件相关权限，并已安装到目标租户。

## 6. SMTP 配置

字段：

- 主机、端口；
- 加密方式：SSL、STARTTLS、明文；
- 用户名、密码/应用授权码；
- 发件人；
- 收件人，多个地址使用逗号或分号分隔；
- 超时，默认 30 秒。

就绪条件：主机、发件人、至少一个收件人存在；填写用户名时必须同时有密码。建议优先使用 SSL 或 STARTTLS，并使用服务商生成的应用专用密码。

测试会真实发送一封标题包含“配置中心连通性测试”的邮件。发送前网页会二次确认。

## 7. 钉钉配置

填写机器人 Webhook；如果机器人启用了加签，填写加签密钥。系统按照钉钉签名规则附加毫秒时间戳和 HMAC-SHA256 签名，并发送 Markdown 消息。机器人创建、安全设置、关键词和 IP 白名单以 [钉钉自定义机器人官方文档](https://open.dingtalk.com/document/dingstart/custom-bot-creation-and-installation) 为准。

若平台返回非零 `errcode`，任务会记录失败且不会写成功投递账本。

## 8. 企业微信配置

填写群机器人 Webhook。系统发送 `msgtype=markdown` 的标准 JSON；平台返回非零 `errcode` 时视为失败。机器人创建步骤以 [企业微信开发者中心群机器人文档](https://developer.work.weixin.qq.com/document/path/91770) 为准。

Webhook URL 通常包含访问密钥，因此读取接口不会返回原文。

## 9. 通用 Webhook 配置

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

## 10. 环境变量兼容方式

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
...
```

使用纯 Python 启动；未激活虚拟环境时必须使用项目 `.venv` 中的解释器。`BIDPILOT_CONTROL_DIR` 默认保持为项目 `data`，即使业务数据库迁移到别处，普通 `status/stop/restart` 也能发现服务：

```bash
python bootstrap.py
.venv\Scripts\python.exe -m bidpilot serve        # Windows
.venv/bin/python -m bidpilot serve                 # macOS / Linux
```

## 11. 故障排查

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
- 模型名称是否存在。

### 重启后敏感配置无法解密

确认 `data/secrets/runtime_config.key` 与数据库来自同一备份。若密钥丢失，系统不会尝试绕过加密；需在配置中心显式清除相关字段并重新填写。
