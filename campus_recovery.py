"""Campus network recovery for the saved Dr.COM Portal configuration."""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
import html
from html.parser import HTMLParser
import hashlib
import zlib
import xml.etree.ElementTree as ET
from datetime import timedelta
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
from threading import Thread
import time
from urllib import error, parse, request

FROZEN = getattr(sys, 'frozen', False)
ROOT = Path(sys.executable if FROZEN else __file__).resolve().parent
DATA = Path(os.environ.get('LOCALAPPDATA', str(ROOT))) / 'CampusNetworkRecovery'
SYSTEM32 = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
PORTAL_HOST = '110.188.66.35'
NETWORKS = ('ChinaTelecom-EDU5.8G', 'ChinaTelecom-EDU2.4G')
PROBES = (
    ('http://www.msftconnecttest.com/connecttest.txt', b'Microsoft Connect Test'),
    ('http://www.msftncsi.com/ncsi.txt', b'Microsoft NCSI'),
)
LOG = logging.getLogger('campus')

class CampusError(Exception):
    pass

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

@dataclass
class Reply:
    status: int
    headers: object
    body: bytes



MAX_HTTP_BODY = 262144


def decode_http_body(body, headers):
    encoding = headers.get('Content-Encoding', '').strip().lower()
    if len(body) > MAX_HTTP_BODY:
        raise CampusError('HTTP response exceeds the supported size; no further processing.')
    # The campus server serves precompressed .js even when gzip was not requested.
    if body.startswith(b'\x1f\x8b') and encoding in ('', 'identity'):
        encoding = 'gzip'
    if encoding in ('', 'identity'):
        return body
    if encoding not in ('gzip', 'x-gzip', 'deflate'):
        raise CampusError('Unsupported HTTP response compression; no further processing.')
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding in ('gzip', 'x-gzip') else zlib.MAX_WBITS)
        decoded = decoder.decompress(body, MAX_HTTP_BODY + 1)
        if len(decoded) > MAX_HTTP_BODY or decoder.unconsumed_tail:
            raise CampusError('Decompressed HTTP response exceeds the supported size.')
        if not decoder.eof or decoder.unused_data:
            raise CampusError('Compressed HTTP response is incomplete or has unexpected trailing data.')
        return decoded
    except zlib.error:
        raise CampusError('Cannot decompress HTTP response; retry before attempting login.') from None


def fetch(url, timeout=6):
    # No system/environment proxies; redirects are inspected before following.
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    req = request.Request(url, headers={'User-Agent': 'CampusNetworkRecovery/1.0', 'Cache-Control': 'no-cache', 'Accept-Encoding': 'gzip, deflate'})
    try:
        with opener.open(req, timeout=timeout) as response:
            return Reply(response.status, response.headers, decode_http_body(response.read(MAX_HTTP_BODY + 1), response.headers))
    except error.HTTPError as exc:
        with exc:
            return Reply(exc.code, exc.headers, decode_http_body(exc.read(MAX_HTTP_BODY + 1), exc.headers))
    except (error.URLError, TimeoutError, OSError):
        return None


def campus_url(url):
    try:
        u = parse.urlsplit(url)
        return (u.scheme == 'http' and u.hostname == PORTAL_HOST
                and u.port in (None, 80, 801) and not u.username and not u.password)
    except ValueError:
        return False


def jump_url(reply, current):
    location = reply.headers.get('Location', '') if reply.status in (301, 302, 303, 307, 308) else ''
    text = reply.body.decode('gb18030', errors='replace')
    if not location:
        patterns = (
            r'(?:window\.|top\.|self\.|document\.)?location(?:\.href)?\s*=\s*[\x27\"]([^\x27\"]+)',
            r'location\.(?:replace|assign)\(\s*[\x27\"]([^\x27\"]+)',
            r'url\s*=\s*(https?://[^\s\x27\"<>]+)',
        )
        for pattern in patterns:
            m = re.search(pattern, text, re.I)
            if m:
                location = m.group(1)
                break
    return parse.urljoin(current, html.unescape(location).strip()) if location else None


def connectivity():
    portal = None
    for url, expected in PROBES:
        reply = fetch(url)
        if reply is None:
            continue
        if reply.status == 200 and reply.body.strip() == expected:
            return 'online', None
        jump = jump_url(reply, url)
        if jump and campus_url(jump):
            portal = jump
    return ('portal', portal) if portal else ('uncertain', None)


