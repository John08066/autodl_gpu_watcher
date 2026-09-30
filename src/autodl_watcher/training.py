from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time


@dataclass(frozen=True)
class TrainingServer:  # SSH凭据仍由用户的OpenSSH管理，本地配置只保存连接别名与读取范围。
    id: str
    name: str
    ssh_alias: str
    python: str = "python3"
    project: str = ""
    entry: str = ""  # 非空时绑定AutoDL入口；空值表示独立服务器页。
    scripts: str = "train.py,trainer.py,train_net.py"
    stalled_minutes: float = 120

    def validate(self):
        if not self.id or not self.name.strip() or not re.fullmatch(r"[\w.@:-]+", self.ssh_alias) or self.ssh_alias.startswith("-"):
            raise ValueError("请填写服务器名称和有效SSH别名")
        if not self.python.strip() or any(char in self.python for char in "\r\n\0"):
            raise ValueError("请填写远程Python程序路径")
        if self.project and not self.project.startswith("/"):
            raise ValueError("项目目录须为远程绝对路径，留空表示当前SSH用户的训练")
        if self.entry and not re.fullmatch(r"autodl-\d+-\d+", self.entry):
            raise ValueError("AutoDL入口应为autodl-203-1这样的编号")
        if not self.scripts.strip() or not math.isfinite(float(self.stalled_minutes)) or float(self.stalled_minutes) <= 0:
            raise ValueError("训练脚本名及无进展提醒时间必须有效")
        return self


def load_servers(path):
    if not Path(path).exists():
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    servers = [TrainingServer(**item).validate() for item in data]
    if len({item.id for item in servers}) != len(servers) or len({item.entry for item in servers if item.entry}) != sum(bool(item.entry) for item in servers):
        raise ValueError("服务器ID或AutoDL绑定入口重复")
    return servers


