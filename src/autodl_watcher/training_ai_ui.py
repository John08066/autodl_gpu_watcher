from __future__ import annotations

from datetime import datetime
import queue
import threading
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from . import ai_credentials
from .training_ai import DEFAULTS, analyze, excerpt, load_settings, save_settings


class AIAssistant:
    def __init__(self, root, path):
        self.root, self.path = root, path
        self.lock = threading.Lock()

    def configure(self):
        return AISettings(self.root, self.path)

    def explain(self, card):
        return AnalysisWindow(self.root, self, card)


class AISettings(tk.Toplevel):
    def __init__(self, parent, path):
        super().__init__(parent)
        self.path = path
        self.title("AI 设置 · AutoDL 独立凭据")
        self.resizable(False, False)
        self.variables = {}
        error = ""
        try:
            config = load_settings(path)
        except (ValueError, OSError):
            config, error = DEFAULTS, "原配置无法读取，请重新填写并保存。"
        for row, (name, label) in enumerate((("base_url", "API 基础地址"), ("protocol", "接口协议"), ("model", "模型 ID"))):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=5)
            variable = self.variables[name] = tk.StringVar(value=config[name])
            if name == "protocol":
                field = ttk.Combobox(self, textvariable=variable, values=("responses", "chat"), state="readonly", width=51)
            else:
                field = ttk.Entry(self, textvariable=variable, width=54)
            field.grid(row=row, column=1, padx=8, pady=5)
        ttk.Label(self, text="API 密钥").grid(row=3, column=0, sticky="w", padx=8, pady=5)
        self.key = ttk.Entry(self, show="●", width=54)
        self.key.grid(row=3, column=1, padx=8, pady=5)
        ttk.Label(self, text="留空保留此地址的已存密钥；更换地址须单独配置。\n密钥存于 Windows 凭据管理器，保存后清空输入框；保存不代表 API 验证成功。\n仅点击“AI 解读”并发送片段时调用，不随监控自动调用。", wraplength=490).grid(row=4, column=0, columnspan=2, padx=8, pady=8, sticky="w")
        self.status = ttk.Label(self, text=error, wraplength=490)
        self.status.grid(row=5, column=0, columnspan=2, padx=8, sticky="w")
        bar = ttk.Frame(self)
        bar.grid(row=6, column=0, columnspan=2, sticky="e", padx=8, pady=8)
        ttk.Button(bar, text="删除此地址密钥", command=self.delete_key).pack(side="left", padx=4)
        ttk.Button(bar, text="保存", command=self.save).pack(side="left")
        if not error:
            self.show_credential()

    def show_credential(self):
        try:
            configured = bool(ai_credentials.read(self.variables["base_url"].get().strip().rstrip("/")))
            self.status.configure(text="此地址已保存密钥；未进行真实 API 验证。" if configured else "此地址未保存密钥。", foreground="#245c45")
        except ai_credentials.CredentialError as exc:
            self.status.configure(text=str(exc), foreground="#b42318")

    def save(self):
        try:
            config = save_settings(self.path, {name: var.get() for name, var in self.variables.items()}, self.key.get())
            self.key.delete(0, "end")
            self.variables["base_url"].set(config["base_url"])
            self.show_credential()
        except (ValueError, OSError, ai_credentials.CredentialError) as exc:
            self.status.configure(text=str(exc), foreground="#b42318")

    def delete_key(self):
        try:
            ai_credentials.delete(self.variables["base_url"].get().strip().rstrip("/"))
            self.key.delete(0, "end")
            self.show_credential()
        except ai_credentials.CredentialError as exc:
            self.status.configure(text=str(exc), foreground="#b42318")