def run_ps(script):
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    result = run_windows_command(
        [str(SYSTEM32 / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'),
         '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25,
    )
    if result.returncode:
        raise CampusError('Cannot read Windows network state. See the local guide.')
    try:
        return json.loads(result.stdout.decode('utf-8-sig').strip())
    except (ValueError, UnicodeError):
        raise CampusError('Windows returned an unreadable network state.') from None


def run_windows_command(args, **kwargs):
    # System commands must use Windows DLLs, not the bundled runtime's DLLs.
    if FROZEN:
        set_dll_directory = ctypes.WinDLL('kernel32').SetDllDirectoryW
        set_dll_directory.argtypes = [wintypes.LPCWSTR]
        set_dll_directory.restype = wintypes.BOOL
        set_dll_directory(None)
    try:
        return subprocess.run(args, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), **kwargs)
    finally:
        if FROZEN:
            set_dll_directory(sys._MEIPASS)


def network_state():
    return run_ps(r"""
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$wifi=@(Get-NetAdapter -Physical | Where-Object { $_.InterfaceType -eq 71 -or $_.NdisPhysicalMedium -eq 9 })
$profiles=@(Get-NetConnectionProfile -ErrorAction SilentlyContinue)
$campus=$profiles | Where-Object { $_.Name -match '^ChinaTelecom-EDU(5\.8G|2\.4G)( \d+)?$' -and $_.InterfaceIndex -in $wifi.ifIndex } | Select-Object -First 1
$a=$wifi | Where-Object { $_.ifIndex -eq $campus.InterfaceIndex } | Select-Object -First 1
if (-not $a) { $a=$wifi | Where-Object { $_.Status -eq 'Up' } | Select-Object -First 1 }
if (-not $a) { $a=$wifi | Sort-Object @{Expression={ $_.Name -ne 'WLAN' }}, ifIndex | Select-Object -First 1 }
if (-not $a) { [pscustomobject]@{profile='';up=$false;ips=@();mac='';interface=''} | ConvertTo-Json -Compress; exit }
$p=$profiles | Where-Object { $_.InterfaceIndex -eq $a.ifIndex } | Select-Object -First 1
$ips=@(Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {$_.IPAddress -notlike '169.254.*'} | ForEach-Object {$_.IPAddress})
[pscustomobject]@{profile=if($p){$p.Name}else{''};up=($a.Status -eq 'Up');ips=$ips;mac=$a.MacAddress;interface=$a.Name} | ConvertTo-Json -Compress
""")


def is_campus(state):
    name = state.get('profile', '')
    return bool(state.get('up')) and any(name == ssid or re.fullmatch(re.escape(ssid) + r' \d+', name) for ssid in NETWORKS)


def auth_context(url, state):
    if not campus_url(url):
        raise CampusError('Authentication redirect is not an allowed campus server.')
    q = parse.parse_qs(parse.urlsplit(url).query)
    def one(name):
        values = q.get(name, [])
        if len(values) != 1 or not values[0]:
            raise CampusError('Fresh authentication redirect is missing ' + name + '.')
        return values[0]
    ip = one('wlanuserip')
    ac_ip = one('wlanacip')
    try:
        ipaddress.IPv4Address(ip)
        ipaddress.IPv4Address(ac_ip)
    except ValueError:
        raise CampusError('Authentication redirect contains an invalid IPv4 address.') from None
    ips = state.get('ips', [])
    if isinstance(ips, str):
        ips = [ips]
    if ip not in ips:
        raise CampusError('Redirect IP does not match this Wi-Fi connection; reconnect and retry.')
    mac = re.sub('[:-]', '', one('usermac')).lower()
    local_mac = re.sub('[:-]', '', state.get('mac', '')).lower()
    if not re.fullmatch('[0-9a-f]{12}', mac) or mac != local_mac:
        raise CampusError('Redirect MAC does not match this computer.')
    return {'login_method': '1', 'wlan_user_ip': ip, 'wlan_user_ipv6': '',
            'wlan_user_mac': mac, 'wlan_ac_ip': ac_ip,
            'wlan_ac_name': one('wlanacname'), 'jsVersion': '3.1'}



class ConfigScriptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'script':
            src = dict(attrs).get('src', '')
            if parse.urlsplit(src).path.lower().endswith('/a41.js') or parse.urlsplit(src).path.lower() == 'a41.js':
                self.sources.append(src)


def configuration_url(page_body, page_url):
    text = page_body.decode('gb18030', errors='replace')
    parser = ConfigScriptParser()
    parser.feed(text)
    # Some versions insert a41.js with document.write, rather than a literal tag.
    references = parser.sources
    if not references:
        references = re.findall(r'src=[\x27\"]([^\x27\"<>]*a41\.js(?:\?[^\x27\"<>]*)?)[\x27\"]', text, re.I)
    source = references[0] if references else '/a41.js'
    url = parse.urljoin(page_url, html.unescape(source))
    if not campus_url(url):
        raise CampusError('Configuration script points outside the allowed campus server; no login attempted.')
    parts = parse.urlsplit(url)
    query = parse.parse_qsl(parts.query, keep_blank_values=True)
    query.append(('_campus_check', str(int(time.time() * 1000))))
    return parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parse.urlencode(query), ''))


