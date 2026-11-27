"""Hysteria2 Windows 内核下载器：版本发现、下载、SHA256 校验、原子安装。

只在 Windows 上使用，纯标准库实现，不依赖 requests。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from hy2_config import BIN_DIR, ensure_dirs

GITHUB_API = "https://api.github.com/repos/apernet/hysteria/releases/latest"
RELEASE_BASE = "https://github.com/apernet/hysteria/releases/download"
OFFICIAL_LATEST = "https://download.hysteria.network/app/latest"
USER_AGENT = "hy2-win-deploy/1.0 (+https://github.com/apernet/hysteria)"
ProgressCB = Callable[[str, int], None]


def _noop(_msg: str, _pct: int) -> None:
    return


def _http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def arch_from_machine() -> str:
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("i386", "i686", "x86"):
        return "386"
    return "amd64"


def filename_for(arch: str) -> str:
    if arch not in ("amd64", "amd64-avx", "386", "arm64"):
        raise ValueError(f"不支持的架构: {arch}")
    return f"hysteria-windows-{arch}.exe"


def latest_release() -> tuple[str, list[str]]:
    """返回 (tag_name, asset 文件名列表)。GitHub API 失败时由调用方降级到官方直链。"""
    raw = json.loads(_http_get(GITHUB_API).decode("utf-8"))
    tag = raw["tag_name"]
    assets = [a["name"] for a in raw.get("assets", [])]
    return tag, assets


def parse_hashes(text: str) -> dict[str, str]:
    """hashes.txt 每行形如 '<sha256>  build/hysteria-windows-amd64.exe'。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, path = parts
        if len(digest) == 64:
            out[Path(path).name] = digest.lower()
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_to(url: str, dest: Path, progress: ProgressCB) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        part = dest.with_name(dest.name + ".part")
        with part.open("wb") as fp:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fp.write(chunk)
                got += len(chunk)
                if total:
                    progress(f"下载中 {got // 1024} / {total // 1024} KB", int(got * 100 / total))
        # 原子替换：下载中断不会留下半个 exe
        part.replace(dest)


def download(arch: str = "", version: str = "latest", progress: ProgressCB = _noop) -> dict:
    """下载内核到 bin/，返回 {path, tag, filename, sha256, verified}。"""
    ensure_dirs()
    arch = arch or arch_from_machine()
    filename = filename_for(arch)
    dest = BIN_DIR / filename

    tag = version
    hashes: dict[str, str] = {}
    if version == "latest":
        try:
            tag, assets = latest_release()
            if filename not in assets:
                progress(f"警告：release {tag} 未列出 {filename}", 0)
        except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as exc:
            progress(f"GitHub API 不可用（{exc}），改用官方直链", 0)
            tag = "latest"
    if tag != "latest":
        try:
            hashes = parse_hashes(_http_get(f"{RELEASE_BASE}/{tag}/hashes.txt", timeout=20).decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as exc:
            progress(f"无法获取校验文件，跳过哈希校验：{exc}", 0)

    url = f"{OFFICIAL_LATEST}/{filename}" if tag == "latest" else f"{RELEASE_BASE}/{tag}/{filename}"
    progress(f"从 {url} 下载", 0)
    _download_to(url, dest, progress)

    digest = sha256_file(dest)
    verified = False
    expected = hashes.get(filename)
    if expected:
        if digest != expected:
            dest.unlink(missing_ok=True)
            raise ValueError(f"SHA256 校验失败：期望 {expected}，实际 {digest}")
        verified = True
        progress("SHA256 校验通过", 100)
    else:
        progress("未取得官方校验值，已记录本地 SHA256", 100)

    meta = {"path": str(dest), "tag": tag, "filename": filename, "arch": arch,
            "sha256": digest, "verified": verified}
    (BIN_DIR / "version.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def installed_meta() -> dict | None:
    path = BIN_DIR / "version.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def binary_path(arch: str = "") -> Path | None:
    meta = installed_meta()
    if meta:
        p = Path(meta["path"])
        if p.exists():
            return p
    arch = arch or arch_from_machine()
    p = BIN_DIR / filename_for(arch)
    return p if p.exists() else None


def binary_version(exe: Path) -> str:
    """调用内核 version 子命令取版本串；失败返回空串，不影响主流程。"""
    try:
        res = subprocess.run([str(exe), "version"], capture_output=True, text=True,
                             timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             env={**os.environ, "HYSTERIA_DISABLE_UPDATE_CHECK": "1"})
        lines = (res.stdout or res.stderr or "").strip().splitlines()
        for line in lines:
            if "Version:" in line:
                return line.strip()
        return lines[0][:120] if lines else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""
