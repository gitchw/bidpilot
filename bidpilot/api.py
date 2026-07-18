from __future__ import annotations

import asyncio
import ipaddress
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from bidpilot import __version__
from bidpilot.config import Settings, get_settings
from bidpilot.control import ControlPlane
from bidpilot.models import (
    HealthResponse,
    IntentComparison,
    Opportunity,
    OpportunityCreate,
    OpportunityStage,
    OpportunityUpdate,
    RunResult,
    SubscriptionCreate,
    SubscriptionUpdate,
    TenderQuerySpec,
    TenderRecord,
)
from bidpilot.runtime_config import (
    ConfigEditTokenManager,
    ConnectionTestResult,
    RuntimeConfigError,
    RuntimeConfigUpdate,
    RuntimeConfigView,
)
from bidpilot.scheduler import SubscriptionWorker
from bidpilot.service import BidPilotService, SubscriptionBusyError
from bidpilot.sources.base import SourceAdapter


class QueryRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "query": "最近1个月深圳充电桩招标信息",
                    "delivery_channel": "local",
                }
            ]
        }
    )

    query: str = Field(description="中文招投标需求，长度 2～500 字", min_length=2, max_length=500)
    delivery_channel: str = Field(
        default="local",
        description="本轮投递通道；解析接口不会执行该字段",
    )


class ResumeRequest(BaseModel):
    run_immediately: bool = Field(
        default=False,
        description="恢复后是否把下一次时间设为立即到期",
    )


class ConfigEditTokenResponse(BaseModel):
    edit_token: str = Field(description="仅用于本机配置写入的短期令牌")
    expires_in: int = Field(description="令牌剩余有效秒数")


OPENAPI_TAGS = [
    {"name": "系统", "description": "健康检查、运行状态与数据源诊断。"},
    {"name": "意图解析", "description": "把中文自然语言编译为可执行的结构化检索条件。"},
    {"name": "情报任务", "description": "执行多源检索、证据聚合、报告生成与投递。"},
    {"name": "报告", "description": "列出和下载系统真实生成的 Word 报告。"},
    {"name": "长期订阅", "description": "创建、编辑、暂停、恢复和审计持久化订阅。"},
    {"name": "机会工作台", "description": "把已抓取标讯转为项目级跟进机会并查看生命周期。"},
    {"name": "配置中心", "description": "安全管理模型与推送通道，并执行真实连通性测试。"},
]


def _api_docs(
    *,
    tag: str,
    summary: str,
    purpose: str,
    parameters: str,
    returns: str,
    side_effects: str,
    errors: str,
    example: str,
    responses: dict[int, str] | None = None,
) -> dict[str, Any]:
    response_docs = {
        code: {"description": description} for code, description in (responses or {}).items()
    }
    return {
        "tags": [tag],
        "summary": summary,
        "description": (
            f"### 用途\n{purpose}\n\n"
            f"### 参数与请求体\n{parameters}\n\n"
            f"### 返回值\n{returns}\n\n"
            f"### 副作用\n{side_effects}\n\n"
            f"### 常见错误\n{errors}\n\n"
            f"### 示例\n```text\n{example}\n```"
        ),
        "response_description": returns,
        "responses": response_docs,
    }


def _key_error_detail(error: KeyError) -> str:
    return str(error.args[0]) if error.args else str(error)


