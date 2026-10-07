from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
import json
from collections import deque
import queue
import threading
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time


DEFAULT_SCRIPTS = "train.py,trainer.py,train_*.py"


@dataclass(frozen=True)
class TrainingServer:  # SSH凭据仍由用户的OpenSSH管理，本地配置只保存连接别名与读取范围。
    id: str
    name: str
    ssh_alias: str
    python: str = "python3"
    project: str = ""
    entry: str = ""  # 非空时绑定AutoDL入口；空值表示独立服务器页。
    scripts: str = DEFAULT_SCRIPTS
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
    for item in data:
        if {name.strip() for name in item.get("scripts", "").split(",")} == {"train.py", "trainer.py", "train_net.py"}:
            item["scripts"] = DEFAULT_SCRIPTS  # 仅迁移旧默认值，不放宽用户自定义范围，也不改写文件。
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


class TrainingConnection:  # 每个训练监控保留自己的SSH进程；不借用或终止VS Code的连接。
    TIMEOUT = 35

    def __init__(self, server):
        self.server = server.validate()
        self.process = None
        self.closed = False
        self.attempts = self.samples = 0
        self._lifecycle = threading.RLock()
        self._request = threading.Lock()

    def _command(self):
        bootstrap = ('import json,sys;scope={"__name__":"readonly_probe"};'
                     'exec(compile(json.loads(sys.stdin.readline())["code"],"<readonly-training-probe>","exec"),scope)\n'
                     'for line in sys.stdin:\n'
                     ' print(json.dumps(scope["collect"](json.loads(line)),ensure_ascii=False),flush=True)')
        # MaxStartups的服务端横幅只在调试级别可见；stderr在内存限量读取，不显示密钥诊断明细。
        return [ssh_executable(), "-v", "-T", "-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey",
                "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                "-o", "StrictHostKeyChecking=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none",
                "-o", "PermitLocalCommand=no", self.server.ssh_alias,
                shlex.quote(self.server.python) + " -u -c " + shlex.quote(bootstrap)]

    def _open(self):
        with self._lifecycle:
            if self.closed:
                raise OSError("训练SSH连接已停止")
            if self.process is not None and self.process.poll() is not None:
                self._discard()
            if self.process is not None:
                return False
            source = Path(__file__).with_name("remote_probe.py").read_text(encoding="utf-8")
            self._bootstrap_message = json.dumps({"code": source}, ensure_ascii=False) + "\n"
            self.attempts += 1
            self.process = subprocess.Popen(self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self._responses, self._errors = queue.Queue(), deque(maxlen=32)
            process, responses, errors = self.process, self._responses, self._errors
            def read_output():
                try:
                    for line in process.stdout:
                        responses.put(line)
                except (OSError, ValueError):
                    pass
                finally:
                    responses.put(None)
            def read_errors():
                try:
                    for line in process.stderr:
                        errors.append(line.rstrip())
                except (OSError, ValueError):
                    pass
            self._stderr_thread = threading.Thread(target=read_errors, daemon=True)
            self._stderr_thread.start()
            threading.Thread(target=read_output, daemon=True).start()
            return True

    def _error(self):  # 保留握手前的MaxStartups原因，不能只取最后一行“连接关闭”。
        detail = "\n".join(self._errors)
        if "MaxStartups" in detail:
            return "SSH接入被限流：未认证连接过多（MaxStartups）"
        if "Permission denied" in detail:
            return "SSH公钥认证失败，请检查SSH别名与密钥"
        if "Host key verification failed" in detail or "REMOTE HOST IDENTIFICATION HAS CHANGED" in detail:
            return "SSH主机身份核验失败，请核对服务器主机密钥"
        if "timed out" in detail.lower():
            return "SSH连接超时"
        return "SSH连接中断" + ("：" + self._errors[-1][:200] if self._errors else "")

    def collect(self, previous=()):
        with self._request:  # 同一会话只允许一问一答，避免不同轮次的响应串线。
            with self._lifecycle:
                fresh = self._open()
                process, responses = self.process, self._responses
            request = {"project": self.server.project, "scripts": [x.strip() for x in self.server.scripts.split(",") if x.strip()],
                       "previous": list(previous)}
            message = json.dumps(request, ensure_ascii=False) + "\n"
            if fresh:
                message = self._bootstrap_message + message
            def send():
                try:
                    process.stdin.write(message)
                    process.stdin.flush()
                except (OSError, ValueError):
                    responses.put(None)
            threading.Thread(target=send, daemon=True).start()  # 超时也覆盖握手期间阻塞的管道写入。
            try:
                line = responses.get(timeout=self.TIMEOUT)
                if line is None:
                    self._stderr_thread.join(timeout=1)
                    raise OSError(self._error())
                payload = json.loads(line)
                if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list) or not isinstance(payload.get("gpus"), list):
                    raise ValueError("服务器监控响应格式错误")
                self.samples += 1
                return payload
            except queue.Empty:
                self._discard()
                raise OSError("SSH采集超时，本次连接已关闭") from None
            except (OSError, ValueError):
                self._discard()
                raise

    def _discard(self):
        with self._lifecycle:
            process, self.process = self.process, None
            if process is None:
                return
            if process.poll() is None:
                process.terminate()  # 仅关闭此对象创建的本机ssh.exe；远端训练不是它的子进程。
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def close(self):
        with self._lifecycle:
            self.closed = True
            self._discard()


