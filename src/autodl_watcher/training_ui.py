from __future__ import annotations

from datetime import datetime
import threading
import time
import unicodedata
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from .training import TrainingTracker, TrainingConnection


class TrainingService:  # 每台服务器最多一个只读请求；线程只投递结果，不接触Tk控件。
    def __init__(self, events):
        self.events = events
        self.states = {}

    def register(self, server):
        state = self.states.get(server.id)
        if state and (state["active"] or state["busy"]):
            state["pending"] = server if server != state["server"] else None  # 只保存下次使用的连接，本轮SSH请求继续使用原配置。
            return
        if state:
            state["connection"].close()
        self.states[server.id] = dict(server=server, tracker=TrainingTracker(server, lambda task: self.rule_entry(server, task)), active=False, busy=False,
                                      generation=(state["generation"] + 1 if state else 0), due=0, interval=60, snapshot=None, pending=None,
                                      connection=TrainingConnection(server), failures=0)

    def rule_entry(self, server, task):
        if getattr(self, "ai", None):
            from .training_rules import task_identity
            identity = task_identity(server, task)
            entry = self.ai.store.entries.get(identity)
            if not entry and self.ai.store.error:
                return {"message": "默认监控 · " + self.ai.store.error}
            if entry and "正在生成" in entry.get("message", "") and self.ai.busy != identity:
                return dict(entry, message="默认监控 · 上次规则请求未完成，可手动重试")
            return entry
        return None

    def reparse(self, server_id):
        state = self.states.get(server_id)
        if state and state.get("snapshot"):
            old = state["snapshot"]
            state["snapshot"] = dict(state["tracker"].update(old), received_at=old["received_at"], error=old.get("error", ""))

    def start(self, server_id, interval):
        state = self.states[server_id]
        if state["busy"] and not state["active"]:
            raise ValueError("前一次采集尚未结束，请稍后再开始")
        if state["active"]:
            return
        if state.get("pending"):
            self.register(state["pending"])  # 已结束旧请求后才换连接、清空旧来源的快照。
            state = self.states[server_id]
        if any(other["active"] and other["server"].ssh_alias.casefold() == state["server"].ssh_alias.casefold()
               for key, other in self.states.items() if key != server_id):
            raise ValueError("此SSH连接已在另一服务器页面监控，请先停止那一页")
        if state["connection"].closed:
            state["connection"] = TrainingConnection(state["server"])
        state.update(active=True, interval=interval, due=state["due"] if state["failures"] else 0)

    def stop(self, server_id):
        state = self.states[server_id]
        state.update(active=False, generation=state["generation"] + 1)
        state["connection"].close()

    def tick(self):
        if getattr(self, "ai", None):
            self.ai.tick()
        now = time.monotonic()
        for server_id, state in self.states.items():
            if state["active"] and not state["busy"] and now >= state["due"]:
                state.update(busy=True, due=now + state["interval"])
                threading.Thread(target=self._collect, args=(server_id, state["generation"], state["tracker"].previous(), state["connection"]), daemon=True).start()

    def _collect(self, server_id, generation, previous, connection):
        try:
            result, error = connection.collect(previous), ""
        except Exception as exc:
            result, error = None, f"{type(exc).__name__}：{exc}"
        self.events.put(("training", (server_id, generation, result, error)))

    def receive(self, value):
        server_id, generation, result, error = value
        state = self.states[server_id]
        state["busy"] = False
        if generation != state["generation"]:
            return  # 停止或改配置后的旧响应不能把界面重新变绿。
        if not error:
            try:
                state["snapshot"] = state["tracker"].update(result)
                state.update(failures=0, due=time.monotonic() + state["interval"])
                return
            except Exception as exc:
                error = f"训练信息解析失败：{exc}"
        state["failures"] += 1
        delay = max(state["interval"], min(300, 60 * 2 ** min(state["failures"] - 1, 3)))
        state["due"] = time.monotonic() + delay  # 单次失败只退避，不在采集线程内重拨。
        old = state["snapshot"] or {"cards": [], "gpus": []}
        state["snapshot"] = dict(old, error=f"{error}；{delay:g}秒后重试", received_at=time.time())  # 断连保留最近摘要并标黄，不能变成“无训练”。

    def busy(self):
        return any(state["busy"] for state in self.states.values())

    def stop_all(self):
        for key in self.states:
            self.stop(key)


COLORS = {"success": ("#10372a", "#8bf0ad"), "error": ("#481f26", "#ff9ca8"),
          "warning": ("#40361f", "#ffe09a"), "idle": ("#27313e", "#f2f4f7")}


