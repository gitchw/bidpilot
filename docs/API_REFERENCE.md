# 标擎 BidPilot API 参考（v0.4.0）

本文档对应当前代码中的 29 个 OpenAPI 操作。启动服务后，可在 `http://127.0.0.1:8000/docs` 使用同样的中文说明和交互式调试界面，也可访问 `/openapi.json` 获取机器可读定义。

## 1. 调用约定

- 默认地址：`http://127.0.0.1:8000`。
- 默认只监听本机，不内置多用户登录。公网发布必须在前置网关增加 TLS、身份认证、限流和审计。
- JSON 请求应使用 `Content-Type: application/json`。
- 日期使用 ISO 8601；调度时区默认是 `Asia/Shanghai`。
- `400/422` 表示输入问题，`403` 表示配置编辑令牌无效，`404` 表示资源不存在，`409` 表示订阅正在执行，`502` 表示真实来源、模型或投递链路失败。
- 密钥、密码、Webhook 完整地址、签名密钥和 Bearer Token 永远不会从配置读取接口、系统状态、运行日志或错误详情返回。

## 2. 系统与来源

### `GET /health` — 健康检查

- 用途：供浏览器、容器和运维探针确认 API 进程可响应。
- 参数：无。
- 返回：`status`、版本、数据库路径、报告目录。
- 副作用：无，不访问外网、不写数据库。
- 错误：进程不可达时连接失败；接口本身无业务错误。
- 示例：`curl http://127.0.0.1:8000/health`

### `GET /api/v1/system/status` — 系统运行状态

- 用途：读取 worker 心跳、启用/到期/执行中订阅数量、时区和投递通道就绪状态。
- 参数：无。
- 返回：调度状态、订阅计数、`delivery_channels`；通道只返回 `configured`，不返回凭据。
- 副作用：无，只读数据库心跳和内存配置。
- 错误：数据库不可用时返回 500。
- 示例：`curl http://127.0.0.1:8000/api/v1/system/status`

### `GET /api/v1/sources/status` — 来源运行状态

- 用途：查看每个数据源的官方/行业属性、公开/授权模式、最近运行和抓取/保留数量。
- 参数：无。
- 返回：来源状态列表。未授权源明确返回 `auth_required`，不会伪装成成功。
- 副作用：无；查看状态不会临时访问来源站点。
- 错误：数据库不可用时返回 500。
- 示例：`curl http://127.0.0.1:8000/api/v1/sources/status`

## 3. 意图解析与即时运行

### `POST /api/v1/intent/parse` — 解析中文意图

- 用途：把自然语言拆成主题、地域、时间、公告类型、排除词、计划和投递通道。
- 请求：`query` 必填，2～500 字；`delivery_channel` 仅为请求模型兼容字段，本接口不执行投递。
- 返回：`TenderQuerySpec`、字段置信度、警告和 `parser_version`。
- 副作用：无；不抓取、不生成报告、不创建订阅。
- 错误：422，问题过短、日期/时间/每月日期非法或 JSON 格式错误。

```bash
curl -X POST http://127.0.0.1:8000/api/v1/intent/parse \
  -H "Content-Type: application/json" \
  -d '{"query":"最近1个月深圳充电桩招标信息"}'
```

关键返回字段示例：

```json
{
  "topic": "充电桩",
  "region": "深圳",
  "region_code": "440000",
  "event_types": [],
  "parser_version": "rules-v2",
  "warnings": []
}
```

普通“招标信息/采购信息”不限制公告类型，以保留后续更正和中标生命周期；只有“只看招标公告”“中标公告”“更正公告”等明确表达才设置 `event_types`。

### `POST /api/v1/runs` — 立即执行任务

- 用途：解析问题、访问来源、过滤、去重、摘要、持久化，并在有新增时生成 Word。
- 请求：`query`；`delivery_channel` 可为 `local`、`feishu_webhook`、`feishu_app`、`email`、`dingtalk_webhook`、`wecom_webhook`、`generic_webhook`。
- 返回：`RunResult`，包含运行 ID、意图、记录、来源诊断、新增数、报告路径和投递回执。
- 副作用：会真实访问公开/已授权来源、写数据库、可能生成 DOCX，并可能向外部通道发送。
- 错误：422 请求非法；502 抓取、报告或投递失败。失败运行仍保存诊断。

```json
{
  "query": "最近1个月深圳充电桩招标信息",
  "delivery_channel": "local"
}
```

### `GET /api/v1/runs` — 列出运行

- 用途：审计手动和自动任务历史。
- 参数：`limit`，默认 30，服务限制在 1～100。
- 返回：按开始时间倒序的运行记录。
- 副作用：无。
- 错误：422，`limit` 不是整数。
- 示例：`GET /api/v1/runs?limit=20`。

### `GET /api/v1/runs/{run_id}` — 运行详情

