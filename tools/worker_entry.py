import os
from pathlib import Path
import sys


def main():  # console 类型的内部 EXE 由 GUI 隐藏启动，保留 stdin/stdout 实时管道。
    root = Path(os.environ.get("AUTODL_APP_ROOT", Path(sys.executable).parent.parent)).resolve()
    os.chdir(root)
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", line_buffering=True, write_through=True)
    task, *args = sys.argv[1:]
    sys.argv = [sys.argv[0], *args]  # 业务模块只看到自身参数。
    if task == "monitor":
        from autodl_watcher.main import main as run
    elif task == "discover":
        from autodl_watcher.gui import discover as run
    elif task == "login":
        from autodl_watcher.login import main as run
    elif task == "export":
        from autodl_watcher.usage_report import main as run
    else:
        raise ValueError(f"Unknown worker task: {task}")
    run()


if __name__ == "__main__":
    main()
