"""A notification-area icon, with nothing but Windows' own API.

Its own thread runs a tiny hidden window and a message loop, because
Shell_NotifyIcon talks to a window procedure and Tk's is not ours to
subclass. Clicks and menu choices come back through `on_event(name)`, which
the application turns into a queue message for the Tk thread. Balloons are
the system's own notifications - they appear over a full-screen game the
way any other does, and Windows' focus assist decides when.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes as w

WM_USER = 0x0400
WM_TRAY = WM_USER + 20
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
NIN_BALLOONUSERCLICK = WM_USER + 5
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO, NIIF_WARNING, NIIF_USER = 0x1, 0x2, 0x4
MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x2, 0x100
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", w.HICON)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
                ("lpszClassName", w.LPCWSTR)]


class Tray:
    MENU = ((1, "open DLSS 5 Autopilot"), (0, ""), (2, "stop watching and quit"))

    def __init__(self, tip: str, icon_path: str, on_event):
        self.tip, self.icon_path, self.on_event = tip, icon_path, on_event
        self.hwnd = None
        self._ready = threading.Event()
        self._thread = None

    def show(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._thread = threading.Thread(target=self._run, name="dlss5-tray", daemon=True)
        self._thread.start()
        return self._ready.wait(3.0) and self.hwnd is not None

    def hide(self) -> None:
        if self.hwnd:
            u = ctypes.WinDLL("user32")
            u.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
            u.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def notify(self, title: str, text: str, warn: bool = False) -> None:
        if not self.hwnd:
            return
        nid = self._nid(NIF_INFO)
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = NIIF_WARNING if warn else NIIF_INFO
        sh = ctypes.WinDLL("shell32")
        sh.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        sh.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _nid(self, flags):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        return nid

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32")
        shell32 = ctypes.WinDLL("shell32")
        kernel32 = ctypes.WinDLL("kernel32")
        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        user32.CreateWindowExW.restype = w.HWND
        user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
        user32.LoadImageW.restype = w.HANDLE
        user32.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
        user32.CreatePopupMenu.restype = w.HMENU
        user32.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND,
                                          w.LPVOID]
        user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
        user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
        user32.DestroyMenu.argtypes = [w.HMENU]
        user32.SetForegroundWindow.argtypes = [w.HWND]
        user32.DestroyWindow.argtypes = [w.HWND]
        user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
        user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        shell32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        kernel32.GetModuleHandleW.restype = w.HMODULE

        def proc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                ev = lparam & 0xFFFF
                if ev in (WM_LBUTTONUP, NIN_BALLOONUSERCLICK):
                    self.on_event("open")
                elif ev == WM_RBUTTONUP:
                    self._menu(user32, hwnd)
                return 0
            if msg == WM_CLOSE:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(proc)         # kept alive as long as the window
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = "dlss5AutopilotTray"
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "dlss5 tray", 0, 0, 0, 0, 0, None, None, hinst,
                                           None)
        if not self.hwnd:
            self._ready.set()
            return
        icon = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = icon
        nid.szTip = self.tip[:127]
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._ready.set()
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self.hwnd = None

    def _menu(self, user32, hwnd):
        menu = user32.CreatePopupMenu()
        for cid, label in self.MENU:
            if cid == 0:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            else:
                user32.AppendMenuW(menu, MF_STRING, cid, label)
        pt = w.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)
        chosen = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None)
        user32.DestroyMenu(menu)
        if chosen == 1:
            self.on_event("open")
        elif chosen == 2:
            self.on_event("quit")