def save_servers(path, servers):
    for server in servers:
        server.validate()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps([asdict(item) for item in servers], ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def ssh_executable():
    path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/OpenSSH/ssh.exe" if os.name == "nt" else Path(shutil.which("ssh") or "/usr/bin/ssh")
    if not path.is_file():
        raise OSError(f"未找到OpenSSH客户端：{path}")
    return str(path)


def collect_remote(server, previous=()):
    server.validate()
    source = Path(__file__).with_name("remote_probe.py").read_text(encoding="utf-8")
    request = {"project": server.project, "scripts": [item.strip() for item in server.scripts.split(",") if item.strip()], "previous": list(previous)}
    bootstrap = 'import json,sys;p=json.load(sys.stdin);exec(compile(p["code"],"<readonly-training-probe>","exec"),{"__name__":"__main__","REQUEST":p["request"]})'
    command = [ssh_executable(), "-T", "-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=10",
               "-o", "StrictHostKeyChecking=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "PermitLocalCommand=no",
               server.ssh_alias, shlex.quote(server.python) + " -c " + shlex.quote(bootstrap)]
    response = subprocess.run(command, input=json.dumps({"code": source, "request": request}, ensure_ascii=False),
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=35,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if response.returncode:
        detail = response.stderr.strip().splitlines()
        raise OSError("SSH采集失败：" + (detail[-1][:300] if detail else f"退出码{response.returncode}"))
    payload = json.loads(response.stdout)
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list) or not isinstance(payload.get("gpus"), list):
        raise ValueError("服务器监控响应格式错误")
    return payload


_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_FATAL = re.compile(r"(?:CUDA out of memory|OutOfMemoryError|(?:Runtime|Value|Assertion|FloatingPoint|Memory|NCCL|ChildFailed)Error:|Segmentation fault|Traceback \(most recent call last\))", re.I)
_TIMESTAMP = re.compile(r"(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)")


def parse_progress(text, utc_offset="+0000"):  # DeepfakeBench适配器及通用JSONL事件协议；指标总是携带各自epoch/step。
    result = {"phase": "unknown", "epoch": None, "total_epochs": None, "raw_epoch": None, "step": None,
              "train": {}, "tests": {}, "error": "", "completed": False, "progress_at": None}
    deepfake = bool(re.search(r"Epoch\[\d+\]|training-metric,|nEpochs:", text))
    last_epoch = re.search(r"\bnEpochs:\s*(\d+)", text)
    start_epoch = re.search(r"\bstart_epoch:\s*(\d+)", text)
    first = int(start_epoch[1]) if start_epoch else 0
    if deepfake and last_epoch:
        result["total_epochs"] = int(last_epoch[1]) - first + 1  # 已核验此项目range(start_epoch,nEpochs+1)，包含末轮。
    stamp = None
    for line in text.replace("\r", "\n").splitlines():
        match = _TIMESTAMP.search(line)
        if match:
            try:
                stamp = datetime.strptime(match[1].replace("T", " ") + utc_offset, "%Y-%m-%d %H:%M:%S%z").timestamp()
            except ValueError:
                stamp = None
        advanced = False
        if line.lstrip().startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                event = {}
            if not isinstance(event, dict):
                event = {}
            if event.get("event") in {"train", "test", "progress", "completed", "error"}:
                phase = event.get("phase", event["event"])
                if phase in {"train", "test"}:
                    result["phase"] = phase
                for key in ("epoch", "total_epochs", "step"):
                    value = event.get(key)
                    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                        result[key] = value
                values = event.get("metrics", {})
                metrics = {str(key): value for key, value in (values.items() if isinstance(values, dict) else ())
                           if isinstance(value, (int, float)) and math.isfinite(value)}
                if metrics:
                    summary = {"metrics": metrics, "epoch": result["epoch"], "step": result["step"], "time": stamp}
                    if phase == "test":
                        result["tests"][str(event.get("dataset", "test"))] = summary
                    else:
                        result["train"] = summary
                result["completed"] = event["event"] == "completed"
                result["error"] = str(event.get("message", "训练错误"))[:500] if event["event"] == "error" else ""
                advanced = True
        epoch = re.search(r"Epoch\[(\d+)\]\s+start", line)
        generic = re.search(r"\bEpoch\s*[:= ]\s*(\d+)\s*/\s*(\d+)", line, re.I)
        if epoch:
            result.update(raw_epoch=int(epoch[1]), epoch=int(epoch[1]) - first + 1, phase="train", error="", completed=False)
            advanced = True
        elif generic:
            result.update(epoch=int(generic[1]), total_epochs=int(generic[2]), phase="train", error="", completed=False)
            advanced = True
        if "===> Test start!" in line:
            result["phase"] = "test"
            advanced = True
        if "===> Test Done!" in line:
            result["phase"] = "train"
            advanced = True
        step = re.search(r"(?:Iter:|\bstep:)\s*(\d+)", line)
        if step:
            result["step"] = int(step[1])
        dataset = re.search(r"dataset:\s*(\S+)", line)
        metrics = {name: float(value) for name, value in re.findall(r"(?:training|testing)-(?:metric|loss),\s*([\w]+):\s*(" + _NUMBER + ")", line)}
        metrics = {"loss" if name == "overall" else name: value for name, value in metrics.items() if math.isfinite(value)}
        if metrics:
            phase = "test" if "testing-" in line else "train"
            old = result["tests"].get(dataset[1], {}) if phase == "test" and dataset else result["train"]
            if old.get("epoch") != result["epoch"] or old.get("step") != result["step"]:
                old = {}
            summary = {"epoch": result["epoch"], "step": result["step"], "time": stamp, "metrics": {**old.get("metrics", {}), **metrics}}
            if phase == "test" and dataset:
                result["tests"][dataset[1]] = summary
            elif phase == "train":
                result["train"] = summary
            result.update(phase=phase, error="", completed=False)
            advanced = True
        if "Stop Training on best Testing metric" in line or "Experiment summary saved to" in line:
            result.update(completed=True, error="")
            advanced = True
        if _FATAL.search(line):
            result.update(error=line.strip()[-500:], completed=False)
            advanced = True
        if advanced and stamp is not None:
            result["progress_at"] = stamp
    return result


def log_text(log):
    if log.get("gap"):
        config = "\n".join(line for line in log["head"].splitlines() if re.match(r"\s*(?:nEpochs|start_epoch):", line))
        return config + "\n" + log["tail"]  # 日志截断后不能借用文件头旧轮次解释当前指标。
    return log["head"] + log["tail"]


class TrainingTracker:
    def __init__(self, server):
        self.server = server
        self.known = {}
        self.references = {}

    def previous(self):
        return list(self.references.values())[-32:]

    def update(self, snapshot):
        now = snapshot["observed_at"]
        cards = []
        for task in snapshot["tasks"]:
            key = task["key"]
            previous = self.known.get(key, {})
            logs = [item for item in task.get("logs", []) if "error" not in item]
            primary = next((item for item in logs if Path(item["path"]).name == "training.log"), logs[0] if logs else None)
            progress = parse_progress(log_text(primary), snapshot.get("utc_offset", "+0000")) if primary else previous.get("progress", parse_progress(""))
            details = []
            for log in logs:
                text = log_text(log)
                details.extend([log["path"], *text.splitlines()[-12:]])
                if log is not primary and log["modified_at"] >= (primary["modified_at"] if primary else 0):
                    extra = parse_progress(text, snapshot.get("utc_offset", "+0000"))
                    if extra["error"] and log["modified_at"] >= task.get("started_at", 0):
                        progress["error"] = extra["error"]
                    if extra["completed"]:
                        progress["completed"] = True
            indicator = {k: progress[k] for k in ("phase", "epoch", "step", "completed", "error")}
            indicator["train"] = {k: v for k, v in progress["train"].items() if k != "time"}
            indicator["tests"] = {name: {k: v for k, v in value.items() if k != "time"} for name, value in progress["tests"].items()}
            marker = json.dumps(indicator, sort_keys=True)
            advanced_at = progress["progress_at"] or (primary["modified_at"] if primary else task.get("started_at", now))
            if previous.get("marker") == marker:
                advanced_at = previous["advanced_at"]  # 重复打印同一指标不是训练进展。
            alive = task.get("alive", False)
            if progress["error"]:
                level, status = "error", "错误已记录 · 进程仍存活" if alive else "异常中止"
            elif progress["completed"]:
                level, status = "idle", "已完成 · 收尾中" if alive else "已完成"
            elif not alive:
                level, status = "warning", "进程已退出 · 结果未确认"
            elif not primary or progress["phase"] == "unknown":
                level, status = "warning", "进程存活 · 训练进度不可读"
            elif now - advanced_at > self.server.stalled_minutes * 60:
                level, status = "warning", "长时间无新进度 · 待核查"
            else:
                level, status = "success", "正在测试" if progress["phase"] == "test" else "正在训练"
            card = dict(task, progress=progress, level=level, status=status, marker=marker,
                        advanced_at=advanced_at, details="\n".join(details)[-12000:])
            card.pop("logs", None)
            self.known[key] = card
            self.references[key] = {k: task[k] for k in ("key", "pid", "name", "started_at", "gpu_indices", "gpu_memory_mb", "user", "cwd") if k in task}
            self.references[key]["log_paths"] = [log["path"] for log in task.get("logs", [])]
            cards.append(card)
        if not snapshot["scan_ok"]:
            existing = {card["key"] for card in cards}
            cards.extend(dict(card, level="warning", status="进程列表不完整 · 状态待确认") for key, card in self.known.items() if key not in existing)
        return dict(snapshot, cards=sorted(cards, key=lambda card: (not card.get("alive", False), -card.get("started_at", 0))),
                    received_at=time.time(), error="" if snapshot["scan_ok"] else "进程列表读取不完整")
