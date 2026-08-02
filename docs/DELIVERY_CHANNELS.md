# 标擎 BidPilot 投递渠道与可靠性指南

版本：v0.8.0

更新时间：2026-07-24

这份指南回答四个实际问题：可以同时发到哪里、文件和链接有什么差别、一个渠道失败后系统会怎么做、用户如何在网页恢复单个死信。日常使用不需要编辑配置文件。

## 1. 先理解“多目标”

即时任务、普通订阅和买方监控都可以同时选择 1～10 个交付目标。网页会为每个目标显示能力徽章：

- “文字”：可发送运行消息或无新增回执；
- “Word”：可直接交付 `.docx` 文件；
- “链接”：可发送报告下载地址；
- “结构化事件”：通用 Webhook 发送 JSON，由接收端自行展示。

未配置渠道仍会显示，并提供“去配置”入口，但不能新选中。历史订阅里已经选中、后来失去配置的目标不会被静默删掉；用户可先修复配置，或在编辑订阅时明确移除。

API 推荐提交：

```json
{
  "delivery_targets": ["local", "telegram_bot", "email"],
  "delivery_channel": "local"
}
```

`delivery_channel` 是旧客户端兼容镜像，必须等于列表第一个目标。新代码以 `delivery_targets` 为准。

## 2. 渠道能力矩阵

| 通道 ID | 主动提醒 | 文字 | 直接 Word | 报告链接 | 必要配置 | 主要边界 |
|---|---:|---:|---:|---:|---|---|
| `local` | 否 | 否 | 是 | 是 | 无 | 只保存到报告中心 |
| `feishu_webhook` | 是 | 是 | 否 | 是 | 群机器人 Webhook | 机器人卡片不能直接上传 Word |
| `feishu_app` | 是 | 是 | 是 | 否 | App ID、Secret、接收 ID | 需要应用权限和租户安装 |
| `email` | 是 | 是 | 是 | 否 | SMTP 主机、发件人、收件人 | 受邮箱端口和反垃圾策略影响 |
| `dingtalk_webhook` | 是 | 是 | 否 | 是 | 钉钉 Webhook | 受关键词、IP、签名和频率策略影响 |
| `wecom_webhook` | 是 | 是 | 否 | 是 | 企业微信 Webhook | 受平台频率和消息长度限制 |
| `generic_webhook` | 是 | 结构化 | 否 | 是 | HTTP(S) URL | 接收端负责消费、认证和业务幂等 |
| `telegram_bot` | 是 | 是 | 是 | 是 | Bot Token、Chat ID | 普通云端 Bot API 文件上限 50 MB |
| `slack_webhook` | 是 | 是 | 否 | 是 | 官方 Incoming Webhook | Webhook 绑定固定频道且不能上传文件 |

系统状态接口还会返回 `supports_text`、`supports_file`、`supports_link`、`configuration_group` 和 `delivery_semantics`，网页不靠硬编码猜能力。

## 3. 可靠投递顺序

一次运行的真实顺序是：

1. worker 领取订阅租约；
2. 抓取、过滤、去重并生成证据和 Word；
3. 对每个目标分别计算尚未确认投递的公告版本；
4. 报告记录、全部目标 Outbox 和目标公告集合在首次外发前原子写入 SQLite；
5. 每个目标独立领取并外发；
6. 某目标成功时，“成功状态 + 该目标公告账本”在同一事务提交；
7. 临时失败只重试该目标，不重新抓取，也不重发成功目标；
8. 永久错误或第五次失败进入死信，等待用户修复后单目标重试。

这保证数据库内的目标级防重复，但外部平台通常没有统一幂等键。如果平台已收到、进程却在写回 SQLite 前崩溃，租约接管后可能重复一次。因此外部语义诚实标记为 `at_least_once`，不宣称端到端 exactly-once。Telegram/Slack 消息会附带 BidPilot 投递编号，便于识别极小重复窗口。

自动重试采用有界指数退避：基准依次为 60 秒、5 分钟、15 分钟、1 小时和 3 小时；每次只增加正向随机抖动，最多为基准的 10%，且不超过 60 秒，避免大量失败任务在同一秒再次冲击平台。平台返回 `Retry-After` 时，系统取本地退避与平台要求中更晚的时间，绝不提前重试。

