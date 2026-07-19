# 标擎 BidPilot API 参考（v0.7.0）

本文档对应当前代码中的 50 个 OpenAPI 操作。启动服务后，可在 `http://127.0.0.1:8000/docs` 使用同样的中文说明和交互式调试界面，也可访问 `/openapi.json` 获取机器可读定义。每个接口均说明用途、输入、返回、副作用、常见错误和示例；“副作用”不是警告装饰，而是告诉调用者该请求是否会抓取外部站点、写数据、真实推送、打开可见浏览器或停止进程。

## 1. 调用约定

- 默认地址：`http://127.0.0.1:8000`。
- 默认业务数据位于 `data/bidpilot.db`，报告位于 `outputs/reports`，生命周期控制记录位于 `data/runtime` 与 `data/secrets/control.token`。控制目录与可选业务数据目录解耦，避免迁移数据库后普通 `status/stop` 找不到服务。
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
- 返回：调度状态、订阅计数、`delivery_channels` 和安全的 `intent_engine` 计数；通道只返回 `configured`，不返回凭据，意图状态不返回模型地址或原始回复。
- 副作用：无，只读数据库心跳和内存配置。
- 错误：数据库不可用时返回 500。
- 示例：`curl http://127.0.0.1:8000/api/v1/system/status`

### `POST /api/v1/system/shutdown` — 优雅停止本机服务

- 用途：只供跨平台 `bidpilot stop` 命令调用，让当前可管理 Uvicorn 先停止接收新请求，再执行内嵌 worker 和数据库生命周期清理。未激活虚拟环境时，应使用启动画面打印的完整 `.venv` Python 命令。
- 参数：无 JSON 请求体；必须从 `127.0.0.1`/`::1` 发起，并提供请求头 `X-BidPilot-Control-Token`。令牌位于本机 `data/secrets/control.token`，不应复制到网页、脚本仓库或远程主机。
- 返回：HTTP 202 和 `{"accepted":true,"message":"已接收停止请求，正在完成清理"}`；返回后连接会在数秒内不可用，这是成功现象。
- 副作用：停止 Web 进程及其内嵌长期任务 worker；不会删除 SQLite、报告、订阅、机会、投递账本或配置。
- 错误：403 表示不是回环请求或控制令牌错误；409 表示服务由普通 `uvicorn ...` 启动、没有可控退出回调，此时必须在启动终端按 `Ctrl+C`。
- 示例：日常用户不要手写令牌请求；在同一项目目录运行启动画面打印的准确命令，例如 Windows 的 `.venv\Scripts\python.exe -m bidpilot stop`。系统不会在失败后按 PID 强杀未知进程。

### `GET /api/v1/sources/status` — 来源运行状态

- 用途：查看每个数据源的官方/行业属性、公开/授权模式、最近运行和抓取/保留数量。
- 参数：无。
- 返回：来源状态列表。未授权源明确返回 `auth_required`，不会伪装成成功。
- 副作用：无；查看状态不会临时访问来源站点。
- 错误：数据库不可用时返回 500。
- 示例：`curl http://127.0.0.1:8000/api/v1/sources/status`

### `GET /api/v1/sources/health` — 来源健康历史与趋势

- 用途：聚合每个来源最近的真实运行样本，区分健康、降级、持续失败、等待授权、按地域跳过和无数据。
- 参数：可选查询参数 `window`，表示每个来源最多统计多少次运行，范围 5–100，默认 20。
- 返回：总体摘要，以及逐来源状态计数、健康率、完成率、平均/P95 耗时、候选保留率、趋势和逐次诊断。
- 副作用：无；只读取 SQLite 历史，不访问外站、不刷新授权、不执行检索。
- 错误：数据库不可读时返回 500；没有历史返回 `no_data`，不算错误。
- 示例：`curl "http://127.0.0.1:8000/api/v1/sources/health?window=20"`

### `GET /api/v1/sources/authorizations` — 来源授权总览

- 用途：让来源中心区分“无需授权”“支持受管授权”“正在登录”“已授权”“已过期”和“原站有工作台但授权不能增强当前检索”。
- 参数：无请求体、无查询参数、无需编辑令牌。
- 返回：每个来源的 `source_id`、`source_name`、`managed`、`state`、官方 `login_url`、`authorization_scope`、授权/过期/测试时间、测试结论和脱敏消息。响应不含 Cookie 名称、Cookie 值、账号、密码、验证码、CA 信息或本机密钥。
- 副作用：无；只读内存状态和 SQLite，不会打开浏览器、刷新会话或访问来源网站。
- 错误：数据库不可用时返回 500。
- 示例：`curl http://127.0.0.1:8000/api/v1/sources/authorizations`

### `POST /api/v1/sources/{source_id}/auth/start` — 开始可见浏览器授权