class AnalysisWindow(tk.Toplevel):
    def __init__(self, parent, assistant, card):
        super().__init__(parent)
        self.assistant = assistant
        self.results = queue.Queue()
        self.timer = None
        self.title(f"{card['name']} · PID {card['pid']} · AI 辅助解读")
        self.geometry("800x630")
        self.config_snapshot = None
        try:
            self.config_snapshot = load_settings(assistant.path)
            target = self.config_snapshot["base_url"] + " · " + (self.config_snapshot["model"] or "尚未配置模型")
        except (ValueError, OSError):
            target = "AI 设置无法读取；请关闭此窗口，修正设置后重新打开。"
        ttk.Label(self, text="仅分析本次日志快照，可能解释错误；不会改变训练状态、卡片颜色或开机决策。", wraplength=760).pack(anchor="w", padx=8, pady=4)
        ttk.Label(self, text="发送目标：" + target, wraplength=760).pack(anchor="w", padx=8)
        ttk.Label(self, text="待发送片段（最多12000字符，可删改）；已遮蔽常见密钥字段，请确认剩余内容可外发。", wraplength=760).pack(anchor="w", padx=8, pady=4)
        self.source = ScrolledText(self, height=12, wrap="word", font=("Consolas", 9))
        self.source.pack(fill="both", expand=True, padx=8)
        self.source.insert("1.0", excerpt(card.get("details", "")))
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=5)
        self.send = ttk.Button(bar, text="发送片段并解析（会产生费用）", command=self.start)
        self.send.pack(side="left")
        ttk.Button(bar, text="AI 设置", command=assistant.configure).pack(side="right")
        self.status = ttk.Label(self, text="尚未调用 API；配置更改后请重新打开本窗口。", wraplength=760)
        self.status.pack(anchor="w", padx=8)
        self.output = ScrolledText(self, height=12, wrap="word", state="disabled", font=("Microsoft YaHei UI", 9))
        self.output.pack(fill="both", expand=True, padx=8, pady=8)
        self.bind("<Destroy>", self._destroyed)

    def start(self):
        if not self.config_snapshot:
            self.status.configure(text="请先配置 AI 接口并重新打开本窗口。")
            return
        source = excerpt(self.source.get("1.0", "end-1c"))
        if not source.strip():
            self.status.configure(text="没有可发送的日志片段。")
            return
        if not self.assistant.lock.acquire(blocking=False):
            self.status.configure(text="已有 AI 请求进行中，请等待结束；不会重复发送。")
            return
        self.source.delete("1.0", "end")
        self.source.insert("1.0", source)
        self.source.configure(state="disabled")
        self.send.configure(state="disabled")
        self.status.configure(text="正在读取 AI 响应；监控照常运行。关闭窗口不会撤回已发请求。", foreground="#245c45")
        self._output("")
        threading.Thread(target=self._run, args=(dict(self.config_snapshot), source), daemon=True).start()
        self.timer = self.after(100, self._poll)

    def _run(self, config, source):
        try:
            value = analyze(config, source)
            self.results.put((value, ""))
        except Exception as exc:
            from .training_ai import AnalysisError
            error = str(exc) if isinstance(exc, (AnalysisError, ai_credentials.CredentialError)) else "AI 解析失败；未自动重试，未改变监控结果。"
            self.results.put((None, error))
        finally:
            self.assistant.lock.release()

    def _poll(self):
        self.timer = None
        try:
            value, error = self.results.get_nowait()
        except queue.Empty:
            self.timer = self.after(100, self._poll)
            return
        self.send.configure(state="normal")
        self.source.configure(state="normal")
        if error:
            self.status.configure(text=error, foreground="#b42318")
            return
        self.status.configure(text=f"AI 辅助结果 · {value['model']} · {datetime.fromtimestamp(value['analyzed_at']):%H:%M:%S} · 仅供参考", foreground="#245c45")
        lines = [value["summary"]]
        for row in value["metrics"]:
            lines.extend(["", row["name"] + "：" + row["value"], "原文：" + row["evidence"]])
        self._output("\n".join(lines))

    def _output(self, text):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.configure(state="disabled")

    def _destroyed(self, event):
        if event.widget is self and self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
