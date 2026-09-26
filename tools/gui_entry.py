import ctypes
import os
from pathlib import Path
import sys
import traceback


def main():  # pythonw 没有控制台；启动异常写入本地日志并通过窗口提示。
    root = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[1])
    log_path = root / ".ui" / "startup.log"
    original_streams = sys.stdout, sys.stderr  # pythonw 的标准流可能为 None，先保存以便退出时还原。
    try:
        log_path.parent.mkdir(exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            sys.stdout = sys.stderr = log  # 启动阶段输出与异常进入文件；工作任务输出由 GUI 管道显示。
            try:
                os.environ["AUTODL_APP_ROOT"] = str(root)  # 给 frozen GUI 和后台任务同一配置根目录。
                os.chdir(root)  # 双击入口时工作目录不固定，统一到项目根目录。
                if not getattr(sys, "frozen", False):
                    sys.path.insert(0, str(root / "src"))  # 优先导入本仓库源码，避免运行环境中曾安装的旧版本。
                from autodl_watcher.gui import main as run_gui
                run_gui()
            except Exception:
                traceback.print_exc()  # 保留完整异常调用链，弹窗只显示简短错误和日志位置。
                raise
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(None, f"启动失败：{exc}\n详细信息：{log_path}", "AutoDL GPU Watcher", 16)
    finally:
        sys.stdout, sys.stderr = original_streams


if __name__ == "__main__":
    main()
