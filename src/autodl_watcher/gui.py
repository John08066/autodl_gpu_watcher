from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__
from .config import load_config
from .main import positive_seconds

ROOT = Path(__file__).resolve().parents[2]
DATA_PREFIX = "WATCHER_HOSTS "


def monitor_command(config, entry, user, poll, usage, live, stop_file):  # 构造与 CLI 相同的监控命令。
    from .cli import normalize_host
    if not entry:
        raise ValueError("请先选择服务器入口")
    if not user.strip():
        raise ValueError("请填写占用列表中的本人用户名")
    poll, usage = positive_seconds(str(poll)), positive_seconds(str(usage))
    target = next((item for item in config.auto_start.targets
                   if item.machine_name == entry and item.enabled and item.instance_uuid), None)
    if live and (target is None or not config.auto_start.enabled):
        raise ValueError("此入口尚未配置启用的开机实例，请先只读监控或在 config.yaml 配置 targets")
    return [sys.executable, "-u", "-m", "autodl_watcher.main", "--config", str(ROOT / "config.yaml"),
            "--host", normalize_host(entry), "--entry", entry, "--user", user.strip(),
            "--poll-seconds", str(poll), "--usage-seconds", str(usage),
            "--live" if live else "--dry-run", "--no-login", "--stop-file", str(stop_file)]