- 用途：在运行 BidPilot 的同一台电脑上打开独立可见 Chromium。用户必须亲自登录、扫码、输入验证码，或在原站支持时按自己的权限使用 CA。
- 路径参数：`source_id` 当前支持 `qianlima` 与 `cecbid`。其他来源只有在适配器真实消费会话并能增强检索后才会加入，不能因为原站有“登录”按钮就伪装为支持。
- 请求头：必须包含 10 分钟短期 `X-BidPilot-Config-Token`，且请求必须来自 `127.0.0.1`、`::1` 或同源本机网页。
- 返回：`session_id`、来源、`authorizing` 状态、开始时间、15 分钟过期时间、官方登录地址和下一步提示。
- 副作用：启动一个本机可见浏览器进程并导航到官方登录页；不会自动填写或提交账号、密码、手机、验证码，不会识别验证码，不会绕过付费墙、角色权限、访问控制或频率限制。
- 错误：403 表示不是回环请求或编辑令牌无效；409 表示来源不支持、未安装 `.[auth]`、未安装 Chromium 或浏览器无法启动。
- 示例：先调用 `POST /api/v1/config/edit-token`，再发送 `POST /api/v1/sources/qianlima/auth/start` 并携带返回令牌。

### `GET /api/v1/sources/auth/sessions/{session_id}` — 查看授权窗口状态

- 用途：查询一个授权窗口是否仍在进行、已经完成、失败或超过 15 分钟。
- 路径参数：`session_id` 来自开始授权接口；没有请求体。
- 返回：与开始接口相同的脱敏 `SourceAuthSessionView`。服务重启后，尚未点击完成的临时会话不会恢复。
- 副作用：只会关闭已经过期的临时浏览器，不读取、不保存 Cookie。
- 错误：404 表示会话不存在、已经被清理或服务重启后失效。
- 示例：`curl http://127.0.0.1:8000/api/v1/sources/auth/sessions/<session_id>`

### `POST /api/v1/sources/auth/sessions/{session_id}/complete` — 完成并加密保存授权

- 用途：只在用户明确确认登录完成后，从临时浏览器上下文读取允许域名的 Cookie，使用本机 Fernet 密钥加密写入 SQLite，然后关闭浏览器。
- 路径参数：`session_id`；请求头必须含短期编辑令牌，并且只允许回环请求；无请求体。
- 返回：`completed` 或 `failed` 状态和脱敏说明。即使成功也不返回 Cookie 名称或值。
- 副作用：写入 `source_authorizations` 表及 `data/secrets/source_auth.key`；旧版本千里马明文 Cookie 文件首次读取后会迁移到加密表并删除明文文件。不会保存账号、密码、验证码、短信、二维码内容或 CA 私钥。
- 错误：403 表示请求来源/令牌不符合要求；404 表示会话不存在；409 表示没有检测到允许域名的会话 Cookie，通常意味着尚未登录成功。
- 示例：`POST /api/v1/sources/auth/sessions/<session_id>/complete`，请求头同开始接口。

### `POST /api/v1/sources/{source_id}/auth/test` — 真实测试授权

- 用途：携带已加密保存的会话执行一次关键词为“服务器”的有界真实搜索，验证原站不再要求登录，并且确实读取到会员可见内容，而不是只检查“数据库里有 Cookie”。
- 路径参数：`source_id`；必须从本机携带短期编辑令牌；无请求体。
- 返回：`source_id`、`success`、`passed/failed`、脱敏诊断和耗时。测试结果会写回授权状态供网页展示。
- 副作用：会访问来源网站，可能消耗少量免费账号查询次数；遵守全局限速和有界重试，不下载付费文件、不执行投标操作。
- 错误：403 表示请求来源/令牌问题；409 表示未保存授权或来源不支持；502 表示站点仍要求登录、没有证明详情解锁或网络/站点结构变化。
- 示例：`POST /api/v1/sources/cecbid/auth/test`，请求头 `X-BidPilot-Config-Token: <token>`。

### `DELETE /api/v1/sources/{source_id}/auth` — 清除来源授权

- 用途：关闭该来源仍在进行的授权窗口，永久删除本机 SQLite 中的加密 Cookie，并让后续检索立即恢复公开/未授权模式。
- 路径参数：`source_id`；必须从本机携带短期编辑令牌；无请求体。
- 返回：该来源最新的 `not_authorized` 脱敏状态。
- 副作用：删除本机授权数据；不会注销或删除原网站账号，不修改原网站密码，也不会影响其他来源。
- 错误：403 表示请求来源/令牌问题；409 表示来源没有受管授权流程。
- 示例：`DELETE /api/v1/sources/qianlima/auth`，请求头 `X-BidPilot-Config-Token: <token>`。

## 3. 意图解析与即时运行

### `POST /api/v1/intent/parse` — 解析中文意图