def collect_remote(server, previous=()):  # 单次诊断入口；GUI使用TrainingConnection跨轮次复用。
    connection = TrainingConnection(server)
    try:
        return connection.collect(previous)
    finally:
        connection.close()


_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_FATAL = re.compile(r"(?:CUDA out of memory|OutOfMemoryError|[\w.]*(?:Error|Exception)\s*:|Segmentation fault|CUDA error:|\bSIG(?:KILL|SEGV|ABRT)\b|^\s*Killed\s*$|\[(?:ERROR|FATAL|CRITICAL)\]|Traceback \(most recent call last\))", re.I | re.M)
_TIMESTAMP = re.compile(r"(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?)")


def _structured_event(line):  # 把带前缀JSON日志转成已有事件协议；普通批次追踪JSON不当成训练证据。
    match = re.search(r"(?:^|\s)(TRAIN|TEST|EVAL|EVALUATED|TRAINING_STARTED|TRAINING_COMPLETE|SMOKE_PASS|TRAINING_FAILED|ERROR)\s+(?:(\S+)\s+)?(\{.*\})\s*$", line)
    try:
        event = json.loads(match[3] if match else line)
    except ValueError:
        return {}
    if not isinstance(event, dict):
        return {}
    if match:
        kind = match[1]
        if kind == "EVALUATED":
            return {"event": "test", "datasets": {f"{match[2] or 'test'}/{name}": values for name, values in event.items() if isinstance(values, dict)}}
        event["event"] = {"TRAIN": "train", "TEST": "test", "EVAL": "test", "TRAINING_STARTED": "train",
                          "TRAINING_COMPLETE": "completed", "SMOKE_PASS": "completed", "TRAINING_FAILED": "error", "ERROR": "error"}[kind]
        event["smoke"] = kind == "SMOKE_PASS"
        if "steps" in event and "step" not in event:
            event["step"] = event["steps"]
        if "steps_total" in event:
            event["total_steps"] = event["steps_total"]
        if "steps_per_arm" in event:
            event["step"] = event["steps_per_arm"]
            raw = event.get("epoch")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw) and raw >= 0:
                event.update(raw_epoch=raw, epoch=raw + 1)  # 此协议Epoch从0开始；没有提供总轮数时保持未知。
        if event.get("status") in {"failed", "error", "aborted"}:
            event["event"] = "error"
    if "loss" in event:
        event["metrics"] = {**(event.get("metrics") if isinstance(event.get("metrics"), dict) else {}), "loss": event["loss"]}
    return event


def _flat_metrics(values):  # 保留任意指标和嵌套分支名。
    result = {}
    for name, value in (values.items() if isinstance(values, dict) else ()):
        if isinstance(value, dict):
            result.update({f"{name}/{child}": number for child, number in _flat_metrics(value).items()})
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            result[str(name)] = value
    return result


