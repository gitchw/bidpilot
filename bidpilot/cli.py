from __future__ import annotations

import asyncio
import ipaddress
import json
import shlex
import socket
import sys
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
from bidpilot.db import Database
from bidpilot.runtime_config import RuntimeConfiguration
from bidpilot.scheduler import SubscriptionWorker
from bidpilot.service import BidPilotService
from bidpilot.source_auth import SourceAuthError

app = typer.Typer(
    name="bidpilot",
    help="标擎 BidPilot - 证据优先的招投标情报 Agent",
    no_args_is_help=True,
)
console = Console()


def _load_service_settings():
    settings = get_settings()
    RuntimeConfiguration(Database(settings.database_path), settings)
    if settings.network_access_mode == "local":
        settings.host = "127.0.0.1"
    elif settings.network_access_mode == "lan":
        settings.host = "0.0.0.0"
    return settings


def _python_module_command(command: str) -> str:
    """Return a copyable command using the interpreter that runs BidPilot now."""
    executable = str(Path(sys.executable).resolve())
    if sys.platform == "win32":
        launcher = f'& "{executable}"' if any(char.isspace() for char in executable) else executable
    else:
        launcher = shlex.quote(executable)
    return f"{launcher} -m bidpilot {command}"


def _lan_access_urls(port: int) -> list[str]:
    addresses: set[str] = set()
    try:
        for row in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(row[4][0])
    except OSError:
        pass
    usable = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if address.is_loopback or address.is_unspecified or not address.is_private:
            continue
        usable.append(address)
    return [f"http://{address}:{port}" for address in sorted(usable, key=str)]


@app.command("parse")
def parse_command(query: str = typer.Argument(..., help="自然语言查询")) -> None:
    spec = asyncio.run(BidPilotService(get_settings()).parse_intent(query))
    console.print_json(spec.model_dump_json())


@app.command("run")
def run_command(
    query: str = typer.Argument(..., help="自然语言查询"),
    channel: str = typer.Option(
        "local",
        help="兼容旧脚本的单目标；同时提供 --target 时忽略此项",
    ),
    target: Annotated[
        list[str] | None,
        typer.Option(
            "--target",
            "-t",
            help=(
                "交付目标，可重复：local / feishu_webhook / feishu_app / email / "
                "dingtalk_webhook / wecom_webhook / generic_webhook / telegram_bot / "
                "slack_webhook"
            ),
        ),
    ] = None,
) -> None:
    result = asyncio.run(
        BidPilotService(get_settings()).run_query(
            query,
            delivery_channel=channel,
            delivery_targets=target,
        )
    )
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
    port: int | None = typer.Option(None, min=1, max=65535, help="端口（1～65535）"),
    reload: bool = typer.Option(False, help="开发模式自动重载"),
) -> None:
    """前台启动 Web 与内嵌长期任务 worker；Ctrl+C 可随时优雅停止。"""
    if reload:
        raise typer.BadParameter(
            "可管理服务不启用自动重载。开发时请直接使用 uvicorn bidpilot.api:app --reload，"
            "并在该终端按 Ctrl+C 停止。"
        )
    settings = _load_service_settings()
    bind_host = host or settings.host

    _validate_bind_host(settings, bind_host)
    _serve(settings, bind_host, port or settings.port)


def _validate_bind_host(settings, bind_host: str) -> None:
    """Apply the same network exposure rule to serve and restart."""
    try:
        loopback_bind = (
            bind_host.lower() == "localhost"
            or ipaddress.ip_address(bind_host.strip("[]")).is_loopback
        )
    except ValueError:
        loopback_bind = False
    if settings.network_access_mode == "local" and not loopback_bind:
        raise typer.BadParameter(
            "当前访问范围为“仅本机”，不能用 --host 暴露到其他设备。请先在网页配置中心"
            "或 .env 设置 BIDPILOT_NETWORK_ACCESS_MODE=lan/enterprise，保存后再启动。"
        )