- 用途：先用确定性规则拆成主题、地域、时间、公告类型、排除词、计划和投递通道；低置信、缺失或冲突字段才交给 LLM 提议修复，所有提议再经过本地校验。
- 请求：`query` 必填，2～500 字；`delivery_channel` 仅为请求模型兼容字段，本接口不执行投递。
- 返回：`TenderQuerySpec`、字段置信度、警告、`parser_version` 和 `resolution`。`resolution` 解释调用原因、模型状态、字段级接受/拒绝/锁定决定和耗时，不包含 API Key、端点或模型原文。
- 副作用：不抓取、不生成报告、不创建订阅；`auto/always` 模式满足条件时，会把原问题、规则基线和当前时间发送给用户配置的模型服务。不会发送标讯正文、机会备注、订阅历史或密钥。
- 错误：422 表示问题过短、规则日期/计划非法或 JSON 格式错误。模型超时、HTTP 错误、Markdown 包裹、额外字段或非法 JSON 不返回 502，而是在 `resolution.llm_status` 中披露并安全回退。
- 示例：见下方 `curl`。它只解析意图，不会开始检索或创建长期任务。

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
  "parser_version": "hybrid-v1",
  "resolution": {
    "mode": "hybrid",
    "llm_status": "confirmed",
    "trigger_reasons": ["主题置信度 0.82 低于阈值 0.85"],
    "decisions": [],
    "latency_ms": 2800,
    "summary": "LLM 复核结果与规则一致，没有改动已确认字段。"
  },
  "warnings": []
}
```

普通“招标信息/采购信息”不限制公告类型，以保留后续更正和中标生命周期；只有“只看招标公告”“中标公告”“更正公告”等明确表达才设置 `event_types`。

`llm_status` 取值：

| 值 | 中文含义 | 最终结果来源 |
|---|---|---|
| `not_needed` | 规则字段都达到阈值，没有调用模型 | 规则 |
| `disabled` | 用户在配置中心关闭意图模型 | 规则 |
| `not_configured` | 需要复核但模型地址/名称未配置 | 规则安全回退 |
| `applied` | 至少一个提议通过本地校验并真正改变字段 | 混合 |
| `confirmed` | 模型有效复核，但与规则一致 | 混合复核、字段不变 |
| `rejected` | JSON 合法，但所有改动被本地校验拒绝 | 规则安全回退 |
| `invalid_response` | 不是严格 JSON、schema 多字段/少字段或枚举非法 | 规则安全回退 |
| `unavailable` | 超时、网络或模型 HTTP 故障 | 规则安全回退 |

### `POST /api/v1/intent/compare` — 对比规则与混合结果

- 用途：给开发者、测试人员和高级用户检查“模型究竟做了什么”，一次返回纯规则基线和安全合并后的最终结果。
- 请求：`query` 必填，2～500 字；`delivery_channel` 兼容接收但不参与对比。
- 返回：`rules`、`resolved` 和 `changed_fields`。`changed_fields` 只列真正变化，如 `topic`、`start_date`；地域虽被模型确认但未改变时不会虚报。
- 副作用：不抓取、不写运行、不建订阅；与解析接口一样，满足模式条件时可能调用用户配置的模型服务。
- 错误：422 仅限规则输入错误；模型失败仍返回 200 和带回退状态的 `resolved`。
- 示例：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/intent/compare \
  -H "Content-Type: application/json" \
  -d '{"query":"帮我查近四十五日泉州储能系统项目"}'
```

### `POST /api/v1/runs` — 立即执行任务

- 用途：解析问题、访问来源、过滤、去重、摘要和持久化。即时任务即使最终保留 0 条，也会生成包含诊断信息的 Word；长期订阅在无新增时是否生成/外发报告由通知策略决定。
- 请求：`query`；`delivery_channel` 可为 `local`、`feishu_webhook`、`feishu_app`、`email`、`dingtalk_webhook`、`wecom_webhook`、`generic_webhook`。
- 返回：`RunResult`，包含运行 ID、意图、记录、来源诊断、`search_explanation`、新增数、报告路径和投递回执。
- 副作用：会真实访问公开/已授权来源、写数据库、可能生成 DOCX，并可能向外部通道发送。
- 错误：422 请求非法；502 抓取、报告或投递失败。失败运行仍保存诊断。
- 示例：请求体见下方 JSON；发送到 `POST /api/v1/runs` 后会立即执行一次真实任务。

`diagnostics` 中每个来源都有三个容易混淆的数字：

- `scanned_count`：从该来源列表页或 API 实际读到多少条；
- `fetched_count`：通过来源端初筛、进入统一证据核验多少条；
- `kept_count`：最终通过时间、地域、主题、公告类型、排除词和去重后留下多少条。

`search_explanation` 是面向用户的零结果解释：

