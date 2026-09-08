"""Opt-in real Windows common-dialog test; never interacts with another PID.

Run with AEVUM_NATIVE_TEST=1 on an interactive Windows desktop.
"""
import os
import sys
import threading
import time
import pytest
from aevum_cookies import PICKER_TEXT, select_cookie_file, validate_cookies

pytestmark = pytest.mark.skipif(
    sys.platform != 'win32' or os.environ.get('AEVUM_NATIVE_TEST') != '1',
    reason='opt-in Windows desktop integration')


def own_dialog(expected_title):
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    found = []
    @callback_type
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            title = ctypes.create_unicode_buffer(256)
            api.GetWindowTextW(hwnd, title, len(title))
            if title.value == expected_title:
                found.append(hwnd)
        return True
    api.EnumWindows(visit, 0)
    return (api, found[0]) if found else (api, None)


@pytest.mark.parametrize('cancel', [False, True])
@pytest.mark.parametrize('language', list(PICKER_TEXT))
def test_real_native_picker_unicode_selection_and_cancel(tmp_path, cancel, language):
    import ctypes
    from ctypes import wintypes
    directory = tmp_path / 'Profil 2 Türkçe'
    directory.mkdir()
    selected = directory / 'çerezler.txt'
    raw = b'# Netscape HTTP Cookie File\n.example.test\tTRUE\t/\tTRUE\t0\tauth\tsynthetic-only\n'
    selected.write_bytes(raw)
    answer = []
    thread = threading.Thread(target=lambda: answer.append(select_cookie_file(str(directory), language)), daemon=True)
    thread.start()
    hwnd = None
    for _ in range(100):
        api, hwnd = own_dialog('Aevum - ' + PICKER_TEXT[language][0])
        if hwnd:
            break
        time.sleep(.1)
    assert hwnd, 'Native common dialog did not open'
    api.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    try:
        if not cancel:
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            api.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
            api.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            api.GetDlgCtrlID.argtypes = [wintypes.HWND]
            edits = []
            @callback_type
            def child(control, _):
                cls = ctypes.create_unicode_buffer(64)
                api.GetClassNameW(control, cls, len(cls))
                if cls.value == 'Edit':
                    edits.append((control, api.GetDlgCtrlID(control)))
                return True
            api.EnumChildWindows(hwnd, child, 0)
            # edt1 on the classic dialog; edit 1001 inside the Explorer combo.
            edit = next((h for h, ident in edits if ident in (1148, 1152, 1001)), None)
            assert edit, ('Filename edit not found', [ident for _, ident in edits])
            api.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            text = ctypes.create_unicode_buffer(str(selected))
            api.SendMessageW(edit, 0x000C, 0, ctypes.cast(text, ctypes.c_void_p).value)
        api.PostMessageW(hwnd, 0x0111, 2 if cancel else 1, 0)
        thread.join(10)
        assert not thread.is_alive(), 'Native dialog did not complete'
        assert answer == [None if cancel else str(selected)]
        if not cancel:
            assert validate_cookies(selected.read_bytes())
    finally:
        if thread.is_alive():
            api.PostMessageW(hwnd, 0x0111, 2, 0)
            thread.join(3)
