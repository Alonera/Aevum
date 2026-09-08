"""Session-only Netscape cookie imports; never opens a browser database."""
import io
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.cookiejar import MozillaCookieJar
from pathlib import Path

BROWSERS = frozenset(('chrome', 'edge', 'firefox', 'brave', 'zen'))
MAX_COOKIE_BYTES = 2 * 1024 * 1024
PICKER_TEXT = {
    'en': ('Select Netscape cookies.txt', 'All files'),
    'tr': ('Netscape cookies.txt seç', 'Tüm dosyalar'),
    'es': ('Seleccionar Netscape cookies.txt', 'Todos los archivos'),
    'de': ('Netscape cookies.txt auswählen', 'Alle Dateien'),
    'fr': ('Choisir Netscape cookies.txt', 'Tous les fichiers'),
    'it': ('Seleziona Netscape cookies.txt', 'Tutti i file'),
    'pt': ('Selecionar Netscape cookies.txt', 'Todos os arquivos'),
    'ru': ('Выбрать Netscape cookies.txt', 'Все файлы'),
}


class CookieError(ValueError):
    """Only fixed, public error codes may cross the API boundary."""


def validate_cookies(raw):
    if len(raw) > MAX_COOKIE_BYTES:
        raise CookieError('cookieTooLarge')
    if raw.startswith(b'SQLite format 3'):
        raise CookieError('cookieDatabase')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError:
        raise CookieError('cookieInvalid') from None
    lines = text.splitlines()
    if not lines or lines[0].strip() not in ('# Netscape HTTP Cookie File', '# HTTP Cookie File'):
        raise CookieError('cookieInvalid')
    normalized = ['# Netscape HTTP Cookie File']
    usable = 0
    for line in lines[1:]:
        if not line.strip() or (line.startswith('#') and not line.startswith('#HttpOnly_')):
            continue
        if any(ord(c) < 32 and c != '\t' for c in line):
            raise CookieError('cookieInvalid')
        fields = line.split('\t')
        if len(fields) != 7:
            raise CookieError('cookieInvalid')
        domain, include, path, secure, expires, name, value = fields
        host = domain.removeprefix('#HttpOnly_')
        if (not host or any(c.isspace() for c in host) or '/' in host or ':' in host
                or include not in ('TRUE', 'FALSE') or secure not in ('TRUE', 'FALSE')
                or host.startswith('.') != (include == 'TRUE') or not path.startswith('/')
                or (expires and not re.fullmatch(r'[0-9]{1,12}', expires))
                or not (name or value)):
            raise CookieError('cookieInvalid')
        # yt-dlp treats expiry 0 as a session cookie; MozillaCookieJar does not.
        fields[4] = '' if expires == '0' else expires
        if not fields[4] or int(fields[4]) > time.time():
            usable += 1
        normalized.append('\t'.join(fields))
    if len(normalized) == 1:
        raise CookieError('cookieEmpty')
    if not usable:
        raise CookieError('cookieExpired')
    canonical = '\n'.join(normalized) + '\n'
    # Validate only after strict field checks: cookiejar's parser can otherwise
    # include a bad cookie line in warnings/errors. No input reaches diagnostics.
    jar = MozillaCookieJar()
    try:
        jar._really_load(io.StringIO(canonical), '<session>', True, True)
    except Exception:
        raise CookieError('cookieInvalid') from None
    return canonical.encode('utf-8')


class SessionCookies:
    def __init__(self):
        self._lock = threading.RLock()
        self._imports = {}
        self._temporary = set()
        self._closed = False

    def load(self, browser, raw):
        if browser not in BROWSERS:
            raise CookieError('cookieChooseBrowser')
        canonical = validate_cookies(raw)
        with self._lock:
            if self._closed:
                raise CookieError('appClosed')
            if len(self._imports) >= 32:
                raise CookieError('cookieLimit')
            token = secrets.token_urlsafe(24)
            self._imports[token] = (browser, canonical)
            return token

    def snapshot(self, browser, token=''):
        if browser not in BROWSERS and browser != 'none':
            raise CookieError('cookieChooseBrowser')
        with self._lock:
            if self._closed:
                raise CookieError('appClosed')
            if not token:
                return None
            record = self._imports.get(token)
            if not record or record[0] != browser:
                raise CookieError('cookieStale')
            # A queued job keeps this immutable snapshot even if the selection
            # changes. Only jobs already accepted can retain a removed import.
            return record[1]

    def remove(self, token):
        with self._lock:
            self._imports.pop(token, None)

    @contextmanager
    def file(self, content):
        if content is None:
            yield ''
            return
        with self._lock:
            if self._closed:
                raise CookieError('appClosed')
            tmp = tempfile.TemporaryDirectory(prefix='aevum-cookies-')
            self._temporary.add(tmp)
            try:
                path = os.path.join(tmp.name, 'cookies.txt')
                # Each subprocess gets a separate jar: yt-dlp writes updates
                # back on exit, so concurrent probe/download cannot share it.
                with open(path, 'xb') as f:
                    os.chmod(path, 0o600)
                    f.write(content.replace(b'\n', os.linesep.encode()))
            except Exception:
                tmp.cleanup()
                self._temporary.discard(tmp)
                raise
        try:
            yield path
        finally:
            with self._lock:
                try:
                    tmp.cleanup()
                    self._temporary.discard(tmp)
                except OSError:
                    pass  # retry at process shutdown

    def cleanup(self):
        with self._lock:
            self._closed = True
            self._imports.clear()
            for tmp in list(self._temporary):
                try:
                    tmp.cleanup()
                    self._temporary.discard(tmp)
                except OSError:
                    pass