_METADATA = {"epoch", "total_epochs", "raw_epoch", "step", "steps", "global_step", "iteration", "iter", "total_steps", "steps_total", "steps_per_arm",
             "pid", "time", "timestamp", "elapsed_seconds", "wall_time", "frames_done", "frames_total", "seed", "batch_size", "workers",
             "peak_gpu_allocated", "elapsed", "max_epochs"}


def _generic_event(line, event):
    if event.get("event") in {"train", "test", "progress", "completed", "error"}:
        if event["event"] in {"train", "test", "progress"} and "datasets" not in event:
            metrics = {name: value for name, value in _flat_metrics(event).items()
                       if name not in _METADATA and not name.startswith("metrics/")}
            event["metrics"] = {**metrics, **_flat_metrics(event.get("metrics", {}))}
            if "global_step" in event and "step" not in event:
                event["step"] = event["global_step"]
        return event
    body = line[line.find("{"):] if "{" in line else ""
    values = event
    if not values and body:
        try:
            values = json.loads(body)
        except ValueError:
            try:
                values = ast.literal_eval(body)
            except (ValueError, SyntaxError):
                values = {}
    if not isinstance(values, dict) or any(key in values for key in ("indexes", "image_sha256", "rng_sha256")):
        return event
    structured = bool(values)
    if not structured:
        values = {name: float(number) for name, number in re.findall(
            r"(?<![\w/])([A-Za-z_][\w./@%-]*)\s*[:=]\s*(" + _NUMBER + r"|[-+]?(?:nan|inf))(?=$|[\s,;|\]])", line, re.I)}
    phase_text = str(values.get("phase", values.get("event", values.get("status", "")))) if structured else line
    phase = ("test" if re.search(r"\b(?:test(?:ing)?|eval(?:uation|uating)?|valid(?:ation|ating)?|val)\b", phase_text, re.I)
             else "train" if re.search(r"\btrain(?:ing)?\b", phase_text, re.I) else "unknown")
    epoch = re.search(r"\bEpoch\s*[:= ]\s*(\d+)(?:\s*/\s*(\d+))?", line, re.I) if not structured else None
    if epoch:
        values["epoch"] = int(epoch[1])
        if epoch[2]:
            values["total_epochs"] = int(epoch[2])
        if phase == "unknown":
            phase = "train"
    progress = {key: values[key] for key in ("epoch", "total_epochs", "pid", "total_steps") if key in values}
    for name in ("step", "steps", "global_step", "iteration", "iter"):
        if name in values:
            progress["step"] = values[name]
            break
    metrics = _flat_metrics(values.get("metrics", {}))
    metrics.update({name: value for name, value in _flat_metrics(values).items()
                    if name not in _METADATA and not name.startswith("metrics/")})
    if not metrics or not (progress or phase != "unknown" or "metrics" in values or "loss" in values or "eval_loss" in values):
        return event
    if phase == "unknown" and "loss" in values:
        phase = "train"
    if phase == "unknown" and any(name.startswith(("eval_", "val_")) for name in metrics):
        phase = "test"
    return {**progress, "event": "progress", "phase": phase, "metrics": metrics,
            "dataset": str(values.get("dataset", "validation")), "generic": True}