- 用途：读取一轮运行的状态、口径、计数、诊断和错误。
- 参数：`run_id`，运行接口返回的标识。
- 返回：单条运行数据库记录。
- 副作用：无。
- 错误：404，运行不存在。
- 示例：`GET /api/v1/runs/2c4d8f0a1b2c3d4e5f60718293a4b5c6`。

## 4. 报告

### `GET /api/v1/reports` — 报告历史

- 用途：列出真实生成并登记的 Word 报告。
- 参数：无。
- 返回：报告 ID、运行/订阅 ID、路径、记录数和生成时间，不返回文件正文。
- 副作用：无。
- 错误：数据库不可用时 500。
- 示例：`GET /api/v1/reports`。

### `GET /api/v1/reports/{filename}` — 下载 Word

- 用途：下载报告目录中的 `.docx`。
- 参数：`filename` 必须来自报告历史；服务剥离目录并校验解析后路径，阻止路径穿越。
- 返回：Word 二进制流。
- 副作用：只读取本机文件。
- 错误：404，文件不存在、扩展名错误或越过报告目录。
- 示例：`GET /api/v1/reports/深圳充电桩招标信息_202607181530.docx`。

## 5. 长期订阅

### `POST /api/v1/subscriptions` — 创建订阅

- 用途：保存每天、每周、每月或一次性未来计划；同规则同通道重复提交会复用原订阅。
- 请求：`name`、`query`、`delivery_channel`、`delivery_policy`（`always`/`on_change`）、`run_immediately`。
- 返回：订阅 ID、解析规则、启用状态、下一次时间和最近状态。
- 副作用：写订阅表；立即模式把首轮置为到期，由持久 worker 领取。
- 错误：422，无法形成计划、通道未配置或字段非法。

```json
{
  "name": "深圳充电桩日报",
  "query": "每天9点汇总最近1个月深圳充电桩信息",
  "delivery_channel": "local",
  "delivery_policy": "always",
  "run_immediately": true
}
```

### `GET /api/v1/subscriptions` — 订阅列表

- 用途：管理全部长期任务。
- 参数：无。
- 返回：订阅、下次时间、租约、失败次数和最近回执。
- 副作用：无。
- 错误：数据库不可用时 500。
- 示例：`GET /api/v1/subscriptions`。

### `GET /api/v1/subscriptions/{subscription_id}` — 订阅详情

- 用途：读取完整规则、状态和是否正在执行。
- 参数：`subscription_id`。
- 返回：单个订阅。
- 副作用：无。
- 错误：404，订阅不存在。
- 示例：`GET /api/v1/subscriptions/f8b24edfbe64481dbcc7d4fdee85d94f`。

### `PATCH /api/v1/subscriptions/{subscription_id}` — 编辑订阅

- 用途：局部修改名称、规则、通道或无新增策略；改规则后重算下次时间，保留既有防重复账本。
- 请求：只提交要改的 `name`、`query`、`delivery_channel`、`delivery_policy`。
- 返回：更新后的订阅。
- 副作用：更新订阅和计划时间，不立即执行。
- 错误：404 不存在；409 正在执行；422 规则或通道非法。

### `POST /api/v1/subscriptions/{subscription_id}/run` — 手动运行订阅

- 用途：立即执行已保存订阅，并与后台 worker 租约互斥。
- 参数：`subscription_id`；无请求体。
- 返回：本轮 `RunResult`。
- 副作用：真实抓取、写运行/标讯/报告，并可能外发；成功投递后才记账。
- 错误：404 不存在；409 正在执行；502 本轮失败。

### `POST /api/v1/subscriptions/{subscription_id}/pause` — 暂停

- 用途：停止后续自动领取，保留规则、历史和增量账本。
- 参数：`subscription_id`。
- 返回：`enabled=false` 的订阅。
- 副作用：持久化修改启用状态。
- 错误：404 不存在；409 正在执行。

### `POST /api/v1/subscriptions/{subscription_id}/resume` — 恢复

- 用途：重新启用并计算下次时间。
- 请求：`{"run_immediately": false}`；true 表示尽快由 worker 领取。
- 返回：恢复后的订阅。
- 副作用：修改启用状态和下一次时间。
- 错误：404 不存在；409 正在执行；422 计划非法。

### `DELETE /api/v1/subscriptions/{subscription_id}` — 删除

- 用途：永久删除订阅及其成功投递增量账本。
- 参数：`subscription_id`。
- 返回：`{"deleted": true}`。
- 副作用：不可逆；重建同规则后可能再次推送历史版本。网页端要求二次点击。
- 错误：404 不存在；409 正在执行。

### `GET /api/v1/subscriptions/{subscription_id}/runs` — 运行日志

- 用途：定位自动/手动执行、失败和新增数量。
- 参数：`subscription_id`；`limit` 默认 20，范围 1～100。
- 返回：按时间倒序的运行记录。
- 副作用：无。
- 错误：404 不存在；422 limit 非法。

### `GET /api/v1/subscriptions/{subscription_id}/deliveries` — 投递回执

