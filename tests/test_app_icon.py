import ctypes
from ctypes import wintypes
import gc
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tkinter as tk
import unittest

from PIL import Image, ImageOps
from autodl_watcher.app_icon import IconManager, IconSettings, SIZES, load_icon, save_shortcut


def authored_icon(path, colors=((16, "#ef2030"), (32, "#2050ed"), (256, "#19b560"))):
    frames = [Image.new("RGBA", (size, size), color) for size, color in colors]
    frames[-1].save(path, format="ICO", sizes=[im.size for im in frames], append_images=frames[:-1])
    return frames


def native_icon(root, small=True, handle=None):
    user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
    user.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT];user.GetAncestor.restype=wintypes.HWND
    user.SendMessageW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM];user.SendMessageW.restype=ctypes.c_ssize_t
    user.GetClassLongPtrW.argtypes=[wintypes.HWND,ctypes.c_int];user.GetClassLongPtrW.restype=ctypes.c_size_t
    hwnd=0
    source='Shell resource'
    if handle is None:
        hwnd=user.GetAncestor(root.winfo_id(),2)
        handle=user.SendMessageW(hwnd,0x7f,2 if small else 1,0)
        source='WM_GETICON'
        if not handle:
            handle=user.GetClassLongPtrW(hwnd,-34 if small else -14)
            source='class icon'
        if not handle and small:
            handle=user.GetClassLongPtrW(hwnd,-14)
    assert handle, '窗口及窗口类均无图标'
    class Info(ctypes.Structure):
        _fields_=[('fIcon',wintypes.BOOL),('x',wintypes.DWORD),('y',wintypes.DWORD),('mask',wintypes.HBITMAP),('color',wintypes.HBITMAP)]
    class Bitmap(ctypes.Structure):
        _fields_=[('type',wintypes.LONG),('width',wintypes.LONG),('height',wintypes.LONG),('row',wintypes.LONG),('planes',wintypes.WORD),('bits',wintypes.WORD),('pixels',ctypes.c_void_p)]
    class Header(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('width',wintypes.LONG),('height',wintypes.LONG),('planes',wintypes.WORD),('bits',wintypes.WORD),('compression',wintypes.DWORD),('image_size',wintypes.DWORD),('x',wintypes.LONG),('y',wintypes.LONG),('used',wintypes.DWORD),('important',wintypes.DWORD)]
    user.GetIconInfo.argtypes=[wintypes.HICON,ctypes.POINTER(Info)]
    gdi.GetObjectW.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p]
    gdi.GetDIBits.argtypes=[wintypes.HDC,wintypes.HBITMAP,wintypes.UINT,wintypes.UINT,ctypes.c_void_p,ctypes.c_void_p,wintypes.UINT]
    gdi.CreateCompatibleDC.argtypes=[wintypes.HDC];gdi.CreateCompatibleDC.restype=wintypes.HDC
    gdi.DeleteObject.argtypes=[wintypes.HANDLE];gdi.DeleteDC.argtypes=[wintypes.HDC]
    info=Info();assert user.GetIconInfo(handle,ctypes.byref(info))
    dc=gdi.CreateCompatibleDC(None)
    try:
        bitmap=Bitmap();assert gdi.GetObjectW(info.color,ctypes.sizeof(bitmap),ctypes.byref(bitmap))
        header=Header(ctypes.sizeof(Header),bitmap.width,-bitmap.height,1,32,0,0,0,0,0,0)
        data=ctypes.create_string_buffer(bitmap.width*bitmap.height*4)
        assert gdi.GetDIBits(dc,info.color,0,bitmap.height,data,ctypes.byref(header),0)
        image=Image.frombytes('RGBA',(bitmap.width,bitmap.height),data.raw,'raw','BGRA')
    finally:
        gdi.DeleteDC(dc);gdi.DeleteObject(info.color);gdi.DeleteObject(info.mask)
    return image, {'hwnd':int(hwnd),'handle':int(handle),'source':source,'size':list(image.size)}


def shell_icon(path, small=False):
    class Info(ctypes.Structure):
        _fields_=[('icon',wintypes.HICON),('index',ctypes.c_int),('attributes',wintypes.DWORD),('display',wintypes.WCHAR*260),('type',wintypes.WCHAR*80)]
    shell=ctypes.windll.shell32
    shell.SHGetFileInfoW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(Info),wintypes.UINT,wintypes.UINT]
    shell.SHGetFileInfoW.restype=ctypes.c_size_t
    info=Info();assert shell.SHGetFileInfoW(str(path),0,ctypes.byref(info),ctypes.sizeof(info),0x100 | int(small))
    try:
        return native_icon(None,small,info.icon)
    finally:
        ctypes.windll.user32.DestroyIcon.argtypes=[wintypes.HICON]
        ctypes.windll.user32.DestroyIcon(info.icon)


class IconImportTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()

    def test_ico_keeps_original_bytes_and_authored_small_frames(self):
        path=self.folder/'多帧.ico';original=authored_icon(path)
        data,frames,description=load_icon(path)
        self.assertEqual(data,path.read_bytes())
        by_size={im.size:im for im in frames}
        for frame in original:self.assertEqual(by_size[frame.size].tobytes(),frame.tobytes())
        self.assertIn('ICO原样保留',description)

    def test_png_sizes_aspect_alpha_and_direct_source_conversion(self):
        source=Image.new('RGBA',(400,180))
        for x in range(400):
            for y in range(180):source.putpixel((x,y),(x%256,y%256,(x+y)%256,128))
        path=self.folder/'source.png';source.save(path)
        data,frames,_=load_icon(path)
        with Image.open(BytesIO(data)) as icon:
            self.assertEqual(icon.ico.sizes(),{(s,s) for s in SIZES})
            for size in SIZES:
                decoded=icon.ico.getimage((size,size)).convert('RGBA')
                fitted=ImageOps.contain(source,(size,size),Image.Resampling.LANCZOS)
                expected=Image.new('RGBA',(size,size));expected.alpha_composite(fitted,((size-fitted.width)//2,(size-fitted.height)//2))
                self.assertEqual(decoded.tobytes(),expected.tobytes())
                self.assertEqual(decoded.getpixel((0,0))[3],0)
                self.assertEqual(decoded.getpixel((size//2,size//2))[3],128)

    def test_bad_or_other_format_is_rejected(self):
        path=self.folder/'bad.ico';path.write_bytes(b'not an icon')
        with self.assertRaises(OSError):load_icon(path)
        path=self.folder/'other.jpg';Image.new('RGB',(16,16)).save(path)
        with self.assertRaisesRegex(ValueError,'ICO或PNG'):load_icon(path)

    @unittest.skipUnless(os.name=='nt','Windows快捷方式')
    def test_shortcut_reads_back_target_and_icon(self):
        exe=self.folder/'带空格 app.exe';exe.write_bytes(b'test only; never execute')
        icon=self.folder/'author.ico';authored_icon(icon)
        link=self.folder/'测试入口.lnk';save_shortcut(link,exe,icon)
        env=os.environ.copy();env['AUTODL_LINK']=str(link)
        command="$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:AUTODL_LINK); [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); @{target=$s.TargetPath;icon=$s.IconLocation;working=$s.WorkingDirectory}|ConvertTo-Json -Compress"
        result=subprocess.run(['powershell.exe','-NoProfile','-Command',command],env=env,capture_output=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW,check=True)
        value=json.loads(result.stdout)
        self.assertEqual(value['target'],str(exe));self.assertEqual(value['icon'],str(icon)+',0');self.assertEqual(value['working'],str(self.folder))
        for small,color in ((True,(239,32,48)),(False,(32,80,237))):
            image,meta=shell_icon(link,small)
            self.assertEqual(image.getpixel((image.width//2,image.height//2))[:3],color,meta)


@unittest.skipUnless(os.name=='nt','Windows原生窗口图标')
class WindowIconTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
        self.root=tk.Tk();self.root.withdraw()
        self.manager=IconManager(self.root,self.folder/'.ui')
        self.path=self.folder/'author.ico';authored_icon(self.path)
    def tearDown(self):
        self.root.destroy();self.temp.cleanup()

    def assert_loaded(self,root,small_color=(239,32,48),large_color=(32,80,237)):
        root.update_idletasks()
        for small,color in ((True,small_color),(False,large_color)):
            image,meta=native_icon(root,small)
            self.assertEqual(image.getpixel((image.width//2,image.height//2))[:3],color,meta)

    def test_live_replace_lifetime_and_new_dialog(self):
        self.manager.import_file(self.path);self.root.deiconify();self.root.update()
        gc.collect();self.assert_loaded(self.root)
        self.assertEqual(self.manager.path.read_bytes(),self.path.read_bytes())
        dialog=IconSettings(self.manager);self.root.update();self.assert_loaded(dialog)
        replacement=self.folder/'replacement.ico';authored_icon(replacement,((16,'#fed010'),(32,'#ae1080'),(256,'#30c090')))
        self.manager.import_file(replacement);gc.collect();self.root.update()
        self.assert_loaded(self.root,(254,208,16),(174,16,128));self.assert_loaded(dialog,(254,208,16),(174,16,128))
        dialog.destroy()

    def test_saved_icon_on_first_map_and_default_reset(self):
        self.manager.import_file(self.path)
        other=tk.Toplevel(self.root);other.withdraw()
        restored=IconManager(other,self.folder/'.ui');other.deiconify();self.root.update()
        self.assert_loaded(other)
        restored.reset();self.root.update()
        image,_=native_icon(other,True)
        self.assertNotEqual(image.getpixel((image.width//2,image.height//2))[:3],(239,32,48))
        self.assertIsNone(json.loads(restored.config.read_text())['file'])
        other.destroy()

    def test_invalid_import_keeps_current_icon_and_preference(self):
        self.manager.import_file(self.path)
        before=self.manager.config.read_bytes();current=self.manager.path
        bad=self.folder/'bad.ico';bad.write_bytes(b'bad')
        with self.assertRaises(OSError):self.manager.import_file(bad)
        self.assertEqual(self.manager.path,current);self.assertEqual(self.manager.config.read_bytes(),before)
        self.assert_loaded(self.root)

    def test_missing_saved_file_does_not_break_startup(self):
        self.manager.folder.mkdir();self.manager.config.write_text('{"file":"missing.ico"}')
        other=IconManager(self.root,self.manager.folder)
        self.assertIn('无法加载',other.message);self.assertIsNone(other.path)


if __name__=='__main__':unittest.main()
