from __future__ import annotations

import csv
from fnmatch import fnmatchcase
import io
import json
import os
from pathlib import Path
if os.name == "posix":
    import pwd
import re
import stat
import subprocess
import time


def read_process(path, boot_id, boot_time, ticks):  # /proc字段按进程启动时刻识别，避免PID复用串任务。
    if path.stat().st_uid != os.getuid():
        return None
    args = (path / "cmdline").read_bytes().decode("utf-8", "replace").strip("\0").split("\0")
    fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
    return {"pid": int(path.name), "ppid": int(fields[1]), "start_ticks": int(fields[19]),
            "key": f"{boot_id}:{path.name}:{fields[19]}", "started_at": boot_time + int(fields[19]) / ticks,
            "args": args, "cwd": os.readlink(path / "cwd") if args and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(args[0]).name) else "", "process_state": fields[0]}


def python_entry(args):
    if not args or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(args[0]).name):
        return ""
    index = 1
    while index < len(args):
        arg = args[index]
        if arg in {"-c", "-"}:
            return ""
        if arg == "-m":
            return args[index + 1] if index + 1 < len(args) else ""
        if arg in {"-W", "-X"}:
            index += 2
        elif arg.startswith("-"):
            index += 1
        else:
            return arg
    return ""


def training_roots(processes, script_names, project):  # 默认结合运行证据；自定义脚本范围仍严格匹配。
    automatic = set(script_names) == {"train.py", "trainer.py", "train_*.py"}
    candidates = {}
    for item in processes:
        entry = python_entry(item["args"])
        if not entry:
            continue
        if not any(fnmatchcase(Path(entry).name, pattern) for pattern in script_names) and not (automatic and item.get("training_candidate")):
            continue
        if project and not (item["cwd"] == project.rstrip("/") or item["cwd"].startswith(project.rstrip("/") + "/")):
            continue
        candidates[item["pid"]] = item
    parents = {item["pid"]: item["ppid"] for item in processes}
    result = []
    for pid, item in candidates.items():
        parent, visited = item["ppid"], {pid}
        while parent in parents and parent not in visited and parent not in candidates:
            visited.add(parent)
            parent = parents[parent]
        if parent not in candidates:
            result.append(item)
    return result


def read_log(path):  # 只读普通文件的有限首尾；不加载pickle、检查点或执行日志内容。
    result = {"path": str(path)}
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("不是本人普通日志文件")
            head = stream.read(16384)
            offset = max(len(head), info.st_size - 65536)
            stream.seek(offset)
            tail = stream.read(65536)
        if offset > len(head):
            tail = tail.partition(b"\n")[2]  # 从行中间开始时丢弃不完整行。
        result.update(head=head.decode("utf-8", "replace"), tail=tail.decode("utf-8", "replace"),
                      size=info.st_size, gap=offset > len(head), modified_at=info.st_mtime, identity=f"{info.st_dev}:{info.st_ino}")
    except (OSError, ValueError) as exc:
        result["error"] = f"日志不可读：{type(exc).__name__}"
    return result


def task_files(process):
    paths = set()
    try:
        for fd in (Path("/proc") / str(process["pid"]) / "fd").iterdir():
            try:
                path = Path(os.readlink(fd))
                if path.suffix.lower() in {".log", ".out", ".jsonl"} and not path.name.endswith("batch_trace.jsonl") and path.is_file():
                    paths.add(str(path))
            except OSError:
                continue
    except OSError:
        pass
    return sorted(paths, key=lambda name: (Path(name).name != "training.log", name))[:4]


def tmux_panes():
    try:
        result = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_pid}\t#{session_name}"],
                                capture_output=True, text=True, timeout=3)
        return {int(pid): name for line in result.stdout.splitlines()
                for pid, separator, name in [line.partition("\t")] if separator and pid.isdigit()}
    except (OSError, subprocess.TimeoutExpired):
        return {}  # tmux是辅助关联；缺少tmux不阻断普通训练采集。


