from __future__ import annotations

from datetime import datetime
import threading
import time
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from .training import TrainingTracker, collect_remote


class TrainingService:  # 每台服务器最多一个只读请求；线程只投递结果，不接触Tk控件。
    def __init__(self, events):
        self.events = events
        self.states = {}

    def register(self, server):
        state = self.states.get(server.id)
        if state and (state["active"] or state["busy"]):
            raise ValueError("请先停止此服务器的训练监控并等待当前请求结束")
        self.states[server.id] = dict(server=server, tracker=TrainingTracker(server), active=False, busy=False,
                                      generation=(state["generation"] + 1 if state else 0), due=0, interval=60, snapshot=None)

    def start(self, server_id, interval):
        state = self.states[server_id]
        if state["busy"] and not state["active"]:
            raise ValueError("前一次采集尚未结束，请稍后再开始")
        state.update(active=True, interval=interval, due=0)

    def stop(self, server_id):
        state = self.states[server_id]
        state.update(active=False, generation=state["generation"] + 1)

    def tick(self):
        now = time.monotonic()
        for server_id, state in self.states.items():
            if state["active"] and not state["busy"] and now >= state["due"]:
                state.update(busy=True, due=now + state["interval"])
                threading.Thread(target=self._collect, args=(server_id, state["server"], state["generation"], state["tracker"].previous()), daemon=True).start()

    def _collect(self, server_id, server, generation, previous):
        try:
            result, error = collect_remote(server, previous), ""
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
                return
            except Exception as exc:
                error = f"训练信息解析失败：{exc}"
        old = state["snapshot"] or {"cards": [], "gpus": []}
        state["snapshot"] = dict(old, error=error, received_at=time.time())  # 断连保留最近摘要并标黄，不能变成“无训练”。

    def busy(self):
        return any(state["busy"] for state in self.states.values())

    def stop_all(self):
        for key in self.states:
            self.stop(key)


COLORS = {"success": ("#10372a", "#8bf0ad"), "error": ("#481f26", "#ff9ca8"),
          "warning": ("#40361f", "#ffe09a"), "idle": ("#27313e", "#f2f4f7")}


def metrics_text(summary):
    names = {"loss": "Loss", "acc": "ACC", "auc": "AUC", "video_auc": "视频AUC", "eer": "EER", "ap": "AP"}
    metrics = summary.get("metrics", {})
    return " · ".join(f"{names.get(name, name)} {value:.4f}" for name, value in metrics.items() if name in names) or "—"


class TrainingPane(ttk.Frame):
    def __init__(self, parent, service, configure_server, interval, show_gpus=False):
        super().__init__(parent)
        self.service, self.configure_server, self.interval = service, configure_server, interval
        self.show_gpus, self.server_id = show_gpus, None
        self.render_key = None
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 5))
        self.start_button = ttk.Button(bar, text="开始训练监控", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(bar, text="停止", command=self.stop)
        self.stop_button.pack(side="left", padx=4)
        ttk.Button(bar, text="设置连接", command=configure_server).pack(side="right")
        self.summary = ttk.Label(self, text="尚未配置训练连接", wraplength=560)
        self.summary.pack(fill="x", pady=(0, 5))
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
            self.server_id, self.render_key = server_id, None
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
               snapshot["received_at"] if snapshot else None, expired)
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
        self.summary.configure(text=text, foreground="#9a6700" if warning else "#245c45")
        position = self.canvas.yview()[0]
        for child in self.content.winfo_children():
            child.destroy()
        if self.show_gpus and snapshot:
            frame = self._card("idle")
            self._label(frame, "GPU 状态（整卡）", bold=True)
            if snapshot.get("gpu_error") or expired or warning:
                self._label(frame, "GPU数据不可用或已过期", color=COLORS["warning"][1])
            else:
                for gpu in snapshot["gpus"]:
                    self._label(frame, f"GPU {gpu['index']} · {gpu['name']} · {gpu['temperature']:.0f}°C")
                    self._label(frame, f"利用率 {gpu['util']:.0f}%   显存 {gpu['memory_used']/1024:.2f}/{gpu['memory_total']/1024:.2f} GiB")
        cards = snapshot.get("cards", []) if snapshot else []
        if not cards:
            frame = self._card("warning" if warning else "idle")
            self._label(frame, "无法确认训练状态" if warning else "无匹配训练进程" if snapshot and state["active"] else "等待训练采集", bold=True)
        for card in cards:
            level = "warning" if warning or not state["active"] else card["level"]
            frame = self._card(level)
            self._label(frame, card["name"], bold=True)
            self._label(frame, card["status"] + (" · 最近结果" if warning or not state["active"] else ""), color=COLORS[level][1])
            gpu = ",".join(f"#{index}" for index in card.get("gpu_indices", [])) or "关联未确认"
            self._label(frame, f"PID {card['pid']} · 用户 {card.get('user', '—')} · GPU {gpu}")
            progress = card["progress"]
            epoch, total = progress.get("epoch"), progress.get("total_epochs")
            raw = f" · 日志Epoch {progress['raw_epoch']}" if progress.get("raw_epoch") is not None else ""
            self._label(frame, f"轮次 {epoch if epoch is not None else '—'}/{total if total is not None else '—'}{raw} · 全局Step {progress.get('step') if progress.get('step') is not None else '—'}")
            if epoch is not None and total and total > 0:
                ttk.Progressbar(frame, maximum=total, value=total if progress.get("completed") else min(total, max(0, epoch - 1)), style="Memory.Horizontal.TProgressbar").pack(fill="x", padx=8, pady=3)  # 条形仅表示此前已完成轮次，不猜当前轮内进度。
            train = progress.get("train", {})
            self._label(frame, f"训练（第{train.get('epoch', '—')}轮，Step {train.get('step', '—')}）：{metrics_text(train)}")
            for dataset, summary in progress.get("tests", {}).items():
                self._label(frame, f"测试 {dataset}（第{summary.get('epoch', '—')}轮，Step {summary.get('step', '—')}）：{metrics_text(summary)}")
            if progress.get("error"):
                self._label(frame, progress["error"], color=COLORS["error"][1])
            when = datetime.fromtimestamp(card["advanced_at"]).strftime("%m-%d %H:%M:%S")
            self._label(frame, f"最近进展 {when}")
            ttk.Button(frame, text="日志详情", command=lambda item=card: self.details(item)).pack(anchor="e", padx=8, pady=(2, 6))

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
        window.title(f"{card['name']} · 只读日志详情")
        window.geometry("780x440")
        text = ScrolledText(window, wrap="word", font=("Microsoft YaHei UI", 9))
        text.pack(fill="both", expand=True)
        text.insert("end", card.get("details") or "没有可读取的日志")
        text.configure(state="disabled")
