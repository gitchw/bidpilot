# 标擎 BidPilot 跨平台测试与发布审计

报告日期：2026-08-12｜版本：v0.8.0｜分支：`main`

## 1. 当前结论

本次 Windows 工作树已完成 311 项 Pytest、Ruff 规则与格式、Python 编译、JavaScript 语法、发布结构和差异检查。此前审计发现 13 项时间敏感失败：测试来源固定写死 2026-07-10，在 2026-08-11 已落到“最近 1 个月”窗口之外。夹具现改为使用查询 `end_date` 当天，受影响用例与完整 311 项均重新通过。

最终版本已直接推送 `main`。最终发布前的 Actions 矩阵已完成 7/7；Windows、Ubuntu、macOS × Python 3.11/3.13 均执行同一套 311 项测试，Ubuntu 企业 HTTPS 容器完成真实构建和健康检查。交付包 `05_验收与校验/构建信息.json` 记录本次构建对应的精确 HEAD、构建时间和最终 Actions run URL。

## 2. Windows 本地门禁

环境：Windows 11 专业版 64 位（10.0.26200）、Python 3.13.9、Ruff 0.15.22、Node.js v24.18.0。

| 检查 | 命令 | 结果 |
|---|---|---|
| 完整测试 | `.venv\Scripts\python.exe -m pytest -p no:cacheprovider` | 311 passed，1 个 Starlette 弃用提示；本地隔离运行与 CI 以同一套测试为准 |
| Ruff 规则 | `.venv\Scripts\python.exe -m ruff check .` | 通过 |
| Ruff 格式 | `.venv\Scripts\python.exe -m ruff format --check .` | 72 个 Python 文件已合规 |
| Python 编译 | `.venv\Scripts\python.exe -m compileall -q bidpilot tests tools bootstrap.py` | 通过 |
| JavaScript | `node --check bidpilot/static/app.js` | 通过 |
| 发布结构 | `.venv\Scripts\python.exe tools/validate_release.py` | 通过 |
| 差异检查 | `git diff --check` | 通过；只有 Git 的 LF→CRLF 提示 |
| OpenAPI | `create_app().openapi()` | 41 条路径、52 个操作、52 个唯一 operationId |

Starlette 的 `httpx` 兼容提示属于依赖未来迁移提醒，不影响本次断言；`.pytest_cache` 历史 Windows ACL 拒绝写入，因此完整发布命令使用 `-p no:cacheprovider`，测试产物不进入交付包。

## 3. CI 矩阵与职责

| 作业 | 覆盖内容 | 当前证据 |
|---|---|---|
| Ubuntu × Python 3.11 / 3.13 | 安装、Ruff、编译、JS、发布结构、Pytest | 最终 run：2/2 成功 |
| Windows × Python 3.11 / 3.13 | 同上，覆盖 Windows 路径与进程行为 | 最终 run：2/2 成功 |
| macOS × Python 3.11 / 3.13 | 同上，覆盖 POSIX/macOS 解释器与路径 | 最终 run：2/2 成功 |
| Ubuntu container | 基础/企业 Compose、Linux 镜像、企业 HTTPS 栈、端口绑定、`/health`、清理 | 最终 run：1/1 成功 |

远端证据：[main 分支 cross-platform-ci 运行列表](https://github.com/gitchw/bidpilot/actions/workflows/ci.yml?query=branch%3Amain)，最终交付包中的构建信息记录精确 run URL 与提交对应关系。

## 4. 发布结构校验实际检查什么

`tools/validate_release.py` 会检查：

- `feature_list.json`、`pyproject.toml` 可解析；
- 基础 Compose 同时包含 `web`、`worker`；
- 企业 Compose 同时包含 `web`、`worker`、`gateway`；
- Web 只 `expose` 8000，不直接 `publish`；
- 企业后端网络为 internal，网关连接 HTTPS 入口与后端网络；
- Web/worker 具备未发布的出站网络；
- Nginx 模板、两个 systemd unit 与 Bash smoke test 存在且结构完整。

## 5. 真实覆盖和未覆盖范围

- 本机没有 Docker，不能把 Windows 开发机写成已完成 Compose 或 Linux 镜像实跑；该部分由 Ubuntu container 作业负责。
- 本机有 WSL2 Debian 和 Bash，但本次本地门禁不把 WSL 结果替代原生 Linux CI。
- CI 的 Windows/macOS 作业覆盖 Python、静态检查和测试，没有声称存在原生安装包或系统后台服务。
- 容器启动验证只在 Ubuntu 运行；Python 3.12 没有单独矩阵，声明范围由 3.11 和 3.13 两端覆盖。
- CI 尚未执行真实浏览器 E2E；本次额外使用 Playwright 做桌面、平板和 390×844 手工截图/布局验收，结果记录在交付材料与截图目录。
- 真实外部来源会随站点、验证码、频率和授权变化；单测使用冻结最小夹具，线上验证必须同时保留来源诊断。

## 6. 最终提交闸门

- [x] Windows 当前工作树 311 项测试及本地静态门禁通过；
- [x] 时间敏感夹具不再依赖会过期的固定日期；
- [x] 代码终版提交已推送 `main`；
- [x] 代码终版提交的 7 个 GitHub Actions 作业全部成功；
- [x] `main` 最终发布线上 7 个作业已全部成功；
- [x] Word、截图和真实运行样本均来自当前实现或明确标注生成时间。