def metrics_text(summary):
    names = {"loss": "Loss", "acc": "ACC", "auc": "AUC", "video_auc": "视频AUC", "eer": "EER", "ap": "AP", "ce": "CE"}
    metrics = summary.get("metrics", {})
    parts = []
    for name, value in metrics.items():
        metric, separator, branch = name.partition("/")
        number = f"{value:.4g}" if 0 < abs(value) < 0.0001 else f"{value:.4f}"
        parts.append(f"{names.get(metric, metric)}{separator}{branch} {number}")
    return " · ".join(parts) or "—"



def _column(value, width):  # 中文姓名按显示宽度补空格，黑框列对齐且不截断原始采集数据。
    result, used = "", 0
    for char in str(value).replace("\n", " ").replace("\r", " "):
        size = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + size > width:
            break
        result, used = result + char, used + size
    return result + " " * (width - used)


def gpu_table(snapshot, name):
    stamp = datetime.fromtimestamp(snapshot["received_at"]).strftime("%H:%M:%S")
    lines = [f"===== {stamp} | {name} | 显存：整卡 / 进程分列 =====",
             "GPU   VRAM整卡(GiB)  UTIL  TEMP  USER                 PID       进程GiB  PROCESS / CONFIG"]
    for gpu in snapshot.get("gpus", []):
        rows = [row for row in snapshot.get("gpu_processes", []) if row.get("gpu_index") == gpu["index"]]
        for row in rows or [{}]:
            total = f"{gpu['memory_used']/1024:.2f}/{gpu['memory_total']/1024:.2f}"
            memory = row.get("memory_used")
            process = row.get("process", "未读到GPU进程")
            if row.get("config"):
                process += " [" + row["config"] + "]"
            lines.append(_column(("容器" if snapshot.get("container") else "GPU") + str(gpu["index"]), 6) + _column(total, 15) + _column(f"{gpu['util']:.0f}%", 6)
                         + _column(f"{gpu['temperature']:.0f}°C", 6) + _column(row.get("user", "—"), 21)
                         + _column(row.get("pid", "—"), 10) + _column(f"{memory/1024:.2f}" if memory is not None else "—", 9) + process)
    if snapshot.get("gpu_error"):
        lines.append(snapshot["gpu_error"])
    if snapshot.get("container"):
        lines.append("容器编号不等于平台INDEX；宿主PID与用户归属未确认。")
    if not snapshot.get("gpus"):
        lines.append("GPU数据不可用")
    return "\n".join(lines) + "\n"


def task_memory(card):
    value = card.get("gpu_memory_mb")
    if not card.get("alive"):
        return "显存 —（已退出）"
    if value is None:
        peak = card.get("progress", {}).get("peak_memory_bytes")
        return f"显存峰值 {peak/1024**3:.2f} GiB（日志）" if peak is not None else "显存 无法核验"
    return f"显存 {value/1024:.2f} GiB"

