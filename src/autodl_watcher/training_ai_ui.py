from __future__ import annotations

import queue
import sqlite3
import threading
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from . import ai_credentials
from .training_ai import DEFAULT_PROMPT, DEFAULTS, AnalysisError, analyze, excerpt, load_settings, save_settings, settings
from .training_rules import RuleError, RuleStore, task_identity
from .training_ai_usage import UsageStore, call_text, LUNA_PRICES, pricing_settings


class AIAssistant:
    """一个应用共享配置及请求队列，规则按服务器和进程身份独立缓存。"""
    def __init__(self, root, path, service):
        self.root, self.path, self.service = root, path, service
        self.store = RuleStore(path.with_name("training_rules.json"))
        self.usage = UsageStore(path.with_name("ai_usage.sqlite3"))
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

    def application_status(self, server_id, card):
        snapshot = self.service.states.get(server_id, {}).get("snapshot") or {}
        current = next((item for item in snapshot.get("cards", []) if item["key"] == card["key"]), None)
        if not current:
            return "主卡片暂无此任务样本；等待下次采样验证"
        suffix = "（上次采样，当前连接异常）" if snapshot.get("error") else ""
        return "主卡片：" + current.get("rule_status", "默认监控") + suffix

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
            value = {"call": exc.call} if getattr(exc, "call", None) else None
            error = str(exc) if isinstance(exc, (AnalysisError, RuleError, ai_credentials.CredentialError)) else "AI 规则生成失败"
        if value and value.get("call"):
            try:
                self.usage.record(value["call"])
            except (OSError, sqlite3.Error):
                value["usage_warning"] = "用量账本保存失败，本次用量仅随任务结果保存"
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


def profile_text(entry, application="主卡片尚未核验"):
    lines = [entry.get("message", "默认监控")]
    if entry.get("profile"):
        lines.append("规则：" + entry["profile"]["name"])
        lines.append("模型：" + entry.get("model", "—"))
        lines.append("请求等级：" + entry.get("requested_tier", "—") + "；实际等级：" + entry.get("service_tier", "未返回"))
        for rule in entry["profile"]["rules"]:
            names = ", ".join(rule.get("metrics", {})) or rule.get("metrics_path", "按进度字段")
            lines.append(f"{rule['phase']} · 指标 {names} · 进度 " + ", ".join(rule.get("fields", {})))
        lines.append(application)
        lines.append("后续按各服务器采样间隔解析新日志；不会逐轮调用AI。")
    lines.append(call_text(entry.get("call")))
    if entry.get("usage_warning"):
        lines.append(entry["usage_warning"])
    return "\n".join(lines)


