# 标擎 BidPilot 投递渠道指南

## 1. 投递语义

标擎不是“任务启动成功就算推送成功”。一次订阅执行按以下顺序完成：

1. worker 原子领取订阅租约；
2. 抓取、过滤、去重并生成本轮候选；
3. 对照 `delivery_ledger` 计算尚未成功确认投递的公告版本；
4. 有新增时生成 Word，无新增时按通知策略决定发送回执或静默；
5. 调用选定渠道；
6. 只有渠道明确成功后，才把本轮公告版本写入成功投递账本；
7. 失败时保留错误、增加失败次数并安排有限退避重试。

因此：

- 同一公告同一版本不会在成功投递后重复发送；
- 更正、中标等新生命周期版本可以再次提醒；
- 渠道失败不会把未送达内容误标为已送达；
- `on_change` 模式在无新增时不外发，但仍记录运行完成；
- `always` 模式在无新增时发送回执，便于证明长期任务确实在运行。

## 2. 渠道对比

| 通道 ID | 适用场景 | 报告方式 | 无新增回执 | 鉴权 | 主要限制 |
|---|---|---|---|---|---|
| `local` | 本机试用、比赛演示 | 保存 DOCX | 本地记录 | 无 | 不主动提醒 |
| `feishu_webhook` | 飞书群轻量通知 | 卡片链接或主机文件名 | 卡片 | Webhook，可选签名 | 机器人通常不能直接上传 Word |
| `feishu_app` | 飞书正式应用 | 直接发送 Word | 文本消息 | App ID/Secret + tenant token | 需要应用权限与租户安装 |
| `email` | 跨平台正式通知 | SMTP 附件 | 纯文本邮件 | SMTP 用户名/授权码 | 服务商端口与反垃圾策略 |
| `dingtalk_webhook` | 钉钉群 | Markdown 链接/文件名 | Markdown | Webhook，可选加签 | 关键词、IP 白名单、频率限制 |
| `wecom_webhook` | 企业微信群 | Markdown 链接/文件名 | Markdown | Webhook | 平台频率和消息长度限制 |
| `generic_webhook` | n8n、Make、自建自动化 | JSON 中的 URL/文件名 | JSON 事件 | 可选 Bearer Token | 接收端需自行消费和幂等 |

## 3. 渠道选择规则

自然语言中可直接表达“发送到飞书/邮箱/钉钉/企业微信/自定义 Webhook”。解析器把它写入 `delivery_channel`。网页下拉框只启用已满足必填条件的通道，避免创建“看起来会推送、实际没有凭据”的虚假承诺。

兼容通道 `feishu` 的解析规则为：

- 飞书应用配置完整：使用 `feishu_app`；
- 否则使用 `feishu_webhook`；
- 两者都未配置：创建订阅时返回 422。

用户也可以在订阅中心为现有订阅切换通道。切换前目标通道必须已配置；既有成功投递账本保持不变。

## 4. 飞书群机器人

### 发送内容

有新增时发送交互卡片：标题、订阅名称、新增数量、报告名，以及可选下载链接。无新增时发送完成回执。

### 下载链接

设置 `public_base_url` 后，卡片链接为：

```text
{public_base_url}/api/v1/reports/{url-encoded-filename}
```

当前应用没有内置公网登录，因此 `public_base_url` 只能指向已经配置 TLS 和访问控制的反向代理。没有安全网关时应留空。

### 签名

填写签名密钥后，系统自动加入平台要求的时间戳和 HMAC-SHA256 签名。平台创建和安全设置参见 [飞书自定义机器人官方指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。

## 5. 飞书应用

有报告时：

1. 申请 tenant access token；
2. 上传 `.docx`；
3. 使用指定 `receive_id_type` 发送文件消息；
4. 把平台 message_id 记入投递回执。

无新增时直接发送文本。若 token、上传或发送任一步失败，本轮不会记入成功投递账本。

## 6. SMTP 邮件

有报告时发送 MIME Word 附件，无新增时发送纯文本回执。连接模式：

- `ssl`：连接建立即 TLS，常见端口 465；
- `starttls`：先连接再升级 TLS，常见端口 587；
- `plain`：不加密，仅适用于可信内网测试。

SMTP 是同步协议，系统把发送放入后台线程，避免阻塞 async worker。真实密码建议使用邮箱应用授权码。

## 7. 钉钉群机器人

请求主体：

```json
{
  "msgtype": "markdown",
  "markdown": {
    "title": "标擎 BidPilot 情报回执",
    "text": "### 标擎 BidPilot\n\n..."
  }
}
```

配置加签密钥时，系统生成毫秒时间戳并按平台规则计算签名。返回 `errcode != 0` 即失败。创建机器人和安全策略参见 [钉钉官方文档](https://open.dingtalk.com/document/dingstart/custom-bot-creation-and-installation)。

## 8. 企业微信群机器人

请求主体：

```json
{
  "msgtype": "markdown",
  "markdown": {
    "content": "### 标擎 BidPilot\n\n..."
  }
}
```

返回 `errcode != 0` 即失败。平台入口参见 [企业微信开发者中心](https://developer.work.weixin.qq.com/document/path/91770)。

## 9. 通用 Webhook

### 报告事件

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

### 无新增事件

```json
{
  "event": "bidpilot.run.no_change",
  "occurred_at": "2026-07-18T08:30:00+00:00",
  "subscription": {"name": "深圳充电桩日报"},
  "result": {
    "new_count": 0,
    "report_filename": null,
    "report_url": null
  }
}
```

接收端返回任意 2xx 即视为 HTTP 成功。设置 Bearer Token 时会发送 `Authorization: Bearer ...`。接收端应按 `event` 和业务字段自行幂等，且快速返回；长耗时流程应进入自己的异步队列。

## 10. 连通性测试

配置中心的测试按钮会：

1. 保存当前表单；
2. 要求用户确认真实外发；
3. 发送一条名为“配置中心连通性测试”的无新增回执；
4. 显示脱敏结果和延迟。

测试不会写订阅成功投递账本，也不会发送历史招标数据。API 调用方法见 [API_REFERENCE.md](API_REFERENCE.md)。

## 11. 错误与重试

为了避免敏感 URL 进入日志，HTTP 客户端异常会转换为脱敏信息，例如：

- `投递通道 generic_webhook 返回 HTTP 403`；
- `投递通道 dingtalk_webhook 连接超时`；
- `投递通道 email 连接或认证失败`。

错误中不会包含 Webhook query token、API Key 或 SMTP 密码。调度失败采用有限退避，不绕过平台频率限制，不自动破解验证码，也不绕过账号、付费或访问控制。

## 12. 上线检查单

- 通道显示“已就绪”；
- 配置中心真实测试成功；
- 订阅中心显示 worker 在线；
- `always` 与 `on_change` 符合业务预期；
- 公网报告链接已加 TLS 和身份认证；
- 群机器人关键词/IP/签名策略已核对；
- 邮箱使用应用授权码；
- Webhook 接收端具备幂等、限流和日志脱敏；
- 模拟一次失败，确认失败不写成功投递账本；
- 重启服务，确认订阅、配置、下一次时间和历史仍存在。
