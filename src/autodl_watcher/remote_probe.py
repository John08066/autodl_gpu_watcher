from __future__ import annotations

import csv
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


def training_roots(processes, script_names, project):  # 仅识别Python训练入口，排除timeout包装和同一训练树的DataLoader子进程。
    candidates = {}
    for item in processes:
        args = item["args"]
        if not args or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(args[0]).name):
            continue
        if next((Path(arg).name for arg in args[1:] if arg.endswith(".py")), "") not in script_names:
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
                if path.suffix.lower() in {".log", ".out", ".jsonl"} and path.is_file():
                    paths.add(str(path))
            except OSError:
                continue
    except OSError:
        pass
    return sorted(paths, key=lambda name: (Path(name).name != "training.log", name))[:4]


def query_gpu():
    fields = "index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu"
    gpus, apps, errors = [], [], []
    for query in (f"--query-gpu={fields}", "--query-compute-apps=gpu_uuid,pid,used_memory"):
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
                        apps.append(dict(uuid=row[0], pid=int(row[1]), memory_used=float(row[2])))
                except (ValueError, IndexError):
                    errors.append("部分GPU字段不可用")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            errors.append("GPU查询不可用")
    return gpus, apps, "; ".join(dict.fromkeys(errors))


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
    roots = training_roots(processes, request.get("scripts", ["train.py", "trainer.py", "train_net.py"]), request.get("project", ""))
    gpus, apps, gpu_error = query_gpu()
    container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    try:
        container = container or bool(re.search("docker|kubepods|lxc|containerd", Path("/proc/1/cgroup").read_text()))
    except OSError:
        container = True  # 无法核验命名空间时不猜GPU进程归属。
    tasks = []
    for item in roots[:32]:
        try:
            current = read_process(Path("/proc") / str(item["pid"]), boot_id, boot_time, ticks)
            if not current or current["key"] != item["key"]:
                continue
        except OSError:
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
        item.update(name=option("--task_target") or Path(detector).stem or next((Path(arg).name for arg in args[1:] if arg.endswith(".py")), "训练进程"),
                    config=detector, alive=True, user=pwd.getpwuid(os.getuid()).pw_name)
        item["logs"] = [read_log(path) for path in task_files(item)]
        item["gpu_indices"] = sorted({gpu["index"] for app in apps for gpu in gpus
                                      if not container and app["pid"] == item["pid"] and app["uuid"] == gpu["uuid"]})
        item["gpu_memory_mb"] = sum(app["memory_used"] for app in apps if not container and app["pid"] == item["pid"]) if item["gpu_indices"] else None
        tasks.append(item)
    keys = {item["key"] for item in tasks}
    scan_ok = restricted == 0 and len(roots) <= 32
    if scan_ok:
        for previous in request.get("previous", [])[:32]:
            if previous["key"] not in keys:
                tasks.append(dict(previous, alive=False, logs=[read_log(path) for path in previous.get("log_paths", [])[:4]]))
    return {"observed_at": time.time(), "tasks": tasks, "gpus": gpus, "gpu_error": gpu_error,
            "scan_ok": scan_ok, "utc_offset": time.strftime("%z"), "container": container, "user": pwd.getpwuid(os.getuid()).pw_name}


if __name__ == "__main__":
    print(json.dumps(collect(REQUEST), ensure_ascii=False), flush=True)
