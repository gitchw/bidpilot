from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Annotated

import httpx
import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from bidpilot import __version__
from bidpilot.api import create_app
from bidpilot.config import get_settings
from bidpilot.control import ControlPlane, local_control_url
from bidpilot.scheduler import SubscriptionWorker
from bidpilot.service import BidPilotService
from bidpilot.source_auth import SourceAuthError

app = typer.Typer(
    name="bidpilot",
    help="标擎 BidPilot - 证据优先的招投标情报 Agent",
    no_args_is_help=True,
)
console = Console()


@app.command("parse")
def parse_command(query: str = typer.Argument(..., help="自然语言查询")) -> None:
    spec = asyncio.run(BidPilotService(get_settings()).parse_intent(query))
    console.print_json(spec.model_dump_json())


@app.command("run")
def run_command(
    query: str = typer.Argument(..., help="自然语言查询"),
    channel: str = typer.Option("local", help="local / feishu / feishu_app / email"),
) -> None:
    result = asyncio.run(BidPilotService(get_settings()).run_query(query, delivery_channel=channel))
    console.print(f"[bold green]完成[/bold green]：{result.new_count} 条结果")
    if result.report_path:
        console.print(f"报告：{result.report_path}")
    table = Table("来源", "状态", "抓取", "保留", "说明")
    for diagnostic in result.diagnostics:
        table.add_row(
            diagnostic.source,
            diagnostic.status.value,
            str(diagnostic.fetched_count),
            str(diagnostic.kept_count),
            diagnostic.message[:80],
        )
    console.print(table)


@app.command("serve")
def serve_command(
    host: str | None = typer.Option(None, help="监听地址"),
    port: int | None = typer.Option(None, help="端口"),
    reload: bool = typer.Option(False, help="开发模式自动重载"),
) -> None:
    """前台启动 Web 与内嵌长期任务 worker；Ctrl+C 可随时优雅停止。"""
    if reload:
        raise typer.BadParameter(
            "可管理服务不启用自动重载。开发时请直接使用 uvicorn bidpilot.api:app --reload，"
            "并在该终端按 Ctrl+C 停止。"
        )
    settings = get_settings()
    _serve(settings, host or settings.host, port or settings.port)


def _serve(settings, host: str, port: int) -> None:
    control = ControlPlane(settings)
    control.ensure_token()
    web_app = create_app(settings)
    config = uvicorn.Config(web_app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)

    def request_shutdown() -> None:
        server.should_exit = True

    web_app.state.shutdown_callback = request_shutdown
    state = control.write_state(host=host, port=port, version=__version__)
    url = local_control_url(host, port)
    console.print(f"[bold green]标擎服务正在启动[/bold green]：{url}")
    console.print(
        "停止方法：在本窗口按 [bold]Ctrl+C[/bold]，或在另一个终端运行 `python -m bidpilot stop`。"
    )
    try:
        server.run()
    finally:
        control.clear_state(pid=state["pid"])
        console.print("[yellow]标擎服务已停止；数据库、订阅和报告均已保留。[/yellow]")


def _service_target(settings) -> tuple[ControlPlane, dict | None, str]:
    control = ControlPlane(settings)
    state = control.read_state()
    host = str(state.get("host")) if state else settings.host
    port = int(state.get("port")) if state else settings.port
    return control, state, local_control_url(host, port)


@app.command("status")
def status_command() -> None:
    """检查服务是否可访问，并显示长期任务 worker 与订阅数量。"""
    settings = get_settings()
    _control, state, base_url = _service_target(settings)
    try:
        with httpx.Client(timeout=3.0) as client:
            health = client.get(f"{base_url}/health")
            health.raise_for_status()
            system = client.get(f"{base_url}/api/v1/system/status")
            system.raise_for_status()
        data = system.json()
    except (httpx.HTTPError, ValueError):
        console.print(f"[bold red]服务未运行或无法访问[/bold red]：{base_url}")
        if state:
            console.print(
                f"发现旧运行记录（PID {state.get('pid')}），但健康检查失败；"
                "它可能刚退出或端口已改变。可重新运行 `python -m bidpilot serve`。"
            )
        else:
            console.print("下一步：在项目目录运行 `python -m bidpilot serve`。")
        raise typer.Exit(code=1) from None

    table = Table("项目", "当前状态")
    table.add_row("Web 服务", f"在线 · v{health.json().get('version', '?')} · {base_url}")
    table.add_row("长期任务 worker", "在线" if data.get("worker_online") else "离线")
    table.add_row(
        "订阅",
        f"{data.get('enabled_subscription_count', 0)} 个启用 / {data.get('subscription_count', 0)} 个总计",
    )
    table.add_row("正在执行", str(data.get("running_subscription_count", 0)))
    table.add_row("等待领取", str(data.get("due_count", 0)))
    intent = data.get("intent_engine", {})
    table.add_row(
        "混合意图",
        f"{intent.get('mode', 'auto')} · 阈值 {intent.get('confidence_threshold', 0.85)}",
    )
    console.print(table)
    if state:
        console.print(
            f"运行记录：PID {state.get('pid')}，启动于 {state.get('started_at', '未知时间')}。"
        )