def discover_evidence(processes, project, panes, gpu_pids):
    parents = {item["pid"]: item for item in processes}
    for item in processes:
        entry = python_entry(item["args"])
        if not entry or any(name in entry for name in ("ipykernel", "jupyter", "tensorboard", "multiprocessing")):
            continue
        if project and not (item["cwd"] == project.rstrip("/") or item["cwd"].startswith(project.rstrip("/") + "/")):
            continue
        parent = parents.get(item["ppid"])
        if parent and parent["args"] == item["args"]:
            continue  # DataLoader继承相同命令和日志，无需重复读取。
        pid, visited = item["pid"], set()
        while pid in parents and pid not in visited:
            visited.add(pid)
            if pid in panes:
                item["tmux_session"] = panes[pid]
                break
            pid = parents[pid]["ppid"]
        item["logs"] = [read_log(path) for path in task_files(item)]
        evidence = any(re.search(r'(?:\b(?:TRAIN|EVAL|TEST)\s+\{|"event"\s*:\s*"(?:train|test|progress)"|Epoch\[|\bEpoch\s*[:= ]\s*\d+\s*/|training-(?:loss|metric))',
                                 log.get("head", "") + log.get("tail", "")) for log in item["logs"])
        item["training_candidate"] = bool(item.get("tmux_session") or item["pid"] in gpu_pids or evidence)


