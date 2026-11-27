"""无界面命令行：自动化部署、CI 校验、无 GUI 环境下使用。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import yaml

import hy2_config as C
import hy2_downloader as D
import hy2_service as S


def _values(profile: str) -> dict:
    return C.load_profile(profile)


def _fail_if_invalid(values: dict) -> int:
    errors = C.validate(values)
    for w in C.warnings(values):
        print(f"[提示] {w}")
    if errors:
        for e in errors:
            print(f"[错误] {e}", file=sys.stderr)
        return 2
    return 0


def cmd_download(args) -> int:
    def progress(msg: str, pct: int) -> None:
        print(f"[{pct:3d}%] {msg}" if pct else f"      {msg}", flush=True)

    meta = D.download(arch=args.arch, version=args.version, progress=progress)
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


def cmd_validate(args) -> int:
    values = _values(args.profile)
    code = _fail_if_invalid(values)
    if code == 0:
        print("校验通过")
        print(C.dump_yaml(C.build_config(values)))
    return code


def cmd_gen(args) -> int:
    values = _values(args.profile)
    code = _fail_if_invalid(values)
    if code:
        return code
    path = C.write_config(values, args.out)
    print(f"已生成 {path}")
    if args.print:
        print(path.read_text(encoding="utf-8"))
    return 0


def cmd_share(args) -> int:
    print(C.share_uri(_values(args.profile), args.profile))
    return 0


def cmd_status(args) -> int:
    runner = S.Runner()
    meta = D.installed_meta()
    print(json.dumps({
        "running": runner.is_running(),
        "kernel": meta or None,
        "autostart": S.autostart_status(),
        "admin": S.is_admin(),
        "profiles": C.list_profiles(),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_start(args) -> int:
    values = _values(args.profile)
    code = _fail_if_invalid(values)
    if code:
        return code
    exe = D.binary_path()
    if not exe:
        print("未安装内核，请先执行 download", file=sys.stderr)
        return 3
    cfg = C.write_config(values)
    runner = S.Runner()
    try:
        pid = runner.start(exe, cfg, values.get("_logLevel", "info"))
        print(f"已启动 pid={pid}，Ctrl+C 停止", flush=True)
        while runner.proc is not None and runner.proc.poll() is None:
            for line in S.drain(runner):
                print(line, flush=True)
            time.sleep(0.2)
        for line in S.drain(runner):
            print(line, flush=True)
        return 0
    except KeyboardInterrupt:
        print("\n正在停止…")
        print(runner.stop())
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"启动失败: {exc}", file=sys.stderr)
        return 4


def cmd_stop(args) -> int:
    print(S.Runner().stop())
    return 0


def cmd_task(args) -> int:
    values = _values(args.profile)
    exe = D.binary_path()
    if not exe and args.action == "install":
        print("未安装内核，请先执行 download", file=sys.stderr)
        return 3
    cfg = C.write_config(values)
    try:
        if args.action == "install":
            print(S.install_autostart(exe, cfg, values.get("_logLevel", "info")))
        elif args.action == "uninstall":
            print(S.uninstall_autostart())
        else:
            print("已安装" if S.autostart_status() else "未安装")
        return 0
    except (PermissionError, RuntimeError) as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 5


def cmd_firewall(args) -> int:
    try:
        if args.remove:
            print(S.firewall_delete_udp(args.listen))
        else:
            print(S.firewall_allow_udp(args.listen))
        return 0
    except (PermissionError, RuntimeError, ValueError) as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 5


def cmd_import(args) -> int:
    cfg = yaml.safe_load(Path(args.file).read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        print("配置文件顶层必须是映射", file=sys.stderr)
        return 6
    values = C.values_from_yaml(cfg)
    path = C.save_profile(args.profile, values)
    print(f"已导入为配置 {args.profile}: {path}")
    return 0


def cmd_profiles(args) -> int:
    print("\n".join(C.list_profiles()) or "(无)")
    return 0


def cmd_selftest(args) -> int:
    """不启动 GUI 也能验证 schema、配置生成、校验、URI、往返转换。"""
    schema = C.load_schema()
    values = C.default_values(schema)
    values["_serverHost"] = "hy.example.net"
    values["acme.domains"] = [{"domain": "hy.example.net"}]
    values["acme.email"] = "admin@hy.example.net"
    assert "tabs" in schema and len(schema["tabs"]) >= 10
    errors = C.validate(values)
    assert not errors, errors
    cfg = C.build_config(values)
    assert cfg["auth"]["type"] == "password"
    assert cfg["acme"]["type"] == "http"
    assert cfg["masquerade"]["type"] == "proxy"
    assert "tls" not in cfg and "obfs" not in cfg
    text = C.dump_yaml(cfg)
    assert yaml.safe_load(text) == cfg
    assert C.share_uri(values, "t").startswith("hysteria2://")
    back = C.values_from_yaml(cfg, schema)
    assert back["_authType"] == "password" and back["_tlsMode"] == "acme"
    hashes = D.parse_hashes("abc  build/hysteria-windows-amd64.exe\n" + "0" * 64 + "  build/x.exe\n")
    assert hashes["x.exe"] == "0" * 64
    print("selftest OK")
    print(f"字段数: {len(C.fields(schema))}, 页签数: {len(schema['tabs'])}, YAML 字节数: {len(text.encode('utf-8'))}")
    return 0