def validate_configuration(reply, source):
    if reply is None:
        LOG.warning('CONFIG_UNAVAILABLE: timeout before login.')
        raise CampusError('Cannot download portal configuration; no login attempted. Retry on campus Wi-Fi.')
    text = reply.body.decode('gb18030', errors='replace')
    # Ignore comments so a commented-out setting is not accepted as active configuration.
    clean = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    clean = re.sub(r'(?m)^\s*//[^\n]*', '', clean)
    settings = {}
    for name in ('enMd5', 'enHttps', 'enPerceive'):
        assignments = re.findall(r'\b' + name + r'\s*=\s*([\x27\"]?)([0-9]+)\1\s*(?:;|,|$)', clean, re.M)
        settings[name] = [int(value) for _, value in assignments]
    summary = {
        'checked_at': datetime.now().isoformat(),
        'script_path': parse.urlsplit(source).path,
        'http_status': reply.status,
        'content_type': reply.headers.get('Content-Type', ''),
        'bytes': len(reply.body),
        'sha256': hashlib.sha256(reply.body).hexdigest(),
        'looks_like_html': bool(re.search(r'<(?:!doctype|html|head|body|script)\b', text, re.I)),
        'redirect_to_allowed_portal': bool(reply.headers.get('Location') and campus_url(parse.urljoin(source, reply.headers['Location']))),
        'settings': settings,
    }
    # Only metadata and public feature flags, never page bodies, cookies, URLs with query strings or credentials.
    write_json(DATA / 'portal-config-diagnostic.json', summary)
    LOG.info('CONFIG_CHECK: status=%s bytes=%s html=%s settings=%s', reply.status, len(reply.body), summary['looks_like_html'], json.dumps(settings, sort_keys=True))
    if reply.status != 200 or summary['looks_like_html']:
        raise CampusError('Configuration request returned a redirect/error/HTML page instead of JavaScript; no login attempted. Diagnostic saved locally.')
    missing = [name for name, values in settings.items() if not values]
    if missing:
        raise CampusError('Configuration script is missing expected settings: ' + ', '.join(missing) + '. No login attempted; diagnostic saved locally.')
    changed = [name + '=' + '/'.join(map(str, values)) for name, values in settings.items() if values != [0]]
    if changed:
        raise CampusError('Unsupported or ambiguous portal settings: ' + ', '.join(changed) + '. No login attempted; diagnostic saved locally.')



def diagnose_portal():
    """Collect public JavaScript only. Never load credentials or call login endpoints."""
    state = network_state()
    if not is_campus(state):
        raise CampusError('Connect to campus Wi-Fi first; no collection performed on a hotspot.')
    folder = DATA / 'portal-public-scripts'
    folder.mkdir(parents=True, exist_ok=True)
    page_url = 'http://' + PORTAL_HOST + '/a79.htm'
    page = fetch(page_url)
    urls = ['http://' + PORTAL_HOST + '/a41.js', 'http://' + PORTAL_HOST + ':801/a41.js']
    if page and page.status == 200:
        referenced = configuration_url(page.body, page_url)
        urls.append(referenced)
    records = []
    for index, url in enumerate(dict.fromkeys(urls)):
        reply = fetch(url, timeout=8)
        parts = parse.urlsplit(url)
        item = {'host': parts.netloc, 'path': parts.path, 'reachable': reply is not None}
        if reply:
            filename = 'public-config-' + str(index) + '.js.txt'
            # These requests contain no account/password and target only public a41.js.
            (folder / filename).write_bytes(reply.body)
            item.update(status=reply.status, bytes=len(reply.body),
                        content_type=reply.headers.get('Content-Type', ''),
                        sha256=hashlib.sha256(reply.body).hexdigest(), file=filename)
        records.append(item)
        LOG.info('PUBLIC_SCRIPT_COLLECTED: host=%s status=%s bytes=%s', parts.netloc, item.get('status', 'unreachable'), item.get('bytes', 0))
    write_json(folder / 'manifest.json', {'checked_at': datetime.now().isoformat(), 'scripts': records})
    print('Public-script collection finished. No credentials read; no login attempted.')
    print('Files:', folder)


def parse_jsonp(body):
    text = body.decode('gb18030', errors='replace').strip()
    m = re.fullmatch(r'(?:[A-Za-z_$][\w$]*\s*\(\s*)?(\{.*\})\s*\)?\s*;?', text, re.S)
    if not m:
        raise CampusError('Authentication server returned an unexpected response.')
    try:
        value = json.loads(m.group(1))
    except ValueError:
        raise CampusError('Authentication response is not valid JSON.') from None
    return value


class Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(raw, decrypt=False):
    if os.name != 'nt':
        raise CampusError('Windows is required.')
    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    dest = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), 'Campus network credentials', None, None, None, 1, ctypes.byref(dest))
    if not ok:
        raise CampusError('Windows credential encryption failed; use the same Windows account.')
    try:
        return ctypes.string_at(dest.pbData, dest.cbData)
    finally:
        kernel.LocalFree(dest.pbData)