def create_app(
    settings: Settings | None = None,
    sources: list[SourceAdapter] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    service = BidPilotService(settings, sources=sources)
    config_tokens = ConfigEditTokenManager()
    control_plane = ControlPlane(settings)

    def require_config_token(
        x_bidpilot_config_token: Annotated[
            str | None,
            Header(alias="X-BidPilot-Config-Token"),
        ] = None,
    ) -> None:
        if not config_tokens.validate(x_bidpilot_config_token):
            raise HTTPException(status_code=403, detail="配置编辑令牌缺失、无效或已过期")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        worker = None
        if settings.embedded_worker:
            worker = SubscriptionWorker(service, kind="embedded")
            service.worker = worker
            worker.start()
        try:
            yield
        finally:
            if worker:
                await worker.stop()

    app = FastAPI(
        title="标擎 BidPilot API",
        version=__version__,
        description=(
            "标擎是证据优先的招投标情报 Agent。所有业务接口都在下方说明用途、参数、"
            "返回值、副作用、错误码和调用示例；带有真实抓取、外部推送、配置写入或删除"
            "副作用的接口会醒目标注。默认服务仅监听本机。"
        ),
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.config_tokens = config_tokens
    app.state.control_plane = control_plane
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static_dir / "index.html")

    @app.get(
        "/health",
        response_model=HealthResponse,
        **_api_docs(
            tag="系统",
            summary="检查服务健康状态",
            purpose="供浏览器、容器编排和运维探针确认 API 进程可响应，并查看当前版本与存储位置。",
            parameters="无请求体、无查询参数。",
            returns="HTTP 200；返回状态、版本、SQLite 路径和报告目录。",
            side_effects="无。不会访问外部网站，也不会写入数据库。",
            errors="服务未启动或进程崩溃时无法建立连接；该接口本身不产生业务错误码。",
            example="GET /health",
        ),
    )
    async def health():
        return HealthResponse(
            status="ok",
            version=__version__,
            database=str(settings.database_path),
            report_dir=str(settings.report_dir),
        )

    @app.post(
        "/api/v1/intent/parse",
        response_model=TenderQuerySpec,
        **_api_docs(
            tag="意图解析",
            summary="解析中文招投标意图",
            purpose="先用确定性规则拆解中文需求；仅在低置信、缺失或冲突时调用已配置 LLM 提议修复，再由本地地域、日期、计划和枚举校验器逐字段决定是否合并。",
            parameters="JSON 请求体：`query` 为 2～500 字自然语言；`delivery_channel` 在本接口中仅兼容接收，不改变解析结果。",
            returns="HTTP 200；返回完整 TenderQuerySpec、字段置信度、警告，以及不含密钥和模型原文的 resolution 解释轨迹。",
            side_effects="不抓取网站、不生成报告、不创建订阅。若模式为 auto/always 且满足触发条件，会把原问题和规则基线发送到用户配置的模型服务；失败自动回退。",
            errors="422：问题太短、规则层日期或计划数值非法，或请求体格式不正确。模型错误不会让本接口失败。",
            example='POST /api/v1/intent/parse\n{"query":"最近1个月深圳充电桩招标信息"}',
            responses={422: "自然语言为空、过短或包含无效的日期/计划值。"},
        ),
    )
    async def parse_intent(request: QueryRequest):
        try:
            return await service.parse_intent(request.query)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/v1/intent/compare",
        response_model=IntentComparison,
        **_api_docs(
            tag="意图解析",
            summary="对比规则基线与混合解析结果",
            purpose="用于调试和人工确认：在一个响应中并列纯规则基线、严格校验后的最终结果、实际变化字段和字段级接受/拒绝理由。",
            parameters="JSON 请求体只使用 `query`；兼容字段 `delivery_channel` 不参与意图对比。",
            returns="HTTP 200；`rules` 为确定性规则结果，`resolved` 为最终结果，`changed_fields` 仅列出真正发生变化的结构化字段。",
            side_effects="不抓取标讯、不写运行或订阅。与解析接口相同，满足配置条件时可能调用用户自己的模型服务。",
            errors="422：自然语言为空、过短或规则日期非法；模型不可用时仍以 HTTP 200 返回安全回退结果。",
            example='POST /api/v1/intent/compare\n{"query":"帮我查最近45天泉州储能系统项目"}',
            responses={422: "自然语言或规则字段不合法。"},
        ),
    )
    async def compare_intent(request: QueryRequest):
        try:
            return await service.compare_intent(request.query)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/v1/runs",
        response_model=RunResult,
        **_api_docs(
            tag="情报任务",
            summary="立即执行一次情报任务",
            purpose="解析问题、访问已启用来源、清洗去重、生成证据摘要并持久化结果。即时任务即使保留 0 条，也会生成包含扫描漏斗、排除原因和覆盖边界的 Word 诊断报告。",
            parameters="JSON 请求体：`query` 为自然语言；`delivery_channel` 可选 local、feishu_webhook、feishu_app、email、dingtalk_webhook、wecom_webhook 或 generic_webhook。",
            returns="HTTP 200；返回运行 ID、结构化意图、可信记录、逐来源扫描/候选/保留诊断、`search_explanation`、新增数量、报告路径和投递结果。`search_explanation` 会区分没有候选与候选全部被过滤，并给出不会自动执行的安全放宽建议。",
            side_effects="【有副作用】会访问公开/已授权来源、写入运行与标讯记录、可能生成 DOCX，并可能向选定外部通道推送。",
            errors="422：请求体不合法；502：来源执行、报告生成或投递失败。失败运行仍保留诊断记录。",
            example='POST /api/v1/runs\n{"query":"最近1个月深圳充电桩招标信息","delivery_channel":"local"}',
            responses={422: "请求格式或字段校验失败。", 502: "抓取、报告或投递链路执行失败。"},
        ),
    )
    async def run_query(request: QueryRequest):
        try:
            return await service.run_query(request.query, delivery_channel=request.delivery_channel)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get(
        "/api/v1/runs",
        **_api_docs(
            tag="情报任务",
            summary="列出最近运行记录",
            purpose="查看手动和自动任务的历史状态，便于审计失败、部分完成和报告生成情况。",
            parameters="查询参数 `limit`：返回条数，接口会限制在 1～100，默认 30。",
            returns="HTTP 200；按开始时间倒序返回运行数据库记录。",
            side_effects="无，只读 SQLite。",
            errors="参数无法转换为整数时返回 422。",
            example="GET /api/v1/runs?limit=20",
            responses={422: "limit 不是有效整数。"},
        ),
    )
    async def list_runs(limit: int = 30):
        return service.list_runs(min(max(limit, 1), 100))

    @app.get(
        "/api/v1/runs/{run_id}",
        **_api_docs(
            tag="情报任务",
            summary="读取单次运行详情",
            purpose="按运行 ID 获取状态、查询口径、计数、诊断和错误信息。",
            parameters="路径参数 `run_id`：任务返回的 32 位运行标识。",
            returns="HTTP 200；返回一条运行记录。",
            side_effects="无，只读 SQLite。",
            errors="404：运行 ID 不存在。",
            example="GET /api/v1/runs/2c4d8f0a1b2c3d4e5f60718293a4b5c6",
            responses={404: "未找到指定运行记录。"},
        ),
    )
    async def get_run(run_id: str):
        result = service.get_run(run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="运行记录不存在")
        return result

    @app.get(
        "/api/v1/reports",
        **_api_docs(
            tag="报告",
            summary="列出报告历史",
            purpose="展示已成功登记的 Word 报告及其运行、订阅、记录数和生成时间。",
            parameters="无请求体、无查询参数。",
            returns="HTTP 200；按生成时间倒序返回报告元数据，不返回文件正文。",
            side_effects="无，只读 SQLite。",
            errors="通常无业务错误；数据库不可用时返回 500。",
            example="GET /api/v1/reports",
        ),
    )
    async def list_reports():
        return service.list_reports()

    @app.get(
        "/api/v1/reports/{filename}",
        response_class=FileResponse,
        **_api_docs(
            tag="报告",
            summary="下载 Word 报告",
            purpose="下载报告目录中真实生成的 `.docx` 文件。服务端会剥离目录并校验扩展名，阻止路径穿越。",
            parameters="路径参数 `filename`：报告历史返回的文件名，只接受当前报告目录中的 `.docx`。",
            returns="HTTP 200；返回 Word 二进制流和下载文件名。",
            side_effects="无业务写入；会读取本机报告文件。",
            errors="404：文件不存在、扩展名不正确或路径不在报告目录内。",
            example="GET /api/v1/reports/深圳充电桩招标信息_202607181530.docx",
            responses={404: "报告不存在或文件名未通过安全校验。"},
        ),
    )
    async def download_report(filename: str):
        safe_name = Path(filename).name
        path = (settings.report_dir / safe_name).resolve()
        report_root = settings.report_dir.resolve()
        if report_root not in path.parents or not path.exists() or path.suffix.lower() != ".docx":
            raise HTTPException(status_code=404, detail="报告不存在")
        return FileResponse(
            path,
            filename=path.name,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    @app.post(
        "/api/v1/subscriptions",
        **_api_docs(
            tag="长期订阅",
            summary="创建持久化订阅",
            purpose="把带有每天、每周、每月或一次性未来计划的自然语言保存为长期任务，并计算下一次执行时间。相同规则和通道重复提交会复用原订阅。",
            parameters="JSON：`name` 名称、`query` 自然语言、`delivery_channel`、`delivery_policy`（always/on_change）和 `run_immediately`。",
            returns="HTTP 200；返回订阅 ID、解析后的规则、启用状态、下次时间和最近运行状态。",
            side_effects="【有副作用】写入订阅表；`run_immediately=true` 会把首轮设为立即到期，由持久 worker 领取。不会在请求线程内假装完成推送。",
            errors="422：规则不是可调度任务、通道未配置或字段非法。",
            example='POST /api/v1/subscriptions\n{"name":"深圳充电桩日报","query":"每天9点汇总最近1个月深圳充电桩信息","delivery_channel":"local","delivery_policy":"always","run_immediately":true}',
            responses={422: "计划无法解析、通道未配置或请求字段非法。"},
        ),
    )
    async def create_subscription(request: SubscriptionCreate):
        try:
            return await service.create_subscription_hybrid(
                request.name,
                request.query,
                request.delivery_channel,
                request.delivery_policy,
                request.run_immediately,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get(
        "/api/v1/subscriptions",
        **_api_docs(
            tag="长期订阅",
            summary="列出全部订阅",
            purpose="读取所有长期任务及其下次时间、租约、失败次数和最近回执，供订阅中心管理。",
            parameters="无请求体、无查询参数。",
            returns="HTTP 200；按创建时间倒序返回订阅列表。",
            side_effects="无，只读 SQLite。",
            errors="通常无业务错误；数据库不可用时返回 500。",
            example="GET /api/v1/subscriptions",
        ),
    )
    async def list_subscriptions():
        return service.list_subscriptions()

    @app.get(
        "/api/v1/subscriptions/{subscription_id}",
        **_api_docs(
            tag="长期订阅",
            summary="读取单个订阅",
            purpose="按订阅 ID 查看完整规则、状态、下次执行时间和是否正在被 worker 处理。",
            parameters="路径参数 `subscription_id`：创建订阅时返回的 ID。",
            returns="HTTP 200；返回订阅详情。",
            side_effects="无，只读 SQLite。",
            errors="404：订阅不存在。",
            example="GET /api/v1/subscriptions/f8b24edfbe64481dbcc7d4fdee85d94f",
            responses={404: "未找到指定订阅。"},
        ),
    )
    async def get_subscription(subscription_id: str):
        result = service.get_subscription(subscription_id)
        if result is None:
            raise HTTPException(status_code=404, detail="订阅不存在")
        return result

    @app.patch(
        "/api/v1/subscriptions/{subscription_id}",
        **_api_docs(
            tag="长期订阅",
            summary="编辑订阅规则或通道",
            purpose="局部修改名称、自然语言规则、投递通道或无新增策略；修改规则后重新计算下一次时间，但保留既有防重复账本。",
            parameters="路径参数 `subscription_id`；JSON 仅提交要修改的 `name`、`query`、`delivery_channel`、`delivery_policy`。",
            returns="HTTP 200；返回更新后的订阅。",
            side_effects="【有副作用】更新持久化订阅和计划时间；不会立即执行任务。",
            errors="404：订阅不存在；409：订阅正在执行；422：新规则非法或新通道未配置。",
            example='PATCH /api/v1/subscriptions/{id}\n{"query":"每周一9点汇总深圳充电桩中标公告","delivery_policy":"on_change"}',
            responses={
                404: "订阅不存在。",
                409: "订阅当前有活跃租约。",
                422: "规则或通道校验失败。",
            },
        ),
    )
    async def update_subscription(subscription_id: str, request: SubscriptionUpdate):
        try:
            return await service.update_subscription_hybrid(subscription_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SubscriptionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/v1/subscriptions/{subscription_id}/run",
        response_model=RunResult,
        **_api_docs(
            tag="长期订阅",
            summary="立即运行一个订阅",
            purpose="用户手动触发已保存订阅，并通过租约与后台 worker 互斥，避免同一订阅并发执行。",
            parameters="路径参数 `subscription_id`；无请求体。",
            returns="HTTP 200；返回本轮 RunResult，包括新增数、报告和投递回执。",
            side_effects="【有副作用】真实抓取、写入运行/标讯/报告记录，并按订阅通道可能向外部发送。成功投递后才写防重复账本。",
            errors="404：订阅不存在；409：正在执行；502：抓取、报告或投递失败。",
            example="POST /api/v1/subscriptions/{id}/run",
            responses={404: "订阅不存在。", 409: "订阅正在执行。", 502: "本轮执行失败。"},
        ),
    )
    async def run_subscription(subscription_id: str):
        try:
            return await service.run_subscription(subscription_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SubscriptionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post(
        "/api/v1/subscriptions/{subscription_id}/pause",
        **_api_docs(
            tag="长期订阅",
            summary="暂停订阅",
            purpose="停止 worker 后续自动领取该订阅，同时保留规则、历史和防重复账本。",
            parameters="路径参数 `subscription_id`；无请求体。",
            returns="HTTP 200；返回 enabled=false 的订阅。",
            side_effects="【有副作用】持久化修改启用状态；不会中断已开始的抓取。",
            errors="404：订阅不存在；409：订阅正在执行，需等待本轮结束。",
            example="POST /api/v1/subscriptions/{id}/pause",
            responses={404: "订阅不存在。", 409: "订阅正在执行。"},
        ),
    )
    async def pause_subscription(subscription_id: str):
        try:
            return service.pause_subscription(subscription_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SubscriptionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/v1/subscriptions/{subscription_id}/resume",
        **_api_docs(
            tag="长期订阅",
            summary="恢复订阅",
            purpose="重新启用已暂停的订阅，并按原规则计算下一次时间；可选择立即进入待领取队列。",
            parameters="路径参数 `subscription_id`；JSON `run_immediately` 默认为 false。",
            returns="HTTP 200；返回恢复后的订阅与新 next_run_at。",
            side_effects="【有副作用】修改订阅启用状态和下一次时间；立即模式会让 worker 尽快执行。",
            errors="404：订阅不存在；409：已有活跃执行；422：原规则已无法形成有效计划。",
            example='POST /api/v1/subscriptions/{id}/resume\n{"run_immediately":false}',
            responses={404: "订阅不存在。", 409: "订阅正在执行。", 422: "计划无法恢复。"},
        ),
    )
    async def resume_subscription(subscription_id: str, request: ResumeRequest):
        try:
            return service.resume_subscription(
                subscription_id, run_immediately=request.run_immediately
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SubscriptionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete(
        "/api/v1/subscriptions/{subscription_id}",
        **_api_docs(
            tag="长期订阅",
            summary="删除订阅及增量账本",
            purpose="永久删除订阅；数据库外键会同时清除该订阅的成功投递账本。运行和报告审计记录按现有保留策略处理。",
            parameters="路径参数 `subscription_id`；无请求体。网页端要求二次点击确认。",
            returns='HTTP 200；返回 `{"deleted":true}`。',
            side_effects="【不可逆副作用】删除订阅和该订阅的防重复账本；删除后用同一规则重建可能再次推送历史版本。",
            errors="404：订阅不存在；409：订阅正在执行，拒绝删除。",
            example="DELETE /api/v1/subscriptions/{id}",
            responses={404: "订阅不存在。", 409: "订阅正在执行。"},
        ),
    )
    async def delete_subscription(subscription_id: str):
        try:
            service.delete_subscription(subscription_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SubscriptionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"deleted": True}

    @app.get(
        "/api/v1/subscriptions/{subscription_id}/runs",
        **_api_docs(
            tag="长期订阅",
            summary="查看订阅运行日志",
            purpose="读取指定订阅最近的自动/手动运行，定位失败、部分来源受限和新增数量。",
            parameters="路径参数 `subscription_id`；查询参数 `limit` 限制在 1～100，默认 20。",
            returns="HTTP 200；按开始时间倒序返回运行记录。",
            side_effects="无，只读 SQLite。",
            errors="404：订阅不存在；422：limit 不是整数。",
            example="GET /api/v1/subscriptions/{id}/runs?limit=10",
            responses={404: "订阅不存在。", 422: "limit 参数非法。"},
        ),
    )
    async def list_subscription_runs(subscription_id: str, limit: int = 20):
        if service.get_subscription(subscription_id) is None:
            raise HTTPException(status_code=404, detail="订阅不存在")
        return service.list_subscription_runs(subscription_id, min(max(limit, 1), 100))

    @app.get(
        "/api/v1/subscriptions/{subscription_id}/deliveries",
        **_api_docs(
            tag="长期订阅",
            summary="查看订阅投递回执",
            purpose="审计每轮是否实际投递、是否因 on_change 跳过、通道消息和外部消息 ID。",
            parameters="路径参数 `subscription_id`；无请求体。",
            returns="HTTP 200；返回最近投递尝试列表，敏感凭据不会出现在回执中。",
            side_effects="无，只读 SQLite。",
            errors="404：订阅不存在。",
            example="GET /api/v1/subscriptions/{id}/deliveries",
            responses={404: "订阅不存在。"},
        ),
    )
    async def list_subscription_deliveries(subscription_id: str):
        if service.get_subscription(subscription_id) is None:
            raise HTTPException(status_code=404, detail="订阅不存在")
        return service.list_delivery_attempts(subscription_id)

    @app.post(
        "/api/v1/opportunities",
        response_model=Opportunity,
        **_api_docs(
            tag="机会工作台",
            summary="把真实标讯加入机会工作台",
            purpose="从 tender_items 中已抓取、已规范化的公告创建项目级机会；同一 project_key 重复加入返回原机会。",
            parameters="JSON：`canonical_id` 与 `version_hash` 必须来自 RunResult 中的真实记录。客户端不能上传自造标题或摘要。",
            returns="HTTP 200；返回机会、当前最新项目快照和人工跟进字段。",
            side_effects="【有副作用】写入机会表；若同项目已存在则只返回已有机会，不覆盖负责人、阶段和备注。",
            errors="404：标讯版本不存在，或提交的是伪造标识。",
            example='POST /api/v1/opportunities\n{"canonical_id":"cec-abc123","version_hash":"4b9f..."}',
            responses={404: "指定标讯不存在，不能创建无证据机会。"},
        ),
    )
    async def create_opportunity(request: OpportunityCreate):
        try:
            return service.create_opportunity(request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_key_error_detail(exc)) from exc

    @app.get(
        "/api/v1/opportunities",
        response_model=list[Opportunity],
        **_api_docs(
            tag="机会工作台",
            summary="筛选机会列表",
            purpose="按阶段和关键字读取项目级机会，用于看板、负责人检索和待办管理。",
            parameters="可选查询参数 `stage`：new/following/bidding/won/lost/archived；`search`：匹配项目、采购人、负责人、标签或备注。",
            returns="HTTP 200；返回匹配机会及每个项目的最新公告快照。",
            side_effects="无，只读 SQLite。",
            errors="422：stage 不是合法枚举值。",
            example="GET /api/v1/opportunities?stage=following&search=深圳",
            responses={422: "stage 参数非法。"},
        ),
    )
    async def list_opportunities(
        stage: OpportunityStage | None = None,
        search: str | None = None,
    ):
        return service.list_opportunities(stage=stage, search=search)

    @app.get(
        "/api/v1/opportunities/{opportunity_id}",
        response_model=Opportunity,
        **_api_docs(
            tag="机会工作台",
            summary="读取单个机会",
            purpose="按机会 ID 获取最新项目快照、阶段、负责人、下一步、标签和备注。",
            parameters="路径参数 `opportunity_id`。",
            returns="HTTP 200；返回 Opportunity。",
            side_effects="无，只读 SQLite。",
            errors="404：机会不存在。",
            example="GET /api/v1/opportunities/8f1c...",
            responses={404: "机会不存在。"},
        ),
    )
    async def get_opportunity(opportunity_id: str):
        result = service.get_opportunity(opportunity_id)
        if result is None:
            raise HTTPException(status_code=404, detail="机会不存在")
        return result

    @app.patch(
        "/api/v1/opportunities/{opportunity_id}",
        response_model=Opportunity,
        **_api_docs(
            tag="机会工作台",
            summary="更新机会跟进信息",
            purpose="局部更新阶段、负责人、下一步时间、备注、标签或已读状态；后续公告刷新不会覆盖这些人工字段。",
            parameters="路径参数 `opportunity_id`；JSON 可提交 `stage`、`owner`、`next_action_at`、`notes`、`tags`、`is_read`。",
            returns="HTTP 200；返回更新后的机会。",
            side_effects="【有副作用】写入人工跟进状态。不会修改原始标讯证据。",
            errors="404：机会不存在；422：阶段、日期或字段长度非法。",
            example='PATCH /api/v1/opportunities/{id}\n{"stage":"following","owner":"王同学","tags":["重点","充电桩"],"is_read":true}',
            responses={404: "机会不存在。", 422: "字段格式或枚举值非法。"},
        ),
    )
    async def update_opportunity(opportunity_id: str, request: OpportunityUpdate):
        try:
            return service.update_opportunity(opportunity_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_key_error_detail(exc)) from exc

    @app.get(
        "/api/v1/opportunities/{opportunity_id}/timeline",
        response_model=list[TenderRecord],
        **_api_docs(
            tag="机会工作台",
            summary="查看项目生命周期",
            purpose="读取同一 project_key 的采购意向、招标、更正、中标和合同事件，并保留每条原文证据。",
            parameters="路径参数 `opportunity_id`。",
            returns="HTTP 200；按发布时间升序返回 TenderRecord 列表。",
            side_effects="无，只读 SQLite。",
            errors="404：机会不存在。",
            example="GET /api/v1/opportunities/{id}/timeline",
            responses={404: "机会不存在。"},
        ),
    )
    async def opportunity_timeline(opportunity_id: str):
        try:
            return service.opportunity_timeline(opportunity_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_key_error_detail(exc)) from exc

    @app.get(
        "/api/v1/system/status",
        **_api_docs(
            tag="系统",
            summary="读取运行时系统状态",
            purpose="为网页展示 worker 心跳、启用/到期/执行中订阅数量、时区和各投递通道是否已配置。",
            parameters="无请求体、无查询参数。",
            returns="HTTP 200；返回调度器与通道状态。只返回 configured 布尔值，不返回密钥、密码或 Webhook。",
            side_effects="无，只读数据库心跳和内存配置。",
            errors="通常无业务错误；数据库不可用时返回 500。",
            example="GET /api/v1/system/status",
        ),
    )
    async def system_status():
        return service.system_status()

    @app.post(
        "/api/v1/system/shutdown",
        **_api_docs(
            tag="系统",
            summary="优雅停止本机服务",
            purpose="供 `python -m bidpilot stop` 使用：先返回接收确认，再让当前 Uvicorn 服务停止接收新请求并执行 lifespan 清理。",
            parameters="请求必须来自回环地址，并在 `X-BidPilot-Control-Token` 请求头携带本机 `data/secrets/control.token`；普通网页和远程请求不能调用。",
            returns='HTTP 202；返回 `{"accepted":true}`。随后服务通常在数秒内退出。',
            side_effects="【进程级副作用】停止当前 Web 进程及其内嵌 worker；不删除数据库、报告、订阅、机会或配置。",
            errors="403：不是本机请求或令牌错误；409：当前进程不是由可控 `serve` 命令启动；服务已停止时无法连接。",
            example="POST /api/v1/system/shutdown\nX-BidPilot-Control-Token: <本机控制令牌>",
            responses={
                403: "本机来源或控制令牌校验失败。",
                409: "当前启动方式不支持远程优雅停止。",
            },
        ),
        status_code=202,
    )
    async def shutdown_service(
        request: Request,
        x_bidpilot_control_token: Annotated[
            str | None,
            Header(alias="X-BidPilot-Control-Token"),
        ] = None,
    ):
        client_host = request.client.host if request.client else ""
        try:
            is_loopback = ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            is_loopback = client_host == "testclient"
        if not is_loopback or not control_plane.verify(x_bidpilot_control_token):
            raise HTTPException(status_code=403, detail="只允许持有本机控制令牌的回环请求停止服务")
        callback = getattr(request.app.state, "shutdown_callback", None)
        if callback is None:
            raise HTTPException(
                status_code=409, detail="当前服务不是由可控 serve 命令启动，请在终端按 Ctrl+C"
            )
        asyncio.get_running_loop().call_later(0.2, callback)
        return {"accepted": True, "message": "已接收停止请求，正在完成清理"}

    @app.get(
        "/api/v1/sources/status",
        **_api_docs(
            tag="系统",
            summary="读取数据源状态",
            purpose="查看每个来源的官方/行业属性、公开或授权模式、最近状态、抓取/保留数量和诊断信息。",
            parameters="无请求体、无查询参数。",
            returns="HTTP 200；返回来源状态列表；未授权来源明确标记 auth_required，不伪装为成功。",
            side_effects="无。不会为了查看状态而临时访问来源网站。",
            errors="通常无业务错误；数据库不可用时返回 500。",
            example="GET /api/v1/sources/status",
        ),
    )
    async def source_status():
        return service.source_status()

    @app.get(
        "/api/v1/config",
        response_model=RuntimeConfigView,
        **_api_docs(
            tag="配置中心",
            summary="读取脱敏后的运行时配置",
            purpose="为网页配置中心读取混合意图模式、置信阈值、模型、飞书、SMTP、钉钉、企业微信和通用 Webhook 的非敏感字段与就绪状态。",
            parameters="无请求体、无查询参数。",
            returns="HTTP 200；敏感字段仅返回 `{configured:true/false}`，永不返回 API Key、密码、Webhook 完整地址、签名密钥或 Bearer Token。",
            side_effects="无，只读 SQLite 与当前内存设置。",
            errors="通常无业务错误；数据库不可用时返回 500。",
            example="GET /api/v1/config",
        ),
    )
    async def get_runtime_config():
        return service.runtime_config.snapshot()

    @app.post(
        "/api/v1/config/edit-token",
        response_model=ConfigEditTokenResponse,
        **_api_docs(
            tag="配置中心",
            summary="签发短期配置编辑令牌",
            purpose="同源网页在保存或测试前获取一次性会话令牌，用自定义请求头抵御跨站表单直接修改本机配置。",
            parameters="无请求体。后续写请求把返回值放入 `X-BidPilot-Config-Token` 请求头。",
            returns="HTTP 200；返回随机编辑令牌和有效秒数。响应带 `Cache-Control: no-store`。",
            side_effects="在当前进程内登记一个短期令牌；不写数据库，进程重启后自动失效。",
            errors="令牌池异常时返回 500；正常情况下无业务错误。",
            example="POST /api/v1/config/edit-token\n后续请求头：X-BidPilot-Config-Token: <返回的短期令牌>",
        ),
    )
    async def issue_config_edit_token(response: Response):
        token, expires_in = config_tokens.issue()
        response.headers["Cache-Control"] = "no-store"
        return ConfigEditTokenResponse(edit_token=token, expires_in=expires_in)

    @app.put(
        "/api/v1/config",
        response_model=RuntimeConfigView,
        **_api_docs(
            tag="配置中心",
            summary="保存白名单运行时配置",
            purpose="在网页中保存模型和推送通道设置。仅接受 schema 明列字段；保存后当前进程立即生效，重启后从 SQLite 恢复。",
            parameters="请求头必须含短期编辑令牌。JSON 可局部提交 `intent_llm_mode`（off/auto/always）、`intent_llm_confidence_threshold`（0.50～0.99）及模型/渠道字段；敏感字段留空表示保持原值，清除时放入 `clear_secrets`。",
            returns="HTTP 200；返回脱敏后的最新 RuntimeConfigView。",
            side_effects="【有副作用】写入 runtime_config 表并更新共享 Settings。未知字段被拒绝；响应和日志不回显敏感原文。",
            errors="403：编辑令牌缺失/过期；422：未知字段、URL、端口、超时或枚举值非法。",
            example='PUT /api/v1/config\nX-BidPilot-Config-Token: <token>\n{"llm_base_url":"http://127.0.0.1:8045/v1","llm_model":"my-model","llm_api_key":"<仅写入不回显>"}',
            responses={403: "编辑令牌无效或过期。", 422: "字段不在白名单或值未通过校验。"},
        ),
    )
    async def update_runtime_config(
        request: RuntimeConfigUpdate,
        x_bidpilot_config_token: Annotated[
            str | None,
            Header(alias="X-BidPilot-Config-Token"),
        ] = None,
    ):
        require_config_token(x_bidpilot_config_token)
        return service.runtime_config.update(request)

    @app.post(
        "/api/v1/config/model/test",
        response_model=ConnectionTestResult,
        **_api_docs(
            tag="配置中心",
            summary="测试 OpenAI-compatible 模型",
            purpose="使用当前已保存地址、模型和可选 API Key 调用 `/chat/completions`，验证真实连通性与兼容响应格式。",
            parameters="请求头必须含短期编辑令牌；无请求体。请先保存 `llm_base_url` 与 `llm_model`。",
            returns="HTTP 200；返回成功标志、目标、脱敏消息、延迟和最多 120 字的固定测试回复。",
            side_effects="【外部调用】向用户配置的模型服务发送固定测试句，可能消耗极少量模型额度；不发送招标数据，也不回显 API Key。",
            errors="403：令牌无效；422：模型地址/名称未配置；502：超时、HTTP 错误或响应不兼容。",
            example="POST /api/v1/config/model/test\nX-BidPilot-Config-Token: <token>",
            responses={
                403: "编辑令牌无效或过期。",
                422: "模型配置不完整。",
                502: "模型连接或兼容性测试失败。",
            },
        ),
    )
    async def test_model_connection(
        x_bidpilot_config_token: Annotated[
            str | None,
            Header(alias="X-BidPilot-Config-Token"),
        ] = None,
    ):
        require_config_token(x_bidpilot_config_token)
        if not service.runtime_config.snapshot().ai.ready:
            raise HTTPException(status_code=422, detail="请先保存模型 API 地址和模型名称")
        try:
            return await service.runtime_config.test_model()
        except RuntimeConfigError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post(
        "/api/v1/config/channels/{channel}/test",
        response_model=ConnectionTestResult,
        **_api_docs(
            tag="配置中心",
            summary="真实测试一个推送通道",
            purpose="通过指定通道发送一条明确标记为“配置中心连通性测试”的无新增回执，验证凭据、网络和平台配置。",
            parameters="路径 `channel`：feishu_webhook、feishu_app、email、dingtalk_webhook、wecom_webhook 或 generic_webhook；请求头必须含短期编辑令牌。",
            returns="HTTP 200；返回实际通道、平台回执、延迟和成功状态。",
            side_effects="【真实外发副作用】会向已配置群聊、用户、邮箱或 Webhook 接收方发送一条测试消息；网页调用前会二次确认。",
            errors="403：令牌无效；422：通道不支持或未配置；502：网络、认证、平台安全设置或响应失败。",
            example="POST /api/v1/config/channels/dingtalk_webhook/test\nX-BidPilot-Config-Token: <token>",
            responses={
                403: "编辑令牌无效或过期。",
                422: "通道不存在或配置不完整。",
                502: "真实测试消息发送失败。",
            },
        ),
    )
    async def test_delivery_channel(
        channel: str,
        x_bidpilot_config_token: Annotated[
            str | None,
            Header(alias="X-BidPilot-Config-Token"),
        ] = None,
    ):
        require_config_token(x_bidpilot_config_token)
        status = next(
            (item for item in service.delivery.channel_status() if item["id"] == channel),
            None,
        )
        if status is None or channel == "local":
            raise HTTPException(status_code=422, detail="不支持测试该投递通道")
        if not status["configured"]:
            raise HTTPException(status_code=422, detail="该投递通道尚未完成配置")
        started = perf_counter()
        try:
            receipt = await service.delivery.deliver(
                None,
                channel,
                new_count=0,
                subscription_name="配置中心连通性测试",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail="通道测试失败，请检查地址、凭据、网络和平台安全设置",
            ) from exc
        return ConnectionTestResult(
            success=receipt.success,
            target=receipt.channel,
            message=receipt.message,
            latency_ms=round((perf_counter() - started) * 1000),
        )

    return app


app = create_app()
