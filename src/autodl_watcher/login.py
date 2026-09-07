"""
AutoDL 登录初始化与会话恢复。

v0.5.0 的核心原则：
    - 人工登录/验证码永远使用真正的普通 Microsoft Edge；
    - 登录浏览器使用独立的 ``login_profile``，不再直接复用 Playwright 的
      ``browser_profile``，避免 ``--no-sandbox`` / ``remote-debugging-pipe``
      等自动化启动参数污染登录环境；
    - 登录完成后，把干净登录配置同步到监控用 ``browser_profile``；
    - 同步前后都会清理占用这两个专用 profile 的残留 Edge 进程，避免 profile 锁。

验证码仍需用户本人完成；本模块不会自动绕过网站安全验证。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from .config import AppConfig, load_config


_AUTOMATION_FLAGS = ("--no-sandbox", "--remote-debugging-pipe", "--headless")
_PROFILE_IGNORE_NAMES = {
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
    "lockfile",
}


def edge_executable_candidates() -> tuple[Path, ...]:
    """返回 Windows 上常见的 Microsoft Edge 可执行文件候选路径。"""
    candidates: list[Path] = []
    path_hit = shutil.which("msedge")
    if path_hit:
        candidates.append(Path(path_hit))

    for env_name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if base:
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")

    unique: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item).lower()
        if key not in seen:
            unique.append(item)
            seen.add(key)
    return tuple(unique)


def find_edge_executable() -> Path | None:
    """查找系统安装的 Microsoft Edge；找不到时返回 None。"""
    for candidate in edge_executable_candidates():
        if candidate.is_file():
            return candidate
    return None


def login_profile_dir(browser_profile: Path) -> Path:
    """返回与监控 profile 同级的“纯人工登录 profile”目录。"""
    return browser_profile.parent / "login_profile"


def _ps_quote(value: str) -> str:
    """PowerShell 单引号字符串转义。"""
    return value.replace("'", "''")


def _edge_process_query_script(user_data_dir: Path, *, kill: bool) -> str:
    """构造只匹配指定 user-data-dir 的 Edge 进程查询/清理脚本。"""
    marker = _ps_quote(str(user_data_dir))
    action = (
        "ForEach-Object { Stop-Process -Id $PSItem.ProcessId -Force "
        "-ErrorAction SilentlyContinue }"
        if kill
        else "Select-Object -ExpandProperty CommandLine"
    )
    return (
        "$p='" + marker + "'; "
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { $PSItem.Name -eq 'msedge.exe' -and "
        "$PSItem.CommandLine -and $PSItem.CommandLine.Contains($p) } | "
        + action
    )


def edge_process_command_lines(user_data_dir: Path) -> list[str]:
    """读取使用指定 profile 的 Edge 进程命令行；非 Windows 返回空列表。"""
    if os.name != "nt":
        return []
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _edge_process_query_script(user_data_dir, kill=False),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def terminate_profile_edge_processes(user_data_dir: Path) -> None:
    """静默关闭仅使用指定专用 profile 的 Edge 进程。"""
    if os.name != "nt":
        return
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _edge_process_query_script(user_data_dir, kill=True),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def automation_flags_present(user_data_dir: Path) -> bool:
    """判断指定 profile 的 Edge 是否混入 Playwright/自动化启动参数。"""
    lines = edge_process_command_lines(user_data_dir)
    return any(flag in line for line in lines for flag in _AUTOMATION_FLAGS)


def launch_native_edge(edge_path: Path, user_data_dir: Path, page_url: str) -> subprocess.Popen[bytes]:
    """启动不受 Playwright 控制的普通 Edge。"""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(edge_path),
        f"--user-data-dir={user_data_dir}",
        "--new-window",
        page_url,
    ]
    return subprocess.Popen(command)


def _ignore_profile_copy(_directory: str, names: list[str]) -> set[str]:
    """复制 profile 时忽略锁文件；缓存保留与否不影响登录态。"""
    ignored = set()
    for name in names:
        if name in _PROFILE_IGNORE_NAMES or name.startswith("Singleton"):
            ignored.add(name)
    return ignored


def sync_login_profile_to_browser(login_profile: Path, browser_profile: Path) -> None:
    """把纯人工登录 profile 同步为监控使用的 browser_profile。"""
    if not login_profile.exists():
        raise RuntimeError(f"登录 profile 不存在：{login_profile}")

    terminate_profile_edge_processes(login_profile)
    terminate_profile_edge_processes(browser_profile)
    time.sleep(1.0)

    if browser_profile.exists():
        shutil.rmtree(browser_profile)
    shutil.copytree(
        login_profile,
        browser_profile,
        ignore=_ignore_profile_copy,
    )


def _notify_login_required() -> None:
    """Windows 下播放提示音；失败不影响登录流程。"""
    if os.name != "nt":
        return
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        time.sleep(0.15)
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        return


def interactive_login(config: AppConfig | None = None, *, reason: str = "manual") -> None:
    """用纯人工 Edge 完成登录，并把会话同步给监控 profile。"""
    config = config or load_config()
    browser_profile = config.platform.user_data_dir
    clean_profile = login_profile_dir(browser_profile)
    browser_profile.parent.mkdir(parents=True, exist_ok=True)

    edge_path = find_edge_executable()
    if edge_path is None:
        raise RuntimeError(
            "未找到系统 Microsoft Edge。v0.5.0 在 Windows 上不再回退到 Playwright "
            "做人工验证码登录，请先安装/修复 Edge。"
        )

    # 最关键的 v0.5.0 修复：登录前先杀掉两个专用 profile 的所有残留 Edge，
    # 然后只用从未被 Playwright 控制的 login_profile 打开普通 Edge。
    terminate_profile_edge_processes(browser_profile)
    terminate_profile_edge_processes(clean_profile)
    time.sleep(1.0)

    if reason == "expired":
        _notify_login_required()
        print("\n检测到 AutoDL 登录会话失效，已自动打开普通 Microsoft Edge。")
    else:
        print("正在打开普通 Microsoft Edge 登录窗口。")

    print("该窗口使用独立 login_profile，不带 Playwright 的 --no-sandbox / headless 参数。")
    print("完成 AutoDL 登录并确认能看到“所有主机”后，关闭这个 Edge 窗口。")
    print("然后回到此终端按 Enter；验证码仍需手动完成。")

    launch_native_edge(edge_path, clean_profile, config.platform.page_url)
    time.sleep(1.5)

    if automation_flags_present(clean_profile):
        # 理论上不会发生；若系统复用了污染进程，则强制清理并再启动一次。
        terminate_profile_edge_processes(clean_profile)
        time.sleep(1.0)
        launch_native_edge(edge_path, clean_profile, config.platform.page_url)
        time.sleep(1.5)
        if automation_flags_present(clean_profile):
            terminate_profile_edge_processes(clean_profile)
            raise RuntimeError(
                "普通 Edge 仍检测到自动化启动参数。请关闭所有 watcher/Edge 后重试。"
            )

    input("\n登录完成并关闭 Edge 后按 Enter：")
    terminate_profile_edge_processes(clean_profile)
    time.sleep(1.0)
    sync_login_profile_to_browser(clean_profile, browser_profile)
    print("登录会话已同步到 runtime/browser_profile，监控可继续使用。")


def main() -> None:
    interactive_login(reason="manual")


if __name__ == "__main__":
    main()