def query_gpu():
    fields = "index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu"
    gpus, apps, errors = [], [], []
    for query in (f"--query-gpu={fields}", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory"):
        try:
            response = subprocess.run(["nvidia-smi", query, "--format=csv,noheader,nounits"],
                                      capture_output=True, text=True, timeout=8)
            if response.returncode:
                raise ValueError("nvidia-smi查询失败")
            for row in csv.reader(io.StringIO(response.stdout), skipinitialspace=True):
                try:
                    if query.startswith("--query-gpu="):
                        gpus.append(dict(index=int(row[0]), uuid=row[1], name=row[2], util=float(row[3]),
                                         memory_used=float(row[4]), memory_total=float(row[5]), temperature=float(row[6])))
                    else:
                        memory = float(row[3]) if re.fullmatch(r"\d+(?:\.\d+)?", row[3].strip()) else None
                        apps.append(dict(uuid=row[0], pid=int(row[1]), process_name=row[2], memory_used=memory))
                except (ValueError, IndexError):
                    errors.append("部分GPU字段不可用")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            errors.append("GPU查询不可用")
    return gpus, apps, "; ".join(dict.fromkeys(errors))



def public_gpu_processes(gpus, apps, container):  # 其他用户只读系统允许的进程元信息，不读取其训练日志或环境变量。
    rows = []
    devices = {gpu["uuid"]: gpu["index"] for gpu in gpus}
    for app in apps:
        row = dict(app, gpu_index=devices.get(app["uuid"]), user="未知", process=Path(app.get("process_name", "未知")).name, config="", own=False)
        if not container:  # 容器NVML返回宿主PID，同号容器PID也不能证明归属。
            path = Path("/proc") / str(app["pid"])
            try:
                uid = path.stat().st_uid
                before = (path / "stat").read_text().rsplit(")", 1)[1].split()[19]
                row.update(user=pwd.getpwuid(uid).pw_name, own=uid == os.getuid(), start_ticks=int(before))
                args = (path / "cmdline").read_bytes()[:16384].decode("utf-8", "replace").split("\0")
                row["process"] = next((Path(arg).name for arg in args if arg.endswith(".py")), row["process"])
                for index, arg in enumerate(args):
                    if arg in {"--task_target", "--detector_path", "--config"} and index + 1 < len(args):
                        row["config"] = Path(args[index + 1]).name  # 不展示其他命令行参数，避免泄露令牌。
                        if arg == "--task_target":
                            break
                if before != (path / "stat").read_text().rsplit(")", 1)[1].split()[19]:
                    row.update(user="未知", own=False, process="进程已变化", config="")
            except (OSError, ValueError, IndexError, KeyError):
                row["config"] = ""  # 权限不足仍显示NVML已返回的GPU/PID/显存，用户名可能已可核验。
        else:
            row.update(user="宿主用户未知", process="宿主进程", config="容器内归属未确认")
        rows.append(row)
    return rows

def collect(request):  # 整次远端请求仅扫描本人进程和已关联日志，无写入、信号或GPU计算。
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    boot_time = int(next(line.split()[1] for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime ")))
    ticks = os.sysconf("SC_CLK_TCK")
    processes, restricted = [], 0
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            item = read_process(path, boot_id, boot_time, ticks)
            if item and item["process_state"] != "Z":
                processes.append(item)
        except FileNotFoundError:
            continue  # 扫描期间已经退出的进程留待后续完整快照确认。
        except PermissionError:
            restricted += 1
        except (OSError, ValueError, IndexError):
            continue
    gpus, apps, gpu_error = query_gpu()
    container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    try:
        container = container or bool(re.search("docker|kubepods|lxc|containerd", Path("/proc/1/cgroup").read_text()))
    except OSError:
        container = True  # 无法核验命名空间时不猜GPU进程归属。
    process_rows = public_gpu_processes(gpus, apps, container)
    discover_evidence(processes, request.get("project", ""), tmux_panes(),
                      {row["pid"] for row in process_rows if row["own"]} if not container else set())
    roots = training_roots(processes, request.get("scripts", ["train.py", "trainer.py", "train_*.py"]), request.get("project", ""))
    tasks = []
    for item in roots[:32]:
        try:
            current = read_process(Path("/proc") / str(item["pid"]), boot_id, boot_time, ticks)
            if not current or current["key"] != item["key"]:
                continue
        except FileNotFoundError:
            continue
        except OSError:
            restricted += 1
            continue
        args = item.pop("args")
        def option(name):
            for index, arg in enumerate(args):
                if arg == name and index + 1 < len(args):
                    return args[index + 1]
                if arg.startswith(name + "="):
                    return arg.split("=", 1)[1]
            return ""
        detector = option("--detector_path")
        item.update(name=option("--task_target") or Path(detector).stem or Path(python_entry(args)).name or "训练进程",
                    config=detector, alive=True, user=pwd.getpwuid(os.getuid()).pw_name)
        if "logs" not in item:
            item["logs"] = [read_log(path) for path in task_files(item)]
        matched = [app for app in process_rows if not container and app["pid"] == item["pid"] and app["own"]
                   and app.get("start_ticks") == item["start_ticks"] and app["gpu_index"] is not None]
        item["gpu_indices"] = sorted({app["gpu_index"] for app in matched})
        item["gpu_memory_mb"] = sum(app["memory_used"] for app in matched) if matched and all(app["memory_used"] is not None for app in matched) else None
        item["gpu_memory_by_device"] = [{"gpu_index": app["gpu_index"], "memory_mb": app["memory_used"]} for app in matched]
        tasks.append(item)
    keys = {item["key"] for item in tasks}
    scan_ok = restricted == 0 and len(roots) <= 32
    live_keys = {item["key"] for item in processes}
    if scan_ok:
        for previous in request.get("previous", [])[:32]:
            if previous["key"] not in keys:
                tasks.append(dict(previous, alive=previous["key"] in live_keys, gpu_memory_mb=None, gpu_memory_by_device=[], logs=[read_log(path) for path in previous.get("log_paths", [])[:4]]))
    return {"observed_at": time.time(), "tasks": tasks, "gpus": gpus, "gpu_error": gpu_error,
            "gpu_processes": process_rows, "scan_ok": scan_ok, "utc_offset": time.strftime("%z"), "container": container, "user": pwd.getpwuid(os.getuid()).pw_name}


if __name__ == "__main__":
    print(json.dumps(collect(REQUEST), ensure_ascii=False), flush=True)