class AISettings(tk.Toplevel):
    def __init__(self, parent, path, on_save=lambda: None):
        super().__init__(parent)
        self.path, self.on_save = path, on_save
        self.title("全局 AI 设置 · 所有服务器共用")
        self.geometry("720x650")
        self.minsize(650, 570)
        self.variables = {}
        error = ""
        try:
            config = load_settings(path)
        except (ValueError, OSError):
            config, error = DEFAULTS, "原配置无法读取，请重新填写并保存。"
        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True, padx=8, pady=8)
        connection, prompt, pricing, usage = (ttk.Frame(tabs, padding=8) for _ in range(4))
        for page, label in ((connection, "连接"), (prompt, "监控提示词"), (pricing, "计价设置"), (usage, "用量与消费")):
            tabs.add(page, text=label)
        connection.columnconfigure(1, weight=1)
        for row, (name, label) in enumerate((("base_url", "API 基础地址"), ("protocol", "接口协议"), ("model", "模型 ID"), ("service_tier", "服务等级"))):
            ttk.Label(connection, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=5)
            variable = self.variables[name] = tk.StringVar(value=config[name])
            if name in {"protocol", "service_tier"}:
                field = ttk.Combobox(connection, textvariable=variable, values=("responses", "chat") if name == "protocol" else ("flex", "default"), state="readonly")
            else:
                field = ttk.Entry(connection, textvariable=variable)
            field.grid(row=row, column=1, padx=8, pady=5, sticky="ew")
        ttk.Label(connection, text="API 密钥").grid(row=4, column=0, sticky="w", padx=8, pady=5)
        self.key = ttk.Entry(connection, show="●")
        self.key.grid(row=4, column=1, padx=8, pady=5, sticky="ew")
        self.auto = tk.BooleanVar(value=config.get("auto_generate", False))
        ttk.Checkbutton(connection, text="新训练自动生成监控规则（每任务一次，会发送日志并产生费用）", variable=self.auto).grid(row=5, column=0, columnspan=2, padx=8, pady=6, sticky="w")
        ttk.Label(connection, text="适用于 AutoDL、4090 和以后新增的所有服务器；任务规则分别保存。\nflex 为弹性等级，default 为 Standard；失败不自动提价重试。\n密钥独立存于 Windows 凭据管理器，留空保留此地址已存密钥。\n仅生成规则时调用AI；日常采样使用本地规则。保存不代表API验证成功。", wraplength=620).grid(row=6, column=0, columnspan=2, padx=8, pady=8, sticky="w")
        ttk.Button(connection, text="删除此地址密钥", command=self.delete_key).grid(row=7, column=1, sticky="e", padx=8)
        ttk.Label(prompt, text="所有服务器生成规则时共用。保存后用于后续请求；已有规则需手动更新。\n固定JSON格式契约由程序补充；这里配置指标与显示需求，不填写Shell命令。", wraplength=620).pack(anchor="w", pady=6)
        self.prompt = ScrolledText(prompt, height=12, wrap="word", font=("Microsoft YaHei UI", 10))
        self.prompt.pack(fill="both", expand=True)
        self.prompt.insert("1.0", config.get("prompt", DEFAULT_PROMPT))
        ttk.Button(prompt, text="恢复默认提示词（保存后生效）", command=self.reset_prompt).pack(anchor="e", pady=8)
        ttk.Label(pricing, text="Standard 单价：美元 / 百万Token；Flex 按下方系数折算。\n与当前模型及地址绑定；费用按调用时单价保存，修改不会重算历史。", wraplength=620).grid(row=0, column=0, columnspan=2, sticky="w", pady=8)
        prices = pricing_settings(config)
        self.price_scope = (prices["base_url"], prices["model"])
        self.prices = {}
        for row, (name, label) in enumerate((("input", "普通输入"), ("cached", "缓存命中"), ("write", "缓存写入"), ("output", "输出"), ("flex_multiplier", "Flex 系数"), ("usd_cny", "美元兑人民币估算汇率")), 1):
            ttk.Label(pricing, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=6)
            self.prices[name] = tk.StringVar(value="" if prices[name] is None else str(prices[name]))
            ttk.Entry(pricing, textvariable=self.prices[name], width=25).grid(row=row, column=1, padx=8, pady=6)
        ttk.Button(pricing, text="填入 gpt-6-luna 官方案例价", command=self.preset_prices).grid(row=7, column=0, columnspan=2, pady=8, sticky="w")
        ttk.Label(pricing, text="案例：2026-10-07 官方标准价，Flex 0.5倍，汇率7.0仅为估计。\n其它模型或第三方接口请填写自己的单价；留空则费用显示未知。\n缓存明细未返回时暂按0估算并标注，账单可能不同。", wraplength=620).grid(row=8, column=0, columnspan=2, sticky="w")
        self.usage = UsageStore(path.with_name("ai_usage.sqlite3"))
        self.usage_text = ScrolledText(usage, wrap="word", state="disabled", font=("Microsoft YaHei UI", 9))
        self.usage_text.pack(fill="both", expand=True)
        ttk.Button(usage, text="刷新用量", command=self.refresh_usage).pack(anchor="e", pady=8)
        tabs.bind("<<NotebookTabChanged>>", lambda event: self.refresh_usage() if tabs.select() == str(usage) else None)
        self.status = ttk.Label(self, text=error, wraplength=670)
        self.status.pack(fill="x", padx=16)
        ttk.Button(self, text="保存", command=self.save).pack(anchor="e", padx=16, pady=8)
        if not error:
            self.show_credential()

    def reset_prompt(self):
        self.prompt.delete("1.0", "end")
        self.prompt.insert("1.0", DEFAULT_PROMPT)

    def preset_prices(self):
        if self.variables["base_url"].get().strip().rstrip("/") != "https://api.openai.com/v1" or self.variables["model"].get().strip() != "gpt-6-luna":
            self.status.configure(text="此案例价只适用于官方 gpt-6-luna；当前模型/地址请填写自己的单价。", foreground="#b42318")
            return
        for name, value in LUNA_PRICES.items():
            self.prices[name].set(str(value))
        self.price_scope = ("https://api.openai.com/v1", "gpt-6-luna")

    def refresh_usage(self):
        try:
            text = self.usage.summary()
        except (OSError, ValueError, sqlite3.Error):
            text = "用量账本无法读取；费用未知。"
        self.usage_text.configure(state="normal")
        self.usage_text.delete("1.0", "end")
        self.usage_text.insert("1.0", text)
        self.usage_text.configure(state="disabled")

    def show_credential(self):
        try:
            configured = bool(ai_credentials.read(self.variables["base_url"].get().strip().rstrip("/")))
            self.status.configure(text="此地址已保存密钥；保存不代表API验证成功。" if configured else "此地址未保存密钥。", foreground="#245c45")
        except ai_credentials.CredentialError as exc:
            self.status.configure(text=str(exc), foreground="#b42318")

    def save(self):
        try:
            values = {name: var.get() for name, var in self.variables.items()}
            scope = (values["base_url"].strip().rstrip("/"), values["model"].strip())
            prices = {name: var.get() for name, var in self.prices.items()}
            # 切换模型/地址时不能静默沿用上一模型单价，留空或修改后再保存。
            if scope != self.price_scope and self.price_scope != ("", "") and any(prices.values()):
                self.price_scope = scope
                for var in self.prices.values():
                    var.set("")
                raise ValueError("模型或地址已变更，旧单价已清空；请填写新单价，或留空再次保存（费用未知）。")
            values.update(auto_generate=self.auto.get(), prompt=self.prompt.get("1.0", "end-1c"),
                          pricing={**prices, "base_url": scope[0], "model": scope[1]})
            config = save_settings(self.path, values, self.key.get())
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
        self._output(profile_text(assistant.entry(server_id, card), assistant.application_status(server_id, card)))
        self.bind("<Destroy>", self._destroyed)
        self.timer = self.after(1000, self._poll)

    def start(self):
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
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
        if self.identity and self.assistant.busy == self.identity:
            self.timer = self.after(100, self._poll)
            return
        entry = self.assistant.entry(self.server_id, self.card)
        self.send.configure(state="normal")
        self.source.configure(state="normal")
        application = self.assistant.application_status(self.server_id, self.card)
        self.status.configure(text=application, foreground="#245c45" if "专用规则已启用" in application else "#b42318")
        self._output(profile_text(entry, application))
        self.timer = self.after(1000, self._poll)

    def _output(self, text):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.configure(state="disabled")

    def _destroyed(self, event):
        if event.widget is self and self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
