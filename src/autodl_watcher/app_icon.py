"""图标导入、Tk加载与用户入口分别处理；ICO原件不重新编码。"""
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import tkinter as tk
from tkinter import filedialog, ttk
import uuid

from PIL import Image, ImageOps, ImageTk

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def load_icon(path):
    data = Path(path).read_bytes()
    with Image.open(BytesIO(data)) as source:
        if source.format == "ICO":
            frames = [source.ico.frame(i).convert("RGBA") for i in range(len(source.ico.entry))]
            return data, frames, "ICO原样保留 · " + "、".join(f"{f.width}×{f.height}" for f in frames)
        if source.format != "PNG":
            raise ValueError("请选择ICO或PNG图标")
        original = source.convert("RGBA")
    frames = []
    for size in SIZES:
        # 每档直接从原图生成，居中透明补边，不裁切、不拉伸。
        fitted = ImageOps.contain(original, (size, size), Image.Resampling.LANCZOS)
        frame = Image.new("RGBA", (size, size))
        frame.alpha_composite(fitted, ((size - fitted.width) // 2, (size - fitted.height) // 2))
        frames.append(frame)
    output = BytesIO()
    frames[-1].save(output, format="ICO", sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    return output.getvalue(), frames, f"PNG原图 {original.width}×{original.height} · 已生成 " + "、".join(map(str, SIZES))


def save_shortcut(target, executable, icon):
    """另存用户选择的快捷方式，不覆盖其它入口或修改任务栏固定项。"""
    if os.name != "nt":
        raise ValueError("启动快捷方式仅支持Windows")
    if not executable.is_file():
        raise ValueError("未找到已打包的AutoDLWatcher.exe")
    env = os.environ.copy()
    env.update(AUTODL_LINK=str(target), AUTODL_EXE=str(executable), AUTODL_ICON=str(icon))
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = "$ErrorActionPreference='Stop'; $w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut($env:AUTODL_LINK); $s.TargetPath=$env:AUTODL_EXE; $s.WorkingDirectory=Split-Path -LiteralPath $env:AUTODL_EXE; $s.IconLocation=$env:AUTODL_ICON+',0'; $s.Save()"
    result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command", command], env=env,
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    if result.returncode:
        raise OSError("快捷方式创建失败，请检查保存目录")


class IconManager:
    def __init__(self, root, folder):
        self.root, self.folder = root, folder
        self.config = folder / "icon.json"
        self.photos, self.frames = [], []
        self.path, self.message = None, "使用系统默认图标"
        try:
            if self.config.exists():
                name = json.loads(self.config.read_text(encoding="utf-8")).get("file")
                if name:
                    self.path = folder / name
                    _, frames, self.message = load_icon(self.path)
                    self.apply(frames)
        except (OSError, ValueError, tk.TclError) as exc:
            self.path = None
            self.message = f"已保存图标无法加载，使用默认图标：{exc}"
        self.root.bind_all("<Map>", self._mapped, add="+")

    def windows(self):
        def visit(widget):
            for child in widget.winfo_children():
                if isinstance(child, tk.Toplevel):
                    yield child
                yield from visit(child)
        return [self.root, *visit(self.root)]

    def _mapped(self, event):
        if self.photos and isinstance(event.widget, tk.Toplevel):
            event.widget.iconphoto(False, *self.photos)

    def apply(self, frames):
        photos = [ImageTk.PhotoImage(frame, master=self.root) for frame in frames]
        for window in self.windows():
            window.iconphoto(False, *photos)
        self.photos, self.frames = photos, frames  # 保留引用，运行中换图及后续弹窗均不丢图。

    def import_file(self, path):
        data, frames, description = load_icon(path)
        self.folder.mkdir(parents=True, exist_ok=True)
        icons = self.folder / "icons"
        icons.mkdir(exist_ok=True)
        destination = icons / (uuid.uuid4().hex + ".ico")
        destination.write_bytes(data)  # 独立路径避开旧快捷方式图标缓存，保留原件全部帧。
        previous = self.frames
        try:
            self.apply(frames)
            pending = self.config.with_suffix(".tmp")
            pending.write_text(json.dumps({"file": destination.relative_to(self.folder).as_posix()}, ensure_ascii=False), encoding="utf-8")
            pending.replace(self.config)
        except (OSError, tk.TclError):
            if previous:
                self.apply(previous)
            else:
                for window in self.windows():
                    window.iconbitmap("")
                self.photos, self.frames = [], []
            raise
        self.path, self.message = destination, description

    def reset(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        self.config.write_text('{"file": null}', encoding="utf-8")
        for window in self.windows():
            window.iconbitmap("")
        self.photos, self.frames = [], []
        self.path, self.message = None, "已恢复系统默认图标"

    def configure(self):
        return IconSettings(self)


class IconSettings(tk.Toplevel):
    def __init__(self, manager):
        super().__init__(manager.root)
        self.manager = manager
        self.title("应用图标 · 所有服务器共用")
        self.geometry("640x420")
        self.minsize(600, 390)
        ttk.Label(self, text="选择ICO或PNG，保存后立即设置窗口图标，下次启动自动恢复。", wraplength=590).pack(anchor="w", padx=12, pady=12)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=12)
        ttk.Button(bar, text="选择并应用图标", command=self.choose).pack(side="left")
        ttk.Button(bar, text="恢复默认", command=self.reset).pack(side="left", padx=6)
        ttk.Button(bar, text="另存启动快捷方式", command=self.shortcut).pack(side="right")
        self.description = ttk.Label(self, wraplength=600)
        self.description.pack(fill="x", padx=12, pady=10)
        self.preview = ttk.Frame(self)
        self.preview.pack(fill="x", padx=12, pady=6)
        ttk.Label(self, text="上方预览不代表Windows最终显示效果。\n窗口标题栏和运行中任务栏使用窗口图标；已固定的旧入口可能仍使用旧图标。\n磁盘EXE图标由打包资源决定，需重新打包；本设置不会改写运行中的EXE。\n快捷方式使用当前图标，更换后请重新导出；固定旧入口需重新固定新快捷方式。", wraplength=600).pack(fill="x", padx=12, pady=10)
        self.status = ttk.Label(self, wraplength=600)
        self.status.pack(fill="x", padx=12, pady=6)
        self.render()

    def render(self):
        self.description.configure(text=self.manager.message)
        for widget in self.preview.winfo_children():
            widget.destroy()
        self.previews = []
        for frame in self.manager.frames:
            if frame.width > 64:
                continue
            image = ImageTk.PhotoImage(frame, master=self)
            self.previews.append(image)
            label = ttk.Label(self.preview, image=image, text=f"{frame.width}×{frame.height}", compound="top")
            label.pack(side="left", padx=5)

    def choose(self):
        path = filedialog.askopenfilename(parent=self, title="选择应用图标", filetypes=[("应用图标", "*.ico *.png")])
        if not path:
            return
        try:
            self.manager.import_file(path)
            self.render()
            self.status.configure(text="已保存并设置窗口图标；请查看标题栏和任务栏实际显示。", foreground="#245c45")
        except (OSError, ValueError, tk.TclError) as exc:
            self.status.configure(text=f"图标未应用：{exc}", foreground="#b42318")

    def reset(self):
        try:
            self.manager.reset()
            self.render()
            self.status.configure(text="已恢复默认；已有快捷方式不会被修改。", foreground="#245c45")
        except (OSError, tk.TclError) as exc:
            self.status.configure(text=str(exc), foreground="#b42318")

    def shortcut(self):
        path = filedialog.asksaveasfilename(parent=self, title="另存启动快捷方式", initialfile="AutoDL GPU Watcher.lnk", defaultextension=".lnk", filetypes=[("Windows快捷方式", "*.lnk")])
        if not path:
            return
        try:
            executable = self.manager.folder.parent / "AutoDLWatcher.exe"
            save_shortcut(Path(path), executable, self.manager.path or executable)
            self.status.configure(text="快捷方式已写入；请从所选位置确认图标。", foreground="#245c45")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            self.status.configure(text=str(exc), foreground="#b42318")