## 4. Outbox 状态与用户动作

| 状态 | 发生了什么 | 会不会自动继续 | 正确动作 |
|---|---|---|---|
| `pending` | 已安全入队，尚未领取 | 会 | 确认 worker 在线并等待 |
| `sending` | worker 正在发送 | 会 | 不要删除目标或重复点击 |
| `retrying` | 临时失败，已安排退避 | 会 | 查看下次时间，不要手工连点 |
| `succeeded` | 已确认成功并记账 | 不会 | 无需操作 |
| `dead_letter` | 永久错误或达到五次上限 | 不会 | 修复配置后点“只重试此渠道” |
| `skipped` | 无新增保持安静，或目标被移除 | 不会 | 阅读该行原因 |

即时任务在结果页按目标展示状态、错误和死信恢复按钮；订阅与买方监控在“订阅中心 → 日志 → 交付控制塔”按“运行 × 目标”展示状态、尝试次数、下次时间、脱敏错误和平台回执。日志展开期间 15 秒自动刷新不会把它关闭。两条路径都能在网页只恢复一个死信目标，不需要调用 API 或重新运行整轮检索。

### 死信恢复

1. 阅读死信错误；
2. 点击该目标对应的“去配置”，修复 Token、Webhook、Chat ID、SMTP 或权限；
3. 在配置卡先保存，再明确确认真实测试；
4. 即时任务回到结果页，订阅/买方监控回到订阅日志，只点击这一行的“只重试此渠道”；
5. 状态先变为 `retrying`，随后由 worker 领取；
6. 刷新日志确认 `succeeded` 或新的可操作错误。

按钮只重新排队原 Outbox 消息及其可选报告，不重新抓取来源，也不发送本轮已成功的其他目标。原消息没有报告时仍可恢复文字回执；原本应交付的报告已被手工删除时，系统会拒绝伪装成功并给出可操作错误。

## 5. 本地报告中心

`local` 永远可用，不主动向外部平台提醒。有新增时保存 Word；即时任务即使 0 条也会保存诊断报告。它适合首次验收、敏感环境和所有外部渠道的安全兜底。

配置中心的“报告发布与下载链接”是独立卡片，不是新的交付目标。它保存链接型渠道共用的 `public_base_url`；Telegram 和 Slack 卡片中的“设置报告链接”会准确跳到这里。只有已有 TLS、身份认证和访问控制的安全入口才能作为公网报告根地址，填写字段本身不会自动建立这些能力。

## 6. 飞书群机器人

有新增时发送交互卡片和可选报告链接，无新增时发送运行回执。配置 `public_base_url` 后，链接为：

```text
{public_base_url}/api/v1/reports/{url-encoded-filename}
```

