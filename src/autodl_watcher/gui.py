from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import uuid
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__
from .cli import normalize_host
from .config import load_config
from .login import LocalStartupError
from .main import positive_seconds
from .training import TrainingServer, load_servers, save_servers
from .training_ui import TrainingPane, TrainingService
from .session import GPU_PREFIX, REPORT_PREFIX, SESSION_PREFIX, emit_gpu_samples, emit_session

ROOT = (Path(os.environ.get("AUTODL_APP_ROOT", Path(sys.executable).parent)).resolve()
        if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2])  # 配置和运行数据归属于源码根目录或公开 EXE 所在目录。
DATA_PREFIX = "WATCHER_HOSTS "  # 约定的结构化输出前缀；主机列表与普通日志共用 stdout 管道。


def worker_python():  # GUI 用 pythonw，后台任务用带管道且无窗口的 python，保证日志和登录输入可用。
    executable = Path(sys.executable)  # 获取当前 GUI 所属环境，后台任务必须使用同一环境的依赖。
    return str(executable.with_name("python.exe")) if executable.name.lower() == "pythonw.exe" else sys.executable


def worker_command(task, *args):  # 源码使用当前 Python；发布版使用共享依赖目录中的无窗口后台程序。
    modules = {"monitor": "autodl_watcher.main", "discover": "autodl_watcher.gui",
               "login": "autodl_watcher.login"}
    module = modules[task]  # 仅允许这三个应用任务，不执行任意模块。
    if getattr(sys, "frozen", False):
        return [str(ROOT / "_internal" / "AutoDLWorker.exe"), task, *args]
    return [worker_python(), "-u", "-m", module, *(["--discover"] if task == "discover" else []), *args]


def log_tag(line):  # 配置说明不是故障；实际失败优先于同一行的成功或占用信息。
    lowered = line.strip().lower()
    if lowered.startswith("telemetry容错："):
        return "normal"
    if any(word in lowered for word in ("失败", "断连", "断开", "异常", "错误", "error", "traceback", "timeout", "超时", "未通过", "失效", "额度不足", "拒绝", "暂时不可用",
                                        "不允许开机", "无法核对", "暂缓释放额度", "暂停释放无卡实例")):
        return "error"
    if "受理" in lowered or "验证成功后" in lowered:
        return "normal"  # 电源请求受理不等于实例已占用GPU。
    if any(word in lowered for word in ("已开机", "已连接", "连接正常", "会话有效", "采集恢复", "已占用", "有卡运行",
                                        "本人 gpu 占用 有", "本人gpu占用 有", "本人占用 有")):
        return "success"
    if "成功" in lowered:
        return "success"
    return "normal"


def monitor_command(config, entry, user, poll, live, stop_file, convert_no_gpu=False):  # 构造与 CLI 相同的监控命令。
    if not entry:  # 没选入口就拒绝启动，避免误用命令行默认主机。
        raise ValueError("请先选择服务器入口")
    if not user.strip():  # 此名称用于占用归属匹配，不是登录账号或 SSH 用户名。
        raise ValueError("请填写占用列表中的本人用户名")
    poll = positive_seconds(str(poll))  # 在创建子进程前完成数值验证。
    target = next((item for item in config.auto_start.targets
                   if item.machine_name == entry and item.enabled and item.instance_uuid), None)
    if live and (target is None or not config.auto_start.enabled):  # 可见主机不等于已有可开机实例，真实模式需要有效配置。
        raise ValueError("此入口尚未配置启用的开机实例，请先只读监控或在 config.yaml 配置 targets")
    if convert_no_gpu and not live:
        raise ValueError("无卡转有卡须同时启用真实自动开机")
    command = worker_command("monitor", "--config", str(ROOT / "config.yaml"),
            "--host", normalize_host(entry), "--entry", entry, "--user", user.strip(),
            "--poll-seconds", str(poll), "--ui",
            "--live" if live else "--dry-run", "--no-login", "--stop-file", str(stop_file))  # 参数列表直接传给进程，不经 shell 拼接。
    return command + (["--convert-no-gpu"] if convert_no_gpu else [])