def write_json(path, value):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def setup(parent=None):
    import tkinter as tk
    from tkinter import messagebox
    window = tk.Toplevel(parent) if parent else tk.Tk()
    window.title('校园网自动恢复：本机账号设置')
    window.resizable(False, False)
    tk.Label(window, text='仅用于你自己的校园网账号。密码使用 Windows 当前用户加密保存。\n首次保存只配置账号，不会立即登录。', justify='left').grid(row=0, column=0, columnspan=2, padx=20, pady=15)
    tk.Label(window, text='账号（填写平时网页中的账号）').grid(row=1, column=0, padx=15, pady=8)
    account = tk.Entry(window, width=32)
    account.grid(row=1, column=1, padx=15, pady=8)
    tk.Label(window, text='密码').grid(row=2, column=0, padx=15, pady=8)
    password = tk.Entry(window, width=32, show='*')
    password.grid(row=2, column=1, padx=15, pady=8)
    def save():
        username, secret = account.get().strip(), password.get()
        if not username or not secret:
            messagebox.showerror('未保存', '账号和密码都不能为空。', parent=window)
            return
        try:
            encrypted = dpapi(json.dumps({'username': username, 'password': secret}, ensure_ascii=False).encode('utf-8'))
            write_json(DATA / 'credentials.json', {'version': 1, 'dpapi': base64.b64encode(encrypted).decode('ascii')})
            # Changed credentials require a new successful single-run test.
            (DATA / 'verified.json').unlink(missing_ok=True)
            (DATA / 'authentication-paused.json').unlink(missing_ok=True)
        except (CampusError, OSError):
            messagebox.showerror('未保存', '保存失败，请确认在当前 Windows 用户下运行且目录可写。', parent=window)
            return
        password.delete(0, tk.END)
        messagebox.showinfo('已保存', '账号已在本机加密保存。请在校园 Wi-Fi 需要认证时运行单次认证。', parent=window)
        window.destroy()
    tk.Button(window, text='加密保存', command=save, width=22).grid(row=3, column=0, columnspan=2, pady=15)
    account.focus_set()
    if parent:
        window.transient(parent)
        window.grab_set()
        parent.wait_window(window)
    else:
        window.mainloop()


def credentials():
    try:
        stored = json.loads((DATA / 'credentials.json').read_text(encoding='utf-8'))
        return json.loads(dpapi(base64.b64decode(stored['dpapi']), decrypt=True).decode('utf-8'))
    except (OSError, ValueError, KeyError):
        raise CampusError('Run 01-account-setup.cmd first to save credentials locally.') from None



def authentication_verdict(result):
    # ret_code meanings are documented by this campus portal's own a42.js.
    # Store only bounded numeric codes, never raw messages that may echo account data.
    raw = result.get('ret_code')
    code = int(str(raw)) if re.fullmatch(r'[0-9]{1,4}', str(raw)) else None
    if result.get('result') in (1, '1', 'ok'):
        return 'accepted', code
    if code == 1:
        return 'credentials_rejected', code
    if code == 2:  # Terminal IP already online: verify actual connectivity below.
        return 'already_online', code
    return 'retry', code


def recover_once(allow_auth=True, on_auth_attempt=None):
    state = network_state()
    if not is_campus(state):
        LOG.info('NOT_CAMPUS: no authentication attempted.')
        return 'not_campus'
    status, url = connectivity()
    if status == 'online':
        LOG.info('ONLINE: no authentication needed.')
        return 'online'
    if status != 'portal':
        LOG.info('UNCERTAIN: no trusted authentication redirect; no credentials submitted.')
        return 'uncertain'
    if not allow_auth:
        LOG.info('AUTH_COOLDOWN: portal detected; continuing connectivity checks without submitting credentials.')
        return 'cooldown'
    data = auth_context(url, state)
    # Fetch the current page but do not follow redirects outside the known portal.
    page = fetch(url)
    if page is None or page.status != 200:
        LOG.info('PORTAL_UNAVAILABLE: retry later.')
        return 'uncertain'
    config_url = configuration_url(page.body, url)
    validate_configuration(fetch(config_url), config_url)
    # Verify again after network requests so a network switch cannot reuse stale context.
    current = network_state()
    if not is_campus(current):
        return 'not_campus'
    auth_context(url, current)
    secret = credentials()
    account = secret['username']
    data['user_account'] = account if account.startswith(',0,') else ',0,' + account
    data['user_password'] = secret['password']
    data['callback'] = 'dr' + str(int(time.time() * 1000))
    target = 'http://' + PORTAL_HOST + ':801/eportal/?c=Portal&a=login&' + parse.urlencode(data)
    # Never log target, request data, raw server messages, or exception URLs.
    LOG.info('AUTH_ATTEMPT: submitting one login to the allowed campus server.')
    if on_auth_attempt is not None:
        on_auth_attempt()
    reply = fetch(target, timeout=10)
    if reply is None:
        LOG.info('AUTH_TIMEOUT: login result unknown; backing off.')
        return 'uncertain'
    result = parse_jsonp(reply.body)
    verdict, code = authentication_verdict(result)
    if verdict == 'credentials_rejected':
        LOG.warning('AUTH_CREDENTIALS_REJECTED: ret_code=1.')
        return 'rejected'
    if verdict == 'retry':
        LOG.warning('AUTH_RETRYABLE: ret_code=%s; waiting before another submission; monitoring continues.', code)
        return 'retry'
    for delay in (2, 4, 6):
        time.sleep(delay)
        current = network_state()
        if not is_campus(current):
            return 'not_campus'
        if connectivity()[0] == 'online':
            (DATA / 'authentication-paused.json').unlink(missing_ok=True)
            write_json(DATA / 'verified.json', {'verified_at': datetime.now().isoformat(), 'portal': PORTAL_HOST})
            LOG.info('AUTH_VERIFIED: server accepted login and external connectivity returned.')
            return 'authenticated'
    LOG.warning('AUTH_ACCEPTED_UNVERIFIED: server accepted login but connectivity not confirmed.')
    return 'uncertain'