def _stop_service(settings, wait_seconds: float, *, quiet_if_stopped: bool = False) -> bool:
    control, state, base_url = _service_target(settings)
    token = control.read_token()
    try:
        with httpx.Client(timeout=3.0) as client:
            health = client.get(f"{base_url}/health")
            health.raise_for_status()
    except httpx.HTTPError:
        control.clear_state()
        if not quiet_if_stopped:
            console.print("[green]服务本来就没有运行，无需停止。[/green]")
        return True
    if not token:
        console.print(
            "[red]服务在线，但本机控制令牌不存在。[/red]请回到启动服务的终端按 Ctrl+C；"
            "系统不会冒险按 PID 强制结束进程。"
        )
        return False
    try:
        response = httpx.post(
            f"{base_url}/api/v1/system/shutdown",
            headers={"X-BidPilot-Control-Token": token},
            timeout=5.0,
        )
        if response.status_code != 202:
            detail = response.json().get("detail", "当前启动方式不支持")
            console.print(f"[red]无法通过管理接口停止：{detail}[/red]")
            console.print("请回到启动服务的终端按 Ctrl+C。不会强制结束未知 PID。")
            return False
    except (httpx.HTTPError, ValueError):
        console.print("[red]停止请求发送失败。[/red]请检查服务地址，或在启动终端按 Ctrl+C。")
        return False

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base_url}/health", timeout=0.6).raise_for_status()
        except httpx.HTTPError:
            control.clear_state(pid=state.get("pid") if state else None)
            console.print("[green]服务已优雅停止；数据和长期任务配置均已保留。[/green]")
            return True
        time.sleep(0.2)
    console.print(
        "[yellow]服务已接收停止请求，但在等待时间内仍可访问；请稍后运行 status 检查。[/yellow]"
    )
    return False


@app.command("stop")
def stop_command(
    wait_seconds: float = typer.Option(15.0, min=1.0, max=60.0, help="最多等待退出秒数"),
) -> None:
    """使用本机控制令牌优雅停止由 `bidpilot serve` 启动的服务。"""
    if not _stop_service(get_settings(), wait_seconds):
        raise typer.Exit(code=1)


@app.command("restart")
def restart_command(
    host: str | None = typer.Option(None, help="重新启动后的监听地址"),
    port: int | None = typer.Option(None, help="重新启动后的端口"),
) -> None:
    """先优雅停止现有服务，再在当前终端前台启动新服务。"""
    settings = get_settings()
    if not _stop_service(settings, 15.0, quiet_if_stopped=True):
        raise typer.Exit(code=1)
    _serve(settings, host or settings.host, port or settings.port)


@app.command("worker")
def worker_command() -> None:
    """Run the durable subscription worker as a standalone process."""
    try:
        asyncio.run(_run_worker())
    except KeyboardInterrupt:
        console.print("[yellow]订阅 worker 已停止[/yellow]")


async def _run_worker() -> None:
    service = BidPilotService(get_settings())
    worker = SubscriptionWorker(service, kind="standalone")
    console.print(f"[green]订阅 worker 已启动[/green]：{worker.worker_id}")
    try:
        await worker.run_forever()
    finally:
        await worker.stop()


@app.command("sources")
def sources_command() -> None:
    rows = BidPilotService(get_settings()).source_status()
    table = Table("来源", "模式", "需要登录", "已配置")
    for row in rows:
        table.add_row(
            row["name"],
            row["mode"],
            "是" if row["requires_auth"] else "否",
            "是" if row["configured"] else "否",
        )
    console.print(table)


@app.command("auth")
def auth_command(
    source: str = typer.Argument("qianlima", help="qianlima 或 cecbid"),
    test: bool = typer.Option(True, "--test/--no-test", help="保存后执行一次真实授权测试"),
) -> None:
    asyncio.run(_authorize_source(source.lower(), test=test))


async def _authorize_source(source: str, *, test: bool) -> None:
    settings = get_settings()
    service = BidPilotService(settings)
    try:
        session = await service.source_auth.start(source)
        console.print(
            "[yellow]可见浏览器已经打开。请由你本人完成登录、扫码或验证码，"
            "成功后回到这个终端。[/yellow]"
        )
        await asyncio.to_thread(input, "按 Enter 加密保存本机会话（不会保存密码）...")
        completed = await service.source_auth.complete(session.session_id)
        if completed.status != "completed":
            raise SourceAuthError(completed.message)
        console.print("[green]授权会话已使用本机密钥加密写入 SQLite。[/green]")
        if test:
            result = await service.source_auth.test(source)
            color = "green" if result.success else "yellow"
            console.print(f"[{color}]{result.message}[/{color}]")
    except SourceAuthError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        await service.source_auth.close_all()


@app.command("openapi")
def openapi_command(
    output: Annotated[Path, typer.Option(help="输出路径")] = Path("outputs/openapi.json"),
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(create_app().openapi(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    console.print(f"OpenAPI 已导出：{output}")


if __name__ == "__main__":
    app()