def _serve(settings, host: str, port: int) -> None:
    control = ControlPlane(settings)
    control.ensure_token()
    try:
        state = control.write_state(host=host, port=port, version=__version__)
    except FileExistsError:
        existing = control.read_state()
        if existing:
            existing_url = local_control_url(str(existing["host"]), int(existing["port"]))
            detail = (
                f"PID {existing['pid']} · {existing_url} · "
                f"启动于 {existing.get('started_at', '未知时间')}"
            )
        else:
            detail = f"控制记录正在由另一个启动进程写入：{control.state_path.resolve()}"
        console.print(
            f"[bold red]标擎服务未启动：已经存在受管实例或启动进程。[/bold red]\n{detail}"
        )
        console.print(
            f"请先运行 `{_python_module_command('status')}` 确认状态；若记录对应的服务已经退出，"
            f"运行 `{_python_module_command('stop')}` 清理旧记录后再启动。"
        )
        raise typer.Exit(code=1) from None

    url = local_control_url(host, port)
    try:
        web_app = create_app(settings)
        web_app.state.service.runtime_config.set_effective_endpoint(
            host=host,
            port=port,
            access_mode=settings.network_access_mode,
        )
        config = uvicorn.Config(
            web_app,
            host=host,
            port=port,
            log_level="info",
            # BidPilot applies an explicit trusted-proxy allowlist itself. Letting
            # Uvicorn rewrite request.client first would destroy that trust boundary.
            proxy_headers=False,
        )
        server = uvicorn.Server(config)

        def request_shutdown() -> None:
            server.should_exit = True

        web_app.state.shutdown_callback = request_shutdown
        console.print(f"[bold green]标擎服务正在启动[/bold green]：{url}")
        if settings.network_access_mode == "lan":
            lan_urls = _lan_access_urls(port)
            if lan_urls:
                console.print("局域网设备可尝试打开：" + "  ·  ".join(lan_urls))
            else:
                console.print("局域网模式已开启；请在系统网络设置中查看这台电脑的 IPv4 地址。")
        elif settings.network_access_mode == "enterprise":
            console.print(
                "企业内网模式已开启：业务 API 同时校验 HTTPS、受信网段、浏览器来源和管理员令牌。"
            )
            console.print("请通过已配置的 HTTPS 反向代理地址访问，不要直接打开后端监听端口。")
        console.print(f"版本：v{__version__} · 数据库：{settings.database_path.resolve()}")
        console.print(
            f"报告目录：{settings.report_dir.resolve()} · 控制目录：{settings.control_dir.resolve()}"
        )
        console.print(
            "停止方法：在本窗口按 [bold]Ctrl+C[/bold]，或在同一项目的另一个终端运行 "
            f"`{_python_module_command('stop')}`。"
        )
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
    settings = _load_service_settings()
    control, state, base_url = _service_target(settings)
    try:
        with httpx.Client(timeout=3.0) as client:
            health = client.get(f"{base_url}/health")
            health.raise_for_status()
            system_headers = {}
            if settings.network_access_mode == "enterprise":
                token = control.read_token()
                if token:
                    system_headers["X-BidPilot-Control-Token"] = token
            system = client.get(f"{base_url}/api/v1/system/status", headers=system_headers)
            system.raise_for_status()
        data = system.json()
    except (httpx.HTTPError, ValueError):
        console.print(f"[bold red]服务未运行或无法访问[/bold red]：{base_url}")
        if state:
            console.print(
                f"发现旧运行记录（PID {state.get('pid')}），但健康检查失败；"
                f"它可能刚退出或端口已改变。可重新运行 `{_python_module_command('serve')}`。"
            )
        else:
            console.print(f"下一步：在项目目录运行 `{_python_module_command('serve')}`。")
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
    table.add_row("数据库", str(settings.database_path.resolve()))
    table.add_row("报告目录", str(settings.report_dir.resolve()))
    table.add_row("控制目录", str(settings.control_dir.resolve()))
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
        if state and control.process_is_alive(int(state["pid"])):
            console.print(
                "[yellow]服务进程仍存在，但健康接口暂时不可访问。[/yellow]"
                "它可能仍在启动，或端口被防火墙/代理拦截；为保护单实例声明，stop 不会删除运行记录。"
            )
            console.print(
                f"请稍后重试 status/stop；若确认 PID {state['pid']} 已不是 BidPilot，"
                f"再人工检查 {control.state_path.resolve()}。"
            )
            return False
        if state:
            control.clear_state(pid=int(state["pid"]))
        elif control.state_path.exists():
            console.print(
                "[red]发现无法读取的运行声明，且健康接口不可访问。[/red]"
                "系统不会在所有者未知时自动删除它。"
            )
            console.print(f"请人工检查：{control.state_path.resolve()}")
            return False
        if not quiet_if_stopped:
            console.print("[green]服务本来就没有运行，无需停止。[/green]")
        return True
    if not token:
        console.print(
            "[red]服务在线，但当前控制目录没有本机控制令牌。[/red]"
            f"当前控制目录：{settings.control_dir.resolve()}。请确认 status/stop 与 serve 在同一项目目录运行，"
            "或回到启动服务的终端按 Ctrl+C；系统不会冒险按 PID 强制结束进程。"
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
            console.print(
                f"当前控制目录：{settings.control_dir.resolve()}。请确认与 serve 使用同一项目目录，"
                "或回到启动终端按 Ctrl+C；不会强制结束未知 PID。"
            )
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
    if not _stop_service(_load_service_settings(), wait_seconds):
        raise typer.Exit(code=1)


@app.command("restart")
def restart_command(
    host: str | None = typer.Option(None, help="重新启动后的监听地址"),
    port: int | None = typer.Option(
        None,
        min=1,
        max=65535,
        help="重新启动后的端口（1～65535）",
    ),
) -> None:
    """先优雅停止现有服务，再在当前终端前台启动新服务。"""
    settings = _load_service_settings()
    bind_host = host or settings.host
    _validate_bind_host(settings, bind_host)
    if not _stop_service(settings, 15.0, quiet_if_stopped=True):
        raise typer.Exit(code=1)
    _serve(settings, bind_host, port or settings.port)


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
    source: str = typer.Argument("cecbid", help="当前支持 cecbid（中国招标投标网）"),
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