- `outcome=all_filtered`：有候选，但统一核验后一条未留；
- `outcome=no_candidates`：来源端初筛后没有候选；
- `rejection_reasons`：逐原因排除数量；
- `coverage_complete=false`：至少一个来源覆盖不完整、需要登录或失败，不能把 0 条解释成“全网没有”；
- `suggestions`：最多三条放宽时间、地域或同义词的建议。前端点击只填回查询框，不会自动发起新的外网抓取。

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

## 4. 决策智能数据

### `GET /api/v1/runs/{run_id}/evidence` — 读取本轮固定证据

- 用途：恢复某次运行当时真正返回的 TenderRecord 集合，供重启后的企业适配判断和证据问答使用，防止后来抓取的新公告串入旧对话。
- 参数：路径 `run_id`；无请求体、无需编辑令牌。
- 返回：按当轮排名保存的记录快照；当轮可信新增为 0 时返回空数组，而不是临时查询全局标讯表。
- 副作用：无；只读 `run_items`，不访问来源、不调用模型、不生成报告。
- 错误：404 表示运行不存在；损坏的单条快照会被安全跳过并保留其余证据。
- 示例：`GET /api/v1/runs/2c4d8f0a1b2c3d4e5f60718293a4b5c6/evidence`。

### `POST /api/v1/runs/{run_id}/assessments` — 生成或刷新企业适配判断

- 用途：把固定运行证据、当前企业画像和有界反馈记忆合并为逐机会适配分、建议参与/持续观察/暂不投入、风险与下一步；它不是让模型自由改写公告。
- 参数：路径 `run_id`；无请求体。`decision_assessment_mode`、最多送入模型做语义复核的条数、模型地址、名称和超时均从网页配置中心读取。超过模型窗口的可信结果不会被丢弃，而是继续逐条使用本地画像规则判断。
- 返回：整体模式与回退状态、画像版本、反馈数量、E 编号，以及每条公告由本地回填的标题、采购人、发布日期、地域、阶段和原文链接，再附本地基础适配分、范围为 -12～+12 的反馈调整、最终分、建议、画像命中、逐字证据摘录、缺口、风险和行动。模型只识别画像语义关联并摘录证据，不能提交分数、推荐、URL 或身份字段；这些字段全部由本地计算或回填。
- 副作用：可能调用用户配置的 OpenAI-compatible 模型并消耗额度；只有通过严格 JSON、全量 E 编号、画像词和逐字证据摘录校验的结果才缓存，并写回该运行。缓存键包含完整证据快照、画像、反馈、模型端点/名称和窗口。不会访问标讯来源。
- 错误：404 表示运行不存在；模型未配置、关闭、超时、额外字段、未知/缺失证据、伪造摘录或试图提交分数/推荐/URL 不会使接口 502，而会返回 `not_configured`、`disabled`、`unavailable` 或 `invalid_response` 的完整确定性判断。
- 示例：`POST /api/v1/runs/2c4d8f0a1b2c3d4e5f60718293a4b5c6/assessments`。

### `POST /api/v1/runs/{run_id}/ask` — 只根据本轮固定证据追问

- 用途：针对一次已经结束的检索运行继续提问。系统只读取该运行的 `run_items` 固定快照，不回查后来新增的全局公告，也不重新访问外部网站。模型只负责选择 E 编号和逐字原文，不能自由生成事实答案；最终文本、标题、采购人、发布日期、地域、阶段和可点击链接均由本地组装。
- 参数：路径 `run_id` 必须对应已结束运行；JSON 只能包含 `question`，规范化后长度 2～1000 字，额外字段返回 422。用户主动提交后，问题和有界证据上下文会发送给网页已配置的 OpenAI-compatible 模型；问题明文与聊天历史不会落库。显式引用 E 编号时优先使用该证据；未知 E 编号在调用模型前拒绝。
- 返回：`status` 区分 `applied/cached/not_configured/unavailable/invalid_response/insufficient_evidence/refused/empty/evidence_incomplete`；`mode` 区分模型选证与确定性抽取。`answerable` 表示是否有直接证据，`answer` 是本地组装文本，`claims[].citations[]` 含逐字摘录和本地回填的可点击原文；同时返回本轮总证据数、实际上下文数、是否截断、耗时、修复次数、缓存命中和限制说明。
- 副作用：可能调用模型并消耗额度。只有通过严格 JSON、已知 E 编号、对应证据逐字摘录和 URL 禁止校验的选择结果才写入缓存；缓存不含问题明文、模型原始响应、密钥或 Cookie。不会修改反馈、画像、机会、订阅或运行证据。
- 错误：404 表示运行不存在；409 表示运行仍在 `queued/running`，固定证据尚未完成；422 表示请求字段非法。提示注入、泄露系统提示/密钥、未知引用、完整比较超过上下文、损坏快照、模型超时或连续非法输出均返回结构化拒绝或确定性回退，不返回 502，也不会拿其他运行的数据补答案。
- 示例：`POST /api/v1/runs/2c4d8f0a1b2c3d4e5f60718293a4b5c6/ask`，请求体 `{"question":"E01 的采购人和公告阶段是什么？"}`。

