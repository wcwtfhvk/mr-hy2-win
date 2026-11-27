"""Hysteria2 Windows 部署器 GUI（tkinter，schema 驱动表单）。

所有表单字段来自 schema/fields.json；新增字段只改 JSON，不用改本文件。
"""
from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

import yaml

import hy2_config as C
import hy2_downloader as D
import hy2_service as S
from hy2_service import Runner

FIELD_META: dict[str, dict] = {}

BG = "#f5f6f8"
FG = "#20242b"
MUTED = "#6b7280"
ACCENT = "#185FA5"


class ScrollFrame(ttk.Frame):
    """可滚动的表单容器。"""

    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(self, background=BG, highlightthickness=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.win, width=e.width))

    def _wheel(self, event: tk.Event) -> None:
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class ListEditor(ttk.Frame):
    """通用表格编辑器：用于域名、多用户、ACL 行、出站规则等。"""

    def __init__(self, parent: tk.Widget, field: dict) -> None:
        super().__init__(parent)
        self.field = field
        self.columns = field["columns"]
        keys = [c["key"] for c in self.columns]
        self.tree = ttk.Treeview(self, columns=keys, show="headings", height=5)
        for col in self.columns:
            self.tree.heading(col["key"], text=col["label"])
            self.tree.column(col["key"], width=col.get("width", 150), anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        bar = ttk.Frame(parent)
        self.buttons: list[ttk.Button] = []
        for text, cmd in (("添加", self.add), ("编辑", self.edit), ("删除", self.delete),
                          ("上移", lambda: self.move(-1)), ("下移", lambda: self.move(1))):
            btn = ttk.Button(bar, text=text, width=7, command=cmd)
            btn.pack(side="left", padx=2)
            self.buttons.append(btn)
        self.bar = bar
        self.tree.bind("<Double-1>", lambda e: self.edit())
        self.rows: list[dict] = []

    def grid_at(self, row: int) -> int:
        self.grid(row=row, column=1, sticky="ew", padx=6, pady=3)
        self.bar.grid(row=row + 1, column=1, sticky="w", padx=6, pady=(0, 6))
        return row + 2

    def set_rows(self, rows: list[dict]) -> None:
        self.rows = [dict(r) for r in rows]
        self._redraw()

    def get_rows(self) -> list[dict]:
        return [dict(r) for r in self.rows]

    def _redraw(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in self.rows:
            self.tree.insert("", "end", values=[str(row.get(c["key"], "")) for c in self.columns])

    def _dialog(self, initial: dict | None = None) -> dict | None:
        dlg = tk.Toplevel(self)
        dlg.title("编辑条目")
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()
        dlg.resizable(False, False)
        vars_: dict[str, tk.StringVar] = {}
        for i, col in enumerate(self.columns):
            ttk.Label(dlg, text=col["label"], width=14).grid(row=i, column=0, padx=10, pady=6, sticky="w")
            var = tk.StringVar(value=str((initial or {}).get(col["key"], "")))
            ttk.Entry(dlg, textvariable=var, width=42).grid(row=i, column=1, padx=10, pady=6)
            vars_[col["key"]] = var
        result: dict | None = None

        def ok() -> None:
            nonlocal result
            result = {k: v.get() for k, v in vars_.items()}
            dlg.destroy()

        box = ttk.Frame(dlg)
        box.grid(row=len(self.columns), column=0, columnspan=2, pady=12)
        ttk.Button(box, text="确定", command=ok).pack(side="left", padx=6)
        ttk.Button(box, text="取消", command=dlg.destroy).pack(side="left", padx=6)
        dlg.wait_window()
        return result

    def _selected(self) -> int | None:
        sel = self.tree.selection()
        return self.tree.index(sel[0]) if sel else None

    def add(self) -> None:
        item = self._dialog()
        if item:
            self.rows.append(item)
            self._redraw()

    def edit(self) -> None:
        idx = self._selected()
        if idx is None:
            return
        item = self._dialog(self.rows[idx])
        if item:
            self.rows[idx] = item
            self._redraw()

    def delete(self) -> None:
        idx = self._selected()
        if idx is not None:
            self.rows.pop(idx)
            self._redraw()

    def move(self, delta: int) -> None:
        idx = self._selected()
        if idx is None:
            return
        new = idx + delta
        if 0 <= new < len(self.rows):
            self.rows[idx], self.rows[new] = self.rows[new], self.rows[idx]
            self._redraw()
            self.tree.selection_set(self.tree.get_children()[new])

    def set_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for btn in self.buttons:
            btn.configure(state=state)
        self.tree.configure(selectmode="browse" if enabled else "none")


class App(tk.Tk):
    def __init__(self, profile: str = "default") -> None:
        super().__init__()
        C.ensure_dirs()
        self.title("Hysteria2 Windows 部署器")
        self.geometry("1080x720")
        self.minsize(900, 600)
        self.configure(background=BG)
        self.schema = C.load_schema()
        global FIELD_META
        FIELD_META = C.field_map(self.schema)
        self.vars: dict[str, tk.Variable] = {}
        self.widgets: dict[str, tk.Widget] = {}
        self.texts: dict[str, tk.Text] = {}
        self.editors: dict[str, ListEditor] = {}
        self.conditionals: list[tuple[dict, tk.Widget]] = []
        self.scroll_frames: list[ScrollFrame] = []
        self._tick_count = 0
        self.runner = Runner()
        self.current = profile if C.profile_path(profile).exists() else "default"
        if not C.list_profiles():
            C.save_profile("default", C.default_values(self.schema))

        self._build_style()
        self._build_toolbar()
        self._build_tabs()
        self._build_footer()
        self.load_values(C.load_profile(self.current))
        self.refresh_preview()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(250, self._tick)

    # ---------- 外观 ----------

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG, font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Hint.TLabel", background=BG, foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("TNotebook", background=BG)
        style.configure("TNotebook.Tab", padding=(12, 6))
        style.configure("Accent.TButton", foreground="#ffffff", background=ACCENT)
        style.configure("TButton", padding=(6, 4))

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Label(bar, text="配置方案", style="TLabel").pack(side="left")
        self.profile_var = tk.StringVar(value=self.current)
        self.profile_box = ttk.Combobox(bar, textvariable=self.profile_var, width=22,
                                        values=C.list_profiles(), state="readonly")
        self.profile_box.pack(side="left", padx=6)
        self.profile_box.bind("<<ComboboxSelected>>", lambda e: self.switch_profile())
        for text, cmd in (("新建", self.new_profile), ("另存为", self.save_as),
                          ("删除", self.remove_profile), ("导入 YAML", self.import_yaml),
                          ("导出 YAML", self.export_yaml)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
        self.kernel_label = ttk.Label(bar, text="", style="Hint.TLabel")
        self.kernel_label.pack(side="right", padx=8)
        self._refresh_kernel_label()

    def _build_tabs(self) -> None:
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        for tab in self.schema["tabs"]:
            frame = ScrollFrame(self.nb)
            self.scroll_frames.append(frame)
            self.nb.add(frame, text=tab["title"])
            self._build_fields(frame.inner, tab["fields"])

        log_tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(log_tab, text="运行日志")
        self.log_text = tk.Text(log_tab, wrap="none", background="#ffffff", foreground=FG,
                                font=("Consolas", 10), height=20)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(log_tab, command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set)
        tools = ttk.Frame(self)
        tools.pack(fill="x", padx=10)
        ttk.Button(tools, text="清空日志", command=lambda: self.log_text.delete("1.0", "end")).pack(side="left", padx=2)
        ttk.Button(tools, text="打开日志目录", command=lambda: self._open_dir(C.LOGS_DIR)).pack(side="left", padx=2)

        preview_tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(preview_tab, text="配置预览")
        self.preview_text = tk.Text(preview_tab, wrap="none", background="#ffffff", foreground=FG,
                                    font=("Consolas", 10))
        self.preview_text.pack(side="left", fill="both", expand=True)
        sb2 = ttk.Scrollbar(preview_tab, command=self.preview_text.yview)
        sb2.pack(side="right", fill="y")
        self.preview_text.configure(yscrollcommand=sb2.set)
        # 滚轮只作用于当前选中的页签，避免多个 ScrollFrame 抢占全局绑定
        self.bind_all("<MouseWheel>", self._on_wheel)
        self._bind_traces()

    def _on_wheel(self, event: tk.Event) -> None:
        try:
            idx = self.nb.index(self.nb.select())
        except tk.TclError:
            return
        if 0 <= idx < len(self.scroll_frames):
            self.scroll_frames[idx]._wheel(event)

    def _bind_traces(self) -> None:
        """模式类变量一变化就刷新条件字段的可用状态。"""
        for var in self.vars.values():
            var.trace_add("write", lambda *_: self.after_idle(self.refresh_enabled))

    def _build_fields(self, parent: ttk.Frame, field_defs: list[dict]) -> None:
        parent.columnconfigure(1, weight=1)
        row = 0
        for field in field_defs:
            if field["type"] == "note":
                lbl = ttk.Label(parent, text=field["text"], style="Hint.TLabel",
                                wraplength=900, justify="left", background="#eef3f9",
                                padding=(8, 6))
                lbl.grid(row=row, column=0, columnspan=3, sticky="ew", padx=6, pady=(8, 4))
                row += 1
                continue
            key = field["key"]
            label = ttk.Label(parent, text=field["label"], width=22, anchor="w")
            label.grid(row=row, column=0, sticky="nw", padx=6, pady=5)
            kind = field["type"]
            widget: tk.Widget
            if kind in ("str", "secret", "int"):
                var = tk.StringVar(value=str(field.get("default", "")))
                self.vars[key] = var
                widget = ttk.Entry(parent, textvariable=var, width=42, show="*" if kind == "secret" else "")
                widget.grid(row=row, column=1, sticky="ew", padx=6, pady=4)
            elif kind == "bool":
                var_b = tk.BooleanVar(value=bool(field.get("default")))
                self.vars[key] = var_b
                widget = ttk.Checkbutton(parent, variable=var_b)
                widget.grid(row=row, column=1, sticky="w", padx=6, pady=4)
            elif kind == "choice":
                var_c = tk.StringVar(value=str(field.get("default", "")))
                self.vars[key] = var_c
                widget = ttk.Combobox(parent, textvariable=var_c, values=field["options"],
                                      state="readonly", width=39)
                widget.grid(row=row, column=1, sticky="w", padx=6, pady=4)
            elif kind == "text":
                widget = tk.Text(parent, height=5, width=54, wrap="none", background="#ffffff",
                                 foreground=FG, font=("Consolas", 10))
                widget.grid(row=row, column=1, sticky="ew", padx=6, pady=4)
                self.texts[key] = widget
            elif kind == "list":
                editor = ListEditor(parent, field)
                row = editor.grid_at(row)
                self.editors[key] = editor
                self.widgets[key] = editor
                if field.get("when"):
                    self.conditionals.append((field, editor))
                hint = field.get("hint")
                if hint:
                    ttk.Label(parent, text=hint, style="Hint.TLabel", wraplength=420).grid(
                        row=row, column=1, sticky="w", padx=6, pady=(0, 6))
                    row += 1
                continue
            else:
                continue

            self.widgets[key] = widget
            if field.get("hint"):
                ttk.Label(parent, text=field["hint"], style="Hint.TLabel", wraplength=380).grid(
                    row=row, column=2, sticky="nw", padx=8, pady=5)
            if field.get("when"):
                self.conditionals.append((field, widget))
            row += 1

    def _build_footer(self) -> None:
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        buttons = [
            ("保存并生成", self.save_all, "Accent.TButton"),
            ("校验配置", self.check_config, "TButton"),
            ("下载 / 更新内核", self.download_kernel, "TButton"),
            ("启动", self.start_server, "TButton"),
            ("停止", self.stop_server, "TButton"),
            ("生成分享链接", self.show_share, "TButton"),
            ("开机自启", self.toggle_autostart, "TButton"),
            ("放行防火墙", self.add_firewall, "TButton"),
            ("刷新预览", self.refresh_preview, "TButton"),
        ]
        for text, cmd, style in buttons:
            ttk.Button(bar, text=text, command=cmd, style=style).pack(side="left", padx=3)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var, style="Hint.TLabel").pack(side="left", padx=12)

    # ---------- 表单读写 ----------

    def collect(self) -> dict[str, Any]:
        values = C.default_values(self.schema)
        for key, var in self.vars.items():
            values[key] = var.get()
        for key, text in self.texts.items():
            values[key] = text.get("1.0", "end-1c")
        for key, editor in self.editors.items():
            values[key] = editor.get_rows()
        return values

    def load_values(self, values: dict[str, Any]) -> None:
        for key, var in self.vars.items():
            value = values.get(key, FIELD_META[key].get("default"))
            if isinstance(var, tk.BooleanVar):
                var.set(bool(value))
            else:
                var.set("" if value is None else str(value))
        for key, text in self.texts.items():
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", str(values.get(key) or ""))
        for key, editor in self.editors.items():
            editor.set_rows(values.get(key) or [])
        self.refresh_enabled()

    def refresh_enabled(self) -> None:
        values = self.collect()
        for field, widget in self.conditionals:
            ok = C.active(field, values)
            if isinstance(widget, ListEditor):
                widget.set_enabled(ok)
            elif isinstance(widget, tk.Text):
                widget.configure(state="normal" if ok else "disabled")
            elif isinstance(widget, ttk.Combobox):
                widget.configure(state="readonly" if ok else "disabled")
            elif isinstance(widget, ttk.Entry):
                widget.configure(state="normal" if ok else "disabled")
            else:
                try:
                    widget.configure(state="normal" if ok else "disabled")
                except tk.TclError:
                    pass

    def _tick(self) -> None:
        for line in S.drain(self.runner, limit=100):
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
        # tasklist / schtasks 都是外部进程，2 秒查一次即可，不要每帧拉起
        self._tick_count += 1
        if self._tick_count % 4 == 1:
            running = self.runner.is_running()
            admin = "管理员" if S.is_admin() else "普通用户"
            autostart = "自启已开" if S.autostart_status() else "自启未开"
            self.status_var.set(f"内核 {'运行中' if running else '已停止'} | {admin} | {autostart}")
        self.after(500, self._tick)

    # ---------- 配置方案 ----------

    def switch_profile(self) -> None:
        name = self.profile_var.get()
        if not name:
            return
        self.current = name
        self.load_values(C.load_profile(name))
        self.refresh_preview()
        self.status_var.set(f"已切换到配置 {name}")

    def _refresh_profiles(self) -> None:
        self.profile_box.configure(values=C.list_profiles())
        self.profile_var.set(self.current)

    def new_profile(self) -> None:
        name = simpledialog.askstring("新建配置", "配置名（字母/数字/下划线/连字符）：", parent=self)
        if not name:
            return
        try:
            C.save_profile(name, C.default_values(self.schema))
        except ValueError as exc:
            messagebox.showerror("错误", str(exc), parent=self)
            return
        self.current = name
        self._refresh_profiles()
        self.load_values(C.load_profile(name))
        self.refresh_preview()

    def save_as(self) -> None:
        name = simpledialog.askstring("另存为", "新配置名：", parent=self)
        if not name:
            return
        try:
            C.save_profile(name, self.collect())
        except ValueError as exc:
            messagebox.showerror("错误", str(exc), parent=self)
            return
        self.current = name
        self._refresh_profiles()

    def remove_profile(self) -> None:
        if self.current == "default":
            messagebox.showwarning("提示", "default 不能删除", parent=self)
            return
        if messagebox.askyesno("确认", f"删除配置 {self.current}？", parent=self):
            C.delete_profile(self.current)
            self.current = "default"
            self._refresh_profiles()
            self.load_values(C.load_profile("default"))
            self.refresh_preview()

    def import_yaml(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="导入 config.yaml",
                                          filetypes=[("YAML", "*.yaml;*.yml"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                raise ValueError("顶层必须是映射")
            self.load_values(C.values_from_yaml(cfg, self.schema))
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            messagebox.showerror("导入失败", str(exc), parent=self)
            return
        self.refresh_preview()
        self.status_var.set(f"已导入 {Path(path).name}")

    def export_yaml(self) -> None:
        errors = C.validate(self.collect())
        if errors:
            messagebox.showerror("校验未通过", "\n".join(errors), parent=self)
            return
        path = filedialog.asksaveasfilename(parent=self, title="导出 config.yaml", defaultextension=".yaml",
                                            initialfile="config.yaml", filetypes=[("YAML", "*.yaml")])
        if not path:
            return
        C.write_config(self.collect(), path)
        self.status_var.set(f"已导出 {path}")

    def save_all(self) -> None:
        errors = C.validate(self.collect())
        if errors:
            messagebox.showerror("校验未通过", "\n".join(errors), parent=self)
            return
        values = self.collect()
        C.save_profile(self.current, values)
        path = C.write_config(values)
        self.refresh_preview()
        warns = C.warnings(values)
        msg = f"已保存 {C.profile_path(self.current).name}\n已生成 {path}"
        if warns:
            msg += "\n\n提示：\n" + "\n".join(warns)
        messagebox.showinfo("保存成功", msg, parent=self)

    def check_config(self) -> None:
        values = self.collect()
        errors = C.validate(values)
        warns = C.warnings(values)
        if errors:
            messagebox.showerror("校验未通过", "\n".join(errors), parent=self)
            return
        text = "校验通过"
        if warns:
            text += "\n\n提示：\n" + "\n".join(warns)
        messagebox.showinfo("校验结果", text, parent=self)

    def refresh_preview(self) -> None:
        try:
            text = C.dump_yaml(C.build_config(self.collect()))
        except (ValueError, yaml.YAMLError, json.JSONDecodeError) as exc:
            text = f"# 生成失败: {exc}"
        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert("1.0", text)
        self.preview_text.configure(state="disabled")

    def show_share(self) -> None:
        uri = C.share_uri(self.collect(), self.current)
        self.clipboard_clear()
        self.clipboard_append(uri)
        messagebox.showinfo("客户端分享链接（已复制）", uri, parent=self)

    # ---------- 部署动作 ----------

    def _bg(self, fn, done: str) -> None:
        def work() -> None:
            try:
                result = fn()
                self.after(0, lambda: self.status_var.set(result or done))
            except Exception as exc:  # 后台线程异常必须回主线程展示，不能静默
                self.after(0, lambda e=exc: messagebox.showerror("操作失败", str(e), parent=self))

        threading.Thread(target=work, daemon=True).start()

    def download_kernel(self) -> None:
        values = self.collect()
        arch, version = values.get("_arch", "amd64"), values.get("_version", "latest")
        self.status_var.set("开始下载内核…")

        def progress(msg: str, pct: int) -> None:
            self.after(0, lambda: self.status_var.set(f"{msg} ({pct}%)" if pct else msg))

        def job() -> str:
            meta = D.download(arch=arch, version=version, progress=progress)
            self.after(0, self._refresh_kernel_label)
            return f"内核已安装 {meta['tag']} / {meta['filename']} / SHA256 {'已校验' if meta['verified'] else '未校验'}"

        self._bg(job, "下载完成")

    def _ready_paths(self) -> tuple[Path, Path] | None:
        exe = D.binary_path()
        if not exe:
            messagebox.showerror("缺少内核", "请先点击「下载 / 更新内核」", parent=self)
            return None
        errors = C.validate(self.collect())
        if errors:
            messagebox.showerror("校验未通过", "\n".join(errors), parent=self)
            return None
        C.save_profile(self.current, self.collect())
        cfg = C.write_config(self.collect())
        return exe, cfg

    def start_server(self) -> None:
        paths = self._ready_paths()
        if not paths:
            return
        exe, cfg = paths
        try:
            pid = self.runner.start(exe, cfg, self.collect().get("_logLevel", "info"))
            self.status_var.set(f"已启动 pid={pid}")
        except (OSError, RuntimeError) as exc:
            messagebox.showerror("启动失败", str(exc), parent=self)

    def stop_server(self) -> None:
        try:
            self.status_var.set(self.runner.stop())
        except (OSError, RuntimeError) as exc:
            messagebox.showerror("停止失败", str(exc), parent=self)

    def toggle_autostart(self) -> None:
        paths = self._ready_paths()
        if not paths:
            return
        exe, cfg = paths
        try:
            if S.autostart_status():
                result = S.uninstall_autostart()
            else:
                result = S.install_autostart(exe, cfg, self.collect().get("_logLevel", "info"))
            self.status_var.set(result)
        except (PermissionError, RuntimeError) as exc:
            messagebox.showerror("开机自启", str(exc), parent=self)

    def add_firewall(self) -> None:
        try:
            result = S.firewall_allow_udp(str(self.collect().get("listen") or ":443"))
            self.status_var.set(result)
        except (PermissionError, RuntimeError, ValueError) as exc:
            messagebox.showerror("防火墙", str(exc), parent=self)

    def _refresh_kernel_label(self) -> None:
        meta = D.installed_meta()
        self.kernel_label.configure(text=f"内核: {meta['tag']} ({meta['arch']})" if meta else "内核: 未安装")

    def _open_dir(self, path: Path) -> None:
        import os
        try:
            os.startfile(str(path))  # Windows 资源管理器
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)

    def on_close(self) -> None:
        if self.runner.is_running():
            if not messagebox.askyesno("退出", "内核仍在运行。退出后它会继续后台运行，确定退出？", parent=self):
                return
        self.destroy()


def launch(profile: str = "default", selftest: bool = False) -> None:
    app = App(profile)
    if selftest:
        app.after(400, app.destroy)
    app.mainloop()