class WatcherWindow:  # 只负责交互与进程管理，监控业务仍由 main.run_monitor 执行。
    def __init__(self, root, debug=False):  # 先读基础配置和本地偏好，再创建控件与消息轮询。
        self.root = root  # Tk 主窗口及事件循环的拥有者，子线程不直接操作它。
        self.config = load_config(ROOT / "config.yaml")  # YAML 提供目标实例、阈值、浏览器资料和运行路径。
        self.local = ROOT / ".ui"  # 不纳入 Git 的个人偏好、停止信号和启动日志目录。
        self.local.mkdir(exist_ok=True)
        self.stop_file = self.local / f"stop-{os.getpid()}"  # 以 GUI 进程号区分停止信号，避免读取上一个窗口的信号。
        self.process = None  # None 表示空闲；每个窗口同一时间只管理一个后台任务。
        self.job = ""  # 区分 discover、monitor、login，决定按钮与结束行为。
        self.events = queue.Queue()  # 读管道线程向 Tk 主线程发送 (消息类型, 内容)。
        self.closing = False  # 请求关窗后先等后台任务安全结束，再销毁 Tk。
        self.user = tk.StringVar(value=self.config.occupancy.self_user)  # StringVar 将输入框内容与 Python 变量绑定。
        self.poll = tk.StringVar(value=str(self.config.monitor.poll_seconds))
        self.debug = debug  # 普通模式按用户要求真实开机；显式调试模式只读，打开窗口仍不会自动监控。
        self.live = tk.BooleanVar(value=not debug)
        self.convert_no_gpu = tk.BooleanVar(value=not debug)
        self.status = tk.StringVar(value="未运行 · 选择入口后开始监控")
        self.session_status = "unknown"
        self.session_message = "登录会话尚未核验"
        self.session_text = tk.StringVar(value="会话未核验 · 核验时间：—")
        self.gpu_snapshot = {"samples": [], "error": "尚未采样"}
        self._gpu_render_key = None
        self.saved_entry = ""
        self.training = TrainingService(self.events)
        self.server_file = self.local / "servers.json"
        self.servers, self.external_panes = [], {}
        self.title_text = tk.StringVar(value="AutoDL 平台")
        try:
            self.servers = load_servers(self.server_file)
            for server in self.servers:
                self.training.register(server)
        except (OSError, ValueError, TypeError) as exc:
            self.status.set(f"训练连接配置读取失败：{exc}")
        preferences = self.local / "preferences.json"
        if preferences.exists():
            try:
                saved = json.loads(preferences.read_text(encoding="utf-8"))  # 缺失字段继续使用 YAML 默认值。
                self.user.set(saved.get("user", self.user.get()))
                self.poll.set("60" if "usage" in saved else saved.get("poll", self.poll.get()))  # 旧双间隔偏好一次迁移为60秒。
                self.saved_entry = saved.get("entry", "")
            except (ValueError, OSError, AttributeError):
                self.status.set("本地偏好读取失败，已使用默认配置")
        self._build()
        self._populate([(item.machine_name, "待刷新", "未核验")
                        for item in self.config.auto_start.targets if item.enabled])
        self.root.after(100, self._drain)  # 将首次消息处理排入 Tk 事件队列，而不是阻塞等待后台输出。
        self.root.after(0, self.refresh)  # 启动后只读核验，不开始监控或打开登录浏览器。
        self.root.protocol("WM_DELETE_WINDOW", self.close)  # 标题栏关闭按钮也走安全停止流程。

    def _build(self):  # 从上到下构建工具栏、服务器表、配置区、操作按钮和日志区。
        self.root.title(f"AutoDL GPU Watcher · v{__version__}")
        self.root.geometry("1000x800")  # 保留用户设置的初始窗口高度；单位为像素。
        self.root.minsize(820, 660)  # 限制手动缩小时的最小尺寸，避免控件完全挤出窗口。
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Treeview", rowheight=22, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9))
        style.configure("TButton", padding=(10, 5))
        style.configure("GPU.Horizontal.TProgressbar", background="#397ca8")
        style.configure("Memory.Horizontal.TProgressbar", background="#34865b")
        style.configure("Error.Horizontal.TProgressbar", background="#b43b48")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
        self.check_images = []  # Tk不会持有Python图片引用；保留它们，防止勾选标记被回收。
        for selected in (False, True):
            indicator = tk.PhotoImage(master=self.root, width=16, height=16)
            indicator.put("#ffffff", to=(1, 1, 15, 15))
            for border in ((1, 1, 15, 2), (1, 14, 15, 15), (1, 1, 2, 15), (14, 1, 15, 15)):
                indicator.put("#727272", to=border)
            if selected:
                for x, y in ((4, 8), (5, 9), (6, 10), (7, 11), (8, 10), (9, 9), (10, 8), (11, 7), (12, 6), (12, 5)):
                    indicator.put("#245cc4", to=(x, y, x + 2, y + 2))  # 画✓，替换clam主题默认的X。
            self.check_images.append(indicator)
        style.element_create("Watcher.indicator", "image", self.check_images[0], ("selected", self.check_images[1]))
        style.layout("Watcher.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Watcher.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [("Checkbutton.label", {"sticky": "nswe"})]})]})])
        frame = ttk.Frame(self.root, padding=8)
        frame.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(frame)
        self.notebook.pack(fill="both", expand=True, pady=(3, 0))
        self.auto_page = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(self.auto_page, text="AutoDL 平台")
        page = self.auto_page
        ttk.Label(page, textvariable=self.title_text, style="Title.TLabel").pack(anchor="w")
        toolbar = ttk.Frame(page)
        toolbar.pack(fill="x", pady=4)
        self.refresh_button = ttk.Button(toolbar, text="刷新服务器", command=self.refresh)
        self.refresh_button.pack(side="left")
        self.login_button = ttk.Button(toolbar, text="登录 / 更新会话", command=self.login)
        self.login_button.pack(side="left", padx=5)
        self.login_done = ttk.Button(toolbar, text="已登录并关闭浏览器", command=self.finish_login, state="disabled")
        self.login_done.pack(side="left")
        ttk.Button(toolbar, text="＋ 添加服务器", command=lambda: self.edit_server()).pack(side="right")
        self.session_label = ttk.Label(page, textvariable=self.session_text, foreground="#7a5a18", wraplength=920)
        self.session_label.pack(anchor="w", pady=(0, 4))
        page.bind("<Configure>", lambda event: self.session_label.configure(wraplength=max(200, event.width - 20)))
        body = ttk.Frame(page)
        body.pack(fill="x")
        left = ttk.Frame(body, width=330)
        left.pack(side="left", fill="y")
        right = ttk.LabelFrame(body, text="我的训练任务", padding=6)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.training_pane = TrainingPane(right, self.training, self.configure_training, self.poll.get)
        self.training_pane.pack(fill="both", expand=True)
        servers = ttk.Frame(left)
        servers.pack(fill="x")
        self.table = ttk.Treeview(servers, columns=("entry", "slots", "target", "training"), show="headings", height=5, selectmode="browse")
        for column, title, width in [("entry", "服务器", 100), ("slots", "空闲/总数", 65), ("target", "自动开机", 78), ("training", "训练SSH·设置", 90)]:
            self.table.heading(column, text=title)
            self.table.column(column, width=width, stretch=False)
        self.table.pack(side="left", fill="y")
        scroll = ttk.Scrollbar(servers, orient="vertical", command=self.table.yview)
        scroll.pack(side="right", fill="y")
        self.table.configure(yscrollcommand=scroll.set)
        self.table.bind("<Button-1>", self._table_click)
        self.table.bind("<Key>", lambda event: "break" if self.process is not None else None)
        self.table.bind("<<TreeviewSelect>>", lambda event: self._render_gpus())
        self.gpu_panel = ttk.LabelFrame(left, text="物理 GPU", padding=(4, 3))
        self.gpu_panel.pack(fill="x", pady=4)
        options = ttk.LabelFrame(left, text="AutoDL 监控设置", padding=4)
        options.pack(fill="x", pady=3)
        self.inputs = []
        for col, (label, value) in enumerate([("用户", self.user), ("间隔/秒", self.poll)]):
            ttk.Label(options, text=label).grid(row=0, column=col * 2, sticky="w", padx=2)
            entry = ttk.Entry(options, textvariable=value, width=12 if col == 0 else 6)
            entry.grid(row=0, column=col * 2 + 1, sticky="ew", padx=2, pady=3)
            options.columnconfigure(col * 2 + 1, weight=1)
            self.inputs.append(entry)
        self.live_check = ttk.Checkbutton(options, text="启用真实自动开机（会产生费用）", variable=self.live, style="Watcher.TCheckbutton",
                                          command=lambda: self.convert_no_gpu.set(False) if not self.live.get() else None)
        self.live_check.grid(row=1, column=0, columnspan=4, sticky="w", pady=2)
        self.convert_check = ttk.Checkbutton(options, text="关闭本账号全部无卡实例（会中断）", variable=self.convert_no_gpu, style="Watcher.TCheckbutton")
        self.convert_check.grid(row=2, column=0, columnspan=4, sticky="w", pady=2)
        controls = ttk.Frame(left)
        controls.pack(fill="x", pady=4)
        self.start_button = ttk.Button(controls, text="开始监控", command=self.start, width=8)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止监控", command=self.stop, state="disabled", width=8)
        self.stop_button.pack(side="left", padx=4)
        self.save_button = ttk.Button(controls, text="保存设置", command=self.save, width=8)
        self.save_button.pack(side="left")
        self.status_label = ttk.Label(page, textvariable=self.status, foreground="#245cc4")
        self.status_label.pack(anchor="w", fill="x", pady=4)
        self.status.trace_add("write", lambda *_: self.status_label.configure(
            foreground={"success": "#18733a", "error": "#b42318"}.get(log_tag(self.status.get()), "#245cc4")))
        log_frame = ttk.Frame(page)
        log_frame.pack(fill="both", expand=True)
        horizontal = ttk.Scrollbar(log_frame, orient="horizontal")
        horizontal.pack(side="bottom", fill="x")
        self.log = ScrolledText(log_frame, height=8, wrap="none", state="disabled", font=("Microsoft YaHei UI", 9),
                                xscrollcommand=horizontal.set, background="#17202e", foreground="#e2e8f0", padx=8, pady=6)
        horizontal.configure(command=self.log.xview)
        self.log.tag_configure("success", foreground="#79e69d")
        self.log.tag_configure("error", foreground="#ff8181")
        self.log.pack(fill="both", expand=True)
        self._server_tabs()

    def _server_tabs(self):
        for server in self.servers:
            if server.entry or server.id in self.external_panes:
                continue
            page = ttk.Frame(self.notebook, padding=10)
            self.notebook.add(page, text=server.name)
            title = ttk.Label(page, text=server.name, style="Title.TLabel")
            title.pack(anchor="w")
            options = ttk.Frame(page)
            options.pack(fill="x", pady=4)
            ttk.Label(options, text="采样间隔（秒）").pack(side="left")
            ttk.Button(options, text="＋ 添加服务器", command=lambda: self.edit_server()).pack(side="right")
            interval = tk.StringVar(value="60")
            ttk.Entry(options, textvariable=interval, width=9).pack(side="left", padx=5)
            pane = TrainingPane(page, self.training, lambda key=server.id: self.edit_server(key), interval.get, show_gpus=True)
            pane.pack(fill="both", expand=True)
            pane.select(server.id)
            self.external_panes[server.id] = (page, title, pane)

    def _table_click(self, event):
        entry = self.table.identify_row(event.y)
        if entry and self.table.identify_column(event.x) == "#4":
            self.configure_training(entry)  # 编辑点击行；不改变正在监控的入口或正在查看的训练连接。
            return "break"
        return "break" if self.process is not None else None

    def configure_training(self, entry=None):
        entry = entry or self.selected_entry()
        if not entry:
            return
        server = next((item for item in self.servers if item.entry == entry), None)
        self.edit_server(server.id if server else None, entry)

    def edit_server(self, server_id=None, entry=""):
        from dataclasses import replace
        server = next((item for item in self.servers if item.id == server_id), None)
        server = server or TrainingServer(entry or uuid.uuid4().hex, entry.removeprefix("autodl-") or "新服务器", "", entry=entry)
        dialog = tk.Toplevel(self.root)
        dialog.title("训练监控连接 · 只读SSH")
        dialog.transient(self.root)
        form = ttk.Frame(dialog, padding=14)
        form.pack(fill="both", expand=True)
        values = {}
        fields = [("name", "显示名称"), ("ssh_alias", "SSH别名（用户.ssh/config）"), ("python", "远程Python路径"),
                  ("project", "项目目录（可选，绝对路径）"), ("scripts", "脚本名/通配符（英文逗号分隔）"), ("stalled_minutes", "无进展提醒（分钟）")]
        for row, (key, label) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            values[key] = tk.StringVar(value=str(getattr(server, key)))
            ttk.Entry(form, textvariable=values[key], width=42).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Label(form, text="使用已有SSH密钥和已核验主机；不会保存密码，也不会修改远端训练。\n首次连接请先在终端确认SSH可用。监控中可保存，新配置下次启动本服务器训练监控时生效。", wraplength=570).grid(row=6, column=0, columnspan=2, sticky="w", pady=8)
        reason = ttk.Label(form, foreground="#b42318", wraplength=570)
        reason.grid(row=7, column=0, columnspan=2, sticky="w")
        def apply():
            try:
                updated = replace(server, **{key: value.get().strip() for key, value in values.items() if key != "stalled_minutes"},
                                  stalled_minutes=float(values["stalled_minutes"].get())).validate()
                servers = [updated if item.id == server.id else item for item in self.servers]
                if not any(item.id == server.id for item in self.servers):
                    servers.append(updated)
                save_servers(self.server_file, servers)
                self.servers = servers
                self.training.register(updated)
                self._training_columns()
                self._server_tabs()
                if updated.id in self.external_panes:
                    page, title, pane = self.external_panes[updated.id]
                    self.notebook.tab(page, text=updated.name)
                    title.configure(text=updated.name)
                    pane.render_key = None
                    pane.render()
                self._render_gpus()
                dialog.destroy()
            except (ValueError, OSError) as exc:
                reason.configure(text=str(exc))
        ttk.Button(form, text="保存连接", command=apply).grid(row=8, column=1, sticky="e", pady=8)

    def _render_gpus(self):  # 使用采样消息更新条形图；此处不发起网络查询。
        entry = self.selected_entry()
        self.title_text.set(entry.removeprefix("autodl-") or "AutoDL 平台")
        server = next((item for item in self.servers if item.entry == entry), None)
        self.training_pane.select(server.id if server else None)
        host = normalize_host(entry) if entry else ""
        samples = sorted((item for item in self.gpu_snapshot["samples"] if item["host"] == host),
                         key=lambda item: item["gpu_index"])
        rows = []
        for sample in samples:
            try:
                age = (datetime.now() - datetime.fromisoformat(sample["observed_at"])).total_seconds()
                stale = age > self.gpu_snapshot.get("stale_after_seconds", self.config.monitor.stale_after_seconds)
                util, used, total = float(sample["util_pct"]), float(sample["memory_used_mb"]), float(sample["memory_total_mb"])
                valid = all(math.isfinite(value) for value in (util, used, total)) and 0 <= util <= 100 and 0 <= used <= total and total > 0
            except (ValueError, TypeError, KeyError):
                stale, valid, util, used, total = False, False, 0, 0, 0
            rows.append((sample["gpu_index"], sample["gpu_name"], sample["observed_at"], stale, valid, util, used, total))
        key = (entry, tuple(rows), self.gpu_snapshot.get("error", ""))
        if key == self._gpu_render_key:
            return  # 新数据、入口切换或过期时才重画，避免每次消息轮询销毁控件。
        self._gpu_render_key = key
        for child in self.gpu_panel.winfo_children():
            child.destroy()
        self.gpu_panel.configure(text=f"{entry.removeprefix('autodl-') or '未选入口'} · {host or '物理 GPU'}")
        if not rows:
            ttk.Label(self.gpu_panel, text=self.gpu_snapshot.get("error") or "暂无有效 GPU 数据", foreground="#7a5a18").pack(anchor="w")
            return
        grid = ttk.Frame(self.gpu_panel)
        grid.pack(fill="both", expand=True)
        for col, title in enumerate(("GPU", "GPU 利用率", "显存占用率")):
            ttk.Label(grid, text=title, font=("Microsoft YaHei UI", 8)).grid(row=0, column=col, sticky="w", padx=4)
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(2, weight=1)
        for row, (index, name, stamp, stale, valid, util, used, total) in enumerate(rows, 1):
            ttk.Label(grid, text=f"#{index}").grid(row=row, column=0, padx=4, sticky="w")
            for col, value, label in ((1, util, f"{util:.0f}%"), (2, used / total * 100 if total else 0, f"{used / total * 100 if total else 0:.0f}% · {used:.0f}/{total:.0f} MB")):
                cell = ttk.Frame(grid)
                cell.grid(row=row, column=col, sticky="ew", padx=3, pady=1)
                if stale or not valid:
                    ttk.Label(cell, text="数据已过期" if stale else "数据不可用", foreground="#7a5a18").pack(anchor="w")
                else:
                    ttk.Progressbar(cell, value=value, maximum=100, length=35,
                                    style="GPU.Horizontal.TProgressbar" if col == 1 else "Memory.Horizontal.TProgressbar").pack(side="left", fill="x", expand=True)
                    ttk.Label(cell, text=label, font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(4, 0))
        ttk.Label(self.gpu_panel, text=f"{rows[0][1]} · 数据时间 {min(row[2] for row in rows)[11:19]}",
                  font=("Microsoft YaHei UI", 8)).pack(anchor="w")

    def _populate(self, rows):  # rows 中每项为 (入口名, 空闲/总数文本, 开机配置状态)。
        selected = self.selected_entry() or self.saved_entry  # 刷新时优先保留本次选择，其次恢复上次保存的入口。
        self.table.delete(*self.table.get_children())
        for entry, slots, target in rows:
            self.table.insert("", "end", iid=entry, values=(entry, slots, target, self._training_status(entry)))  # 用入口名作行 ID，选择结果可直接作为监控参数。
        entries = self.table.get_children()
        if entries:
            self.table.selection_set(selected if selected in entries else entries[0])

    def _training_status(self, entry):
        server = next((item for item in self.servers if item.entry == entry), None)
        if not server:
            return "未配置"
        state = self.training.states[server.id]
        snapshot = state["snapshot"]
        if state.get("pending"):
            return "已存待用"
        if snapshot and snapshot.get("error"):
            return "连接失败"
        if state["active"]:
            return "已连接" if snapshot else "连接中"
        return "已停止" if snapshot else "待连接"

    def _training_columns(self):
        for entry in self.table.get_children():
            value = self._training_status(entry)
            if self.table.set(entry, "training") != value:
                self.table.set(entry, "training", value)

    def selected_entry(self):  # Treeview 返回选中 ID 元组；单选表最多有一项。
        selection = self.table.selection()
        return selection[0] if selection else ""

    def _command(self):  # 从控件快照生成启动参数；不改写受 Git 管理的 YAML。
        entry = self.selected_entry()
        verified = bool(entry and self.table.set(entry, "target") == "可开机")
        live = self.live.get() and verified  # 本地旧UUID不代表实际存在容器；未核验入口始终只读。
        return monitor_command(self.config, entry, self.user.get(), self.poll.get(), live,
                               self.stop_file, self.convert_no_gpu.get() and live)

    def save(self):  # 保存输入前复用启动校验，避免把明显无效的设置留到下次。
        try:
            self._command()
            preferences = {"entry": self.selected_entry(), "user": self.user.get().strip(),
                           "poll": self.poll.get()}
            temp = self.local / "preferences.tmp"
            temp.write_text(json.dumps(preferences, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.local / "preferences.json")  # 临时文件写完整后替换，降低中断产生半份 JSON 的风险。
            self.status.set("设置已保存 · 调试模式默认只读" if self.debug else "设置已保存 · 普通模式默认真实开机")
            return True
        except (ValueError, argparse.ArgumentTypeError, OSError) as exc:
            messagebox.showerror("设置无效", str(exc), parent=self.root)
            return False

    def _busy(self, value):  # 统一切换操作权限；监控时能停止，登录时能确认同步。
        for button in [self.refresh_button, self.login_button, self.start_button, self.save_button,
                       self.live_check, self.convert_check, *self.inputs]:
            button.configure(state="disabled" if value else "normal")
        self.stop_button.configure(state="normal" if value and self.job == "monitor" else "disabled")
        self.login_done.configure(state="normal" if value and self.job == "login" else "disabled")

    def _session(self, status, message, checked_at=""):
        labels = {"checking": "会话核验中", "valid": "登录会话有效", "invalid": "登录已失效", "unknown": "无法确认登录会话"}
        if status not in labels or not isinstance(message, str) or not isinstance(checked_at, str):
            raise ValueError("会话消息格式无效")
        self.session_status, self.session_message = status, message
        self.session_text.set(f"{labels[status]} · {message} · 核验时间：{checked_at or '—'}")
        self.session_label.configure(foreground={"valid": "#18733a", "invalid": "#b42318"}.get(status, "#7a5a18"))

    def _launch(self, command, job):  # 为所有任务建立相同的环境、日志管道和退出通知。
        if self.process is not None:  # 禁止同一窗口重复启动任务，避免同时占用浏览器资料。
            return
        env = os.environ.copy()
        if not getattr(sys, "frozen", False):
            env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        env["AUTODL_APP_ROOT"] = str(ROOT)  # 后台 EXE 位于依赖目录，配置与运行数据仍归属于应用根目录。
        env["PYTHONUTF8"] = "1"  # 子进程日志使用 UTF-8，与读取管道时的解码一致。
        try:
            self.process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",  # 错误输出也进入底部日志；stdin 管道用于确认人工登录。
                errors="replace", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as exc:
            messagebox.showerror("启动失败", str(exc), parent=self.root)
            return
        self.job = job
        self.gpu_snapshot = {"samples": [], "error": "等待采样"}
        self._render_gpus()
        self._busy(True)
        if job in {"discover", "monitor", "login"}:
            self._session("checking", "等待人工登录及同步后核验" if job == "login" else "正在核验真实主机接口")
        self.status.set({"monitor": "监控进程已启动 · 等待首次采集", "discover": "正在读取实际服务器列表…",
                         "login": "请在 Edge 登录并关闭窗口，然后点击“已登录并关闭浏览器”"}[job])
        threading.Thread(target=self._read, args=(self.process,), daemon=True).start()  # 管道 readline 可以阻塞，因此放入后台线程。

    def _read(self, process):  # 子线程只传递消息，所有控件更新留在 Tk 主线程。
        try:
            for line in process.stdout:
                self.events.put(("line", line))
        except (OSError, ValueError) as exc:
            self.events.put(("line", f"后台日志读取失败：{exc}\n"))
            if self.job == "monitor":
                self.stop_file.touch()  # 长期监控先请求现有安全停止，不能只等坏管道自行恢复。
            elif self.job == "discover" and process.poll() is None:
                process.terminate()  # 仅结束自有只读任务，不中断登录资料同步或正常监控。
            if self.job != "login":
                try:
                    process.stdout.close()  # 释放损坏的读端，避免worker写满管道后阻塞。
                except (OSError, ValueError):
                    pass
        finally:
            self.events.put(("exit", process.wait()))  # 管道异常仍等待真实退出，再恢复操作按钮。

    def _drain(self):  # 仅由 Tk 的 after 回调调用，在主线程消费跨线程消息。
        reschedule = True
        try:
            for _ in range(200):  # 限制处理量，让鼠标、重绘和停止按钮仍有机会响应。
                try:
                    kind, value = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    if kind == "training":
                        self.training.receive(value)
                    elif kind == "exit":
                        process, self.process = self.process, None  # 真实退出先解除任务锁，流清理异常不再卡住按钮。
                        self._busy(False)
                        if process is not None:
                            for stream in (process.stdout, process.stdin):
                                try:
                                    stream.close()
                                except (OSError, ValueError):
                                    pass
                        if self.job in {"discover", "monitor", "login"} and self.session_status == "checking":
                            self._session("unknown", "核验未完成，后台任务失败，请重试" if value else "核验未完成，后台任务已结束，请重试")
                        self.status.set(self.session_message if self.session_status in {"invalid", "unknown"}
                                        else "任务已结束" if value == 0 else "任务失败 · 请查看日志并重试")
                        if self.closing and not self.training.busy():
                            reschedule = False
                            self.root.destroy()
                            return
                    elif value.startswith(SESSION_PREFIX):
                        data = json.loads(value[len(SESSION_PREFIX):])
                        self._session(data["status"], data["message"], data["checked_at"])
                        if data["status"] in {"invalid", "unknown"}:
                            self.status.set(data["message"])
                    elif value.startswith(GPU_PREFIX):
                        self.gpu_snapshot = json.loads(value[len(GPU_PREFIX):])
                        self._render_gpus()
                    elif value.startswith(DATA_PREFIX):
                        self._populate(json.loads(value[len(DATA_PREFIX):]))
                        self.status.set("服务器列表已刷新")
                    else:
                        tag = log_tag(value)
                        if value.startswith(REPORT_PREFIX):
                            report = json.loads(value[len(REPORT_PREFIX):])
                            tag = report["level"]
                            if tag not in {"normal", "success", "error"} or len(report["lines"]) != 2:
                                raise ValueError("监控消息格式无效")
                            value = "\n".join(report["lines"]) + "\n"
                        if value.startswith("WATCHER_STATUS "):
                            value = value.removeprefix("WATCHER_STATUS ")
                            self.status.set(value.strip())
                        if re.match(r"^\[\d{2}:\d{2}:\d{2}\] 监控", value):
                            self.status.set(value.splitlines()[0])  # 顶部摘要复用首行；整组颜色由采集结果决定，不从用户名猜测。
                            self.status_label.configure(foreground={"success": "#18733a", "error": "#b42318"}.get(tag, "#245cc4"))
                        self.log.configure(state="normal")
                        self.log.insert("end", value, tag)
                        if int(self.log.index("end-1c").split(".")[0]) > 2000:
                            self.log.delete("1.0", "201.0")
                        self.log.see("end")
                        self.log.configure(state="disabled")
                except Exception as exc:
                    self.status.set(f"后台消息处理失败：{exc}")
        finally:
            if reschedule:
                if self.closing and self.process is None and not self.training.busy():
                    self.root.destroy()
                    return
                if not self.closing:
                    self.training.tick()
                self._training_columns()
                self.training_pane.render()
                for _, _, pane in self.external_panes.values():
                    pane.render()
                try:
                    self._render_gpus()
                except (KeyError, TypeError, ValueError) as exc:
                    self.status.set(f"GPU 图表数据不可用：{exc}")
                self.root.after(100, self._drain)  # 坏消息不能中断后续退出通知和按钮恢复。

    def start(self):  # 每次监控使用当前设置启动新进程，状态与上一轮任务隔离。
        if self.process is not None or not self.save():
            return
        self.stop_file.unlink(missing_ok=True)  # 清除该窗口上一次停止留下的信号，否则新进程会立即退出。
        self._launch(self._command(), "monitor")
        if self.process is not None and self.training_pane.server_id:
            self.training_pane.start()

    def stop(self):  # 请求协作退出，不强杀正在操作浏览器或数据库的进程。
        if self.training_pane.server_id:
            self.training_pane.stop()
        if self.process is not None and self.job == "monitor":
            self.stop_file.touch()  # 核心在采集步骤之间及轮询等待期间检查文件是否存在。
            self.stop_button.configure(state="disabled")
            self.status.set("正在安全停止 · 等待当前网络请求完成并保存状态")

    def refresh(self):  # 独立任务读取当前账号的主机列表；不会发起实例开机。
        self._launch(worker_command("discover"), "discover")

    def login(self):  # 浏览器由登录模块打开，验证码仍由用户手动完成。
        self._launch(worker_command("login"), "login")

    def finish_login(self):  # 按钮代替控制台 Enter，让登录模块继续同步浏览器资料。
        if self.process is not None and self.job == "login" and self.process.poll() is None:
            try:
                self.process.stdin.write("\n")
                self.process.stdin.flush()  # 立即把换行发送给 input()，不能让它停留在 Python 缓冲区。
                self.login_done.configure(state="disabled")
                self.status.set("正在同步登录会话…")
            except (OSError, ValueError) as exc:
                self.status.set(f"登录同步失败：{exc}")

    def close(self):  # 空闲时立即关闭；任务运行中等待资源释放；登录中提示先完成同步。
        if self.process is not None and self.job == "login":
            messagebox.showinfo("登录尚未完成", "请先关闭登录浏览器并点击“已登录并关闭浏览器”。", parent=self.root)
        else:
            self.closing = True
            self.training.stop_all()
            self.stop()
            if self.process is None and not self.training.busy():
                self.root.destroy()
            else:
                self.status.set("正在等待后台任务安全结束后关闭窗口…")



def discovery_rows(config, hosts, instances):  # 只有账号真实实例UUID与入口/配置同时相符，才显示可开机资格。
    targets = {item.machine_name: item for item in config.auto_start.targets if item.enabled and item.instance_uuid}
    rows = []
    for host in hosts:
        for name, idle, total in host.source_slots:
            existing = [item for item in (instances or []) if item.get("machine_name") == name]
            target = targets.get(name)
            if instances is None:
                label = "未核验"
            elif not existing:
                label = "无实例"
            elif not target or not config.auto_start.enabled:
                label = "待配置"
            elif not any(item.get("instance_uuid") == target.instance_uuid for item in existing):
                label = "实例已变更"
            else:
                label = "可开机"
            rows.append((name, f"{idle}/{total}", label))
    return rows


def discover():  # 子进程模式：采集结果编码为一行 JSON，供父进程更新服务器表格。
    from .collectors import PlatformBrowserCollector, TelemetryApiCollector
    from .collectors.platform import PlatformAuthenticationError
    config = load_config(ROOT / "config.yaml")
    collector = PlatformBrowserCollector(config.platform)
    try:
        emit_session("checking", "正在核验真实主机接口")
        hosts = collector.collect()
        try:
            instances = collector.get_account_instances()
            if not isinstance(instances, list):
                raise ValueError("实例列表格式错误")
        except PlatformAuthenticationError:
            raise
        except Exception:
            instances = None
            print("容器实例查询失败，自动开机资格未核验；平台仍可只读监控。", flush=True)
        rows = discovery_rows(config, hosts, instances)
        print(DATA_PREFIX + json.dumps(rows, ensure_ascii=False), flush=True)
        emit_session("valid", "主机接口核验成功")
        if hosts:
            telemetry = TelemetryApiCollector(config.telemetry)
            try:
                emit_gpu_samples(telemetry.collect(), hosts, config.monitor.stale_after_seconds)
            except Exception:
                emit_gpu_samples([], hosts, config.monitor.stale_after_seconds, "GPU 数据采集失败；可重试刷新")
                print("GPU 数据采集失败，平台登录会话已核验有效。", flush=True)
            finally:
                telemetry._session.close()  # 一次性刷新不保留HTTP连接；图表失败不误报登录过期。

    except Exception as exc:
        if isinstance(exc, LocalStartupError):
            message = f"服务器刷新失败，本地启动错误：{exc}"
        elif isinstance(exc, PlatformAuthenticationError):
            message = f"登录已失效，请点击登录 / 更新会话：{exc}"
        else:
            message = f"服务器刷新失败，无法确认会话，请重试：{exc}"
        emit_session("invalid" if isinstance(exc, PlatformAuthenticationError) else "unknown", message)
        print(message, flush=True)
        raise SystemExit(1) from None  # 预期刷新错误不显示PyInstaller未处理异常堆栈。
    finally:
        try:
            collector.close()  # 刷新成功与否都释放浏览器上下文，供后续任务使用。
        except Exception as exc:
            print(f"刷新浏览器清理失败：{exc}", flush=True)


def main():  # 同一模块既可作为桌面入口，也可作为无界面的主机发现子进程。
    if "--discover" in sys.argv:
        discover()
        return
    parser = argparse.ArgumentParser(description="AutoDL GPU Watcher 图形界面")
    parser.add_argument("--debug", action="store_true", help="调试开发模式，默认只读且不切换无卡实例")
    args = parser.parse_args()
    root = tk.Tk()
    WatcherWindow(root, debug=args.debug)
    root.mainloop()  # Tk 处理用户输入、重绘和 after 回调，直到窗口被销毁。


if __name__ == "__main__":
    main()
