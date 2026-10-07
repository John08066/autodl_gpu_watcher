from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from . import ai_credentials
from .training_ai import DEFAULTS, AnalysisError, analyze, excerpt, load_settings, save_settings, settings
from .training_rules import RuleError, RuleStore, task_identity


class AIAssistant:
    """一个应用共享配置及请求队列，规则按服务器和进程身份独立缓存。"""
    def __init__(self, root, path, service):
        self.root, self.path, self.service = root, path, service
        self.store = RuleStore(path.with_name("training_rules.json"))
        self.results = queue.Queue()
        self.busy = None
        self.revision = 0
        self.reload()

    def reload(self):
        try:
            self.config = load_settings(self.path)
        except (OSError, ValueError):
            self.config = {**DEFAULTS, "auto_generate": False}
        self.revision += 1

    def configure(self):
        return AISettings(self.root, self.path, self.reload)

    def explain(self, server_id, card):
        return AnalysisWindow(self.root, self, server_id, card)

    def entry(self, server_id, card):
        server = self.service.states[server_id]["server"]
        return self.store.entries.get(task_identity(server, card), {})

    def generate(self, server_id, card, source, config=None):
        if self.busy:
            raise AnalysisError("已有 AI 请求进行中；继续默认监控，不会重复发送")
        config = settings(config or self.config)
        source = excerpt(source)
        if not source.strip() or not card.get("log_path"):
            raise AnalysisError("没有可读日志；继续默认监控")
        server = self.service.states[server_id]["server"]
        identity = task_identity(server, card)
        # 请求前落盘，防止断流/重启后把同一个付费请求再次自动发送。
        entry = {"message": "默认监控 · 正在生成专用规则", "attempted": True, "log_path": card["log_path"]}
        self.store.put(identity, entry)
        self.busy = identity
        self.revision += 1
        self.service.reparse(server_id)
        threading.Thread(target=self._run, args=(identity, server_id, card["pid"], config, source), daemon=True).start()
        return identity

    def _run(self, identity, server_id, pid, config, source):
        try:
            value, error = analyze(config, source, pid), ""
        except Exception as exc:
            value = None
            error = str(exc) if isinstance(exc, (AnalysisError, RuleError, ai_credentials.CredentialError)) else "AI 规则生成失败"
        self.results.put((identity, server_id, value, error))

    def poll(self):
        try:
            identity, server_id, value, error = self.results.get_nowait()
        except queue.Empty:
            return
        self.busy = None
        entry = dict(self.store.entries[identity])
        entry["message"] = "默认监控 · " + error if error else "专用规则已生成"
        if value:
            entry.update(value)
        try:
            self.store.put(identity, entry)
        except OSError:
            entry = {"attempted": True, "message": "默认监控 · 规则保存失败，请检查本地目录"}
            self.store.entries[identity] = entry
        self.revision += 1
        self.service.reparse(server_id)  # 仅重解析最近样本；不发SSH，不伪造新的采集时间。

    def tick(self):
        self.poll()
        if self.busy or not self.config.get("auto_generate") or self.store.error or not self.config.get("model"):
            return
        for server_id, state in self.service.states.items():
            snapshot = state.get("snapshot")
            if not state["active"] or not snapshot or snapshot.get("error"):
                continue
            for card in snapshot.get("cards", []):
                if not card.get("alive") or not card.get("details") or self.entry(server_id, card):
                    continue
                try:
                    self.generate(server_id, card, card["details"])
                except (ValueError, OSError) as exc:
                    self.store.error = "默认监控 · 自动生成已暂停：" + str(exc)
                return


def profile_text(entry):
    lines = [entry.get("message", "默认监控")]
    if entry.get("profile"):
        lines.append("规则：" + entry["profile"]["name"])
        lines.append("模型：" + entry.get("model", "—"))
        lines.append("请求等级：" + entry.get("requested_tier", "—") + "；实际等级：" + entry.get("service_tier", "未返回"))
        for rule in entry["profile"]["rules"]:
            names = ", ".join(rule.get("metrics", {})) or rule.get("metrics_path", "按进度字段")
            lines.append(f"{rule['phase']} · 指标 {names} · 进度 " + ", ".join(rule.get("fields", {})))
        lines.append("已用于主卡片，后续每轮从新日志提取；不再逐轮调用AI。")
    return "\n".join(lines)