def zen_profile_root(environ=None, platform=None):
    """Find Zen's own profile root; never fall back to Firefox's account data.

    yt-dlp supports Firefox forks through firefox:<absolute profile directory>,
    not a browser named 'zen'. It handles the databases/profiles inside this root.
    Only directory existence is inspected here, never cookie/key contents.
    """
    env = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    user_home = Path(env.get('USERPROFILE') or env.get('HOME') or str(Path.home()))
    if platform == 'win32':
        roaming = Path(env.get('APPDATA') or user_home / 'AppData' / 'Roaming')
        candidates = [roaming / name / 'Profiles' for name in ('zen', 'ZenBrowser')]
    elif platform == 'darwin':
        support = user_home / 'Library' / 'Application Support'
        candidates = [support / name / 'Profiles' for name in ('zen', 'ZenBrowser')]
    else:
        config = Path(env.get('XDG_CONFIG_HOME') or user_home / '.config')
        candidates = [user_home / '.zen', config / 'zen']
        for app_id in ('app.zen_browser.zen', 'io.github.zen_browser.zen'):
            base = user_home / '.var' / 'app' / app_id
            candidates.extend((base / 'zen', base / '.zen', base / 'config' / 'zen'))
    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate.absolute()
        except OSError:
            continue
    # Keep a missing Zen root explicit so yt-dlp reports the missing profile.
    # Passing plain 'firefox' here would silently use the wrong browser's login.
    return candidates[0].absolute()


def browser_cookie_source(browser):
    return 'firefox:' + str(zen_profile_root()) if browser == 'zen' else browser


def browser_directory(browser, environ=None):
    """Windows profile roots; directory names only, no cookie/key inspection."""
    env = os.environ if environ is None else environ
    if browser not in BROWSERS:
        return None
    if browser == 'zen':
        root = zen_profile_root(env)
    elif browser == 'firefox':
        base = env.get('APPDATA')
        root = Path(base) / 'Mozilla' / 'Firefox' / 'Profiles' if base else None
    else:
        base = env.get('LOCALAPPDATA')
        relative = {'chrome': ('Google', 'Chrome', 'User Data'),
                    'edge': ('Microsoft', 'Edge', 'User Data'),
                    'brave': ('BraveSoftware', 'Brave-Browser', 'User Data')}[browser]
        root = Path(base).joinpath(*relative) if base else None
    try:
        if root is None or not root.is_dir():
            return None
        profiles = [p for p in root.iterdir() if p.is_dir() and
                    (browser in ('firefox', 'zen') or p.name == 'Default' or p.name.startswith('Profile '))]
        # Multiple profiles: show their common root so users choose their own.
        if len(profiles) == 1:
            network = profiles[0] / 'Network'
            return str(network if network.is_dir() else profiles[0])
        return str(root)
    except OSError:
        return None


def select_cookie_file(initial_directory=None, language='en'):
    """Windows native picker. None = canceled. No tkinter packaging dependency."""
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [('lStructSize', wintypes.DWORD), ('hwndOwner', wintypes.HWND),
                    ('hInstance', wintypes.HINSTANCE), ('lpstrFilter', wintypes.LPCWSTR),
                    ('lpstrCustomFilter', wintypes.LPWSTR), ('nMaxCustFilter', wintypes.DWORD),
                    ('nFilterIndex', wintypes.DWORD), ('lpstrFile', wintypes.LPWSTR),
                    ('nMaxFile', wintypes.DWORD), ('lpstrFileTitle', wintypes.LPWSTR),
                    ('nMaxFileTitle', wintypes.DWORD), ('lpstrInitialDir', wintypes.LPCWSTR),
                    ('lpstrTitle', wintypes.LPCWSTR), ('Flags', wintypes.DWORD),
                    ('nFileOffset', wintypes.WORD), ('nFileExtension', wintypes.WORD),
                    ('lpstrDefExt', wintypes.LPCWSTR), ('lCustData', wintypes.LPARAM),
                    ('lpfnHook', ctypes.c_void_p), ('lpTemplateName', wintypes.LPCWSTR),
                    ('pvReserved', ctypes.c_void_p), ('dwReserved', wintypes.DWORD),
                    ('FlagsEx', wintypes.DWORD)]

    title, all_files = PICKER_TEXT.get(language if isinstance(language, str) else 'en', PICKER_TEXT['en'])
    filename = ctypes.create_unicode_buffer(32768)
    dialog = OPENFILENAMEW()
    dialog.lStructSize = ctypes.sizeof(dialog)
    dialog.lpstrFilter = 'Netscape cookies.txt\0*.txt\0' + all_files + '\0*.*\0\0'
    dialog.lpstrFile = ctypes.cast(filename, wintypes.LPWSTR)
    dialog.nMaxFile = len(filename)
    dialog.lpstrInitialDir = initial_directory
    dialog.lpstrTitle = 'Aevum - ' + title
    dialog.Flags = 0x80000 | 0x1000 | 0x800 | 0x8 | 0x2000000  # no cwd change/recent-file entry
    api = ctypes.WinDLL('comdlg32', use_last_error=True)
    api.GetOpenFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
    api.GetOpenFileNameW.restype = wintypes.BOOL
    if api.GetOpenFileNameW(ctypes.byref(dialog)):
        return filename.value
    if api.CommDlgExtendedError():
        raise OSError('Native file picker unavailable')
    return None
