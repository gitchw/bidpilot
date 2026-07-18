# 标擎 BidPilot 配置中心指南

## 1. 推荐方式：网页配置

启动服务后打开 `http://127.0.0.1:8000`，进入“配置中心”。所有模型和推送通道都可直接在网页填写，点击“保存全部配置”后立即生效，不需要编辑 `.env`，也不需要重启服务。

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

首次保存敏感值时，系统在 `data/secrets/runtime_config.key` 生成本机 Fernet 密钥，以仅当前用户可读为目标设置文件权限，再把密文写入 SQLite。API 和网页只返回“已配置/未配置”，不会回显原文。

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

这不是公网账号系统。默认应用仅监听 `127.0.0.1`；如需多人或公网使用，必须在反向代理增加：

- HTTPS/TLS；
- 登录与角色权限；
- CSRF/来源校验；
- 请求限流；
- 访问与变更审计；
- 独立密钥管理系统。

## 3. AI 模型配置

### 必填项

- API 基础地址：OpenAI-compatible 服务的版本根，例如 `http://127.0.0.1:8045/v1`。
- 模型名称：服务实际支持的模型 ID。

### 可选项

- API 密钥：本机无鉴权服务可以留空；云服务通常必须填写。
- 超时：3～120 秒，默认 30 秒。
- 意图辅助模式：`auto`（推荐，只在低置信/缺失/冲突时调用）、`off`（完全关闭意图模型）、`always`（每次复核，但高置信规则字段仍锁定）。
- 意图置信阈值：0.50～0.99，默认 0.85。阈值越高越容易触发模型，调用次数与费用也可能增加；阈值不会放宽本地校验。

系统会在基础地址后调用 `/chat/completions`；如果用户直接填了以 `/chat/completions` 结尾的完整端点，则不会重复拼接。请求主体使用标准 `model`、`messages`、`temperature` 和 `max_tokens` 字段，响应需至少包含 `choices[0].message.content`。可参考 [OpenAI Chat Completions API](https://developers.openai.com/api/reference/resources/chat)。

点击“测试模型连接”时，网页会先保存当前表单，再发送固定测试句。测试不发送任何招投标内容。成功后显示延迟和短回复；失败只显示脱敏诊断，不返回密钥或完整认证 URL。

### 混合意图引擎如何使用模型

1. `rules-v2` 先生成完整、可独立执行的规则基线。
2. `auto` 模式只检查低于阈值、缺失或冲突的字段；`always` 模式会请求复核，但不自动解锁高置信字段。
3. 模型必须返回严格 JSON；Markdown 代码围栏、解释性前缀、额外字段和非法枚举都会让整份提议失效。
4. 本地逐字段验证主题是否来自原句、地域是否存在于行政区词表、日期是否合法有界、计划字段是否完整、渠道/公告类型是否属于允许枚举。
5. `region_code` 永远由本地词表派生，模型没有输出该字段的权限。
6. 超时、网络错误、非法 JSON 或提议被拒绝时，任务继续使用规则结果。

意图模型只收到自然语言原句、规则基线、当前时间、允许修复字段和 schema；不会收到抓取到的标讯正文、机会备注、订阅历史、API Key 或 Webhook。若模型服务位于云端，原句会离开本机，请先确认其隐私政策；敏感查询可把模式设为 `off`。

模型还有独立的“证据约束摘要”用途：它只在任务抓取阶段对证据生成摘要，并受事实回指门控。意图解析成功不代表摘要一定使用模型，反之亦然。

### 模型失败时的行为

模型是增强项，不是单点故障：

- 无模型配置：使用本地证据抽取摘要；
- 模型超时、HTTP 错误或格式异常：自动回退到证据抽取；
- 模型摘要出现证据中不存在的数字：拒绝该摘要并回退；
- 模型配置保存后：后续运行立即使用，无需重启。

## 4. 飞书配置

### 4.1 群机器人

填写：

- 机器人 Webhook；
- 签名密钥（如果在飞书安全设置中启用了“签名校验”）；
- 公网报告地址（可选）。

群机器人发送交互卡片。若未配置公网报告地址，卡片只提示报告已保存在部署主机；若配置，则附带 `/api/v1/reports/{filename}` 下载链接。公网地址必须由带 TLS 和身份认证的反向代理提供，不应直接暴露当前本机服务。平台创建和安全设置以 [飞书自定义机器人官方指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot) 为准。

### 4.2 飞书应用

填写：

- App ID；
- App Secret；
- 接收 ID；
- 接收 ID 类型：`chat_id`、`open_id`、`user_id`、`union_id` 或 `email`。

应用通道会申请 tenant access token，有报告时上传并发送 Word 文件，无新增时发送文本回执。应用必须具备发送消息和文件相关权限，并已安装到目标租户。

## 5. SMTP 配置

字段：

- 主机、端口；
- 加密方式：SSL、STARTTLS、明文；
- 用户名、密码/应用授权码；
- 发件人；
- 收件人，多个地址使用逗号或分号分隔；
- 超时，默认 30 秒。

就绪条件：主机、发件人、至少一个收件人存在；填写用户名时必须同时有密码。建议优先使用 SSL 或 STARTTLS，并使用服务商生成的应用专用密码。

测试会真实发送一封标题包含“配置中心连通性测试”的邮件。发送前网页会二次确认。

## 6. 钉钉配置

填写机器人 Webhook；如果机器人启用了加签，填写加签密钥。系统按照钉钉签名规则附加毫秒时间戳和 HMAC-SHA256 签名，并发送 Markdown 消息。机器人创建、安全设置、关键词和 IP 白名单以 [钉钉自定义机器人官方文档](https://open.dingtalk.com/document/dingstart/custom-bot-creation-and-installation) 为准。

若平台返回非零 `errcode`，任务会记录失败且不会写成功投递账本。

## 7. 企业微信配置

填写群机器人 Webhook。系统发送 `msgtype=markdown` 的标准 JSON；平台返回非零 `errcode` 时视为失败。机器人创建步骤以 [企业微信开发者中心群机器人文档](https://developer.work.weixin.qq.com/document/path/91770) 为准。

Webhook URL 通常包含访问密钥，因此读取接口不会返回原文。

## 8. 通用 Webhook 配置

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

## 9. 环境变量兼容方式

网页是推荐入口，以下方式用于自动部署。复制 `.env.example` 为 `.env`，但绝不提交真实 `.env`：

```text
BIDPILOT_LLM_BASE_URL=
BIDPILOT_LLM_API_KEY=
BIDPILOT_LLM_MODEL=
BIDPILOT_INTENT_LLM_MODE=auto
BIDPILOT_INTENT_LLM_CONFIDENCE_THRESHOLD=0.85
BIDPILOT_FEISHU_WEBHOOK_URL=
BIDPILOT_SMTP_HOST=
...
```

使用纯 Python 启动，Windows、macOS、Linux 命令一致：

```bash
python bootstrap.py
python -m bidpilot serve
```

## 10. 故障排查

### 保存返回 403

编辑令牌过期。刷新配置中心，网页会自动获取新令牌并重试一次。API 客户端需重新调用 `/api/v1/config/edit-token`。

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