class AISettings(tk.Toplevel):
    def __init__(self, parent, path, on_save=lambda: None):
        super().__init__(parent)
        self.path, self.on_save = path, on_save
        self.title("全局 AI 设置 · 所有服务器共用")
        self.resizable(False, False)
        self.variables = {}
        error = ""
        try:
            config = load_settings(path)
        except (ValueError, OSError):
            config, error = DEFAULTS, "原配置无法读取，请重新填写并保存。"
        for row, (name, label) in enumerate((("base_url", "API 基础地址"), ("protocol", "接口协议"), ("model", "模型 ID"), ("service_tier", "服务等级"))):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=5)
            variable = self.variables[name] = tk.StringVar(value=config[name])
            if name in {"protocol", "service_tier"}:
                field = ttk.Combobox(self, textvariable=variable, values=("responses", "chat") if name == "protocol" else ("flex", "default"), state="readonly", width=51)
            else:
                field = ttk.Entry(self, textvariable=variable, width=54)
            field.grid(row=row, column=1, padx=8, pady=5)
        ttk.Label(self, text="API 密钥").grid(row=4, column=0, sticky="w", padx=8, pady=5)
        self.key = ttk.Entry(self, show="●", width=54)
        self.key.grid(row=4, column=1, padx=8, pady=5)
        self.auto = tk.BooleanVar(value=config.get("auto_generate", False))
        ttk.Checkbutton(self, text="新训练自动生成监控规则（每任务一次，会发送日志并产生费用）", variable=self.auto).grid(row=5, column=0, columnspan=2, padx=8, pady=6, sticky="w")
        ttk.Label(self, text="适用于 AutoDL、4090 和以后新增的所有服务器；任务规则分别保存。\nflex 为弹性等级，default 为 Standard；失败后默认监控，不自动提价重试。\n密钥独立存于 Windows 凭据管理器，留空保留此地址已存密钥。\n仅生成规则时调用AI；每60秒刷新使用本地规则。保存不代表API验证成功。", wraplength=530).grid(row=6, column=0, columnspan=2, padx=8, pady=8, sticky="w")
        self.status = ttk.Label(self, text=error, wraplength=530)
        self.status.grid(row=7, column=0, columnspan=2, padx=8, sticky="w")
        bar = ttk.Frame(self)
        bar.grid(row=8, column=0, columnspan=2, sticky="e", padx=8, pady=8)
        ttk.Button(bar, text="删除此地址密钥", command=self.delete_key).pack(side="left", padx=4)
        ttk.Button(bar, text="保存", command=self.save).pack(side="left")
        if not error:
            self.show_credential()

    def show_credential(self):
        try:
            configured = bool(ai_credentials.read(self.variables["base_url"].get().strip().rstrip("/")))
            self.status.configure(text="此地址已保存密钥；保存不代表API验证成功。" if configured else "此地址未保存密钥。", foreground="#245c45")
        except ai_credentials.CredentialError as exc:
            self.status.configure(text=str(exc), foreground="#b42318")

    def save(self):
        try:
            values = {name: var.get() for name, var in self.variables.items()}
            config = save_settings(self.path, {**values, "auto_generate": self.auto.get()}, self.key.get())
            self.key.delete(0, "end")
            self.variables["base_url"].set(config["base_url"])
            self.on_save()
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
    def __init__(self, parent, assistant, server_id, card):
        super().__init__(parent)
        self.assistant, self.server_id, self.card = assistant, server_id, card
        self.timer, self.identity = None, None
        self.title(f"{card['name']} · PID {card['pid']} · 生成监控规则")
        self.geometry("800x630")
        self.config_snapshot = dict(assistant.config)
        target = self.config_snapshot["base_url"] + " · " + (self.config_snapshot["model"] or "尚未配置模型") + " · " + self.config_snapshot["service_tier"]
        ttk.Label(self, text="规则验证通过后直接更新本任务卡片，后续采样复用规则；日志含义仍可能判断错误。", wraplength=760).pack(anchor="w", padx=8, pady=4)
        ttk.Label(self, text="全局接口：" + target, wraplength=760).pack(anchor="w", padx=8)
        ttk.Label(self, text="待发送片段（最多12000字符，可删改）；已遮蔽常见密钥字段，请确认剩余内容可外发。", wraplength=760).pack(anchor="w", padx=8, pady=4)
        self.source = ScrolledText(self, height=12, wrap="word", font=("Consolas", 9))
        self.source.pack(fill="both", expand=True, padx=8)
        self.source.insert("1.0", excerpt(card.get("details", "")))
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=5)
        self.send = ttk.Button(bar, text="生成并应用规则（会产生费用）", command=self.start)
        self.send.pack(side="left")
        self.status = ttk.Label(self, text="使用全局配置；更改设置后请重新打开本窗口。", wraplength=760)
        self.status.pack(anchor="w", padx=8)
        self.output = ScrolledText(self, height=12, wrap="word", state="disabled", font=("Microsoft YaHei UI", 9))
        self.output.pack(fill="both", expand=True, padx=8, pady=8)
        self._output(profile_text(assistant.entry(server_id, card)))
        self.bind("<Destroy>", self._destroyed)

    def start(self):
        try:
            self.identity = self.assistant.generate(self.server_id, self.card, self.source.get("1.0", "end-1c"), self.config_snapshot)
        except (ValueError, OSError) as exc:
            self.status.configure(text=str(exc), foreground="#b42318")
            return
        self.source.configure(state="disabled")
        self.send.configure(state="disabled")
        minutes = 15 if self.config_snapshot["service_tier"] == "flex" else 3
        self.status.configure(text=f"正在生成；读取超时{minutes}分钟，普通监控继续。关闭窗口不会撤回请求。", foreground="#245c45")
        self._output("")
        self.timer = self.after(100, self._poll)

    def _poll(self):
        self.timer = None
        self.assistant.poll()
        if self.assistant.busy == self.identity:
            self.timer = self.after(100, self._poll)
            return
        entry = self.assistant.store.entries.get(self.identity, {})
        self.send.configure(state="normal")
        self.source.configure(state="normal")
        self.status.configure(text=entry.get("message", "默认监控"), foreground="#245c45" if entry.get("profile") else "#b42318")
        self._output(profile_text(entry))

    def _output(self, text):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.configure(state="disabled")

    def _destroyed(self, event):
        if event.widget is self and self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