class TrainingPane(ttk.Frame):
    def __init__(self, parent, service, configure_server, interval, show_gpus=True):
        super().__init__(parent)
        self.service, self.configure_server, self.interval = service, configure_server, interval
        self.show_gpus, self.server_id = show_gpus, None
        self.render_key = None
        self.gpu_log_key = None
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 5))
        self.start_button = ttk.Button(bar, text="开始训练监控", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(bar, text="停止", command=self.stop)
        self.stop_button.pack(side="left", padx=4)
        self.configure_button = ttk.Button(bar, text="设置连接", command=configure_server)
        self.configure_button.pack(side="right")
        self.summary = ttk.Label(self, text="尚未配置训练连接", wraplength=560)
        self.summary.pack(fill="x", pady=(0, 5))
        if show_gpus:
            output = ttk.Frame(self)
            output.pack(fill="x", pady=(0, 5))
            horizontal = ttk.Scrollbar(output, orient="horizontal")
            horizontal.pack(side="bottom", fill="x")
            self.gpu_log = ScrolledText(output, height=5, width=1, wrap="none", state="disabled", font=("Consolas", 9),
                                       background="#0e151d", foreground="#8bf0ad", xscrollcommand=horizontal.set, padx=5, pady=3)
            self.gpu_log.pack(fill="x")
            horizontal.configure(command=self.gpu_log.xview)
            self.gpu_log.tag_configure("error", foreground="#ff9ca8")
            self.gpu_log.tag_configure("warning", foreground="#ffe09a")
        self.canvas = tk.Canvas(self, background="#17202e", highlightthickness=0, width=300, height=180)
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.content = tk.Frame(self.canvas, background="#17202e")
        self.content_window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._resize)
        self.canvas.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-int(event.delta / 120), "units"))

    def _resize(self, event):
        self.canvas.itemconfigure(self.content_window, width=event.width)
        self.summary.configure(wraplength=max(200, event.width))
        for frame in self.content.winfo_children():
            for child in frame.winfo_children():
                if isinstance(child, tk.Label):
                    child.configure(wraplength=max(160, event.width - 32))

    def select(self, server_id):
        if self.server_id != server_id:
            self.server_id, self.render_key, self.gpu_log_key = server_id, None, None
            if self.show_gpus:
                self.gpu_log.configure(state="normal")
                self.gpu_log.delete("1.0", "end")
                self.gpu_log.configure(state="disabled")
            self.canvas.yview_moveto(0)
        self.render()

    def start(self):
        if self.server_id is None:
            self.configure_server()
            return
        try:
            from .main import positive_seconds
            self.service.start(self.server_id, positive_seconds(str(self.interval())))
            self.service.tick()
            self.render()
        except Exception as exc:
            self.summary.configure(text=str(exc), foreground="#b42318")

    def stop(self):
        if self.server_id is not None:
            self.service.stop(self.server_id)
            self.render()

    def render(self):
        state = self.service.states.get(self.server_id)
        snapshot = state["snapshot"] if state else None
        expired = bool(snapshot and time.time() - snapshot["received_at"] > max(90, state["interval"] * 2))
        key = (self.server_id, state["active"] if state else False, state["busy"] if state else False,
               snapshot["received_at"] if snapshot else None, expired, state.get("pending") if state else None,
               state["generation"] if state else None, getattr(self, "show_history", False),
               self.service.ai.revision if getattr(self.service, "ai", None) else 0)
        if key == self.render_key:
            return
        self.render_key = key
        self.start_button.configure(state="disabled" if state and (state["active"] or state["busy"]) else "normal")
        self.stop_button.configure(state="normal" if state and state["active"] else "disabled")
        warning = (snapshot.get("error", "") if snapshot else "") or ("采样已过期" if expired else "")
        if not state:
            text = "此入口尚未配置SSH训练连接；平台监控仍可使用。"
        elif not state["active"]:
            text = "训练采集已停止，下面保留最近结果。" if snapshot else "尚未开始训练监控（只读）"
        elif warning:
            text = warning
        elif snapshot:
            text = f"{state['server'].name} · SSH用户 {snapshot.get('user', '—')} · 最近采集 {datetime.fromtimestamp(snapshot['received_at']):%H:%M:%S}"
        else:
            text = "正在连接，读取本人训练进程与日志…"
        if state and state["active"]:
            connection = state["connection"]
            text += f" · 采集{connection.samples}次 / 连接尝试{connection.attempts}次"
        if state and state.get("pending"):
            text += " · 新连接已保存，下次启动训练监控生效"
        self.summary.configure(text=text, foreground="#9a6700" if warning else "#245c45")
        position = self.canvas.yview()[0]
        for child in self.content.winfo_children():
            child.destroy()
        if self.show_gpus and snapshot:
            log_key = (snapshot["received_at"], warning)
            if log_key != self.gpu_log_key:
                self.gpu_log_key = log_key
                text = f"[{datetime.now():%H:%M:%S}] {warning}；上次数据不作为当前状态。\n" if warning else gpu_table(snapshot, state["server"].name)
                self.gpu_log.configure(state="normal")
                self.gpu_log.insert("end", text, "warning" if warning else "normal")
                if int(self.gpu_log.index("end-1c").split(".")[0]) > 1000:
                    self.gpu_log.delete("1.0", "201.0")
                self.gpu_log.see("end")
                self.gpu_log.configure(state="disabled")
        cards = snapshot.get("cards", []) if snapshot else []
        if not cards:
            frame = self._card("warning" if warning else "idle")
            self._label(frame, "无法确认训练状态" if warning else "无匹配训练进程" if snapshot and state["active"] else "等待训练采集", bold=True)
            if snapshot and state["active"] and not warning:
                self._label(frame, f"脚本匹配：{state['server'].scripts} · 目录：{state['server'].project or '当前SSH用户'}；可在设置连接中修改。")
        history_count = sum(bool(card.get("history")) for card in cards)
        if history_count:
            def toggle_history():
                self.show_history = not getattr(self, "show_history", False)
                self.render()
            ttk.Button(self.content, text=("收起" if getattr(self, "show_history", False) else "查看") + f"历史运行（{history_count}）", command=toggle_history).pack(anchor="e", padx=6)
        for card in cards:
            if card.get("history") and not getattr(self, "show_history", False):
                continue
            level = "error" if card["level"] == "error" else "warning" if warning or not state["active"] else card["level"]
            frame = self._card(level)
            when = datetime.fromtimestamp(card["advanced_at"]).strftime("%m-%d %H:%M:%S")
            suffix = " · 最近结果" if warning or not state["active"] else ""
            self._label(frame, f"{card['name']} · {card['status']} · 最近进展 {when}{suffix}", bold=True, color=COLORS[level][1])
            gpu = ",".join(f"#{index}" for index in card.get("gpu_indices", [])) or "待确认"
            self._label(frame, f"PID {card['pid']} · 用户 {card.get('user', '—')} · GPU {gpu} · {task_memory(card)}"
                        + (" · 上次采样" if warning or not state["active"] else ""))
            if card.get("tmux_session"):
                self._label(frame, f"tmux：{card['tmux_session']}")
            self._label(frame, card.get("rule_status", "默认监控"), color=COLORS["warning"][1] if "不适用" in card.get("rule_status", "") else "#c0d6cd")
            progress = card["progress"]
            epoch, total = progress.get("epoch"), progress.get("total_epochs")
            raw = f" · 日志Epoch {progress['raw_epoch']}" if progress.get("raw_epoch") is not None else ""
            self._label(frame, f"轮次 {epoch if epoch is not None else '—'}/{total if total is not None else '—'}{raw} · 全局Step {progress.get('step') if progress.get('step') is not None else '—'}" + (f"/{progress['total_steps']}" if progress.get('total_steps') else ""))
            if epoch is not None and total and total > 0:
                ttk.Progressbar(frame, maximum=total, value=total if progress.get("completed") else min(total, max(0, epoch - 1)), style="Error.Horizontal.TProgressbar" if level == "error" else "Memory.Horizontal.TProgressbar").pack(fill="x", padx=8, pady=3)  # 条形仅表示此前已完成轮次，不猜当前轮内进度。
            train = progress.get("train", {})
            self._label(frame, f"训练（第{train.get('epoch') if train.get('epoch') is not None else '—'}轮，Step {train.get('step') if train.get('step') is not None else '—'}）：{metrics_text(train)}")
            for dataset, summary in progress.get("tests", {}).items():
                self._label(frame, f"测试 {dataset}（第{summary.get('epoch') if summary.get('epoch') is not None else '—'}轮，Step {summary.get('step') if summary.get('step') is not None else '—'}）：{metrics_text(summary)}")
            if progress.get("observations"):
                self._label(frame, "指标（阶段未注明）：" + metrics_text(progress["observations"]))
            if progress.get("phase") == "unknown":
                self._label(frame, "原文摘要（未解释）：\n" + card.get("preview", "没有可读输出"))
            if progress.get("error"):
                self._label(frame, progress["error"], color=COLORS["error"][1])
            actions = ttk.Frame(frame)
            actions.pack(anchor="e", padx=8, pady=(2, 6))
            ttk.Button(actions, text="训练原日志", command=lambda item=card: self.details(item)).pack(side="right")
            if getattr(self.service, "ai", None):
                ttk.Button(actions, text="生成 / 更新监控规则", command=lambda item=card: self.service.ai.explain(self.server_id, item)).pack(side="right", padx=4)

        self.content.update_idletasks()
        self.canvas.yview_moveto(position)

    def _card(self, level):
        frame = tk.Frame(self.content, background=COLORS[level][0], highlightbackground=COLORS[level][1], highlightthickness=1)
        frame.pack(fill="x", padx=6, pady=5)
        return frame

    def _label(self, frame, text, bold=False, color="#eef2f6"):
        label = tk.Label(frame, text=text, background=frame.cget("background"), foreground=color, anchor="w", justify="left",
                 wraplength=max(160, self.canvas.winfo_width() - 32), font=("Microsoft YaHei UI", 9, "bold" if bold else "normal"))
        label.pack(fill="x", padx=8, pady=2)
        label.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-int(event.delta / 120), "units"))

    def details(self, card):
        window = tk.Toplevel(self)
        window.title(f"{card['name']} · PID {card['pid']} · 训练原日志")
        window.geometry("780x440")
        ttk.Label(window, text=card.get("log_path") or "未找到训练日志", wraplength=740).pack(anchor="w", padx=8, pady=4)
        ttk.Label(window, text="最近采样的原文片段；长日志显示末尾64 KiB，可横向滚动。").pack(anchor="w", padx=8)
        horizontal = ttk.Scrollbar(window, orient="horizontal")
        horizontal.pack(side="bottom", fill="x")
        text = ScrolledText(window, wrap="none", font=("Consolas", 10), xscrollcommand=horizontal.set)
        horizontal.configure(command=text.xview)
        text.pack(fill="both", expand=True)
        text.insert("end", card.get("details") or "没有可读取的日志")
        text.see("end")
        text.configure(state="disabled")
