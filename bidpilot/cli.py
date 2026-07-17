from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from bidpilot.api import create_app
from bidpilot.config import get_settings
from bidpilot.scheduler import SubscriptionWorker
from bidpilot.service import BidPilotService

app = typer.Typer(
    name="bidpilot",
    help="标擎 BidPilot - 证据优先的招投标情报 Agent",
    no_args_is_help=True,
)
console = Console()


@app.command("parse")
def parse_command(query: str = typer.Argument(..., help="自然语言查询")) -> None:
    spec = BidPilotService(get_settings()).parser.parse(query)
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
    host: str = typer.Option(None, help="监听地址"),
    port: int = typer.Option(None, help="端口"),
    reload: bool = typer.Option(False, help="开发模式自动重载"),
) -> None:
    settings = get_settings()
    uvicorn.run(
        "bidpilot.api:app",
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
    )


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
def auth_command(source: str = typer.Argument("qianlima")) -> None:
    if source.lower() != "qianlima":
        raise typer.BadParameter("当前交互登录仅支持 qianlima")
    asyncio.run(_authorize_qianlima())


async def _authorize_qianlima() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise typer.BadParameter(
            "请先用当前 Python 运行 `python -m pip install -e '.[auth]'`，"
            "再运行 `python -m playwright install chromium`"
        ) from exc

    settings = get_settings()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://wap.qianlima.com/login.jsp")
        console.print("[yellow]请在打开的浏览器中完成免费会员登录。登录成功后回到此窗口。[/yellow]")
        await asyncio.to_thread(input, "按 Enter 保存本机会话（不会保存密码）...")
        cookies = await context.cookies("https://wap.qianlima.com")
        await browser.close()
    if not cookies:
        raise typer.BadParameter("未检测到登录会话")
    cookie_header = "; ".join(f"{item['name']}={item['value']}" for item in cookies)
    path: Path = settings.qianlima_cookie_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cookie_header, encoding="utf-8")
    with suppress(OSError):
        path.chmod(0o600)
    console.print(f"[green]授权会话已保存到 {path}（已被 .gitignore 排除）[/green]")


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