Webhook 和可选签名密钥都属于秘密。公网地址只能指向已经配置 TLS、认证和访问控制的反向代理；当前应用不是公网多用户账号系统。平台创建说明见[飞书自定义机器人官方指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。

## 7. 飞书应用

有报告时按“tenant token → 上传 `.docx` → 发送文件消息”执行；无新增时发文本。App ID 可以显示，App Secret 永不回显。任一步失败都不会把该目标误记为成功。

## 8. SMTP 邮件

有报告时发送 MIME Word 附件，无新增时发送纯文本。支持：

- `ssl`：连接建立即 TLS，常见端口 465；
- `starttls`：连接后升级 TLS，常见端口 587；
- `plain`：不加密，只适合可信内网测试。

建议使用应用专用授权码。多个收件人可用逗号或分号分隔。SMTP 是同步协议，系统在线程中发送，不阻塞 async worker。

## 9. 钉钉与企业微信

两者通过官方群机器人 Webhook 发送 Markdown 和可选报告链接。钉钉支持加签；返回业务错误码时视为失败。不要绕过机器人关键词、IP 白名单或平台限流。参考[钉钉官方文档](https://open.dingtalk.com/document/dingstart/custom-bot-creation-and-installation)和[企业微信开发者中心](https://developer.work.weixin.qq.com/document/path/91770)。

## 10. 通用 Webhook

有报告时发送：

```json
{
  "event": "bidpilot.report.ready",
  "occurred_at": "2026-07-24T08:30:00+00:00",
  "subscription": {"name": "深圳充电桩日报"},
  "result": {
    "new_count": 3,
    "report_filename": "深圳充电桩日报_202607241630.docx",
    "report_url": "https://example.com/api/v1/reports/..."
  }
}
```

无新增时事件为 `bidpilot.run.no_change`，文件名和 URL 为 `null`。可选 Bearer Token 放在 `Authorization` 请求头。接收端应快速返回并按事件字段自行幂等；长流程应进入接收端自己的队列。

## 11. Telegram Bot

配置字段：

- Bot Token：由 BotFather 签发，只加密保存；
- Chat ID：整数私聊/群组/频道 ID，或公开频道 `@username`；
- Thread ID：论坛群话题可选，留空发到主会话；
- 静默发送：不触发声音通知；
- 保护内容：请求平台限制转发和保存。

有普通大小 Word 时调用官方 `sendDocument`；无新增时调用 `sendMessage`。超过 50 MB 时，有安全 `public_base_url` 就发送链接，没有则直接进入死信并说明恢复方法。平台 429 的 `retry_after` 或 HTTP `Retry-After` 会覆盖本地退避下限，系统不会提前重试；400/401/403/404 的 Token、Chat ID、权限错误，以及 HTTP/业务 413 的文件过大错误，都会第一次就进入死信。官方能力与参数见 [Telegram Bot API](https://core.telegram.org/bots/api/)。

Bot Token 必须出现在官方请求路径中，但任何异常、API 响应、日志和网页都只返回脱敏原因，不回显 Token。

## 12. Slack Incoming Webhook

只接受以下官方地址：

```text
https://hooks.slack.com/services/{team}/{channel}/{secret}
https://hooks.slack-gov.com/services/{team}/{channel}/{secret}
```

Webhook 与创建时选择的频道绑定，本身就是秘密。BidPilot 发送文本和可选报告链接，不伪装支持附件；没有公网地址时明确说明报告只在部署主机。订阅名中的 `<@用户>`、`<!channel>` 和 `&` 会被转义，避免用户可控文本触发意外提及。429 按 `Retry-After` 等待，其他 4xx 直接进入死信。官方说明见 [Slack Incoming Webhooks](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks)。

若以后需要 Slack 文件上传，应单独配置 Bot Token、`files:write` 和官方外部上传流程；不能把 Incoming Webhook 宣传成文件接口。参考 [Slack 文件指南](https://docs.slack.dev/messaging/working-with-files/)。

## 13. 真实连通性测试

测试按钮不会自动保存任何草稿。正确顺序是：

1. 修改当前配置卡；
2. 点击“保存本卡修改”；
3. 等状态徽章显示已就绪；
4. 点击测试按钮；
5. 在二次确认框确认真实外发；
6. 查看平台回执和延迟。

测试只发送“配置中心连通性测试”消息，不发送历史招标数据、不写订阅目标级成功账本。测试失败会显示已脱敏的具体原因；未知异常才使用通用提示。

## 14. 上线检查单

- 每个目标状态均为“已就绪”；
- 每张配置卡先保存，再分别完成真实测试；
- 订阅中心显示 worker 在线；
- 用 `local + 一个外部目标` 完成首轮验收；
- 模拟一个目标失败，确认其他成功目标没有重发；
- 修复失败配置，确认“只重试此渠道”能恢复；
- `always` 与 `on_change` 符合业务预期；
- 公网报告地址已有 TLS、认证和访问控制；
- 群机器人关键词/IP/签名策略已核对；
- 邮箱使用应用授权码；
- Webhook 接收端具备限流、业务幂等和日志脱敏；
- 重启服务，确认订阅、配置、Outbox、下一次时间和历史仍存在。

安全边界始终不变：不绕过验证码、WAF、付费墙、账号角色或频率限制，不把 Cookie、Token、Webhook、API Key 或 SMTP 密码提交到 Git、截图、报告或工单。
