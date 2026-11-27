"""Windows 进程与部署管理：启动/停止内核、日志采集、计划任务自启、防火墙规则。

不依赖 psutil / pywin32，只调用系统自带命令。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape as xml_escape

from hy2_config import LOGS_DIR, ROOT, STATE_PATH, ensure_dirs, _port_part

TASK_NAME = "Hysteria2 Server"
LOG_FILE = LOGS_DIR / "hysteria.log"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
LineCB = Callable[[str], None]


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=CREATE_NO_WINDOW)


def _state_read() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _state_write(data: dict) -> None:
    ensure_dirs()
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _state_clear() -> None:
    STATE_PATH.unlink(missing_ok=True)


class Runner:
    """持有内核子进程，后台线程把 stdout/stderr 汇入队列与日志文件。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._on_line: LineCB | None = None

    def start(self, exe: Path, config: Path, log_level: str = "info", on_line: LineCB | None = None) -> int:
        if self.is_running():
            raise RuntimeError("内核已在运行，请先停止")
        ensure_dirs()
        exe = Path(exe)
        config = Path(config)
        if not exe.exists():
            raise FileNotFoundError(f"未找到内核: {exe}")
        if not config.exists():
            raise FileNotFoundError(f"未找到配置: {config}")

        cmd = [str(exe), "server", "-c", str(config), "-l", log_level, "--disable-update-check"]
        env = {**os.environ, "HYSTERIA_DISABLE_UPDATE_CHECK": "1", "HYSTERIA_LOG_FORMAT": "console"}
        self.proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=CREATE_NO_WINDOW, env=env,
        )
        self._on_line = on_line
        _state_write({"pid": self.proc.pid, "exe": str(exe), "config": str(config), "cmd": cmd})
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        self._emit(f"[启动] pid={self.proc.pid} cmd={' '.join(cmd)}")
        return self.proc.pid

    def _emit(self, line: str) -> None:
        line = line.rstrip("\r\n")
        self.lines.put(line)
        if self._on_line:
            try:
                self._on_line(line)
            except Exception:
                pass
        try:
            ensure_dirs()
            with LOG_FILE.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
        except OSError:
            pass

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for raw in self.proc.stdout:
            self._emit(raw)
        code = self.proc.wait()
        self._emit(f"[退出] 内核进程结束，退出码 {code}")
        _state_clear()

    def is_running(self) -> bool:
        if self.proc is not None and self.proc.poll() is None:
            return True
        pid = _state_read().get("pid")
        if not pid:
            return False
        try:
            res = _run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], timeout=10)
            return str(pid) in (res.stdout or "") and "INFO:" not in (res.stdout or "")
        except (OSError, subprocess.SubprocessError):
            return False

    def stop(self) -> str:
        """优先终止本实例持有的进程；GUI 重启过则按 pid 文件用 taskkill。"""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
            _state_clear()
            return "已停止内核（本进程）"
        pid = _state_read().get("pid")
        if pid:
            res = _run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=15)
            if res.returncode == 0:
                _state_clear()
                return f"已停止内核 pid={pid}"
            return f"停止失败: {(res.stderr or res.stdout).strip()}"
        return "没有运行中的内核"


def drain(runner: Runner, limit: int = 200) -> list[str]:
    out: list[str] = []
    while len(out) < limit:
        try:
            out.append(runner.lines.get_nowait())
        except queue.Empty:
            break
    return out


def log_tail(n: int = 100) -> str:
    if not LOG_FILE.exists():
        return ""
    return "\n".join(LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])


# ---------- 开机自启（计划任务，SYSTEM 账户，无需登录） ----------

def _task_xml(exe: Path, config: Path, log_level: str) -> str:
    args = f'server -c "{config}" -l {log_level} --disable-update-check'
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Hysteria2 server managed by hy2-win-deploy</Description></RegistrationInfo>
  <Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled><Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec>
    <Command>{xml_escape(str(exe))}</Command>
    <Arguments>{xml_escape(args)}</Arguments>
    <WorkingDirectory>{xml_escape(str(ROOT))}</WorkingDirectory>
  </Exec></Actions>
</Task>'''


def install_autostart(exe: Path, config: Path, log_level: str = "info") -> str:
    if not is_admin():
        raise PermissionError("注册开机自启需要管理员权限，请以管理员身份运行本程序")
    ensure_dirs()
    xml_path = ROOT / "autostart-task.xml"
    # schtasks 要求 UTF-16 编码的任务 XML
    xml_path.write_text(_task_xml(Path(exe), Path(config), log_level), encoding="utf-16")
    res = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"], timeout=30)
    if res.returncode != 0:
        raise RuntimeError(f"创建计划任务失败: {(res.stderr or res.stdout).strip()}")
    return f"已创建计划任务 {TASK_NAME}（开机启动 / SYSTEM）"


def uninstall_autostart() -> str:
    if not is_admin():
        raise PermissionError("删除计划任务需要管理员权限")
    res = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], timeout=30)
    if res.returncode != 0:
        raise RuntimeError(f"删除计划任务失败: {(res.stderr or res.stdout).strip()}")
    xml_path = ROOT / "autostart-task.xml"
    xml_path.unlink(missing_ok=True)
    return f"已删除计划任务 {TASK_NAME}"


def autostart_status() -> bool:
    try:
        res = _run(["schtasks", "/Query", "/TN", TASK_NAME], timeout=15)
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ---------- 防火墙 ----------

def firewall_allow_udp(listen: str) -> str:
    if not is_admin():
        raise PermissionError("修改防火墙需要管理员权限，请以管理员身份运行本程序")
    port = _port_part(listen)
    if not port.isdigit():
        raise ValueError(f"无法从 listen 解析单端口: {listen}")
    name = f"Hysteria2 UDP {port}"
    res = _run(["netsh", "advfirewall", "firewall", "add", "rule",
                f"name={name}", "dir=in", "action=allow", "protocol=UDP",
                f"localport={port}"], timeout=30)
    if res.returncode != 0:
        raise RuntimeError(f"添加防火墙规则失败: {(res.stderr or res.stdout).strip()}")
    return f"已放行 UDP {port} 入站（规则名 {name}）"


def firewall_delete_udp(listen: str) -> str:
    if not is_admin():
        raise PermissionError("修改防火墙需要管理员权限")
    port = _port_part(listen)
    name = f"Hysteria2 UDP {port}"
    res = _run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"], timeout=30)
    if res.returncode != 0:
        raise RuntimeError(f"删除防火墙规则失败: {(res.stderr or res.stdout).strip()}")
    return f"已删除规则 {name}"
