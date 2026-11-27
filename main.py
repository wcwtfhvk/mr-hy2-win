"""Hysteria2 Windows 部署器统一入口。

不带子命令直接启动 GUI；带子命令走无界面 CLI。
"""
from __future__ import annotations

import argparse
import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cli  # noqa: E402
import hy2_config as C  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hy2-deploy", description="Hysteria2 Windows 服务端安装部署程序")
    sub = p.add_subparsers(dest="cmd")

    g = sub.add_parser("gui", help="启动图形界面（默认）")
    g.add_argument("--profile", default="default")
    g.add_argument("--selftest", action="store_true", help="创建窗口后自动关闭，用于验证 GUI 可构造")

    d = sub.add_parser("download", help="下载/更新 Hysteria2 Windows 内核并校验 SHA256")
    d.add_argument("--arch", default="", choices=["", "amd64", "amd64-avx", "386", "arm64"])
    d.add_argument("--version", default="latest")

    v = sub.add_parser("validate", help="校验配置并打印生成的 YAML")
    v.add_argument("--profile", default="default")

    gen = sub.add_parser("gen", help="生成 config.yaml")
    gen.add_argument("--profile", default="default")
    gen.add_argument("--out", default=str(C.CONFIG_PATH))
    gen.add_argument("--print", action="store_true", dest="print")

    s = sub.add_parser("share", help="输出客户端分享链接")
    s.add_argument("--profile", default="default")

    sub.add_parser("status", help="查看运行状态 / 内核 / 自启 / 配置列表")
    st = sub.add_parser("start", help="前台启动内核并打印日志（Ctrl+C 停止）")
    st.add_argument("--profile", default="default")
    sub.add_parser("stop", help="停止内核")

    t = sub.add_parser("task", help="开机自启计划任务")
    t.add_argument("action", choices=["install", "uninstall", "status"])
    t.add_argument("--profile", default="default")

    f = sub.add_parser("firewall", help="放行 UDP 入站")
    f.add_argument("--listen", default=":443")
    f.add_argument("--remove", action="store_true")

    i = sub.add_parser("import", help="导入已有 config.yaml 为可编辑配置方案")
    i.add_argument("--file", required=True)
    i.add_argument("--profile", default="imported")

    sub.add_parser("profiles", help="列出所有配置方案")
    sub.add_parser("selftest", help="不启动界面，验证核心逻辑")
    return p


def _fix_windows_console() -> None:
    """Windows 默认控制台代码页是 936，打包后的 CLI 输出中文会乱码。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    _fix_windows_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["gui"]
    elif argv[0].startswith("-"):
        argv.insert(0, "gui")
    args = build_parser().parse_args(argv)

    handlers = {
        "download": cli.cmd_download,
        "validate": cli.cmd_validate,
        "gen": cli.cmd_gen,
        "share": cli.cmd_share,
        "status": cli.cmd_status,
        "start": cli.cmd_start,
        "stop": cli.cmd_stop,
        "task": cli.cmd_task,
        "firewall": cli.cmd_firewall,
        "import": cli.cmd_import,
        "profiles": cli.cmd_profiles,
        "selftest": cli.cmd_selftest,
    }
    if args.cmd == "gui":
        try:
            from hy2_gui import launch
        except ImportError as exc:
            print(f"GUI 不可用（需要 Python 自带 tkinter）: {exc}", file=sys.stderr)
            return 7
        launch(args.profile, selftest=args.selftest)
        return 0
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