def parse_progress(text, utc_offset="+0000", expected_pid=None):  # DeepfakeBench适配器及通用JSONL事件协议；指标总是携带各自epoch/step。
    result = {"phase": "unknown", "epoch": None, "total_epochs": None, "raw_epoch": None, "step": None,
              "train": {}, "tests": {}, "observations": {}, "error": "", "completed": False, "progress_at": None}
    deepfake = bool(re.search(r"Epoch\[\d+\]|training-metric,|nEpochs:", text))
    last_epoch = re.search(r"\bnEpochs:\s*(\d+)", text)
    start_epoch = re.search(r"\bstart_epoch:\s*(\d+)", text)
    first = int(start_epoch[1]) if start_epoch else 0
    if deepfake and last_epoch:
        result["total_epochs"] = int(last_epoch[1]) - first + 1  # 已核验此项目range(start_epoch,nEpochs+1)，包含末轮。
    stamp = None
    event_pid = None
    for line in text.replace("\r", "\n").splitlines():
        match = _TIMESTAMP.search(line)
        if match:
            try:
                parsed_time = datetime.fromisoformat(match[1].replace("Z", "+00:00"))
                stamp = (parsed_time if parsed_time.tzinfo else parsed_time.replace(tzinfo=datetime.strptime(utc_offset, "%z").tzinfo)).timestamp()
            except ValueError:
                stamp = None
        advanced = False
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
        event = _generic_event(line, _structured_event(line))
        if "pid" in event:
            event_pid = event["pid"]
        evaluation = re.search(r"\bEPOCH_DONE (\S+) (\d+) (\d+) source_best \d+ val (\{.*\})\s*$", line)
        if evaluation and (expected_pid is None or event_pid == expected_pid):
            try:
                values = ast.literal_eval(evaluation[4])
            except (ValueError, SyntaxError):
                values = {}
            event = {"event": "test", "epoch": int(evaluation[2]), "step": int(evaluation[3]),
                     "dataset": evaluation[1] + "/validation", "metrics": values}
        if expected_pid is not None and event.get("pid", expected_pid) != expected_pid:
            continue  # 合并日志中的其他PID不能提供本任务进度或显存。
        if event.get("event") in {"train", "test", "progress", "completed", "error"}:
            phase = event.get("phase", event["event"])
            if phase in {"train", "test"}:
                result["phase"] = phase
            elif event.get("generic"):
                result["phase"] = "unknown"
            for key in ("epoch", "total_epochs", "raw_epoch", "step", "total_steps"):
                value = event.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                    result[key] = value
            groups = event.get("datasets") if isinstance(event.get("datasets"), dict) else {str(event.get("dataset", "test")): event.get("metrics", {})}
            invalid = False
            for dataset, values in groups.items():
                metrics = _flat_metrics(values)
                invalid |= any(not math.isfinite(value) for value in metrics.values())
                metrics = {key: value for key, value in metrics.items() if math.isfinite(value)}
                if metrics:
                    summary = {"metrics": metrics, "epoch": result["epoch"], "step": result["step"], "time": stamp}
                    if phase == "test":
                        result["tests"][str(dataset)] = summary
                    elif phase == "train":
                        result["train"] = summary
                    else:
                        result["observations"] = summary
            result["completed"] = event["event"] == "completed"
            result["smoke"] = event.get("smoke", False)
            if event["event"] == "error" or invalid:
                result.update(error="训练指标出现NaN/Inf" if invalid else str(event.get("message", "训练错误"))[:500], completed=False)
            peak = event.get("peak_gpu_allocated")
            if expected_pid is not None and event.get("pid") == expected_pid and isinstance(peak, (int, float)) and not isinstance(peak, bool) and math.isfinite(peak) and peak >= 0:
                result["peak_memory_bytes"] = peak  # 仅接受本进程明确上报的PyTorch峰值，不替代NVML实时用量。
            advanced = True
        boundary = re.search(r"\bEPOCH_END\s+(\d+)\s+steps_per_arm\s+(\d+)\b", line)
        if boundary:
            result.update(raw_epoch=int(boundary[1]), epoch=int(boundary[1]) + 1, step=int(boundary[2]), phase="between")
            advanced = True
        epoch = re.search(r"Epoch\[(\d+)\]\s+start", line)
        generic = re.search(r"\bEpoch\s*[:= ]\s*(\d+)\s*/\s*(\d+)", line, re.I)
        if epoch:
            result.update(raw_epoch=int(epoch[1]), epoch=int(epoch[1]) - first + 1, phase="train", error="", completed=False)
            advanced = True
        elif generic:
            result.update(epoch=int(generic[1]), total_epochs=int(generic[2]), phase="test" if event.get("phase") == "test" else "train", error="", completed=False)
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
            result.update(phase=phase, completed=False)
            advanced = True
        if "Stop Training on best Testing metric" in line or "Experiment summary saved to" in line:
            result.update(completed=True, error="")
            advanced = True
        if _FATAL.search(line) or re.search(r"(?:training|testing)-(?:loss|metric),\s*\w+:\s*[-+]?(?:nan|inf)\b", line, re.I):
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
            parsed = [(log, parse_progress(log_text(log), snapshot.get("utc_offset", "+0000"), expected_pid=task["pid"])) for log in logs]
            primary, progress = max(parsed, key=lambda pair: (pair[1]["phase"] != "unknown", Path(pair[0]["path"]).name == "training.log", pair[0]["modified_at"])) if parsed else (None, previous.get("progress", parse_progress("")))
            for log, extra in parsed:
                if log is not primary:
                    if extra["error"] and log["modified_at"] >= task.get("started_at", 0) and (
                            extra["progress_at"] is None or extra["progress_at"] >= task.get("started_at", 0) - 1):
                        progress["error"] = extra["error"]
                    if extra["completed"] and log["modified_at"] >= (primary["modified_at"] if primary else task.get("started_at", 0)):
                        progress["completed"] = True
            indicator = {k: progress[k] for k in ("phase", "epoch", "step", "completed", "error")}
            indicator["observations"] = {k: v for k, v in progress["observations"].items() if k != "time"}
            indicator["train"] = {k: v for k, v in progress["train"].items() if k != "time"}
            indicator["tests"] = {name: {k: v for k, v in value.items() if k != "time"} for name, value in progress["tests"].items()}
            marker = json.dumps(indicator, sort_keys=True)
            advanced_at = progress["progress_at"] or (primary["modified_at"] if primary else task.get("started_at", now))
            if previous.get("marker") == marker:
                advanced_at = previous["advanced_at"]  # 重复打印同一指标不是训练进展。
            if previous.get("progress", {}).get("error"):
                progress["error"] = previous["progress"]["error"]  # 同一进程已确认的错误保留为红色，滚动日志或随后断连不能抹掉。
            alive = task.get("alive", False)
            if progress["error"]:
                level, status = "error", "错误已记录 · 进程仍存活" if alive else "异常中止"
            elif progress["completed"]:
                level, status = "idle", ("冒烟检查已完成" if progress.get("smoke") else "已完成") + (" · 收尾中" if alive else "")
            elif not alive:
                level, status = "error", "进程已退出 · 未确认正常完成"
            elif progress["phase"] == "between":
                level, status = "warning", "轮次结束 · 等待后续日志"
            elif not primary or progress["phase"] == "unknown":
                level, status = "warning", "进程存活 · 训练进度不可读"
            elif now - advanced_at > self.server.stalled_minutes * 60:
                level, status = "warning", "长时间无新进度 · 待核查"
            else:
                level, status = "success", "正在测试" if progress["phase"] == "test" else "正在训练"
            card = dict(task, progress=progress, level=level, status=status, marker=marker,
                        advanced_at=advanced_at, log_path=primary["path"] if primary else "",
                        details=(primary["tail"] if primary.get("gap") else primary["head"] + primary["tail"]) if primary else "")
            if primary:
                preview = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", card["details"]).replace("\r", "\n")
                card["preview"] = "\n".join(line[:240] for line in preview.splitlines()[-3:] if line.strip())
            else:
                card["preview"] = "未找到可读日志；进程存在不代表训练进展正常。"
            card.pop("logs", None)
            self.known[key] = card
            self.references[key] = {k: task[k] for k in ("key", "pid", "name", "started_at", "gpu_indices", "gpu_memory_mb", "user", "cwd", "tmux_session") if k in task}
            self.references[key]["log_paths"] = [log["path"] for log in task.get("logs", [])]
            cards.append(card)
        if not snapshot["scan_ok"]:
            existing = {card["key"] for card in cards}
            cards.extend(dict(card, level="error" if card["level"] == "error" else "warning",
                              status=card["status"] if card["level"] == "error" else "进程列表不完整 · 状态待确认")
                         for key, card in self.known.items() if key not in existing)
        for card in cards:
            card["history"] = bool(not card.get("alive") and card.get("log_path") and any(
                other.get("alive") and other.get("log_path") == card["log_path"]
                and other.get("started_at", 0) > card.get("started_at", 0) for other in cards))
            if card["history"]:
                card["status"] = "历史运行 · " + card["status"]
        return dict(snapshot, cards=sorted(cards, key=lambda card: (not card.get("alive", False), -card.get("started_at", 0))),
                    received_at=time.time(), error="" if snapshot["scan_ok"] else "进程列表读取不完整")