CHECK_INTERVAL = 30
REJECTION_RETRY_DELAY = 20
MAX_AUTH_REJECTIONS = 11  # Initial submission plus ten retries.


def credentials_revision():
    try:
        return (DATA / 'credentials.json').stat().st_mtime_ns
    except FileNotFoundError:
        return None


def authentication_paused():
    try:
        pause = json.loads((DATA / 'authentication-paused.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return False
    except ValueError:
        return True
    if isinstance(pause, dict):
        if pause.get('reason') == 'credentials_rejected':
            return False  # Older builds paused after the first rejection.
        if pause.get('reason') == 'repeated_credentials_rejected' and 'credentials_revision' in pause:
            return pause['credentials_revision'] == credentials_revision()
    return True


@dataclass
class AuthenticationRetry:
    attempts: int = 0
    next_attempt: float = 0.0
    rejections: int = 0
    revision: int | None = None

    def allowed(self):
        revision = credentials_revision()
        if self.revision != revision:
            self.reset()
            self.revision = revision
        return (self.rejections < MAX_AUTH_REJECTIONS and not authentication_paused()
                and time.monotonic() >= self.next_attempt)

    def record_attempt(self):
        self.attempts += 1
        delay = min(60 * (2 ** min(self.attempts - 1, 3)), 300)
        self.next_attempt = time.monotonic() + delay
        LOG.debug('AUTH_NETWORK_BACKOFF: at least %s seconds after a temporary failure.', delay)

    def record_rejection(self):
        if self.revision != credentials_revision():
            return
        self.rejections += 1
        self.attempts = 0
        self.next_attempt = time.monotonic() + REJECTION_RETRY_DELAY
        if self.rejections < MAX_AUTH_REJECTIONS:
            LOG.info('AUTH_REJECTION_RETRY: ret_code=1; rejections=%s/%s; retry in at least %s seconds.',
                     self.rejections, MAX_AUTH_REJECTIONS, REJECTION_RETRY_DELAY)
            return
        LOG.warning('MONITOR_AUTH_PAUSED: ret_code=1; rejections=%s/%s; connectivity checks continue.',
                    self.rejections, MAX_AUTH_REJECTIONS)
        write_json(DATA / 'authentication-paused.json', {
            'reason': 'repeated_credentials_rejected',
            'paused_at': datetime.now().isoformat(),
            'credentials_revision': self.revision,
            'rejections': self.rejections,
        })

    def reset_backoff(self):
        self.attempts = 0
        self.next_attempt = 0.0

    def reset(self):
        self.reset_backoff()
        self.rejections = 0


def monitor_cycle(retry):
    state = network_state()
    if not state.get('up') and not state.get('profile'):
        if not state.get('interface'):
            return 'disconnected'
        run_windows_command([str(SYSTEM32 / 'netsh.exe'), 'wlan', 'connect', 'name=' + NETWORKS[0], 'interface=' + state['interface']],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        LOG.info('WIFI_RECONNECT_REQUESTED: next check remains on the regular schedule.')
        return 'disconnected'
    if not is_campus(state):
        return 'not_campus'
    # A short glitch should not result in an authentication submission.
    if connectivity()[0] != 'online':
        time.sleep(10)
    result = recover_once(allow_auth=retry.allowed(), on_auth_attempt=retry.record_attempt)
    if result == 'rejected':
        retry.record_rejection()
    elif result in ('online', 'authenticated'):
        retry.reset()
    return result


def monitor_loop():
    retry = AuthenticationRetry()
    previous_wall = time.time()
    previous_tick = time.monotonic()
    while True:
        started = time.monotonic()
        now = time.time()
        if max(now - previous_wall, started - previous_tick) > 120:
            LOG.info('RESUME_DETECTED: waiting 30 seconds for Wi-Fi and campus session recovery.')
            time.sleep(30)
            # The wake delay has elapsed. Keep the rejection count across sleep.
            retry.reset_backoff()
            started = time.monotonic()
        previous_wall, previous_tick = time.time(), started
        try:
            monitor_cycle(retry)
        except CampusError as exc:
            LOG.warning('MONITOR_RETRY: %s', exc)
        except (OSError, subprocess.SubprocessError):
            LOG.warning('MONITOR_RETRY: temporary Windows/network query failure; next check in 30 seconds.')
        except Exception as exc:
            LOG.error('MONITOR_RECOVERABLE_ERROR: type=%s; retrying without logging sensitive exception text.', type(exc).__name__)
        time.sleep(max(1, CHECK_INTERVAL - (time.monotonic() - started)))


def watch():
    lock = None
    kernel = None
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        lock = kernel.CreateMutexW(None, False, 'Local\\CampusNetworkRecovery')
        if not lock or ctypes.get_last_error() == 183:
            if lock:
                kernel.CloseHandle(lock)
            LOG.info('A monitor is already running; exiting.')
            return
    LOG.info('MONITOR_STARTED: check_interval=%ss; authentication_retry=60/120/240/300s; rejection_retry=%ss; max_rejections=%s',
             CHECK_INTERVAL, REJECTION_RETRY_DELAY, MAX_AUTH_REJECTIONS)
    try:
        monitor_loop()
    finally:
        if lock:
            kernel.CloseHandle(lock)


def startup_path():
    return Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Startup/CampusNetworkRecovery.vbs'


TASK_NAME = 'CampusNetworkRecovery'
TASK_NS = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
TASK_DESCRIPTION = 'Campus network recovery: logon, resume, unlock and missing-process recovery.'


def monitor_command():
    if FROZEN:
        return [str(Path(sys.executable).resolve()), 'watch']
    return [str(Path(sys.executable).with_name('pythonw.exe')), '-B', str(Path(__file__).resolve()), 'watch']


def scheduled_task_xml(sid, command, start=None):
    ET.register_namespace('', TASK_NS)
    def node(parent, name, text=None, **attrs):
        child = ET.SubElement(parent, '{' + TASK_NS + '}' + name, attrs)
        if text is not None:
            child.text = str(text)
        return child
    task = ET.Element('{' + TASK_NS + '}Task', {'version': '1.2'})
    registration = node(task, 'RegistrationInfo')
    node(registration, 'Description', TASK_DESCRIPTION)
    triggers = node(task, 'Triggers')
    logon = node(triggers, 'LogonTrigger')
    node(logon, 'Enabled', 'true'); node(logon, 'UserId', sid); node(logon, 'Delay', 'PT20S')
    event = node(triggers, 'EventTrigger')
    node(event, 'Enabled', 'true')
    node(event, 'Subscription', "<QueryList><Query Id='0' Path='System'><Select Path='System'>*[System[(Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1) or (Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=107)]]</Select></Query></QueryList>")
    node(event, 'Delay', 'PT20S')
    unlock = node(triggers, 'SessionStateChangeTrigger')
    node(unlock, 'Enabled', 'true'); node(unlock, 'UserId', sid)
    node(unlock, 'Delay', 'PT20S'); node(unlock, 'StateChange', 'SessionUnlock')
    timer = node(triggers, 'TimeTrigger')
    repetition = node(timer, 'Repetition')
    node(repetition, 'Interval', 'PT1M'); node(repetition, 'StopAtDurationEnd', 'false')
    node(timer, 'StartBoundary', start or (datetime.now() + timedelta(minutes=1)).isoformat(timespec='seconds'))
    node(timer, 'Enabled', 'true')
    principals = node(task, 'Principals')
    principal = node(principals, 'Principal', id='CurrentUser')
    node(principal, 'UserId', sid); node(principal, 'LogonType', 'InteractiveToken')
    node(principal, 'RunLevel', 'LeastPrivilege')
    settings = node(task, 'Settings')
    for name, value in [('MultipleInstancesPolicy','IgnoreNew'), ('DisallowStartIfOnBatteries','false'),
                        ('StopIfGoingOnBatteries','false'), ('AllowHardTerminate','true'),
                        ('StartWhenAvailable','true'), ('RunOnlyIfNetworkAvailable','false'),
                        ('AllowStartOnDemand','true'), ('Enabled','true'), ('WakeToRun','false'),
                        ('ExecutionTimeLimit','PT0S')]:
        node(settings, name, value)
    restart = node(settings, 'RestartOnFailure')
    node(restart, 'Interval', 'PT1M'); node(restart, 'Count', '3')
    actions = node(task, 'Actions', Context='CurrentUser')
    action = node(actions, 'Exec')
    node(action, 'Command', command[0])
    node(action, 'Arguments', subprocess.list2cmdline(command[1:]))
    node(action, 'WorkingDirectory', str(ROOT))
    return ET.tostring(task, encoding='unicode')


def task_status():
    return run_ps("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
                  "$s=New-Object -ComObject 'Schedule.Service'; $s.Connect(); "
                  "try{$t=$s.GetFolder('\\').GetTask('" + TASK_NAME + "'); "
                  "[pscustomobject]@{installed=$true;state=$t.State;last_result=$t.LastTaskResult}|ConvertTo-Json -Compress}"
                  "catch{'{\"installed\":false}'}")


def stop_monitors():
    if FROZEN:
        # The scheduler stops the complete one-file EXE process tree.
        return
    script = str(Path(__file__).resolve()).replace("'", "''")
    return run_ps("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
                  "$target='" + script + "'; Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'pythonw.exe' -and $_.CommandLine -and $_.CommandLine.Contains($target) -and $_.CommandLine -match '\\bwatch\\b' } | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction Stop }; '{\"stopped\":true}'")


def task_editor():
    return (
        "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
        "$s=New-Object -ComObject 'Schedule.Service'; $s.Connect(); $f=$s.GetFolder('\\'); "
        "$name='" + TASK_NAME + "'; "
        "$old=$f.GetTasks(1) | Where-Object { $_.Name -eq $name } | Select-Object -First 1; "
        "if($old -and $old.Definition.RegistrationInfo.Description -ne '"
        + TASK_DESCRIPTION + "'){throw 'Task name belongs to another application'}; "
    )


def install_startup():
    if not (DATA / 'verified.json').exists():
        raise CampusError('A successful single authentication test is required before enabling startup.')
    credentials()
    command = monitor_command()
    if not Path(command[0]).is_file():
        raise CampusError('Monitor executable not found; startup was not installed.')
    sid = run_ps("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); [pscustomobject]@{sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value}|ConvertTo-Json -Compress")['sid']
    xml = scheduled_task_xml(sid, command)
    encoded = base64.b64encode(xml.encode('utf-8')).decode('ascii')
    result = run_ps(task_editor() +
                    "$xml=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded + "')); "
                    "$t=$f.RegisterTask($name,$xml,6,$null,$null,3,$null); "
                    "if($t.State -eq 4 -and $old){ $t.Stop(0) }; "
                    "[pscustomobject]@{installed=$true;enabled=$t.Enabled}|ConvertTo-Json -Compress")
    if not result.get('installed'):
        raise CampusError('Task registration failed; legacy startup retained.')
    # Replace the old Startup-folder launcher only after registration succeeds.
    stop_monitors()
    run_ps("$ErrorActionPreference='Stop'; $s=New-Object -ComObject 'Schedule.Service'; $s.Connect(); "
           "$t=$s.GetFolder('\\').GetTask('" + TASK_NAME + "'); $t.Run($null)|Out-Null; '{\"started\":true}'")
    startup_path().unlink(missing_ok=True)
    LOG.info('STARTUP_INSTALLED: scheduled task=%s; logon/resume/unlock; missing-process check=60s.', TASK_NAME)


def uninstall_startup():
    run_ps(task_editor() +
           "if($old){$old.Enabled=$false; if($old.State -eq 4){$old.Stop(0)}; $f.DeleteTask($name,0)}; '{\"removed\":true}'")
    startup_path().unlink(missing_ok=True)
    stop_monitors()
    LOG.info('STARTUP_REMOVED: scheduled task removed and monitor stopped; credentials retained locally.')


def show_app():
    import tkinter as tk
    from tkinter import ttk

    window = tk.Tk()
    window.title('校园网自动登录')
    window.minsize(480, 290)
    panel = ttk.Frame(window, padding=20)
    panel.pack(fill='both', expand=True)
    panel.columnconfigure(0, weight=1)
    panel.columnconfigure(1, weight=1)
    status = tk.StringVar(value='正在读取状态…')
    feedback = tk.StringVar()
    ttk.Label(panel, text='校园网自动登录', font=('Microsoft YaHei UI', 15, 'bold')).grid(row=0, columnspan=2, sticky='w')
    ttk.Label(panel, textvariable=status, wraplength=440, justify='left').grid(row=1, columnspan=2, sticky='w', pady=(8, 12))
    ttk.Label(panel, textvariable=feedback, wraplength=440, justify='left').grid(row=5, columnspan=2, sticky='w', pady=(10, 0))
    buttons = []
    results = Queue()
    busy = False

    def read_status():
        task = task_status()
        task_text = {1: '已禁用', 2: '排队中', 3: '已启用', 4: '运行中'}.get(task.get('state'), '未启用')
        saved = (DATA / 'credentials.json').exists()
        verified = (DATA / 'verified.json').exists()
        paused = authentication_paused()
        net = network_state()
        return '\n'.join((
            f"当前网络　{net.get('profile') or '未连接'}",
            f"校园网账号　{'已保存' if saved else '未设置'}　　单次认证　{'已验证' if verified else '待验证'}",
            f"后台恢复　{task_text}" + ('\n已暂停自动认证。多次认证被拒绝，请检查账号设置或在网页尝试。' if paused else ''),
        ))

    def update_ui():
        nonlocal busy
        try:
            summary, message = results.get_nowait()
        except Empty:
            window.after(100, update_ui)
            return
        if summary is not None:
            status.set(summary)
        else:
            status.set('暂时无法读取状态。')
        feedback.set(message or '')
        busy = False
        for button in buttons:
            button.state(['!disabled'])

    def run(action=None, message_during_work='正在读取状态…'):
        nonlocal busy
        busy = True
        feedback.set(message_during_work)
        for button in buttons:
            button.state(['disabled'])

        def worker():
            message = None
            try:
                if action:
                    message = action()
            except CampusError as exc:
                LOG.warning('UI_ACTION_FAILED: %s', exc)
                message = str(exc)
            except Exception as exc:
                LOG.warning('UI_ACTION_FAILED: type=%s', type(exc).__name__)
                message = '操作失败。请查看运行日志。'
            try:
                summary = read_status()
            except Exception as exc:
                LOG.warning('UI_STATUS_FAILED: type=%s', type(exc).__name__)
                summary = None
            results.put((summary, message))

        Thread(target=worker, daemon=True).start()
        window.after(100, update_ui)

    def set_account():
        setup(window)
        run()

    def test_login():
        descriptions = {
            'authenticated': '认证成功，已确认恢复上网。',
            'online': '当前已联网。初次使用请等校园网需要认证时再测试。',
            'not_campus': '请先连接支持的校园 Wi-Fi。',
            'uncertain': '暂未确认恢复上网，请查看日志。',
            'retry': '认证服务器暂未接受登录，请稍后再试。',
            'rejected': '本次认证被服务器拒绝，请稍后重试或检查账号设置。',
        }
        return descriptions[recover_once()]

    def enable():
        if not (DATA / 'verified.json').exists():
            return '请先在校园网需要认证时完成一次成功的单次认证。'
        install_startup()
        return '已启用自启动和后台恢复。'

    def disable():
        uninstall_startup()
        return '已停止后台恢复并取消自启动。'

    for index, (label, callback) in enumerate((
        ('设置账号', set_account),
        ('单次认证', lambda: run(test_login, '正在检测校园网并尝试认证…')),
        ('启用自启动', lambda: run(enable, '正在启用后台恢复…')),
        ('停止并取消自启动', lambda: run(disable, '正在停止后台恢复…')),
        ('刷新状态', run),
        ('打开日志目录', lambda: os.startfile(DATA)),
    )):
        button = ttk.Button(panel, text=label, command=callback)
        button.grid(row=2 + index // 2, column=index % 2, sticky='ew', padx=4, pady=4)
        buttons.append(button)
    def close_window():
        if not busy:
            window.destroy()

    window.protocol('WM_DELETE_WINDOW', close_window)
    run()
    window.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='gui',
                        choices=['gui', 'setup', 'once', 'watch', 'install-startup', 'remove-startup', 'status', 'diagnose'])
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    LOG.setLevel(logging.INFO)
    handler = RotatingFileHandler(DATA / 'recovery.log', maxBytes=256000, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    if sys.stdout:
        LOG.addHandler(logging.StreamHandler(sys.stdout))
    try:
        if args.action == 'gui':
            show_app()
        elif args.action == 'setup':
            setup()
        elif args.action == 'diagnose':
            diagnose_portal()
        elif args.action == 'once':
            result = recover_once()
            print('RESULT:', result)
            print('Log:', DATA / 'recovery.log')
            return 0 if result in ('online', 'authenticated') else 2
        elif args.action == 'watch':
            if not (DATA / 'verified.json').exists():
                raise CampusError('Run a successful single authentication test before monitoring.')
            watch()
        elif args.action == 'install-startup':
            install_startup()
        elif args.action == 'remove-startup':
            uninstall_startup()
        else:
            print(json.dumps(network_state(), ensure_ascii=False))
            print('Credentials configured:', (DATA / 'credentials.json').exists())
            print('Authentication verified:', (DATA / 'verified.json').exists())
            print('Startup:', json.dumps(task_status()))
            print('Authentication paused:', authentication_paused())
            print('Log:', DATA / 'recovery.log')
        return 0
    except CampusError as exc:
        LOG.error('%s', exc)
        return 2
    except KeyboardInterrupt:
        LOG.info('Stopped by user.')
        return 0
    except Exception:
        LOG.error('Operation failed; no raw exception printed to avoid disclosing credentials. Check Windows/network availability.')
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