### `GET /api/v1/company-profile` — 读取企业画像

- 用途：读取用户在网页维护的企业名称、产品服务、优势、目标地域、排除词、偏好买方和决策风格。
- 参数：无。
- 返回：上述字段、`version` 内容版本和 `updated_at`；首次使用返回 `version=empty` 的空画像，可直接在网页填写。
- 副作用：无；画像只从本机 SQLite 读取，不会发送给招标来源，也不会在查看时调用模型。
- 错误：数据库不可用时返回 500；空画像不是错误。
- 示例：`GET /api/v1/company-profile`。

### `PUT /api/v1/company-profile` — 保存企业画像

- 用途：完全通过网页管理 AI 决策上下文，不要求编辑 `.env`、JSON 或 Python 文件。
- 参数：请求头 `X-BidPilot-Config-Token`；JSON 字段为 `company_name`、`offerings`、`strengths`、`target_regions`、`excluded_terms`、`preferred_buyers`、`decision_focus`。`decision_focus` 仅可为 `balanced`、`growth`、`precision`，未知字段拒绝。
- 返回：去空白、去重后的画像、稳定版本和保存时间。
- 副作用：覆盖默认画像，使后续 AI 适配缓存失效；不发起检索或推送，画像不发送给任何标讯来源。
- 错误：403 表示编辑令牌缺失或过期；422 表示条目过多、过长、枚举错误或出现额外字段。
- 示例：先 `POST /api/v1/config/edit-token`，再提交 `{"company_name":"示例科技","offerings":["AI服务器"],"strengths":["信创适配"],"target_regions":["广东"],"excluded_terms":[],"preferred_buyers":["高校"],"decision_focus":"balanced"}`。

### `GET /api/v1/feedback` — 列出反馈记忆

- 用途：查看用户对真实公告做出的“相关、无关、观察、已联系”判断和可选原因，解释后续个性化评分依据。
- 参数：查询参数 `limit`，默认 500，服务收敛到 1～5000。
- 返回：反馈时间、判断、原因及对应的本地 TenderRecord；同一公告版本最多一条。
- 副作用：无；不重新评分、不调用模型、不访问来源。
- 错误：422 表示 `limit` 不是整数；孤立或损坏反馈会被跳过。
- 示例：`GET /api/v1/feedback?limit=100`。

### `PUT /api/v1/feedback/{canonical_id}/{version_hash}` — 新增或修改反馈

- 用途：让用户纠正系统；重复评价同一公告版本会更新原反馈，不累计成多票。
- 参数：路径必须对应 `tender_items` 中真实记录；请求头含短期编辑令牌；JSON `verdict` 为 `relevant`、`irrelevant`、`watch`、`contacted`，`reason` 最多 500 字且可为空。
- 返回：保存后的反馈与本地证据记录。
- 副作用：写入或覆盖一条本地反馈；后续只允许产生有界排序调整，不能突破地域、日期、公告类型或排除词硬过滤。
- 错误：403 表示令牌无效；404 表示公告证据不存在；422 表示枚举、长度或额外字段非法。
- 示例：`PUT /api/v1/feedback/<canonical_id>/<version_hash>`，请求体 `{"verdict":"relevant","reason":"符合信创服务器交付能力"}`。

### `DELETE /api/v1/feedback/{canonical_id}/{version_hash}` — 删除一条反馈

- 用途：撤销一次个性化判断。
- 参数：两个路径标识和短期编辑令牌；无请求体。
- 返回：`{"deleted":true}`。
- 副作用：永久删除这一条反馈；不删除标讯、报告、机会或画像。
- 错误：403 表示令牌无效；404 表示反馈不存在。
- 示例：`DELETE /api/v1/feedback/<canonical_id>/<version_hash>`。

### `DELETE /api/v1/feedback` — 清空全部反馈

- 用途：用户二次确认后完全重置学习记忆。
- 参数：短期编辑令牌；无请求体。网页调用前必须显示清空确认。
- 返回：`deleted_count` 实际删除条数；重复清空返回 0。
- 副作用：批量永久删除全部反馈；企业画像、标讯、运行、报告、订阅、机会和投递账本保持不变。
- 错误：403 表示令牌无效；数据库异常返回 500。
- 示例：`DELETE /api/v1/feedback`，并携带 `X-BidPilot-Config-Token`。

## 5. 报告

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

## 6. 买方雷达

### `GET /api/v1/buyers` — 查看本地买方活动雷达

