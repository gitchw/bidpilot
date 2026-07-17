from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bidpilot import __version__
from bidpilot.config import Settings, get_settings
from bidpilot.models import (
    HealthResponse,
    RunResult,
    SubscriptionCreate,
    SubscriptionUpdate,
    TenderQuerySpec,
)
from bidpilot.scheduler import SubscriptionWorker
from bidpilot.service import BidPilotService, SubscriptionBusyError
from bidpilot.sources.base import SourceAdapter


class QueryRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    delivery_channel: str = "local"


class ResumeRequest(BaseModel):
    run_immediately: bool = False


def create_app(
    settings: Settings | None = None,
    sources: list[SourceAdapter] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    service = BidPilotService(settings, sources=sources)

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
        description="证据优先的招投标情报 Agent，可作为飞书 Aily 的 OpenAPI 工具。",
        lifespan=lifespan,
    )
    app.state.service = service
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static_dir / "index.html")

    @app.get("/health", response_model=HealthResponse)
    async def health():
        return HealthResponse(
            status="ok",
            version=__version__,
            database=str(settings.database_path),
            report_dir=str(settings.report_dir),
        )

    @app.post("/api/v1/intent/parse", response_model=TenderQuerySpec)
    async def parse_intent(request: QueryRequest):
        try:
            return service.parser.parse(request.query)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/runs", response_model=RunResult)
    async def run_query(request: QueryRequest):
        try:
            return await service.run_query(request.query, delivery_channel=request.delivery_channel)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/v1/runs")
    async def list_runs(limit: int = 30):
        return service.list_runs(min(max(limit, 1), 100))

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str):
        result = service.get_run(run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="运行记录不存在")
        return result

    @app.get("/api/v1/reports")
    async def list_reports():
        return service.list_reports()

    @app.get("/api/v1/reports/{filename}", response_class=FileResponse)
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

    @app.post("/api/v1/subscriptions")
    async def create_subscription(request: SubscriptionCreate):
        try:
            return service.create_subscription(
                request.name,
                request.query,
                request.delivery_channel,
                request.delivery_policy,
                request.run_immediately,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/subscriptions")
    async def list_subscriptions():
        return service.list_subscriptions()

    @app.get("/api/v1/subscriptions/{subscription_id}")
    async def get_subscription(subscription_id: str):
        result = service.get_subscription(subscription_id)
        if result is None:
            raise HTTPException(status_code=404, detail="订阅不存在")
        return result

    @app.patch("/api/v1/subscriptions/{subscription_id}")
    async def update_subscription(subscription_id: str, request: SubscriptionUpdate):
        try:
            return service.update_subscription(subscription_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SubscriptionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/subscriptions/{subscription_id}/run", response_model=RunResult)
    async def run_subscription(subscription_id: str):
        try:
            return await service.run_subscription(subscription_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SubscriptionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/v1/subscriptions/{subscription_id}/pause")
    async def pause_subscription(subscription_id: str):
        try:
            return service.pause_subscription(subscription_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SubscriptionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/subscriptions/{subscription_id}/resume")
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

    @app.delete("/api/v1/subscriptions/{subscription_id}")
    async def delete_subscription(subscription_id: str):
        try:
            service.delete_subscription(subscription_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SubscriptionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"deleted": True}

    @app.get("/api/v1/subscriptions/{subscription_id}/runs")
    async def list_subscription_runs(subscription_id: str, limit: int = 20):
        if service.get_subscription(subscription_id) is None:
            raise HTTPException(status_code=404, detail="订阅不存在")
        return service.list_subscription_runs(subscription_id, min(max(limit, 1), 100))

    @app.get("/api/v1/subscriptions/{subscription_id}/deliveries")
    async def list_subscription_deliveries(subscription_id: str):
        if service.get_subscription(subscription_id) is None:
            raise HTTPException(status_code=404, detail="订阅不存在")
        return service.list_delivery_attempts(subscription_id)

    @app.get("/api/v1/system/status")
    async def system_status():
        return service.system_status()

    @app.get("/api/v1/sources/status")
    async def source_status():
        return service.source_status()

    return app


app = create_app()