class WatcherWindow:
    def __init__(self, root):
        self.root = root
        self.config = load_config(ROOT / "config.yaml")
        self.local = ROOT / ".ui"
        self.local.mkdir(exist_ok=True)
        self.stop_file = self.local / f"stop-{os.getpid()}"
        self.process = None
        self.job = ""
        self.events = queue.Queue()
        self.closing = False
        self.user = tk.StringVar(value=self.config.usage_tracking.self_user)
        self.poll = tk.StringVar(value=str(self.config.monitor.poll_seconds))
        self.usage = tk.StringVar(value=str(self.config.usage_tracking.interval_seconds))
        self.live = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="未运行 · 选择入口后开始监控")
        self.saved_entry = ""
        preferences = self.local / "preferences.json"
        if preferences.exists():
            try:
                saved = json.loads(preferences.read_text(encoding="utf-8"))
                self.user.set(saved.get("user", self.user.get()))
                self.poll.set(saved.get("poll", self.poll.get()))
                self.usage.set(saved.get("usage", self.usage.get()))
                self.saved_entry = saved.get("entry", "")
            except (ValueError, OSError, AttributeError):
                self.status.set("本地偏好读取失败，已使用默认配置")
        self._build()
        self._populate([(item.machine_name, "待刷新", "已配置")
                        for item in self.config.auto_start.targets if item.enabled])
        self.root.after(100, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build(self):
        self.root.title(f"AutoDL GPU Watcher · v{__version__}")
        self.root.geometry("1000x720")
        self.root.minsize(820, 580)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Treeview", rowheight=32, font=("Microsoft YaHei UI", 10))
        style.configure("TButton", padding=(12, 7))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="AutoDL GPU Watcher", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="选择服务器 · 配置监控 · 查看实时状态").pack(anchor="w", pady=(4, 12))
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=(0, 8))
        self.refresh_button = ttk.Button(toolbar, text="刷新服务器", command=self.refresh)
        self.refresh_button.pack(side="left")
        self.login_button = ttk.Button(toolbar, text="登录 / 更新会话", command=self.login)
        self.login_button.pack(side="left", padx=8)
        self.login_done = ttk.Button(toolbar, text="已登录并关闭浏览器", command=self.finish_login, state="disabled")
        self.login_done.pack(side="left")
        self.table = ttk.Treeview(frame, columns=("entry", "slots", "target"), show="headings", height=6, selectmode="browse")
        for column, title, width in [("entry", "服务器入口", 350), ("slots", "平台空闲 / 总 GPU ID", 240),
                                     ("target", "自动开机配置", 260)]:
            self.table.heading(column, text=title)
            self.table.column(column, width=width)
        self.table.pack(fill="x")
        self.table.bind("<Button-1>", lambda event: "break" if self.process is not None else None)
        self.table.bind("<Key>", lambda event: "break" if self.process is not None else None)
        ttk.Label(frame, text="列表显示刷新时的快照；持续监控结果见下方日志。未配置开机实例的入口仍可只读监控。").pack(anchor="w", pady=6)
        options = ttk.LabelFrame(frame, text="监控设置", padding=12)
        options.pack(fill="x", pady=8)
        self.inputs = []
        for col, (label, value) in enumerate([("本人用户名", self.user), ("采样间隔（秒）", self.poll),
                                             ("占用采集间隔（秒）", self.usage)]):
            ttk.Label(options, text=label).grid(row=0, column=col, sticky="w", padx=5)
            entry = ttk.Entry(options, textvariable=value, width=24)
            entry.grid(row=1, column=col, sticky="ew", padx=5, pady=5)
            options.columnconfigure(col, weight=1)
            self.inputs.append(entry)
        self.live_check = ttk.Checkbutton(options, text="启用真实自动开机（会产生 AutoDL 费用）", variable=self.live)
        self.live_check.grid(row=2, column=0, columnspan=3, sticky="w", padx=5, pady=5)
        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=8)
        self.start_button = ttk.Button(controls, text="开始监控", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止监控", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.save_button = ttk.Button(controls, text="保存设置", command=self.save)
        self.save_button.pack(side="left")
        self.export_button = ttk.Button(controls, text="导出占用报表", command=self.export)
        self.export_button.pack(side="right")
        ttk.Label(frame, textvariable=self.status, foreground="#245cc4").pack(anchor="w", pady=6)
        self.log = ScrolledText(frame, height=12, wrap="word", state="disabled", font=("Microsoft YaHei UI", 10),
                                background="#17202e", foreground="#e2e8f0", padx=10, pady=10)
        self.log.pack(fill="both", expand=True)

    def _populate(self, rows):
        selected = self.selected_entry() or self.saved_entry
        self.table.delete(*self.table.get_children())
        for entry, slots, target in rows:
            self.table.insert("", "end", iid=entry, values=(entry, slots, target))
        entries = self.table.get_children()
        if entries:
            self.table.selection_set(selected if selected in entries else entries[0])

    def selected_entry(self):
        selection = self.table.selection()
        return selection[0] if selection else ""

    def _command(self):
        return monitor_command(self.config, self.selected_entry(), self.user.get(), self.poll.get(),
                               self.usage.get(), self.live.get(), self.stop_file)

    def save(self):
        try:
            self._command()
            preferences = {"entry": self.selected_entry(), "user": self.user.get().strip(),
                           "poll": self.poll.get(), "usage": self.usage.get()}
            temp = self.local / "preferences.tmp"
            temp.write_text(json.dumps(preferences, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.local / "preferences.json")
            self.status.set("设置已保存 · 下次启动仍默认只读模式")
            return True
        except (ValueError, argparse.ArgumentTypeError, OSError) as exc:
            messagebox.showerror("设置无效", str(exc), parent=self.root)
            return False

    def _busy(self, value):
        for button in [self.refresh_button, self.login_button, self.start_button, self.save_button,
                       self.export_button, self.live_check, *self.inputs]:
            button.configure(state="disabled" if value else "normal")
        self.stop_button.configure(state="normal" if value and self.job == "monitor" else "disabled")
        self.login_done.configure(state="normal" if value and self.job == "login" else "disabled")

    def _launch(self, command, job):
        if self.process is not None:
            return
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUTF8"] = "1"
        try:
            self.process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as exc:
            messagebox.showerror("启动失败", str(exc), parent=self.root)
            return
        self.job = job
        self._busy(True)
        self.status.set({"monitor": "监控进程已启动 · 等待首次采集", "discover": "正在读取实际服务器列表…",
                         "login": "请在 Edge 登录并关闭窗口，然后点击“已登录并关闭浏览器”",
                         "export": "正在导出报表…"}[job])
        threading.Thread(target=self._read, args=(self.process,), daemon=True).start()

    def _read(self, process):  # 子线程只传递消息，所有控件更新留在 Tk 主线程。
        for line in process.stdout:
            self.events.put(("line", line))
        self.events.put(("exit", process.wait()))

    def _drain(self):
        for _ in range(200):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "exit":
                self.process.stdout.close()
                self.process.stdin.close()
                self.process = None
                self._busy(False)
                self.status.set("任务已结束" if value == 0 else "任务失败 · 请查看日志；登录失效时点击登录")
                if self.closing:
                    self.root.destroy()
                    return
            elif value.startswith(DATA_PREFIX):
                self._populate(json.loads(value[len(DATA_PREFIX):]))
                self.status.set("服务器列表已刷新")
            else:
                if value.startswith("WATCHER_STATUS "):
                    value = value.removeprefix("WATCHER_STATUS ")
                    self.status.set(value.strip())
                self.log.configure(state="normal")
                self.log.insert("end", value)
                if int(self.log.index("end-1c").split(".")[0]) > 2000:
                    self.log.delete("1.0", "201.0")
                self.log.see("end")
                self.log.configure(state="disabled")
        self.root.after(100, self._drain)

    def start(self):
        if self.process is not None or not self.save():
            return
        self.stop_file.unlink(missing_ok=True)
        self._launch(self._command(), "monitor")

    def stop(self):
        if self.process is not None and self.job == "monitor":
            self.stop_file.touch()
            self.stop_button.configure(state="disabled")
            self.status.set("正在安全停止 · 等待当前网络请求完成并保存状态")

    def refresh(self):
        self._launch([sys.executable, "-u", "-m", "autodl_watcher.gui", "--discover"], "discover")

    def login(self):
        self._launch([sys.executable, "-u", "-m", "autodl_watcher.login"], "login")

    def finish_login(self):
        if self.process is not None and self.job == "login" and self.process.poll() is None:
            try:
                self.process.stdin.write("\n")
                self.process.stdin.flush()
                self.login_done.configure(state="disabled")
                self.status.set("正在同步登录会话…")
            except (OSError, ValueError) as exc:
                self.status.set(f"登录同步失败：{exc}")

    def export(self):
        self._launch([sys.executable, "-u", "-m", "autodl_watcher.usage_report"], "export")

    def close(self):
        if self.process is None:
            self.root.destroy()
        elif self.job == "login":
            messagebox.showinfo("登录尚未完成", "请先关闭登录浏览器并点击“已登录并关闭浏览器”。", parent=self.root)
        else:
            self.closing = True
            self.stop()
            self.status.set("正在等待后台任务安全结束后关闭窗口…")


def discover():
    from .collectors import PlatformBrowserCollector
    config = load_config(ROOT / "config.yaml")
    collector = PlatformBrowserCollector(config.platform)
    try:
        configured = {item.machine_name for item in config.auto_start.targets if item.enabled and item.instance_uuid}
        rows = [(name, f"{idle}/{total}", "已配置" if name in configured else "只读监控")
                for host in collector.collect() for name, idle, total in host.source_slots]
        print(DATA_PREFIX + json.dumps(rows, ensure_ascii=False), flush=True)
    finally:
        collector.close()


def main():
    if "--discover" in sys.argv:
        discover()
        return
    root = tk.Tk()
    WatcherWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