- 用途：把系统已经抓取并保存在本机的真实公告按采购单位聚合，帮助用户看清“哪些单位最近在采购、处于什么公告阶段、反复关注哪些主题”。它不是联网企业查询，也不会生成联系人或预测未来采购。
- 参数：`search` 可按采购单位名称、高频主题或来源名称筛选，最长 100 字；`limit` 表示最多返回多少个采购单位，范围 1～200、默认 100；`activity_limit` 表示每个采购单位最多展示多少条近期公告证据，范围 1～100、默认 5。
- 返回：`buyers` 是采购单位卡片；每张卡包含本地稳定的 `buyer_id`、公告数、内容版本数、估算项目数、生命周期阶段统计、高频主题、来源和近期证据。同时返回采购单位识别覆盖率、未识别公告数和损坏历史版本数，避免把不完整数据冒充全量市场。
- 副作用：无。只读本机 SQLite 的 `tender_items`；不会访问招投标网站、刷新登录授权、调用大模型、创建订阅或修改机会工作台。
- 错误：422 表示查询文字过长或数量超出范围；数据库不可读时返回 500。单条历史快照损坏不会让整个接口失败，而会被排除并计入 `invalid_version_count`。
- 示例：`GET /api/v1/buyers?search=大学&limit=20&activity_limit=5`。先从响应的 `buyers[].buyer_id` 取得买方 ID，再用下一条接口创建监控任务。

### `POST /api/v1/buyers/{buyer_id}/subscriptions` — 从真实买方创建精准监控

- 用途：选择买方雷达中已经由本地公告证据确认的采购单位，创建长期监控任务。系统会把该单位写入不可由客户端伪造的 `spec.buyer_keywords` 精确过滤条件，并继续复用统一的混合意图解析、检索计划、来源授权、调度和增量投递账本。
- 参数：路径中的 `buyer_id` 必须是上一条接口返回的 24 位小写十六进制本地哈希。JSON 包含 `name`（1～100 字）、带有“每天/每周/每月/未来某时刻”等明确计划的 `query`（2～500 字）、`delivery_channel`、`delivery_policy`（`always` 或 `on_change`）以及 `run_immediately`。客户端不得提交 `buyer_keywords` 改写买方身份。
- 返回：标准 `Subscription`。重点检查 `spec.buyer_keywords` 是否只含所选采购单位；`spec.resolution.decisions` 中也会记录该字段由本地证据锁定，便于审计。
- 副作用：会保存一条持久订阅；意图解析可能调用用户已配置的大模型。`run_immediately=true` 只将任务设为立即到期，持久 worker 领取后才会访问来源、生成报告并按配置投递。
- 错误：404 表示该 `buyer_id` 不在当前本地雷达；422 表示字段非法、查询没有明确调度计划、投递通道未配置、客户端企图提交额外字段，或买方名称无法形成可靠查询。后续来源登录、网络或投递失败会写入该订阅的运行日志。
- 示例：`POST /api/v1/buyers/0123456789abcdef01234567/subscriptions`，请求体可为 `{"name":"安徽大学采购监控","query":"每天9点汇总最近30天服务器采购公告","delivery_channel":"local","delivery_policy":"on_change","run_immediately":false}`。

## 7. 长期订阅

### `POST /api/v1/subscriptions` — 创建订阅

- 用途：保存每天、每周、每月或一次性未来计划；同规则同通道重复提交会复用原订阅。
- 请求：`name`、`query`、`delivery_channel`、`delivery_policy`（`always`/`on_change`）、`run_immediately`。
- 返回：订阅 ID、解析规则、启用状态、下一次时间和最近状态。
- 副作用：写订阅表；立即模式把首轮置为到期，由持久 worker 领取。
- 错误：422，无法形成计划、通道未配置或字段非法。
- 示例：将下方 JSON 发送到 `POST /api/v1/subscriptions`；`local` 表示只在本机报告中心保存结果。

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
- 示例：`curl -X PATCH http://127.0.0.1:8000/api/v1/subscriptions/<subscription_id> -H "Content-Type: application/json" -d '{"name":"深圳充电桩工作日报","delivery_policy":"on_change"}'`。

### `POST /api/v1/subscriptions/{subscription_id}/run` — 手动运行订阅

- 用途：立即执行已保存订阅，并与后台 worker 租约互斥。
- 参数：`subscription_id`；无请求体。
- 返回：本轮 `RunResult`。
- 副作用：真实抓取、写运行/标讯/报告，并可能外发；成功投递后才记账。
- 错误：404 不存在；409 正在执行；502 本轮失败。
- 示例：`curl -X POST http://127.0.0.1:8000/api/v1/subscriptions/<subscription_id>/run`。收到 409 时等待当前任务结束，不要并发重试。

### `POST /api/v1/subscriptions/{subscription_id}/pause` — 暂停

- 用途：停止后续自动领取，保留规则、历史和增量账本。
- 参数：`subscription_id`。
- 返回：`enabled=false` 的订阅。
- 副作用：持久化修改启用状态。
- 错误：404 不存在；409 正在执行。
- 示例：`curl -X POST http://127.0.0.1:8000/api/v1/subscriptions/<subscription_id>/pause`。暂停不会删除历史报告或投递账本。

