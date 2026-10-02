"""An off-screen layered tool window whose pixels we set directly (no taskbar button, never
activated, outside every monitor) — something to capture without touching the user's screen."""
import ctypes
import ctypes.wintypes as wt

import numpy as np

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


_proc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
user32.GetDC.restype = wt.HDC
user32.GetDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wt.HWND]
kernel32 = ctypes.windll.kernel32
kernel32.GetModuleHandleW.restype = wt.HMODULE          # a 64-bit handle, not an int
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]


class ProbeWindow:
    def __init__(self, w=640, h=360, x=-4000, y=-4000):
        self.w, self.h, self.x, self.y = w, h, x, y
        cls = WNDCLASSW()
        cls.lpfnWndProc = _proc
        cls.lpszClassName = "MarinCallProbe"
        cls.hInstance = kernel32.GetModuleHandleW(None)
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wt.ATOM
        user32.RegisterClassW(ctypes.byref(cls))             # 0 the second time: already there
        user32.CreateWindowExW.restype = wt.HWND
        user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
        ex = 0x00080000 | 0x00000080 | 0x08000000          # LAYERED | TOOLWINDOW | NOACTIVATE
        self.hwnd = user32.CreateWindowExW(ex, "MarinCallProbe", "MarinCall probe", 0x80000000,  # POPUP
                                           x, y, w, h, None, None, cls.hInstance, None)
        user32.ShowWindow(self.hwnd, 4)                      # SW_SHOWNOACTIVATE
        self.fill((0, 0, 255))

    def fill(self, rgb, size=None):
        if size:
            self.w, self.h = size
        w, h = self.w, self.h
        pixels = np.zeros((h, w, 4), np.uint8)
        pixels[..., 0], pixels[..., 1], pixels[..., 2], pixels[..., 3] = rgb[2], rgb[1], rgb[0], 255
        pixels[:60, :60] = (255, 255, 255, 255)              # a white corner to check orientation
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        gdi32.CreateDIBSection.restype = wt.HBITMAP
        gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                           wt.HANDLE, wt.DWORD]
        bmp = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, pixels.ctypes.data, pixels.nbytes)
        old = gdi32.SelectObject(mem, bmp)
        pos, sz, src = wt.POINT(self.x, self.y), wt.SIZE(w, h), wt.POINT(0, 0)
        blend = BLENDFUNCTION(0, 0, 255, 1)
        user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
                                               wt.HDC, ctypes.POINTER(wt.POINT), wt.COLORREF,
                                               ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
        ok = user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(pos), ctypes.byref(sz), mem,
                                        ctypes.byref(src), 0, ctypes.byref(blend), 2)
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)
        return ok

    def pump(self):
        msg = wt.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def close(self):
        user32.DestroyWindow(self.hwnd)