- 用途：审计是否真实投递、是否因 `on_change` 跳过和外部消息 ID。
- 参数：`subscription_id`。
- 返回：投递尝试列表，绝不包含凭据。
- 副作用：无。
- 错误：404 不存在。

## 6. 机会工作台

### `POST /api/v1/opportunities` — 加入机会

- 用途：从已抓取标讯创建项目级机会；同项目重复加入返回原机会。
- 请求：RunResult 中的 `canonical_id` 和 `version_hash`。客户端不能上传自造快照。
- 返回：机会和当前最新项目记录。
- 副作用：写机会表，但不覆盖已有人工跟进字段。
- 错误：404，指定标讯版本不存在。

### `GET /api/v1/opportunities` — 筛选机会

- 用途：看板与搜索。
- 参数：可选 `stage`（new/following/bidding/won/lost/archived）和 `search`。
- 返回：匹配机会及最新项目快照。
- 副作用：无。
- 错误：422，stage 非法。

### `GET /api/v1/opportunities/{opportunity_id}` — 机会详情

- 用途：读取快照、阶段、负责人、下一步、标签和备注。
- 参数：`opportunity_id`。
- 返回：`Opportunity`。
- 副作用：无。
- 错误：404，不存在。

### `PATCH /api/v1/opportunities/{opportunity_id}` — 更新跟进

- 用途：修改 `stage`、`owner`、`next_action_at`、`notes`、`tags`、`is_read`。
- 返回：更新后的机会。
- 副作用：写人工跟进状态，不修改原始证据；后续公告刷新不覆盖人工字段。
- 错误：404 不存在；422 枚举、日期或长度非法。

### `GET /api/v1/opportunities/{opportunity_id}/timeline` — 生命周期

- 用途：查看同项目的采购意向、招标、更正、中标和合同事件。
- 参数：`opportunity_id`。
- 返回：按发布时间升序的 `TenderRecord`，每条保留原文 URL。
- 副作用：无。
- 错误：404，不存在。

## 7. 配置中心

### `GET /api/v1/config` — 读取脱敏配置

- 用途：读取模型和六类通道的非敏感字段与就绪状态。
- 返回：`RuntimeConfigView`；敏感字段只有 `configured`。
- 副作用：无。
- 错误：数据库不可用时 500。

### `POST /api/v1/config/edit-token` — 获取编辑令牌

- 用途：同源网页在写配置前取得 10 分钟短期令牌。
- 参数：无。
- 返回：`edit_token`、`expires_in`；响应禁止缓存。
- 副作用：只在当前进程内登记令牌，重启即失效。
- 错误：通常无业务错误。

### `PUT /api/v1/config` — 保存配置

- 用途：局部保存白名单字段，立即生效并持久化。
- 请求头：`X-BidPilot-Config-Token`。
- 请求：敏感字段留空/省略表示保持；清除必须使用 `clear_secrets`。
- 返回：脱敏后的最新配置。
- 副作用：写 `runtime_config`；敏感值用本机 Fernet 密钥加密后存入 SQLite。
- 错误：403 令牌无效；422 未知字段、URL、端口、超时或枚举非法。

```json
{
  "llm_base_url": "http://127.0.0.1:8045/v1",
  "llm_model": "your-compatible-model",
  "llm_api_key": "<仅写入，不回显>",
  "clear_secrets": []
}
```

### `POST /api/v1/config/model/test` — 模型连通测试

- 用途：真实调用当前模型的 `/chat/completions`。
- 请求头：短期编辑令牌；无请求体。
- 返回：成功、延迟和固定测试回复预览。
- 副作用：向模型服务发送固定测试句，可能消耗极少量额度；不发送招标数据。
- 错误：403 令牌；422 配置不完整；502 超时、HTTP 或响应格式错误。

### `POST /api/v1/config/channels/{channel}/test` — 通道测试

- 用途：真实发送“配置中心连通性测试”无新增回执。
- 参数：`channel` 为 `feishu_webhook`、`feishu_app`、`email`、`dingtalk_webhook`、`wecom_webhook`、`generic_webhook`。
- 返回：实际通道、消息、延迟和成功状态。
- 副作用：真实外发；网页会在调用前二次确认。
- 错误：403 令牌；422 通道未配置/不支持；502 网络、认证或平台响应失败。

## 8. 配置写入示例

```python
import requests

base = "http://127.0.0.1:8000"
token = requests.post(f"{base}/api/v1/config/edit-token").json()["edit_token"]
headers = {"X-BidPilot-Config-Token": token}

result = requests.put(
    f"{base}/api/v1/config",
    headers=headers,
    json={
        "llm_base_url": "http://127.0.0.1:8045/v1",
        "llm_model": "your-compatible-model",
        "llm_api_key": "your-key",
    },
)
result.raise_for_status()
assert result.json()["ai"]["llm_api_key"] == {"configured": True}
```

不要把真实密钥写入脚本、Git、截图或工单。推荐直接使用网页配置中心。