### `POST /api/v1/subscriptions/{subscription_id}/resume` — 恢复

- 用途：重新启用并计算下次时间。
- 请求：`{"run_immediately": false}`；true 表示尽快由 worker 领取。
- 返回：恢复后的订阅。
- 副作用：修改启用状态和下一次时间。
- 错误：404 不存在；409 正在执行；422 计划非法。
- 示例：`curl -X POST http://127.0.0.1:8000/api/v1/subscriptions/<subscription_id>/resume -H "Content-Type: application/json" -d '{"run_immediately":false}'`。

### `DELETE /api/v1/subscriptions/{subscription_id}` — 删除

- 用途：永久删除订阅及其成功投递增量账本。
- 参数：`subscription_id`。
- 返回：`{"deleted": true}`。
- 副作用：不可逆；重建同规则后可能再次推送历史版本。网页端要求二次点击。
- 错误：404 不存在；409 正在执行。
- 示例：`curl -X DELETE http://127.0.0.1:8000/api/v1/subscriptions/<subscription_id>`。删除前应先导出或查看运行日志与投递回执。

### `GET /api/v1/subscriptions/{subscription_id}/runs` — 运行日志

- 用途：定位自动/手动执行、失败和新增数量。
- 参数：`subscription_id`；`limit` 默认 20，范围 1～100。
- 返回：按时间倒序的运行记录。
- 副作用：无。
- 错误：404 不存在；422 limit 非法。
- 示例：`curl "http://127.0.0.1:8000/api/v1/subscriptions/<subscription_id>/runs?limit=20"`。

### `GET /api/v1/subscriptions/{subscription_id}/deliveries` — 投递回执

- 用途：审计是否真实投递、是否因 `on_change` 跳过和外部消息 ID。
- 参数：`subscription_id`。
- 返回：投递尝试列表，绝不包含凭据。
- 副作用：无。
- 错误：404 不存在。
- 示例：`curl http://127.0.0.1:8000/api/v1/subscriptions/<subscription_id>/deliveries`。

## 8. 机会工作台

### `POST /api/v1/opportunities` — 加入机会

- 用途：从已抓取标讯创建项目级机会；同项目重复加入返回原机会。
- 请求：RunResult 中的 `canonical_id` 和 `version_hash`。客户端不能上传自造快照。
- 返回：机会和当前最新项目记录。
- 副作用：写机会表，但不覆盖已有人工跟进字段。
- 错误：404，指定标讯版本不存在。
- 示例：`curl -X POST http://127.0.0.1:8000/api/v1/opportunities -H "Content-Type: application/json" -d '{"canonical_id":"<canonical_id>","version_hash":"<version_hash>"}'`。两个值必须来自真实检索结果。

### `GET /api/v1/opportunities` — 筛选机会

- 用途：看板与搜索。
- 参数：可选 `stage`（new/following/bidding/won/lost/archived）和 `search`。
- 返回：匹配机会及最新项目快照。
- 副作用：无。
- 错误：422，stage 非法。
- 示例：`curl "http://127.0.0.1:8000/api/v1/opportunities?stage=following&search=充电桩"`。不传参数则列出全部机会。

### `GET /api/v1/opportunities/{opportunity_id}` — 机会详情

- 用途：读取快照、阶段、负责人、下一步、标签和备注。
- 参数：`opportunity_id`。
- 返回：`Opportunity`。
- 副作用：无。
- 错误：404，不存在。
- 示例：`curl http://127.0.0.1:8000/api/v1/opportunities/<opportunity_id>`。

### `PATCH /api/v1/opportunities/{opportunity_id}` — 更新跟进

- 用途：修改 `stage`、`owner`、`next_action_at`、`notes`、`tags`、`is_read`。
- 请求：路径中的 `opportunity_id` 为机会卡片本地 ID；JSON 只需提交要修改的字段。`stage` 可为 `new/following/bidding/won/lost/archived`，`tags` 是字符串数组，`next_action_at` 使用 ISO 8601 时间或 null；不允许提交原始公告标题、采购人、评分或 URL。
- 返回：更新后的机会。
- 副作用：写人工跟进状态，不修改原始证据；后续公告刷新不覆盖人工字段。
- 错误：404 不存在；422 枚举、日期或长度非法。
- 示例：`curl -X PATCH http://127.0.0.1:8000/api/v1/opportunities/<opportunity_id> -H "Content-Type: application/json" -d '{"stage":"following","owner":"王同学","tags":["重点"],"is_read":true}'`。

### `DELETE /api/v1/opportunities/{opportunity_id}` — 删除工作台卡片

