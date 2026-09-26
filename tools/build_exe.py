from pathlib import Path
import os
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main():  # 只收集应用代码和依赖，绝不把 runtime、.ui 或 .env 加入发行包。
    folder = ROOT / ".tools" / "build"
    folder.mkdir(parents=True, exist_ok=True)
    spec = folder / "AutoDLWatcher.spec"
    library_bin = Path(sys.prefix) / "Library" / "bin"
    binaries = [(str(library_bin / name), ".") for name in
                ("libssl-3-x64.dll", "libcrypto-3-x64.dll") if (library_bin / name).is_file()]
    env = os.environ.copy()
    if library_bin.is_dir():
        env["PATH"] = str(library_bin) + os.pathsep + env.get("PATH", "")  # Conda DLL 必须与打包解释器一致。
    spec.write_text(f"""a = Analysis(
    [{str(ROOT / 'tools/gui_entry.py')!r}, {str(ROOT / 'tools/worker_entry.py')!r}],
    pathex=[{str(ROOT / 'src')!r}], binaries={binaries!r}, datas=[], hiddenimports=[],
    hookspath=[], hooksconfig={{}}, runtime_hooks=[], excludes=[])
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts[:-2] + [a.scripts[-2]], [], exclude_binaries=True,
    name='AutoDLWatcher', debug=False, strip=False, upx=False, console=False,
    contents_directory='_internal')
worker = EXE(pyz, a.scripts[:-2] + [a.scripts[-1]], [], exclude_binaries=True,
    name='AutoDLWorker', debug=False, strip=False, upx=False, console=True,
    contents_directory='.')
COLLECT(gui, a.binaries, a.datas, [('AutoDLWorker.exe', worker.name, 'BINARY')],
    strip=False, upx=False, name='AutoDLWatcher')
""", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm",
                    "--distpath", str(folder / "release"), "--workpath", str(folder / "work"),
                    str(spec)], cwd=ROOT, env=env, check=True)
    release = ROOT  # 主程序直接放项目根目录，沿用已有配置和运行资料。
    staged = folder / "release" / "AutoDLWatcher"
    shutil.copyfile(staged / "AutoDLWatcher.exe", release / "AutoDLWatcher.exe")
    shutil.copytree(staged / "_internal", release / "_internal", dirs_exist_ok=True)  # 只合并生成依赖，保留用户配置、登录资料与日志。
    print(f"GUI: {release / 'AutoDLWatcher.exe'}")
    print(f"Worker: {release / '_internal' / 'AutoDLWorker.exe'}")


if __name__ == "__main__":
    main()