- 用途：移除已经确认不再跟进的机会卡片。网页会先展示影响范围并要求再次点击确认；日常整理优先使用“归档保留”。
- 参数：路径中的 `opportunity_id` 是机会卡片本地 ID；没有请求体。不要传 `canonical_id` 或 `project_key`，以免把工作台操作误解为删除原始公告。
- 返回：HTTP 200 和 `{"deleted":true}`。删除后再读取该机会或它的工作台时间线会返回 404。
- 副作用：只删除 `opportunities` 表中这一张卡片，同时丢弃它的阶段、负责人、下一步时间、备注、标签和已读状态。不会删除 `tender_items` 原始标讯、`runs/run_items` 运行证据、报告、反馈、订阅或投递账本；同一真实标讯以后可以重新加入并生成新的机会 ID。
- 错误：404 表示机会不存在或已经删除；数据库不可用时返回 500。网络中断时不要盲目重复删除，应先用 GET 确认卡片是否仍存在。
- 示例：`DELETE /api/v1/opportunities/8f1c...`。成功后重新执行 `GET /api/v1/opportunities` 刷新看板。

### `GET /api/v1/opportunities/{opportunity_id}/timeline` — 生命周期

- 用途：查看同项目的采购意向、招标、更正、中标和合同事件。
- 参数：`opportunity_id`。
- 返回：按发布时间升序的 `TenderRecord`，每条保留原文 URL。
- 副作用：无。
- 错误：404，不存在。
- 示例：`curl http://127.0.0.1:8000/api/v1/opportunities/<opportunity_id>/timeline`。

## 9. 配置中心

### `GET /api/v1/config` — 读取脱敏配置

- 用途：读取模型和六类通道的非敏感字段与就绪状态。
- 参数：无请求体、无查询参数，也不需要编辑令牌；该只读接口只接受本机服务当前可见配置。
- 返回：`RuntimeConfigView`；敏感字段只有 `configured`。
- 副作用：无。
- 错误：数据库不可用时 500。
- 示例：`curl http://127.0.0.1:8000/api/v1/config`。返回中的 `llm_api_key.configured=true` 只表示已保存，不会回显密钥。

### `POST /api/v1/config/edit-token` — 获取编辑令牌

- 用途：同源网页在写配置前取得 10 分钟短期令牌。
- 参数：无。
- 返回：`edit_token`、`expires_in`；响应禁止缓存。
- 副作用：只在当前进程内登记令牌，重启即失效。
- 错误：通常无业务错误。
- 示例：`curl -X POST http://127.0.0.1:8000/api/v1/config/edit-token`。只应在本机使用返回令牌，且不要写入日志或仓库。

### `PUT /api/v1/config` — 保存配置

- 用途：局部保存白名单字段，立即生效并持久化。
- 请求头：`X-BidPilot-Config-Token`。
- 请求：敏感字段留空/省略表示保持；清除必须使用 `clear_secrets`。
- 返回：脱敏后的最新配置。
- 副作用：写 `runtime_config`；敏感值用本机 Fernet 密钥加密后存入 SQLite。
- 错误：403 令牌无效；422 未知字段、URL、端口、超时或枚举非法。
- 示例：先获取编辑令牌，再将下方 JSON 发送到 `PUT /api/v1/config` 并设置请求头 `X-BidPilot-Config-Token: <token>`。

```json
{
  "llm_base_url": "http://127.0.0.1:8045/v1",
  "llm_model": "your-compatible-model",
  "llm_api_key": "<仅写入，不回显>",
  "intent_llm_mode": "auto",
  "intent_llm_confidence_threshold": 0.85,
  "clear_secrets": []
}
```

### `POST /api/v1/config/model/test` — 模型连通测试

- 用途：真实调用当前模型的 `/chat/completions`。
- 请求头：短期编辑令牌；无请求体。
- 返回：成功、延迟和固定测试回复预览。
- 副作用：向模型服务发送固定测试句，可能消耗极少量额度；不发送招标数据。
- 错误：403 令牌；422 配置不完整；502 超时、HTTP 或响应格式错误。
- 示例：`curl -X POST http://127.0.0.1:8000/api/v1/config/model/test -H "X-BidPilot-Config-Token: <token>"`。

### `POST /api/v1/config/channels/{channel}/test` — 通道测试

- 用途：真实发送“配置中心连通性测试”无新增回执。
- 参数：`channel` 为 `feishu_webhook`、`feishu_app`、`email`、`dingtalk_webhook`、`wecom_webhook`、`generic_webhook`。
- 返回：实际通道、消息、延迟和成功状态。
- 副作用：真实外发；网页会在调用前二次确认。
- 错误：403 令牌；422 通道未配置/不支持；502 网络、认证或平台响应失败。
- 示例：`curl -X POST http://127.0.0.1:8000/api/v1/config/channels/feishu_webhook/test -H "X-BidPilot-Config-Token: <token>"`。该请求会向已配置飞书群真实发送一条测试消息。

## 10. 配置写入示例

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
