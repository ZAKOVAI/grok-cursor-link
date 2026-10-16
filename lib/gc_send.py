"""gc_send: the "hands" half of grok-cursor (macOS). Standard library only.

There is no official API to submit a prompt into a local Cursor IDE chat, so
this drives the UI. It reads the macOS Accessibility (AX) tree to find the
chat input and chat tabs and to check them, and only presses Return once
every check has passed:

    the screen is unlocked and Cursor is frontmost
    the target tab is selected
    the chat input is focused and was empty
    the input now holds exactly our prompt

Everything that could go wrong has its own exit code (see EXIT). When in
doubt, it aborts.

Strategies:
  ax  (default)  AX tree + clipboard paste (clipboard saved and restored)
                 or Cursor's prefill deep link, then Return.
  cdp (primary)  Chrome DevTools Protocol on 127.0.0.1:9222 when that port is
                 open. Skips frontmost and Accessibility. Lock, the selected
                 chat, an empty input, and the prompt landing in that input
                 are read from the page. AX is the fallback when the port is down.
  headless       Cursor CLI `agent -p` in a repo (a new CLI chat, never --resume).

Cursor's internals (AX roles, CSS classes, state.vscdb keys) are
undocumented. Every selector below can be overridden with environment
variables, and `grok-cursor ax-dump` / `grok-cursor targets` exist so the
selectors can be checked on a real Mac.
"""
import base64
import datetime
import glob
import hashlib
import json
import os
import re
import socket
import sqlite3
import struct
import subprocess
import sys
import time
from urllib.parse import quote

DEEPLINK = "cursor://anysphere.cursor-deeplink/prompt?text="
DEEPLINK_MAX_URL = 8000           # documented limit for the whole encoded URL
SEND_MAX_CHARS = 8000
CURSOR_BUNDLE_IDS = ("com.todesktop.230313mzl4w4u92",)

EXIT = {
    "not_macos": 10, "bad_prompt": 11, "secret": 12, "too_long": 13,
    "caffeinate": 14, "locked": 15, "not_running": 16, "repo": 17,
    "deeplink": 18, "not_frontmost": 19, "keystroke": 20,
    "no_agent_cli": 21, "agent_failed": 22, "unverified": 23,
    "failed": 31,
    "no_accessibility": 24, "target": 25, "switch_unverified": 26,
    "input": 27, "cdp": 28, "clipboard": 29, "console": 30,
    "no_card": 32, "card": 33, "slash": 34,
}

# `/name rest`: a Cursor slash command. `name` must be a row in the slash menu.
SLASH_RE = re.compile(r"/([^\s/]\S*)(?:\s+(.*))?\Z", re.S)

# ------------------------------------------------------------------ secrets

_SECRET_NAME = r"[A-Za-z0-9_.-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Za-z0-9_.-]*"
SECRET_RULES = [
    ("private-key", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    ("sk- api key", r"\bsk-[A-Za-z0-9_-]{16,}"),
    ("stripe key", r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("github token", r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{22,}"),
    ("aws access key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("slack token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("google api key", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("bearer token", r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    ("credential assignment", r"(?im)^\s*(?:export\s+)?" + _SECRET_NAME + r"\s*[=:]\s*['\"]?[^\s'\"]{8,}"),
    ("password in url", r"[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]+@"),
]


def find_secrets(text):
    return [name for name, rx in SECRET_RULES if re.search(rx, text)]


class SendAbort(Exception):
    def __init__(self, key, msg):
        Exception.__init__(self, msg)
        self.key, self.code, self.msg = key, EXIT[key], msg


# ------------------------------------------------------------------ small helpers

def norm(text):
    """Whitespace-insensitive form used for every "is it the same text" check."""
    text = (text or "").replace("\u00a0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", text).strip()


def text_matches(have, want):
    """True when `have` (e.g. an input's AX value) holds the prompt `want`."""
    h, w = norm(have), norm(want)
    if not w:
        return False
    if len(w) <= 80:
        return w in h
    return w[:40] in h and w[-40:] in h and len(h) >= len(w) * 0.9


def utf8_env():
    env = dict(os.environ)
    env.setdefault("LANG", "en_US.UTF-8")      # pbcopy/pbpaste mangle non-ASCII without it
    env["LC_CTYPE"] = env.get("LC_CTYPE") or "UTF-8"
    return env


def run(cmd, timeout=15, input=None, cwd=None, env=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              input=input, cwd=cwd, env=env)
    except (FileNotFoundError, PermissionError):
        return None
    except subprocess.TimeoutExpired:
        return None


def osa(script, timeout=10):
    return run(["osascript", "-e", script], timeout=timeout)


def ok(p):
    return p is not None and p.returncode == 0


def delay(default):
    try:
        return float(os.environ.get("GROK_CURSOR_SEND_DELAY", default))
    except ValueError:
        return default


def app_name():
    return os.environ.get("GROK_CURSOR_APP", "Cursor")


def applescript_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ------------------------------------------------------------------ system checks

def console_state():
    """Lock / console info from `ioreg -n Root -d1 -a` (plist XML).
    Returns dict(locked=True|False|None, on_console=True|False|None, user=str|None)."""
    import plistlib
    p = run(["ioreg", "-n", "Root", "-d1", "-a"])
    info = {"locked": None, "on_console": None, "user": None}
    if not ok(p) or not p.stdout.strip():
        return info
    try:
        root = plistlib.loads(p.stdout.encode())
    except Exception:
        flat = re.sub(r"\s+", "", p.stdout)
        if "CGSSessionScreenIsLocked</key><true/>" in flat or "IOConsoleLocked</key><true/>" in flat:
            info["locked"] = True
        return info
    if isinstance(root, list):
        root = root[0] if root else {}
    me = os.environ.get("USER") or ""
    uid = os.getuid() if hasattr(os, "getuid") else None
    users = root.get("IOConsoleUsers") or []
    mine = None
    for u in users:
        if not isinstance(u, dict):
            continue
        if (uid is not None and u.get("kCGSSessionUserIDKey") == uid) or u.get("kCGSSessionUserNameKey") == me:
            mine = u
            break
    locked = bool(root.get("IOConsoleLocked"))
    if mine is not None:
        info["user"] = mine.get("kCGSSessionUserNameKey")
        info["on_console"] = bool(mine.get("kCGSSessionOnConsoleKey", True))
        # The key is usually only present while locked.
        locked = locked or bool(mine.get("CGSSessionScreenIsLocked"))
    elif users:
        info["on_console"] = False
    info["locked"] = locked
    return info


def frontmost():
    """(name, bundle_id) of the frontmost app, or (None, None)."""
    p = osa('tell application "System Events" to set p to first application process whose frontmost is true\n'
            'return (name of p) & "|" & (bundle identifier of p)')
    if not ok(p):
        return None, None
    name, _, bid = p.stdout.strip().partition("|")
    return name or None, bid or None


def is_cursor_front():
    name, bid = frontmost()
    want_bid = os.environ.get("GROK_CURSOR_BUNDLE_ID")
    bids = (want_bid,) if want_bid else CURSOR_BUNDLE_IDS
    return (bid in bids) or (name == app_name()), name


def cursor_pid():
    p = run(["pgrep", "-ax", app_name()])
    if not ok(p):
        return None
    for line in p.stdout.split():
        if line.strip().isdigit():
            return int(line)
    return None


def keys(spec):
    """Send a key combo to the frontmost process. spec like 'cmd+shift+v', 'return', 'cmd+return'."""
    parts = [x.strip().lower() for x in spec.split("+") if x.strip()]
    mods = {"cmd": "command down", "command": "command down", "shift": "shift down",
            "alt": "option down", "option": "option down", "ctrl": "control down", "control": "control down"}
    using = [mods[x] for x in parts[:-1] if x in mods]
    key = parts[-1] if parts else "return"
    codes = {"return": 36, "enter": 36, "escape": 53, "esc": 53, "tab": 48}
    action = ("key code %d" % codes[key]) if key in codes else ("keystroke " + applescript_str(key))
    if using:
        action += " using {%s}" % ", ".join(using)
    p = osa('tell application "System Events" to tell process %s to %s' % (applescript_str(app_name()), action))
    if not ok(p):
        raise SendAbort("keystroke", "%s failed: %s. Grant Accessibility + Automation (System Events) to the app "
                        "that runs this command." % (spec, (p.stderr.strip() if p else "osascript missing")))


# ------------------------------------------------------------------ clipboard

_TEXT_TYPES = ("utf8", "ut16", "string", "text", "styl", "public.utf8-plain-text", "public.utf16-plain-text",
               "uniform resource locator", "furl")


def clipboard_is_text_only():
    p = osa("clipboard info")
    if not ok(p):
        return None
    out = p.stdout.strip()
    if not out:
        return True              # empty clipboard
    types = [t.strip().lower() for t in re.findall(r"(\u00abclass [^\u00bb]+\u00bb|[A-Za-z][A-Za-z .-]*?)\s*,\s*\d+", out)]
    return all(any(tt in t for tt in _TEXT_TYPES) for t in types) if types else True


def clipboard_get():
    p = run(["pbpaste"], env=utf8_env())
    return p.stdout if ok(p) else None


def clipboard_set(text):
    p = run(["pbcopy"], input=text, env=utf8_env())
    return ok(p)


# ------------------------------------------------------------------ AX backends

class AXNode(object):
    """Backend-neutral view of an accessibility element."""
    truncated = False
    __slots__ = ("be", "ref", "role", "subrole", "title", "desc", "value", "placeholder",
                 "classes", "dom_id", "identifier", "bounds", "selected", "_kids", "parent")

    def __init__(self, be, ref, parent=None):
        self.be, self.ref, self.parent, self._kids = be, ref, parent, None
        a = be.attrs(ref)
        self.role = a.get("AXRole") or ""
        self.subrole = a.get("AXSubrole") or ""
        self.title = a.get("AXTitle") or ""
        self.desc = a.get("AXDescription") or ""
        v = a.get("AXValue")
        self.value = v if isinstance(v, str) else ""
        self.selected = bool(a.get("AXSelected")) or (self.role in ("AXRadioButton", "AXTab", "AXCheckBox")
                                                      and v in (1, True))
        self.placeholder = a.get("AXPlaceholderValue") or ""
        cl = a.get("AXDOMClassList") or []
        self.classes = " ".join(c for c in cl if isinstance(c, str)) if isinstance(cl, list) else str(cl)
        self.dom_id = a.get("AXDOMIdentifier") or ""
        self.identifier = ""
        self.bounds = None
        if self.role == "AXWindow":
            ident = a.get("AXIdentifier")
            if not isinstance(ident, str):
                ident_of = getattr(be, "identifier", None)
                if ident_of is not None:
                    try:
                        ident = ident_of(ref)
                    except Exception:
                        ident = ""
            self.identifier = ident if isinstance(ident, str) else ""
            raw_bounds = None
            bounds_of = getattr(be, "bounds", None)
            if bounds_of is not None:
                try:
                    raw_bounds = bounds_of(ref)
                except Exception:
                    raw_bounds = None
            if isinstance(raw_bounds, (list, tuple)) and len(raw_bounds) == 4:
                self.bounds = tuple(raw_bounds)

    @property
    def text(self):
        return norm(self.title or self.desc or (self.value if self.role not in ("AXTextArea", "AXTextField") else ""))

    def children(self):
        if self._kids is None:
            self._kids = [AXNode(self.be, r, self) for r in self.be.children(self.ref)]
        return self._kids

    SKIP = re.compile(os.environ.get("GROK_CURSOR_AX_SKIP_CLASS", r"monaco-editor|xterm|minimap|monaco-list-rows"))

    def walk(self, limit=40000, depth=60, budget=None):
        """Depth-first walk. Skips code editor / terminal subtrees (huge, never a chat
        tab or chat input) and stops at a node count and a time budget."""
        if budget is None:
            budget = float(os.environ.get("GROK_CURSOR_AX_BUDGET", "8"))
        deadline = time.time() + budget
        stack, n = [(self, 0)], 0
        while stack:
            node, d = stack.pop()
            n += 1
            if n > limit or time.time() > deadline:
                AXNode.truncated = True
                return
            yield node
            if d < depth and not (node.classes and AXNode.SKIP.search(node.classes)):
                stack.extend((k, d + 1) for k in reversed(node.children()))

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent

    def press(self):
        return self.be.press(self.ref)

    def focus(self):
        return self.be.focus(self.ref)

    def describe(self):
        bits = [self.role]
        if self.subrole:
            bits.append("/" + self.subrole)
        for k in ("title", "desc", "placeholder"):
            v = getattr(self, k)
            if v:
                bits.append('%s="%s"' % (k, norm(v)[:60]))
        if self.value and self.role in ("AXTextArea", "AXTextField"):
            bits.append('value="%s"' % norm(self.value)[:40])
        if self.selected:
            bits.append("selected")
        if self.classes:
            bits.append('class="%s"' % self.classes[:80])
        if self.dom_id:
            bits.append("#" + self.dom_id)
        return " ".join(bits)


class FakeAX(object):
    """JSON-file AX tree for tests (GROK_CURSOR_AX_FAKE=path). The file is re-read on
    every query so helper processes (shimmed osascript) can mutate the UI state."""

    def __init__(self, path):
        self.path = path

    def _load(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self, st):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        os.replace(tmp, self.path)

    @staticmethod
    def _find(st, nid):
        stack = list(st.get("windows", []))
        while stack:
            n = stack.pop()
            if n.get("id") == nid:
                return n
            stack.extend(n.get("children", []))
        return None

    def trusted(self):
        return bool(self._load().get("trusted", True))

    def app(self, pid):
        return "app"

    def enable(self, app):
        return True

    def windows(self, app):
        st = self._load()
        return [w["id"] for w in st.get("windows", [])]

    def attrs(self, ref):
        n = self._find(self._load(), ref) or {}
        a = {"AXRole": n.get("role"), "AXSubrole": n.get("subrole"), "AXTitle": n.get("title"),
             "AXDescription": n.get("desc"), "AXValue": n.get("value"), "AXSelected": n.get("selected"),
             "AXPlaceholderValue": n.get("placeholder"), "AXDOMClassList": n.get("classes", []),
             "AXDOMIdentifier": n.get("dom_id"), "AXIdentifier": n.get("identifier")}
        return {k: v for k, v in a.items() if v is not None}

    def bounds(self, ref):
        n = self._find(self._load(), ref) or {}
        b = n.get("bounds")
        return tuple(b) if isinstance(b, (list, tuple)) and len(b) == 4 else None

    def children(self, ref):
        n = self._find(self._load(), ref) or {}
        return [c["id"] for c in n.get("children", [])]

    def focused(self, app):
        return self._load().get("focused")

    def same(self, a, b):
        return a is not None and a == b

    def press(self, ref):
        st = self._load()
        n = self._find(st, ref)
        if not n or n.get("press") == "fail":
            return False
        if n.get("press") == "noop":
            return True
        if n.get("conv"):
            st["active_conv"] = n["conv"]
            stack = list(st["windows"])
            while stack:
                p = stack.pop()
                kids = p.get("children", [])
                if any(k.get("id") == ref for k in kids):
                    for k in kids:
                        if "conv" in k:
                            k["selected"] = k.get("id") == ref
                stack.extend(kids)
        self._save(st)
        return True

    def focus(self, ref):
        st = self._load()
        n = self._find(st, ref)
        if not n or n.get("focus") == "fail":
            return False
        st["focused"] = ref
        self._save(st)
        return True


class MacAX(object):
    """ctypes bridge to ApplicationServices' AXUIElement API."""

    def __init__(self):
        import ctypes
        from ctypes import c_void_p, c_bool, c_int, c_int32, c_long, c_ulong, c_char_p, c_uint32, c_float, POINTER
        self.ct = ctypes
        self.cf = cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.ax = ax = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        sig = [
            (cf, "CFStringCreateWithCString", c_void_p, [c_void_p, c_char_p, c_uint32]),
            (cf, "CFGetTypeID", c_ulong, [c_void_p]),
            (cf, "CFStringGetTypeID", c_ulong, []), (cf, "CFBooleanGetTypeID", c_ulong, []),
            (cf, "CFNumberGetTypeID", c_ulong, []), (cf, "CFArrayGetTypeID", c_ulong, []),
            (cf, "CFStringGetLength", c_long, [c_void_p]),
            (cf, "CFStringGetMaximumSizeForEncoding", c_long, [c_long, c_uint32]),
            (cf, "CFStringGetCString", c_bool, [c_void_p, c_char_p, c_long, c_uint32]),
            (cf, "CFBooleanGetValue", c_bool, [c_void_p]),
            (cf, "CFNumberGetValue", c_bool, [c_void_p, c_int, c_void_p]),
            (cf, "CFArrayGetCount", c_long, [c_void_p]),
            (cf, "CFArrayGetValueAtIndex", c_void_p, [c_void_p, c_long]),
            (cf, "CFEqual", c_bool, [c_void_p, c_void_p]),
            (ax, "AXIsProcessTrusted", c_bool, []),
            (ax, "AXUIElementGetTypeID", c_ulong, []),
            (ax, "AXUIElementCreateApplication", c_void_p, [c_int]),
            (ax, "AXUIElementCopyAttributeValue", c_int32, [c_void_p, c_void_p, POINTER(c_void_p)]),
            (ax, "AXUIElementSetAttributeValue", c_int32, [c_void_p, c_void_p, c_void_p]),
            (ax, "AXUIElementPerformAction", c_int32, [c_void_p, c_void_p]),
            (ax, "AXUIElementSetMessagingTimeout", c_int32, [c_void_p, c_float]),
            (ax, "AXValueGetType", c_int, [c_void_p]),
            (ax, "AXValueGetValue", c_bool, [c_void_p, c_int, c_void_p]),
        ]
        for lib, name, res, argt in sig:
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = res, argt
        self.T = {"str": cf.CFStringGetTypeID(), "bool": cf.CFBooleanGetTypeID(),
                  "num": cf.CFNumberGetTypeID(), "arr": cf.CFArrayGetTypeID(),
                  "ax": ax.AXUIElementGetTypeID()}
        self.true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        self._s = {}
        self.ATTRS = ("AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXSelected",
                      "AXPlaceholderValue", "AXDOMClassList", "AXDOMIdentifier")

    def s(self, text):
        if text not in self._s:
            self._s[text] = self.cf.CFStringCreateWithCString(None, text.encode(), 0x08000100)
        return self._s[text]

    def py(self, ref, depth=0):
        if not ref:
            return None
        t = self.cf.CFGetTypeID(ref)
        if t == self.T["str"]:
            n = self.cf.CFStringGetLength(ref)
            size = self.cf.CFStringGetMaximumSizeForEncoding(n, 0x08000100) + 1
            buf = self.ct.create_string_buffer(size)
            if self.cf.CFStringGetCString(ref, buf, size, 0x08000100):
                return buf.value.decode("utf-8", "replace")
            return ""
        if t == self.T["bool"]:
            return bool(self.cf.CFBooleanGetValue(ref))
        if t == self.T["num"]:
            v = self.ct.c_double()
            self.cf.CFNumberGetValue(ref, 13, self.ct.byref(v))   # kCFNumberDoubleType
            return int(v.value) if v.value.is_integer() else v.value
        if t == self.T["arr"] and depth == 0:
            return [self.py(self.cf.CFArrayGetValueAtIndex(ref, i), 1)
                    for i in range(self.cf.CFArrayGetCount(ref))]
        if t == self.T["ax"]:
            return ("ax", ref)
        return None

    def get(self, ref, name):
        out = self.ct.c_void_p()
        err = self.ax.AXUIElementCopyAttributeValue(ref, self.s(name), self.ct.byref(out))
        return self.py(out.value) if err == 0 else None

    def trusted(self):
        return bool(self.ax.AXIsProcessTrusted())

    def app(self, pid):
        ref = self.ax.AXUIElementCreateApplication(pid)
        self.ax.AXUIElementSetMessagingTimeout(ref, 2.0)
        return ref

    def enable(self, app):
        """Ask Electron/Chromium to build its web accessibility tree."""
        err = self.ax.AXUIElementSetAttributeValue(app, self.s("AXManualAccessibility"), self.true)
        return err == 0

    def windows(self, app):
        main = self.get(app, "AXMainWindow") or self.get(app, "AXFocusedWindow")
        ws = self.get(app, "AXWindows") or []
        refs = [w[1] for w in ws if isinstance(w, tuple)]
        # AXMainWindow is also in AXWindows, but as a different AXUIElementRef for the
        # same element. `!=` does not catch that; CFEqual does.
        if isinstance(main, tuple):
            refs = [main[1]] + refs
        keep = []
        for ref in refs:
            if any(self.same(ref, prev) for prev in keep):
                continue
            keep.append(ref)
        return keep

    def bounds(self, ref):
        """(x, y, width, height) from AXPosition and AXSize, or None."""
        pos = self._geom(ref, "AXPosition", 1)   # kAXValueTypeCGPoint
        size = self._geom(ref, "AXSize", 2)      # kAXValueTypeCGSize
        if pos is None or size is None:
            return None
        return (pos[0], pos[1], size[0], size[1])

    def identifier(self, ref):
        v = self.get(ref, "AXIdentifier")
        return v if isinstance(v, str) else ""

    def _geom(self, ref, attr, kind):
        out = self.ct.c_void_p()
        err = self.ax.AXUIElementCopyAttributeValue(ref, self.s(attr), self.ct.byref(out))
        if err != 0 or not out.value:
            return None
        if self.ax.AXValueGetType(out.value) != kind:
            return None
        buf = (self.ct.c_double * 2)()
        if not self.ax.AXValueGetValue(out.value, kind, buf):
            return None
        return (buf[0], buf[1])

    def attrs(self, ref):
        return {k: v for k, v in ((k, self.get(ref, k)) for k in self.ATTRS) if v is not None}

    def children(self, ref):
        kids = self.get(ref, "AXChildren") or []
        return [k[1] for k in kids if isinstance(k, tuple)]

    def focused(self, app):
        f = self.get(app, "AXFocusedUIElement")
        return f[1] if isinstance(f, tuple) else None

    def same(self, a, b):
        # distinct AXUIElementRefs can name the same element: compare with CFEqual
        return bool(a and b and self.cf.CFEqual(a, b))

    def press(self, ref):
        return self.ax.AXUIElementPerformAction(ref, self.s("AXPress")) == 0

    def focus(self, ref):
        return self.ax.AXUIElementSetAttributeValue(ref, self.s("AXFocused"), self.true) == 0


def ax_backend():
    fake = os.environ.get("GROK_CURSOR_AX_FAKE")
    if fake:
        return FakeAX(fake)
    if sys.platform != "darwin":
        return None
    try:
        return MacAX()
    except Exception:
        return None


class CursorAX(object):
    """Cursor-specific queries on top of an AX backend. All selectors are heuristics
    (UNVERIFIED against every Cursor build) and can be overridden by env vars."""

    TAB_ROLES = ("AXRadioButton", "AXTab", "AXButton", "AXRow", "AXCell", "AXLink", "AXMenuItem",
                 "AXGroup", "AXStaticText", "AXOutlineRow", "AXListItem")

    def __init__(self, be, pid, window_hint=None):
        self.be, self.pid = be, pid
        self.app = be.app(pid)
        self.manual = be.enable(self.app)
        self.window_hint = window_hint
        self.INPUT_CLASS = os.environ.get(
            "GROK_CURSOR_INPUT_CLASS", r"aislash-editor-input|composer-input|chat-input|ProseMirror|tiptap|lexical")
        self.INPUT_HINT = os.environ.get(
            "GROK_CURSOR_INPUT_HINT", r"(?i)plan, search|add a follow-?up|ask anything|message|agent|chat|composer")
        self.TAB_CLASS = os.environ.get(
            "GROK_CURSOR_TAB_CLASS", r"(?i)tab|sidebar-cell|composer-bar|agent-sidebar|chat-item|conversation")

    def windows(self):
        """One entry per window. The frontmost window stays first."""
        keep = []
        for node in (AXNode(self.be, r) for r in self.be.windows(self.app)):
            if any(self._same_window(node, prev) for prev in keep):
                continue
            keep.append(node)
        return keep

    def _same_window(self, a, b):
        if self.be.same(a.ref, b.ref):
            return True
        if a.identifier and a.identifier == b.identifier:
            return True
        if a.identifier or b.identifier:
            return False
        if not a.bounds or a.bounds != b.bounds:
            return False
        return bool(norm(a.title)) and norm(a.title) == norm(b.title)

    def window(self):
        ws = self.windows()
        if not ws:
            raise SendAbort("no_accessibility", "no Cursor windows visible to Accessibility")
        if self.window_hint:
            hint = os.path.basename(self.window_hint.rstrip("/")).lower()
            hits = [w for w in ws if hint in (w.title or "").lower()]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise SendAbort("target", "several Cursor windows match %r" % hint)
            raise SendAbort("target", "no Cursor window title contains %r (windows: %s)"
                            % (hint, ", ".join(repr(w.title) for w in ws)))
        return ws[0]                       # main / focused window first

    def inputs(self, win):
        out = []
        for n in win.walk():
            if n.role not in ("AXTextArea", "AXTextField"):
                continue
            if re.search(self.INPUT_CLASS, n.classes or "") or \
                    re.search(self.INPUT_HINT, n.placeholder or n.desc or ""):
                out.append(n)
        return out

    def is_focused(self, node):
        return self.be.same(self.be.focused(self.app), node.ref)

    def chat_input(self, win):
        ins = self.inputs(win)
        if not ins:
            if AXNode.truncated:
                raise SendAbort("input", "AX tree walk hit its size/time budget before finding the chat input; "
                                "raise GROK_CURSOR_AX_BUDGET or close editor splits")
            raise SendAbort("input", "could not find a Cursor chat input in the AX tree. Open the agent "
                            "panel (Cmd+I), or run `grok-cursor ax-dump --grep Text` and set GROK_CURSOR_INPUT_CLASS")
        for n in ins:
            if self.is_focused(n):
                return n
        if len(ins) == 1:
            return ins[0]
        raise SendAbort("input", "%d chat inputs visible and none focused; use --to <tab> or close extra "
                        "panes" % len(ins))

    def tabs(self, win):
        """Elements that look like chat tabs / sidebar entries, with selection state."""
        out = []
        for n in win.walk():
            if n.role in ("AXTextArea", "AXTextField") or not n.text or n.role not in self.TAB_ROLES:
                continue
            tabby = n.role in ("AXRadioButton", "AXTab") or re.search(self.TAB_CLASS, n.classes or "")
            if not tabby and n.role == "AXStaticText":
                tabby = any(re.search(self.TAB_CLASS, a.classes or "") for a in list(n.ancestors())[:3])
            if tabby:
                out.append(n)
        keep = []
        for n in out:
            # static text inside a tab-like element we already have: keep the element only
            if n.role == "AXStaticText" and any(a in out for a in n.ancestors()):
                continue
            keep.append(n)
        return keep

    # Cursor 3's tab aria-label is "Title, pinned, Chat Editors: Editor Group 1".
    _TAB_SUFFIX = re.compile(
        r",\s*(?:pinned,\s*)?(?:Chat Editors:\s*)?Editor Group \d+\s*$", re.I)
    _UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

    @staticmethod
    def clean_tab_label(text):
        return norm(CursorAX._TAB_SUFFIX.sub("", text or ""))

    def tab_label(self, node):
        """Clean title. Prefer the tab-label child (Cursor puts the real name there);
        a child that is only the chat id does not count. Otherwise strip the aria suffix."""
        for kid in node.children():
            classes = kid.classes or ""
            if "tab-label" not in classes and "label-name" not in classes:
                continue
            text = self.clean_tab_label(kid.text)
            if text and not self._UUID.match(text):
                return text
        return self.clean_tab_label(node.text)

    @staticmethod
    def title_match(shown, want):
        s, w = CursorAX.clean_tab_label(shown).lower(), CursorAX.clean_tab_label(want).lower()
        if not s or not w:
            return False
        if s == w:
            return True
        for ell in ("\u2026", "..."):
            if s.endswith(ell) and len(s) > len(ell) + 3 and w.startswith(s[: -len(ell)].rstrip()):
                return True
        return False

    def find_tab(self, win, title):
        hits = [t for t in self.tabs(win) if self.title_match(self.tab_label(t), title)]
        strong = [t for t in hits if t.role in ("AXRadioButton", "AXTab")]
        return strong or hits


# ------------------------------------------------------------------ Cursor state DB (targets)

def cursor_user_dir():
    env = os.environ.get("CURSOR_USER_DIR")
    if env:
        return env
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Cursor/User")
    return os.path.expanduser("~/.config/Cursor/User")


def _db_get(path, key):
    if not os.path.isfile(path):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % quote(path), uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        row = con.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        val = row[0]
        if isinstance(val, bytes):
            val = val.decode("utf-8", "replace")
        return json.loads(val)
    except (sqlite3.Error, ValueError):
        return None
    finally:
        con.close()


def _ws_folder(ws_dir):
    try:
        with open(os.path.join(ws_dir, "workspace.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    uri = data.get("folder") or data.get("workspace") or "" if isinstance(data, dict) else ""
    if uri.startswith("file://"):
        from urllib.parse import unquote, urlparse
        return unquote(urlparse(uri).path)
    return uri or None


def _db_headers_table(path):
    """Cursor 3.x: global state.vscdb has a `composerHeaders` table (one row per chat)."""
    if not os.path.isfile(path):
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % quote(path), uri=True, timeout=2)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute("SELECT composerId, workspaceId, isArchived, isSubagent, value FROM composerHeaders").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out = []
    for cid, wid, archived, sub, val in rows:
        try:
            v = json.loads(val.decode("utf-8", "replace") if isinstance(val, bytes) else val or "{}")
        except ValueError:
            v = {}
        if not archived and not sub and isinstance(v, dict):
            out.append((cid, wid, v))
    return out

def ws_key(s):
    """Loose identity for workspace names: 'acme.io site' == 'Acme-IO-Site'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def ws_name(path):
    """Display name Cursor uses for a workspace: folder basename, or .code-workspace file stem."""
    base = os.path.basename((path or "").rstrip("/"))
    return base[: -len(".code-workspace")] if base.endswith(".code-workspace") else base

def window_label(title):
    """Workspace part of a Cursor window title ('file — folder', 'x — Name (Workspace)', 'folder')."""
    t = norm(title).lstrip("\u25cf ").rsplit(" \u2014 ", 1)[-1]
    return re.sub(r"\s*\((?:Workspace|Remote|Untitled)\)$", "", t)

def match_workspace(name, labels):
    """Index into labels for workspace `name`: exact (loose) match first, then a unique
    substring. Returns (index|None, error|None); several hits is an error, never a guess."""
    k = ws_key(name)
    if not k:
        return None, "empty --workspace"
    for test in (lambda l: ws_key(l) == k, lambda l: k in ws_key(l)):
        hits = [i for i, l in enumerate(labels) if test(l)]
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return None, "--workspace %r matches %d Cursor windows (%s); be more specific" % (
                name, len(hits), ", ".join(repr(labels[i]) for i in hits))
    return None, "no open Cursor window for workspace %r (open: %s)" % (name, ", ".join(repr(l) for l in labels) or "none")

def list_targets():
    """Chats known to Cursor's local state DB (undocumented; Cursor 2.x and 3.x layouts).
    Each: {id, title, workspace, workspace_name, open, focused, updated}. Read-only."""
    user = cursor_user_dir()
    chats = {}

    def add(cid, **kw):
        if not cid or not isinstance(cid, str):
            return
        c = chats.setdefault(cid, {"id": cid, "title": "", "workspace": None, "open": False,
                                   "focused": False, "updated": 0})
        for k, v in kw.items():
            if k == "updated":
                if isinstance(v, (int, float)) and v > c["updated"]:
                    c["updated"] = v
            elif v:
                c[k] = v

    ws_dirs = {os.path.basename(d): d for d in glob.glob(os.path.join(user, "workspaceStorage", "*"))}
    gdb = os.path.join(user, "globalStorage", "state.vscdb")
    for cid, wid, h in _db_headers_table(gdb):
        add(cid, title=safe_title(h.get("name")), workspace=_ws_folder(ws_dirs[wid]) if wid in ws_dirs else None,
            updated=h.get("lastUpdatedAt") or h.get("createdAt") or 0)
    headers = _db_get(gdb, "composer.composerHeaders")
    for h in (headers.get("allComposers") or []) if isinstance(headers, dict) else []:
        if not isinstance(h, dict) or h.get("isArchived"):
            continue
        uri = ((h.get("workspaceIdentifier") or {}).get("uri") or {})
        add(h.get("composerId"), title=safe_title(h.get("name")), workspace=uri.get("fsPath"),
            updated=h.get("lastUpdatedAt") or h.get("createdAt") or 0)
    for ws_dir in sorted(glob.glob(os.path.join(user, "workspaceStorage", "*"))):
        data = _db_get(os.path.join(ws_dir, "state.vscdb"), "composer.composerData")
        if not isinstance(data, dict):
            continue
        folder = _ws_folder(ws_dir)
        for h in data.get("allComposers") or []:
            if isinstance(h, dict) and not h.get("isArchived"):
                add(h.get("composerId"), title=safe_title(h.get("name")), workspace=folder,
                    updated=h.get("lastUpdatedAt") or h.get("createdAt") or 0)
        for cid in data.get("selectedComposerIds") or []:
            add(cid, workspace=folder, open=True)
        focused = data.get("lastFocusedComposerIds") or []
        if focused:
            add(focused[0], workspace=folder, focused=True, open=True)
    for c in chats.values():
        c["workspace_name"] = ws_name(c["workspace"])
    return sorted(chats.values(), key=lambda c: (not c["open"], -(c["updated"] or 0)))


_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def safe_title(t, n=80):
    """Chat titles are user/agent-generated text: strip control/bidi chars, one line, capped."""
    return norm(_CTRL.sub(" ", t or ""))[:n]


def _resolve_hits(key, hits):
    """One chat, or an error when several match. Never picks among several."""
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        shown = "; ".join("%s %s" % ((t.get("id") or "")[:8], t.get("title") or t.get("workspace_name") or "")
                          for t in hits[:5])
        return None, "%r matches %d chats (%s); use the id from `grok-cursor targets`" % (key, len(hits), shown)
    return None, None


def resolve_target(key, targets):
    """(chat|None, error|None). Matches id, id prefix (>=6), exact title, unique title substring,
    then a unique workspace name (loose). Open tabs win over closed chats. Several hits is an error."""
    k = norm(key).lower()
    exact_id = [t for t in targets if (t.get("id") or "").lower() == k]
    if exact_id:
        return exact_id[0], None
    if len(k) >= 6:
        pre = [t for t in targets if (t.get("id") or "").lower().startswith(k)]
        found, err = _resolve_hits(key, pre)
        if found or err:
            if err and len(pre) > 1:
                return None, "id prefix %r matches %d chats" % (key, len(pre))
            return found, err
    for test in (lambda t: norm(t.get("title")).lower() == k,
                 lambda t: k in norm(t.get("title")).lower()):
        for pool in ([t for t in targets if t.get("open")], targets):
            hits = [t for t in pool if t.get("title") and test(t)]
            found, err = _resolve_hits(key, hits)
            if found or err:
                return found, err
    wk = ws_key(key)
    if wk:
        for test in (lambda t: ws_key(t.get("workspace_name")) == wk,
                     lambda t: wk in ws_key(t.get("workspace_name") or "")):
            for pool in ([t for t in targets if t.get("open")], targets):
                hits = [t for t in pool if t.get("workspace_name") and test(t)]
                found, err = _resolve_hits(key, hits)
                if found or err:
                    return found, err
    return None, None


# ------------------------------------------------------------------ CDP (opt-in)

class WS(object):
    """Minimal RFC 6455 client: enough for Chrome DevTools Protocol on localhost."""

    def __init__(self, url, timeout=10):
        m = re.match(r"ws://(127\.0\.0\.1|localhost|\[::1\]):(\d+)(/.*)", url or "")
        if not m:
            raise SendAbort("cdp", "refusing non-localhost CDP url %r" % url)
        host = m.group(1).strip("[]")
        try:
            self.sock = socket.create_connection((host, int(m.group(2))), timeout=timeout)
        except OSError as exc:
            raise SendAbort("cdp", "CDP connect failed: %s" % exc)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (m.group(3), host, m.group(2), key))
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SendAbort("cdp", "CDP websocket handshake failed")
            head += chunk
        head, _, self.buf = head.partition(b"\r\n\r\n")
        status = head.split(b"\r\n")[0]
        if b" 101" not in status:
            raise SendAbort("cdp", "CDP websocket refused: %s" % status.decode("latin1"))
        want = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if want not in head:
            raise SendAbort("cdp", "bad Sec-WebSocket-Accept")
        self.n = 0

    def _read(self, n):
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise SendAbort("cdp", "CDP timed out")
            if not chunk:
                raise SendAbort("cdp", "CDP connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_frame(self, data, opcode=1):
        if isinstance(data, str):
            data = data.encode()
        hdr = bytes([0x80 | opcode])
        n = len(data)
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv_text(self):
        parts = []
        while True:
            b1, b2 = self._read(2)
            fin, op, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 8:
                raise SendAbort("cdp", "CDP connection closed by Cursor")
            if op == 9:
                self.send_frame(data, opcode=10)
                continue
            if op in (0, 1):
                parts.append(data)
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")

    def call(self, method, params=None):
        self.n += 1
        mid = self.n
        self.send_frame(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.recv_text())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise SendAbort("cdp", "%s: %s" % (method, (msg["error"] or {}).get("message")))
                return msg.get("result") or {}

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


_JS_LIB = r"""
const vis = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
const SUFFIX = /,\s*(?:pinned,\s*)?(?:Chat Editors:\s*)?Editor Group \d+\s*$/i;
const clean = s => (s || '').replace(/\s+/g, ' ').replace(SUFFIX, '').trim();
const idOf = e => e.getAttribute('data-resource-name') || '';
const txt = e => {
  const lab = e.querySelector('.label-name, .tab-label');
  const child = lab && clean(lab.textContent);
  if (child && !/^[0-9a-f-]{36}$/i.test(child)) return child;
  return clean(e.getAttribute('aria-label') || e.innerText || e.textContent || '');
};
const sel = e => e.getAttribute('aria-selected') === 'true' ||
  /(^|\s)(active|selected|is-active|is-selected)(\s|$)/.test(String(e.className || ''));
const tabs = () => [...document.querySelectorAll(TAB_SEL)].filter(vis);
const inputs = () => [...document.querySelectorAll(INPUT_SEL)].filter(vis);
const groups = () => [...document.querySelectorAll('.editor-group-container')];
const lbl = b => ((b.innerText || b.getAttribute('aria-label') || '').split('\n')[0] || '').replace(/\u23ce/g, '').trim();
const GO = /^(Run|Allow|Accept)$/, NO = /^(Skip|Reject)$/;
// sha256-start
function sha256hex(ascii) {
  function rightRotate(value, amount) { return (value >>> amount) | (value << (32 - amount)); }
  var mathPow = Math.pow, maxWord = mathPow(2, 32), i, j, result = '', words = [];
  var asciiBitLength = ascii.length * 8;
  var hash = sha256hex.h = sha256hex.h || [];
  var k = sha256hex.k = sha256hex.k || [];
  var primeCounter = k.length, isComposite = {};
  for (var candidate = 2; primeCounter < 64; candidate++) {
    if (!isComposite[candidate]) {
      for (i = 0; i < 313; i += candidate) isComposite[i] = candidate;
      hash[primeCounter] = (mathPow(candidate, .5) * maxWord) | 0;
      k[primeCounter++] = (mathPow(candidate, 1 / 3) * maxWord) | 0;
    }
  }
  ascii += '\x80';
  while (ascii.length % 64 - 56) ascii += '\x00';
  for (i = 0; i < ascii.length; i++) {
    j = ascii.charCodeAt(i);
    words[i >> 2] |= j << ((3 - i) % 4) * 8;
  }
  words[words.length] = ((asciiBitLength / maxWord) | 0);
  words[words.length] = asciiBitLength;
  for (j = 0; j < words.length;) {
    var w = words.slice(j, j += 16), oldHash = hash;
    hash = hash.slice(0, 8);
    for (i = 0; i < 64; i++) {
      var w15 = w[i - 15], w2 = w[i - 2];
      var a = hash[0], e = hash[4];
      var temp1 = hash[7] + (rightRotate(e, 6) ^ rightRotate(e, 11) ^ rightRotate(e, 25))
        + ((e & hash[5]) ^ ((~e) & hash[6])) + k[i]
        + (w[i] = (i < 16) ? w[i] : (w[i - 16]
          + (rightRotate(w15, 7) ^ rightRotate(w15, 18) ^ (w15 >>> 3)) + w[i - 7]
          + (rightRotate(w2, 17) ^ rightRotate(w2, 19) ^ (w2 >>> 10))) | 0);
      var temp2 = (rightRotate(a, 2) ^ rightRotate(a, 13) ^ rightRotate(a, 22))
        + ((a & hash[1]) ^ (a & hash[2]) ^ (hash[1] & hash[2]));
      hash = [(temp1 + temp2) | 0].concat(hash);
      hash[4] = (hash[4] + temp1) | 0;
    }
    for (i = 0; i < 8; i++) hash[i] = (hash[i] + oldHash[i]) | 0;
  }
  for (i = 0; i < 8; i++) for (j = 3; j + 1; j--) {
    var b = (hash[i] >> (j * 8)) & 255;
    result += ((b < 16) ? 0 : '') + b.toString(16);
  }
  return result;
}
const sha16 = async (text) => {
  const bytes = new TextEncoder().encode(String(text));
  let bin = '';
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return sha256hex(bin).slice(0, 16);
};
// sha256-end
// Question and plan cards have no Run/Skip footer. Edit rows are identified and never clicked.
const cards = async (bar) => {
  const out = [];
  const composer = bar.querySelector('.composer-bar[data-composer-id]') || bar;
  const chatId = composer.getAttribute('data-composer-id') || '';
  const q = bar.querySelector('.composer-questionnaire-toolbar');
  if (q && vis(q)) {
    const options = [...q.querySelectorAll('div[role="button"].composer-questionnaire-toolbar-option')].filter(vis)
      .map(o => clean((o.querySelector('.composer-questionnaire-toolbar-option-label') || {}).textContent || ''))
      .filter(Boolean);
    const acts = [...q.querySelectorAll('.composer-questionnaire-toolbar-actions [data-click-ready]')].filter(vis);
    const names = acts.map(lbl);
    const ready = {};
    for (const b of acts) ready[lbl(b)] = b.getAttribute('data-click-ready') === 'true';
    out.push({root: q, btns: acts, info: {kind: 'question', card_id: '', options: options,
      status: statusOf(chatId), command: clean(q.innerText).slice(0, 600), note: '', buttons: names, ready: ready}});
  }
  for (const foot of bar.querySelectorAll('[data-component="transcript-card-footer"]')) {
    const btns = [...foot.querySelectorAll('button')].filter(vis);
    const names = btns.map(lbl);
    if (!names.some(n => GO.test(n)) || !names.some(n => NO.test(n))) continue;
    const root = foot.closest('[data-component="transcript-card-root"]') || foot.parentElement;
    if (out.some(c => c.root === root)) continue;
    const body = clean((root.innerText || '').replace(foot.innerText || '', ''));
    let note = '';
    for (let p = root.parentElement, i = 0; p && i < 4 && !note; p = p.parentElement, i++)
      if ([...p.children].some(c => c.getAttribute('aria-hidden') === 'true'))
        note = clean((p.innerText || '').replace(root.innerText || '', ''));
    const command = body.slice(0, 600);
    const row = foot.closest('[data-tool-call-id]');
    out.push({root, btns, info: {kind: 'run', card_id: await sha16(chatId + command), options: [],
      status: (row && row.getAttribute('data-tool-status')) || '', command: command,
      note: note.slice(0, 600), buttons: names}});
  }
  for (const row of bar.querySelectorAll('[data-tool-call-id]')) {
    if (row.querySelector('[data-component="transcript-card-footer"]')) continue;
    const toolId = row.getAttribute('data-tool-call-id') || '';
    const toolStatus = row.getAttribute('data-tool-status') || '';
    const split = row.querySelector('[data-component="split-button"]');
    if (split) {
      const btns = [...split.querySelectorAll('button, [role="button"]')].filter(vis);
      const root = row.querySelector('[data-component="transcript-card-root"]') || split;
      out.push({root: split, btns: btns, info: {kind: 'plan', card_id: toolId, options: [],
        status: toolStatus, command: clean(root.innerText).slice(0, 600), note: '', buttons: btns.map(lbl)}});
      continue;
    }
    const editRoot = row.querySelector('[data-component="transcript-card-root"]');
    if (!editRoot) continue;
    out.push({root: editRoot, btns: [], info: {kind: 'edit', card_id: toolId, options: [],
      status: toolStatus, command: clean(editRoot.innerText).slice(0, 600), note: '', buttons: []}});
  }
  return out;
};
// Cards render in the transcript, beside (not inside) the chat's composer bar. A chat's scope is the
// largest ancestor of its bar that contains no other chat's bar, capped at its editor group.
const barOf = id => {
  let p = document.querySelector('.composer-bar[data-composer-id="' + id + '"]');
  while (p && p.parentElement && !p.classList.contains('editor-group-container') &&
         p.parentElement.querySelectorAll('.composer-bar').length === 1) p = p.parentElement;
  return p;
};
const statusOf = id => {
  const b = document.querySelector('.composer-bar[data-composer-id="' + id + '"]');
  return (b && b.getAttribute('data-composer-status')) || '';
};
"""
JS = {
    "tabs": "const g = groups();"
            " return tabs().map(e => ({id: idOf(e), text: txt(e), selected: sel(e),"
            " group: g.indexOf(e.closest('.editor-group-container')),"
            # a chat waiting on an approval card shows a question-mark icon on its tab
            " waiting: !!e.querySelector('.composer-tab-label.codicon-question')}));",
    # Cursor's tabs ignore element.click(); they activate on a trusted mousedown. Return where to
    # press, and refuse if anything else (a close button, an overlay) sits on that point.
    "tabpoint": "const t = A.id ? tabs().filter(e => idOf(e) === A.id) : tabs().filter(e => txt(e) === clean(A.title));"
                " if (t.length !== 1) return {n: t.length};"
                " if (A.scroll) t[0].scrollIntoView({block: 'nearest', inline: 'nearest'});"
                " const lab = t[0].querySelector('.label-name, .monaco-icon-label, .tab-label') || t[0];"
                " const r = lab.getBoundingClientRect();"
                " const x = r.left + Math.min(r.width / 2, 40), y = r.top + r.height / 2;"
                " const strip = t[0].parentElement.getBoundingClientRect();"
                " const off = x < strip.left || x > strip.right || x < 0 || x > innerWidth;"
                " const at = off ? null : document.elementFromPoint(x, y);"
                " const ok = !!at && t[0].contains(at) && !at.closest('.tab-actions, .codicon-close, [aria-label^=\"Close\"]');"
                " return {n: 1, x: x, y: y, ok: ok, off: off};",
    "cards": "const bar = barOf(A.id || '');"
             " if (!bar) return {found: false};"
             " const found = await cards(bar);"
             " return {found: true, status: statusOf(A.id),"
             " cards: found.map(c => c.info)};",
    # Clicks one button by exact label, inside the only run card of that chat.
    # Edit rows are identified by cards() and never clicked. Never clicks "Always…".
    "press_card": "const bar = barOf(A.id || '');"
                  " if (!bar) return {found: false};"
                  " const cs = (await cards(bar)).filter(c => c.info.kind === 'run');"
                  " if (cs.length !== 1) return {found: true, n: cs.length};"
                  " const hit = cs[0].btns.filter(b => A.labels.includes(lbl(b)) && !/^always/i.test(lbl(b)));"
                  " if (hit.length !== 1) return {found: true, n: 1, hits: hit.length, buttons: cs[0].info.buttons};"
                  " hit[0].click();"
                  " return {found: true, n: 1, hits: 1, clicked: lbl(hit[0]), command: cs[0].info.command};",
    # Question: re-find the one toolbar, click the option whose label is exactly A.label. Not a coordinate.
    "press_option": "const bar = barOf(A.id || '');"
                    " if (!bar) return {found: false};"
                    " const label = String(A.label || '');"
                    " if (/^always/i.test(label)) return {found: true, hits: 0, refused: 'always'};"
                    " const cs = (await cards(bar)).filter(c => c.info.kind === 'question');"
                    " if (cs.length !== 1) return {found: true, n: cs.length};"
                    " const opts = [...cs[0].root.querySelectorAll('div[role=\"button\"].composer-questionnaire-toolbar-option')].filter(vis);"
                    " const hit = opts.filter(o => clean((o.querySelector('.composer-questionnaire-toolbar-option-label') || {}).textContent || '') === label);"
                    " if (hit.length !== 1) return {found: true, n: 1, hits: hit.length, options: cs[0].info.options};"
                    " hit[0].click();"
                    " const again = (await cards(bar)).filter(c => c.info.kind === 'question');"
                    " const ready = again.length === 1 && again[0].info.ready && again[0].info.ready.Continue === true;"
                    " return {found: true, n: 1, hits: 1, clicked: label, continue_ready: !!ready};",
    # Continue only when this re-read says data-click-ready is true. Edit rows are not buttons here.
    "press_continue": "const bar = barOf(A.id || '');"
                      " if (!bar) return {found: false};"
                      " const cs = (await cards(bar)).filter(c => c.info.kind === 'question');"
                      " if (cs.length !== 1) return {found: true, n: cs.length};"
                      " const acts = [...cs[0].root.querySelectorAll('.composer-questionnaire-toolbar-actions [data-click-ready]')].filter(vis);"
                      " const hit = acts.filter(b => lbl(b) === 'Continue' && b.getAttribute('data-click-ready') === 'true' && !/^always/i.test(lbl(b)));"
                      " if (hit.length !== 1) return {found: true, n: 1, hits: hit.length, ready: cs[0].info.ready};"
                      " hit[0].click();"
                      " return {found: true, n: 1, hits: 1, clicked: 'Continue'};",
    # Plan: one split-button, exact label (Build or Reject). Never Open menu, never an edit row.
    "press_plan": "const bar = barOf(A.id || '');"
                  " if (!bar) return {found: false};"
                  " const label = String(A.label || '');"
                  " if (/^always/i.test(label)) return {found: true, hits: 0, refused: 'always'};"
                  " let cs = (await cards(bar)).filter(c => c.info.kind === 'plan');"
                  " if (A.card_id) cs = cs.filter(c => c.info.card_id === A.card_id);"
                  " if (cs.length !== 1) return {found: true, n: cs.length};"
                  " const hit = cs[0].btns.filter(b => lbl(b) === label && !/^always/i.test(lbl(b)));"
                  " if (hit.length !== 1) return {found: true, n: 1, hits: hit.length, buttons: cs[0].info.buttons};"
                  " hit[0].click();"
                  " return {found: true, n: 1, hits: 1, clicked: lbl(hit[0]), card_id: cs[0].info.card_id};",
    # Same shape as input, without focusing the composer. selftest uses this.
    "input_shape": "const id = A.id || '';"
             " const all = inputs();"
             " let e = null;"
             " if (id) {"
             "   const bars = [...document.querySelectorAll('.composer-bar[data-composer-id=\"' + id + '\"]')].filter(vis);"
             "   if (bars.length > 1) return {n: bars.length, owned: false};"
             "   if (bars.length === 1) {"
             "     const inside = all.filter(el => bars[0].contains(el));"
             "     if (inside.length !== 1) return {n: inside.length, owned: true};"
             "     e = inside[0];"
             "   } else return {n: all.length, owned: false};"
             " } else {"
             "   const f = all.filter(el => el.contains(document.activeElement));"
             "   if (f.length === 1) e = f[0];"
             "   else if (all.length === 1) e = all[0];"
             "   else return {n: all.length, owned: false};"
             " }"
             " return {n: 1, text: (e.innerText || '').trim(), focused: e.contains(document.activeElement)};",
    # Every workbench document, including a same-origin iframe. A bad selector is missing, not a throw.
    "selectors": "const docs = [];"
             " const add = d => { if (d && docs.indexOf(d) < 0) docs.push(d); };"
             " add(document);"
             " for (const frame of [...document.querySelectorAll('iframe')]) {"
             "   try { add(frame.contentDocument); } catch (err) {}"
             " }"
             " const missing = [];"
             " for (const s of (A.selectors || [])) {"
             "   let hit = false;"
             "   for (const doc of docs) {"
             "     try { if (doc.querySelector(s)) { hit = true; break; } } catch (err) {}"
             "   }"
             "   if (!hit) missing.push(s);"
             " }"
             " return {missing: missing, documents: docs.length};",
    "input": "const id = A.id || '';"
             " const all = inputs();"
             " let e = null;"
             " if (id) {"
             "   const bars = [...document.querySelectorAll('.composer-bar[data-composer-id=\"' + id + '\"]')].filter(vis);"
             "   if (bars.length > 1) return {n: bars.length, owned: false};"
             "   if (bars.length === 1) {"
             "     const inside = all.filter(el => bars[0].contains(el));"
             "     if (inside.length !== 1) return {n: inside.length, owned: true};"
             "     e = inside[0];"
             "   } else return {n: all.length, owned: false};"
             " } else {"
             "   const f = all.filter(el => el.contains(document.activeElement));"
             "   if (f.length === 1) e = f[0];"
             "   else if (all.length === 1) e = all[0];"
             "   else return {n: all.length, owned: false};"
             " }"
             " e.focus();"
             " return {n: 1, text: (e.innerText || '').trim(), focused: e.contains(document.activeElement)};",
    "status": "const id = A.id || '';"
              " const bar = id && document.querySelector('.composer-bar[data-composer-id=\"' + id + '\"]');"
              " if (!bar) return {found: false, empty: false, status: ''};"
              " const input = bar.querySelector('.aislash-editor-input');"
              " const text = ((input && input.innerText) || '').replace(/\\s+/g, ' ').trim();"
              " return {found: true, empty: text.length === 0, status: bar.getAttribute('data-composer-status') || ''};",
    # The open slash menu: its focused row and every command row, each by its first word.
    # "Create /x skill" starts with "Create", so it can never match a command.
    "slash_menu": "const ms = [...document.querySelectorAll('.ui-menu[role=menu]')].filter(vis);"
                  " if (ms.length !== 1) return {open: ms.length};"
                  " const tok = r => (r.innerText || '').trim().split(/\\s/)[0] || '';"
                  " const rows = [...ms[0].querySelectorAll('[role=menuitem]')].filter(vis);"
                  " const f = rows.find(r => r.getAttribute('data-focused') === 'true');"
                  " if (!f) return {open: 1, focused: '', names: rows.map(tok).filter(t => t[0] === '/')};"
                  # Enter on the row is unreliable (it can close the menu with no pick); a trusted
                  # press works. Press only if that point hits the focused row itself.
                  " const b = f.getBoundingClientRect(), x = b.left + Math.min(20, b.width / 2), y = b.top + b.height / 2;"
                  " return {open: 1, focused: tok(f), names: rows.map(tok).filter(t => t[0] === '/'),"
                  " x: x, y: y, ok: f.contains(document.elementFromPoint(x, y))};",
    # Command pills in the focused chat input after a menu pick.
    "slash_pill": "const e = inputs().find(el => el.contains(document.activeElement));"
                  " if (!e) return {n: 0};"
                  " return {n: 1, text: (e.innerText || '').trim(),"
                  " pills: [...e.querySelectorAll('[data-mention-name]')].map(p => p.getAttribute('data-mention-name'))};",
}


def page_lib():
    """Page script. GROK_CURSOR_JS_OVERRIDE replaces it for the selector-break test only.
    The installer never sets that variable."""
    override = os.environ.get("GROK_CURSOR_JS_OVERRIDE")
    if not override:
        return _JS_LIB
    with open(override, encoding="utf-8") as fh:
        return fh.read()


def cdp_js(op, **args):
    tab_sel = os.environ.get("GROK_CURSOR_CDP_TAB_SEL",
                             '[role="tab"], .composer-tab, .agent-sidebar-cell, [data-composer-id]')
    inp_sel = os.environ.get("GROK_CURSOR_CDP_INPUT_SEL",
                             '.aislash-editor-input, [contenteditable="true"].ProseMirror, '
                             '[data-lexical-editor="true"], .composer-input [contenteditable="true"]')
    marker = json.dumps(dict(op=op, **args))
    return "/*gc:%s*/(async () => {const TAB_SEL = %s, INPUT_SEL = %s;%s\nconst A = %s;\n%s\n})()" % (
        marker, json.dumps(tab_sel), json.dumps(inp_sel), page_lib(), marker, JS[op])


def cdp_listening(port):
    try:
        sock = socket.create_connection(("127.0.0.1", int(port)), 0.3)
    except OSError:
        return False
    sock.close()
    return True


def auto_cdp_port():
    """DevTools port to use when the caller did not pass --cdp.

    Unit tests set GROK_CURSOR_AX_FAKE, and that must not open the real Cursor
    port. GROK_CURSOR_CDP=0 forces the Accessibility fallback.
    """
    if os.environ.get("GROK_CURSOR_CDP", "1") == "0":
        return None
    if os.environ.get("GROK_CURSOR_AX_FAKE"):
        return None
    raw = os.environ.get("GROK_CURSOR_CDP_PORT", "9222")
    try:
        port = int(raw)
    except ValueError:
        return None
    if port <= 0 or not cdp_listening(port):
        return None
    return port


def cdp_window_label(title):
    """Workspace name in a DevTools page title.

    `folder — Cursor` is the folder. `file — Name (Workspace)` is the name.
    """
    t = norm(title).lstrip("\u25cf ")
    if t.lower().endswith(" \u2014 cursor"):
        return t[: -len(" \u2014 cursor")].strip()
    return window_label(title)


def pick_cdp_page(pages, window_hint=None, workspace_name=None):
    """Choose one workbench page. A chat's workspace wins over a --repo basename.

    `--repo ~/Documents/Cursor_AI_Brain` is not in the window title
    `… — Acme-IO-Site (Workspace)`, so a basename filter matches nothing.
    The chat id's workspace name does match that title.
    """
    pages = [p for p in pages if isinstance(p, dict) and p.get("type") == "page"
             and "workbench" in (p.get("url") or "")]
    if workspace_name:
        labels = [cdp_window_label(p.get("title")) for p in pages]
        idx, err = match_workspace(workspace_name, labels)
        if idx is not None:
            return pages[idx]
        if err and "matches" in err:
            raise SendAbort("cdp", err)
        raise SendAbort("cdp", "no open Cursor window for workspace %r (open: %s)" % (
            workspace_name, ", ".join(repr(label) for label in labels) or "none"))
    if window_hint:
        hint = os.path.basename(str(window_hint).rstrip("/")).lower()
        pages = [p for p in pages if hint and hint in (p.get("title") or "").lower()]
    if len(pages) != 1:
        raise SendAbort("cdp", "%d Cursor workbench windows match; pass --repo to pick one" % len(pages))
    return pages[0]


def cdp_page(port, window_hint=None, workspace_name=None):
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=3) as r:
            pages = json.loads(r.read().decode())
    except Exception as exc:
        raise SendAbort("cdp", "no CDP on 127.0.0.1:%d (%s). Quit Cursor and start it with "
                        "`open -a Cursor --args --remote-debugging-port=%d`" % (port, exc, port))
    return pick_cdp_page(pages, window_hint, workspace_name)


def cdp_eval(ws, op, **args):
    res = ws.call("Runtime.evaluate", {"expression": cdp_js(op, **args), "returnByValue": True,
                                  "awaitPromise": True})
    if res.get("exceptionDetails"):
        raise SendAbort("cdp", "page script failed (%s)" % op)
    return (res.get("result") or {}).get("value")


def cdp_tab_hits(tabs, title, chat_id):
    """Prefer an exact chat id over a title. Title is the fallback only when the page
    exposes no chat ids; a similar title must not win over the id."""
    tabs = [t for t in (tabs or []) if isinstance(t, dict)]
    if chat_id and any(t.get("id") for t in tabs):
        return [t for t in tabs if t.get("id") == chat_id]
    return [t for t in tabs if CursorAX.title_match(t.get("text"), title)]

def tab_ref(tab, title=None):
    ref = {"title": tab.get("text") or title or ""}
    if tab.get("id"):
        ref["id"] = tab["id"]
    return ref

def cdp_press(ws, x, y):
    for kind in ("mousePressed", "mouseReleased"):
        ws.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1})

def cdp_select_tab(ws, tab, title=None, chat_id=None):
    """Bring an open but backgrounded chat tab to the front and wait until Cursor reports it
    selected (and, for a chat id, its composer is mounted). Returns the tab that was selected in
    the same editor group before, so the caller can switch back."""
    tabs = [t for t in (cdp_eval(ws, "tabs") or []) if isinstance(t, dict)]
    prev = next((t for t in tabs if t.get("selected") and t.get("group") == tab.get("group")
                 and t.get("id") != tab.get("id")), None)
    pt = cdp_eval(ws, "tabpoint", scroll=True, **tab_ref(tab, title)) or {}
    if pt.get("n") != 1:
        raise SendAbort("switch_unverified", "%s tabs match %r; refusing to press" % (pt.get("n", 0), title))
    if not pt.get("ok"):
        raise SendAbort("switch_unverified", "tab %r is covered or its close button is under the press "
                        "point; refusing to press" % (tab.get("text") or title))
    cdp_press(ws, pt["x"], pt["y"])
    deadline = time.time() + max(delay(2.0), 0.3)
    while True:
        again = cdp_tab_hits(cdp_eval(ws, "tabs"), title, chat_id)
        mounted = not chat_id or (cdp_eval(ws, "status", id=chat_id) or {}).get("found")
        if len(again) == 1 and again[0].get("selected") and mounted:
            return prev
        if time.time() >= deadline:
            raise SendAbort("switch_unverified", "tab %r not selected after click" % (tab.get("text") or title))
        time.sleep(0.1)


# ------------------------------------------------------------------ post-send verification

# A finished successful turn. `cancelled` and error states are not success.
_IDLE_OK = {"", "completed", "idle", "none", "ready", "done"}
_BAD_STATUS = {"cancelled", "canceled", "error", "failed", "aborted"}


def focused_chat_id():
    for chat in list_targets():
        if chat.get("focused") and chat.get("id"):
            return chat["id"]
    return None


def transcript_paths(cursor_home, chat_id):
    if not cursor_home or not chat_id:
        return []
    return glob.glob(os.path.join(cursor_home, "projects", "*", "agent-transcripts", chat_id, chat_id + ".jsonl"))


def _user_turn_text(obj):
    if not isinstance(obj, dict) or obj.get("role") != "user":
        return ""
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in (None, "text"):
                parts.append(part.get("text") or "")
        return "\n".join(parts)
    return ""


def _turn_error(obj):
    """Error text from a transcript line written after Return, or None."""
    if not isinstance(obj, dict):
        return None
    kind = str(obj.get("type") or "").lower()
    status = str(obj.get("status") or "").lower()
    if kind == "turn_ended" and status in ("error", "failed", "cancelled", "canceled"):
        err = str(obj.get("error") or obj.get("message") or status)
        # Cursor closes the previous turn this way when our prompt arrives: proof of delivery.
        return None if err == "new_message_submitted" else err
    if kind == "error" or obj.get("role") == "error":
        msg = obj.get("error") or obj.get("message") or "error"
        if isinstance(msg, dict):
            msg = msg.get("text") or msg.get("message") or "error"
        return str(msg or "error")
    return None


def new_transcript_signal(cursor_home, chat_id, prompt, marks):
    """('user', None), ('error', message), or (None, None) from bytes appended after Return.
    An error in those bytes wins over a user turn."""
    saw_user = False
    error = None
    for path in transcript_paths(cursor_home, chat_id):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        start = marks.get(path, 0)
        if size <= start:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                fh.seek(start)
                chunk = fh.read()
        except OSError:
            continue
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if error is None:
                error = _turn_error(obj)
            if text_matches(_user_turn_text(obj), prompt):
                saw_user = True
    if error:
        return "error", norm(error)[:300]
    if saw_user:
        return "user", None
    return None, None


def cdp_phase(snap):
    """'busy', 'idle', 'bad', or None. `cancelled` and other error statuses are 'bad', never idle."""
    if not isinstance(snap, dict) or not snap.get("found") or not snap.get("empty"):
        return None
    status = str(snap.get("status") or "").lower()
    if status in _BAD_STATUS:
        return "bad"
    if status in _IDLE_OK:
        return "idle"
    return "busy"


def cdp_composer_status(port, chat_id, window_hint, workspace_name=None):
    page = cdp_page(port, window_hint, workspace_name)
    ws = WS(page.get("webSocketDebuggerUrl"))
    try:
        return cdp_eval(ws, "status", id=chat_id) or {}
    finally:
        ws.close()


# A chat that turns busy right after Return took the prompt. An invalid-model error also flips
# busy, but drops to `cancelled` within a few hundred ms, so busy must hold this long.
BUSY_HOLD = 1.5

def confirm_delivery(prompt, timeout, chat_id, cursor_home, status_before, cdp_poll, marks):
    """(chat_id, 'cdp'|'transcript') or (None, None). Raises SendAbort('failed') on a transcript error.
    Delivered means a new user turn with the prompt, or an empty input whose chat went busy after
    Return and either stayed busy for BUSY_HOLD seconds or came back to a successful idle.
    `cancelled` and turn_ended errors are not delivered. If the chat was already busy before
    Return, busy proves nothing (the prompt may only be queued), so only the transcript counts.
    `marks` is each transcript's size before Return."""
    marks = dict(marks or {})
    deadline = time.time() + timeout
    was_busy = cdp_phase({"found": True, "empty": True, "status": status_before}) == "busy" \
        if status_before else False
    saw_busy = False
    busy_since = None
    while True:
        if chat_id:
            kind, detail = new_transcript_signal(cursor_home, chat_id, prompt, marks)
            if kind == "error":
                raise SendAbort("failed", "failed: %s" % (detail or "turn ended"))
            if kind == "user":
                return chat_id, "transcript"
        if cdp_poll is not None:
            try:
                snap = cdp_poll()
            except SendAbort:
                snap = None
            phase = cdp_phase(snap)
            if was_busy:
                pass
            elif phase == "busy":
                saw_busy = True
                busy_since = busy_since or time.time()
                if time.time() - busy_since >= BUSY_HOLD:
                    return chat_id or "", "cdp"
            elif phase == "idle" and saw_busy:
                return chat_id or "", "cdp"
            elif phase == "bad":
                saw_busy, busy_since = False, None
        if time.time() >= deadline:
            return None, None
        time.sleep(0.2)


# ------------------------------------------------------------------ the send flow

class Sender(object):
    def __init__(self, args, prompt):
        self.a, self.prompt = args, prompt
        self.steps = []
        self.target = None          # resolved chat dict from state DB
        self.dry = args.dry_run
        self.saved_clip = None
        self.pressed = False        # True once Return/Enter has been sent
        self.chat_id = None
        self.status_before = None
        self.prev_tab = None        # tab that was in view before a background-tab switch

    def step(self, s):
        self.steps.append(s)

    # -- shared preflight
    def preflight_mac(self, need_unlocked=True):
        plat = os.environ.get("GROK_CURSOR_PLATFORM", sys.platform)
        if plat != "darwin":
            raise SendAbort("not_macos", "UI send works on macOS only; use --headless")
        if self.dry:
            self.step("would run: caffeinate -u -t 120")
        else:
            try:
                subprocess.Popen(["caffeinate", "-u", "-t", "120"], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                self.step("caffeinate -u: ok (display woken, awake for 120s)")
            except OSError as exc:
                raise SendAbort("caffeinate", "could not start caffeinate: %s" % exc)
            time.sleep(delay(1.0))
        cs = console_state()
        if cs["on_console"] is False:
            raise SendAbort("console", "this user is not the active console session (fast user switching, "
                            "or logged out); UI automation would go nowhere")
        if need_unlocked:
            if cs["locked"] is None:
                raise SendAbort("locked", "could not determine lock state (ioreg failed); refusing to type blind")
            if cs["locked"]:
                raise SendAbort("locked", "screen is locked; unlock the Mac, or use --headless")
            self.step("screen unlocked: ok")
        pid = cursor_pid()
        if not pid:
            raise SendAbort("not_running", "%s is not running; open it first, or use --headless" % app_name())
        self.step("%s running (pid %d): ok" % (app_name(), pid))
        return pid

    def resolve_to(self):
        if not self.a.to:
            return None
        chat, err = resolve_target(self.a.to, list_targets())
        if err:
            raise SendAbort("target", err)
        if chat:
            self.target = chat
            self.step("target chat: %s %r%s" % (chat["id"][:8], chat["title"],
                                                " (open tab)" if chat["open"] else ""))
            if not chat["title"]:
                if self.a.cdp:
                    return ""
                raise SendAbort("target", "chat %s has no title yet; can't find its tab" % chat["id"][:8])
            return chat["title"]
        self.step("target %r not in Cursor's state DB; matching on-screen tab titles" % self.a.to)
        return self.a.to

    def open_repo(self):
        if not self.a.repo:
            return
        if self.dry:
            self.step("would open repo: %s" % self.a.repo)
            return
        p = run(["open", "-a", app_name(), self.a.repo])
        if not ok(p):
            raise SendAbort("repo", "could not open %s in %s" % (self.a.repo, app_name()))
        self.step("opened repo: %s" % self.a.repo)
        time.sleep(delay(3.0))

    def bring_front(self):
        if self.dry:
            front, name = is_cursor_front()
            self.step("would activate %s (frontmost now: %s)" % (app_name(), name or "unknown"))
            return
        osa("tell application %s to activate" % applescript_str(app_name()))
        time.sleep(delay(0.6))
        front, name = is_cursor_front()
        if not front:
            raise SendAbort("not_frontmost", "%s is not frontmost after activate (frontmost: %s)"
                            % (app_name(), name or "unknown"))
        self.step("%s frontmost: ok" % app_name())

    def new_keys(self):
        return os.environ.get("GROK_CURSOR_NEW_CHAT_KEYS", "cmd+n")

    def submit_keys(self):
        return "cmd+return" if self.a.now else "return"

    # -- AX strategy
    def run_ax(self):
        pid = self.preflight_mac()
        title = self.resolve_to()
        self.open_repo()
        self.bring_front()
        be = ax_backend()
        if be is None:
            raise SendAbort("no_accessibility", "Accessibility API unavailable on this system")
        if not be.trusted():
            raise SendAbort("no_accessibility", "this process is not trusted for Accessibility. Add the app that "
                            "runs grok-cursor (Terminal/iTerm, or your agent's local-exec host app) under System "
                            "Settings > Privacy & Security > Accessibility. `grok-cursor doctor --send` shows the "
                            "process chain.")
        cur = CursorAX(be, pid, self.a.repo)
        win = cur.window()
        self.step("window: %r%s" % (win.title, "" if cur.manual else " (AXManualAccessibility not acknowledged)"))

        if title:
            self.switch_tab(cur, title)
            win = cur.window()

        inp = cur.chat_input(win)
        if self.a.new:
            if self.dry:
                self.step("would focus the chat input and press %s for a new chat" % self.new_keys())
            else:
                self.focus_input(cur, inp)
                keys(self.new_keys())
                time.sleep(delay(0.8))
                win = cur.window()
                inp = cur.chat_input(win)
                self.step("new chat (%s): ok" % self.new_keys())

        if norm(inp.value):
            raise SendAbort("input", "chat input already has %d chars of text (a draft?); refusing to touch it"
                            % len(norm(inp.value)))
        method = self.pick_method()
        if self.dry:
            self.step("chat input found and empty: %s" % inp.describe())
            self.step("would insert prompt via %s, verify it, then press %s" % (method, self.submit_keys()))
            return
        self.focus_input(cur, inp)
        if method == "paste":
            self.paste()
        else:
            self.deeplink()
        time.sleep(delay(0.6))
        win = cur.window()
        holder = [n for n in cur.inputs(win) if text_matches(n.value, self.prompt)]
        if len(holder) != 1:
            self.restore_clipboard()
            raise SendAbort("input", "prompt did not appear in exactly one chat input after %s (found %d); "
                            "nothing was sent. The text may be sitting in an input: check Cursor"
                            % (method, len(holder)))
        if not cur.is_focused(holder[0]):
            self.focus_input(cur, holder[0])
        if title:
            self.verify_selected(cur, title, final=True)
        front, name = is_cursor_front()
        if not front:
            self.restore_clipboard()
            raise SendAbort("not_frontmost", "focus moved to %s before Return; prompt left unsent in the chat box"
                            % (name or "unknown"))
        self.step("prompt is in the focused chat input: verified")
        keys(self.submit_keys())
        self.pressed = True
        self.step("%s pressed" % self.submit_keys())
        self.restore_clipboard()

    def focus_input(self, cur, inp):
        if cur.is_focused(inp):
            return
        inp.focus()
        time.sleep(delay(0.3))
        if not cur.is_focused(inp):
            raise SendAbort("input", "could not focus the chat input (AXFocused not honored)")
        self.step("chat input focused")

    def switch_tab(self, cur, title):
        hits = cur.find_tab(cur.window(), title)
        if not hits:
            raise SendAbort("target", "no tab/sidebar entry titled %r is visible. Is it open? "
                            "(`grok-cursor targets --ax` lists what's on screen)" % title)
        if len(hits) > 1:
            raise SendAbort("target", "%d on-screen elements match %r; refusing to guess" % (len(hits), title))
        tab = hits[0]
        label = cur.tab_label(tab)
        if tab.selected:
            self.step("target tab already active: %r" % label)
            return
        if self.dry:
            self.step("would click tab: %s" % tab.describe())
            return
        pressed = any(el.press() for el in [tab] + list(tab.ancestors())[:2])
        if pressed:
            time.sleep(delay(0.8))
            hits = cur.find_tab(cur.window(), title)
            if len(hits) == 1 and hits[0].selected:
                self.step("switched to tab %r: verified" % cur.tab_label(hits[0]))
                return
        self.step("tab click %s; falling back to stepping with %s"
                  % ("had no visible effect" if pressed else "failed", self.step_keys()))
        self.step_to_tab(cur, title)

    def step_keys(self):
        return os.environ.get("GROK_CURSOR_NEXT_CHAT_KEYS", "cmd+]")

    def step_to_tab(self, cur, title):
        """Cmd+] (next chat) until the selected tab is the target. Every step re-reads the
        selected tab; aborts if selection can't be read, the target never comes up, or
        the tabs cycle back to where we started."""
        def selected():
            sel = [t for t in cur.tabs(cur.window()) if t.selected]
            return cur.tab_label(sel[0]) if len(sel) == 1 else None
        start = selected()
        if start is None:
            raise SendAbort("switch_unverified", "can't tell which chat tab is selected; refusing to step blindly")
        # chat shortcuts only fire with the chat focused
        self.focus_input(cur, cur.chat_input(cur.window()))
        limit = max(1, min(int(os.environ.get("GROK_CURSOR_MAX_STEPS", "12")), len(cur.tabs(cur.window())) or 1))
        for i in range(limit):
            keys(self.step_keys())
            time.sleep(delay(0.5))
            now = selected()
            if now is None:
                raise SendAbort("switch_unverified", "lost track of the selected tab after %d step(s)" % (i + 1))
            if CursorAX.title_match(now, title):
                self.step("stepped %d tab(s) to %r: verified" % (i + 1, now))
                return
            if now == start:
                break
        raise SendAbort("switch_unverified", "stepped through the tabs without reaching %r; stopped at %r"
                        % (title, selected()))

    def verify_selected(self, cur, title, final=False):
        if self.dry:
            return
        hits = cur.find_tab(cur.window(), title)
        if len(hits) != 1 or not hits[0].selected:
            raise SendAbort("switch_unverified", "%s the target tab %r is not reported as selected; refusing "
                            "to type" % ("before Return," if final else "after clicking,", title))

    def pick_method(self):
        m = self.a.method
        if m == "auto":
            m = "paste"
            if not self.a.clobber_clipboard and clipboard_is_text_only() is not True:
                if self.a.to or self.a.new:
                    raise SendAbort("clipboard", "clipboard holds non-text data (image/file) that can't be "
                                    "restored; pass --clobber-clipboard to proceed")
                m = "deeplink"          # deep link doesn't touch the clipboard
        if m == "deeplink" and len(DEEPLINK + quote(self.prompt, safe="")) > DEEPLINK_MAX_URL:
            raise SendAbort("too_long", "prompt is too long for a deep link (%d-char URL limit); use "
                            "--method paste" % DEEPLINK_MAX_URL)
        return m

    def paste(self):
        if not self.a.clobber_clipboard:
            if clipboard_is_text_only() is not True:
                raise SendAbort("clipboard", "clipboard holds non-text (or unreadable) data that can't be restored; use "
                                "--method deeplink or --clobber-clipboard")
            self.saved_clip = clipboard_get()
        if not clipboard_set(self.prompt):
            raise SendAbort("clipboard", "pbcopy failed")
        keys(os.environ.get("GROK_CURSOR_PASTE_KEYS", "cmd+shift+v"))
        self.step("pasted prompt (%s)" % os.environ.get("GROK_CURSOR_PASTE_KEYS", "cmd+shift+v"))

    def restore_clipboard(self):
        if self.saved_clip is not None:
            time.sleep(delay(0.3))
            clipboard_set(self.saved_clip)
            self.saved_clip = None
            self.step("clipboard restored")

    def deeplink(self):
        p = run(["open", DEEPLINK + quote(self.prompt, safe="")])
        if not ok(p):
            raise SendAbort("deeplink", "opening the cursor:// deep link failed")
        time.sleep(delay(1.5))
        self.step("prompt prefilled via deep link")

    # -- CDP strategy (primary when 127.0.0.1:9222 is open)
    def run_cdp(self):
        plat = os.environ.get("GROK_CURSOR_PLATFORM", sys.platform)
        if self.a.new:
            raise SendAbort("cdp", "--new is not supported with --cdp yet")
        title = self.resolve_to()
        if plat == "darwin":
            self.preflight_mac(need_unlocked=True)
        self.step("CDP on 127.0.0.1:%d: skipping frontmost and Accessibility checks" % self.a.cdp)
        workspace = (self.target or {}).get("workspace_name")
        page = cdp_page(self.a.cdp, self.a.repo, workspace)
        ws = WS(page.get("webSocketDebuggerUrl"))
        try:
            self.step("CDP window: %r" % page.get("title"))
            cid = self.target["id"] if self.target else None
            hits = None
            if title or cid:
                hits = cdp_tab_hits(cdp_eval(ws, "tabs"), title, cid)
                if len(hits) != 1:
                    raise SendAbort("target", "%d tabs match %r via CDP" % (len(hits), title))
                if hits[0].get("selected"):
                    self.step("target tab already active")
                elif self.dry:
                    pt = cdp_eval(ws, "tabpoint", **tab_ref(hits[0], title)) or {}
                    if pt.get("n") != 1 or not (pt.get("ok") or pt.get("off")):
                        raise SendAbort("switch_unverified", "background tab %r can't be pressed safely" % title)
                    self.step("would switch to background tab %r%s" % (
                        hits[0]["text"], " (scrolled out of the tab strip; would scroll it in)" if pt.get("off") else ""))
                else:
                    self.prev_tab = cdp_select_tab(ws, hits[0], title, cid)
                    self.step("switched to tab %r: verified" % (hits[0].get("text") or title))
            inp = cdp_eval(ws, "input", id=cid or "") or {}
            if inp.get("n") != 1:
                # The target tab is not mounted yet. A dry run must not borrow another chat's input.
                if self.dry and cid and hits and len(hits) == 1 and not hits[0].get("selected"):
                    self.step("would use the composer input for chat %s" % cid[:8])
                    self.step("would insert text and press Enter via CDP")
                    return
                if inp.get("owned"):
                    raise SendAbort("input", "the target chat's composer has %s inputs; refusing to guess"
                                    % inp.get("n", 0))
                raise SendAbort("input", "%s chat inputs found via CDP and none is the target chat's; refusing to guess"
                                % inp.get("n", 0))
            self.step("chat input for %s" % (cid[:8] if cid else "this chat"))
            if norm(inp.get("text")):
                raise SendAbort("input", "chat input already has text; refusing to overwrite it")
            slash = SLASH_RE.match(self.prompt)
            if self.dry:
                self.step("would pick /%s from the slash menu, then press Enter via CDP" % slash.group(1)
                          if slash else "would insert text and press Enter via CDP")
                return
            if slash:
                self.cdp_slash(ws, cid, slash.group(1), slash.group(2) or "")
            else:
                ws.call("Input.insertText", {"text": self.prompt})
                time.sleep(delay(0.4))
                inp = cdp_eval(ws, "input", id=cid or "") or {}
                if not inp.get("focused") or not text_matches(inp.get("text"), self.prompt):
                    raise SendAbort("input", "prompt did not land in the focused chat input via CDP; nothing sent")
                self.step("prompt in chat input: verified")
            self.chat_id = (self.target or {}).get("id")
            if not self.chat_id:
                chosen = [t for t in (cdp_eval(ws, "tabs") or []) if t.get("selected") and t.get("id")]
                if len(chosen) == 1:
                    self.chat_id = chosen[0]["id"]
            before = cdp_eval(ws, "status", id=self.chat_id or "") or {}
            self.status_before = before.get("status") or ""
            mods = 4 if self.a.now else 0          # 4 = Meta (Cmd)
            for t in ("keyDown", "keyUp"):
                ev = {"type": t, "key": "Enter", "code": "Enter", "modifiers": mods,
                      "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}
                if t == "keyDown":
                    ev["text"] = "\r"
                ws.call("Input.dispatchKeyEvent", ev)
            self.pressed = True
            self.step("Enter dispatched via CDP")
        finally:
            ws.close()

    def cdp_slash(self, ws, cid, name, rest):
        """Type `/name`, wait for Cursor's slash menu to focus exactly that command, press that row
        (it becomes a command pill), then add `rest`. Any other row (e.g. the "Create /name skill"
        row Cursor shows for an unknown name) is never pressed."""
        def key(k, code, vk, text=None, mods=0):
            for t in ("keyDown", "keyUp"):
                ev = {"type": t, "key": k, "code": code, "windowsVirtualKeyCode": vk, "modifiers": mods}
                if t == "keyDown" and text:
                    ev["text"] = text
                ws.call("Input.dispatchKeyEvent", ev)

        def bail(msg):
            # The input was verified empty before we typed, so clearing it loses nothing.
            key("Escape", "Escape", 27)
            key("a", "KeyA", 65, mods=4)
            key("Backspace", "Backspace", 8)
            raise SendAbort("slash", msg + "; cleared the chat input, nothing sent")

        key("/", "Slash", 191, "/")
        ws.call("Input.insertText", {"text": name})
        want = "/" + name
        deadline = time.time() + max(delay(2.0), 0.5)
        while True:
            menu = cdp_eval(ws, "slash_menu") or {}
            if menu.get("open") == 1 and menu.get("focused") == want:
                break
            if time.time() >= deadline:
                if menu.get("open") != 1:
                    bail("Cursor's slash menu did not open for %s" % want)
                near = [n for n in menu.get("names") or [] if name.lower() in n.lower()][:5]
                bail("no %s command in Cursor's slash menu (focused: %r%s)" % (
                    want, menu.get("focused") or "nothing",
                    ", similar: " + ", ".join(near) if near else ""))
            time.sleep(0.1)
        self.step("slash menu: %s focused" % want)
        if not menu.get("ok"):
            bail("the %s row is covered; refusing to press" % want)
        cdp_press(ws, menu["x"], menu["y"])
        deadline = time.time() + max(delay(2.0), 0.5)
        while name not in ((cdp_eval(ws, "slash_pill") or {}).get("pills") or []):
            if time.time() >= deadline:
                bail("picked %s but no command pill appeared in the input" % want)
            time.sleep(0.1)
        if rest:
            ws.call("Input.insertText", {"text": rest})
            time.sleep(delay(0.3))
            pill = cdp_eval(ws, "slash_pill") or {}
            if not text_matches(pill.get("text"), rest):
                bail("the text after %s did not land in the input" % want)
        self.step("command %s in chat input: verified" % want)

    def switch_back(self):
        """Return to the tab that was in view before a background-tab send. Best effort: the
        prompt is already in, so a failure here is reported, never raised."""
        if not (self.prev_tab and getattr(self.a, "back", False) and self.a.cdp):
            return
        try:
            page = cdp_page(self.a.cdp, self.a.repo, (self.target or {}).get("workspace_name"))
            ws = WS(page.get("webSocketDebuggerUrl"))
            try:
                cdp_select_tab(ws, self.prev_tab, self.prev_tab.get("text"), None)
            finally:
                ws.close()
            self.step("switched back to tab %r" % self.prev_tab.get("text"))
        except SendAbort as exc:
            self.step("could not switch back to tab %r: %s" % (self.prev_tab.get("text"), exc.msg))

    # -- headless
    def run_headless(self):
        import shutil
        agent = os.environ.get("GROK_CURSOR_AGENT") or shutil.which("agent") or shutil.which("cursor-agent")
        if not agent:
            raise SendAbort("no_agent_cli", "Cursor CLI not found (install: curl https://cursor.com/install -fsS | bash)")
        cwd = self.a.repo or os.getcwd()
        # Fresh CLI chat every time: never --resume (IDE chat ids are not CLI chat ids).
        cmd = [agent, "-p", self.prompt, "--output-format", "text"]
        if self.dry:
            self.step("would run in %s: %s -p <prompt> --output-format text" % (cwd, os.path.basename(agent)))
            return
        p = run(cmd, timeout=int(os.environ.get("GROK_CURSOR_AGENT_TIMEOUT", "1800")), cwd=cwd)
        if p is None or p.returncode != 0:
            raise SendAbort("agent_failed", "agent -p failed: %s" % ((p.stderr.strip()[-500:]) if p else "timeout"))
        sys.stdout.write(p.stdout)
        self.step("headless agent: done")


def send_log(home, entry):
    try:
        os.makedirs(home, mode=0o700, exist_ok=True)
        path = os.path.join(home, "sends.log")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def cmd_send(args, ctx):
    """ctx: dict(redact=fn, one_line=fn, data_home=fn, cursor_dir=fn)."""
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    prompt = (prompt or "").strip()
    if not args.headless and not args.cdp:
        args.cdp = auto_cdp_port()
    mode = "headless" if args.headless else ("cdp" if args.cdp else "ui")
    s = Sender(args, prompt)
    entry = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), "mode": mode,
             "method": args.method, "new": bool(args.new), "to": args.to, "repo": args.repo,
             "dry_run": bool(args.dry_run), "chars": len(prompt),
             "sha256": hashlib.sha256(prompt.encode()).hexdigest()[:16]}
    if os.environ.get("GROK_CURSOR_SEND_LOG_PREVIEW", "1") != "0":
        entry["preview"] = ctx["one_line"](ctx["redact"](prompt), 120)
    started = time.time()
    code, result = 0, "sent"
    try:
        if not prompt:
            raise SendAbort("bad_prompt", "empty prompt")
        if args.max_chars and len(prompt) > args.max_chars:
            raise SendAbort("too_long", "prompt is %d chars; cap is %d (raise with --max-chars, or put the "
                            "details in a file and reference it)" % (len(prompt), args.max_chars))
        hits = find_secrets(prompt)
        if hits and not args.allow_secrets:
            raise SendAbort("secret", "prompt looks like it contains a secret (%s); refusing. Remove it or "
                            "pass --allow-secrets" % ", ".join(hits))
        if args.repo:
            args.repo = os.path.abspath(os.path.expanduser(args.repo))
            if not os.path.isdir(args.repo):
                raise SendAbort("repo", "repo path is not a directory: %s" % args.repo)
        if args.to and args.new:
            raise SendAbort("target", "--to and --new are mutually exclusive")
        # Every transcript's size before Return: only bytes written after it count, whichever chat
        # verify ends up reading (--new falls back to the focused chat, whose history may hold old errors).
        # A chat created after this has no mark and is read from 0.
        s.transcript_marks = {}
        for path in glob.glob(os.path.join(ctx["cursor_dir"](), "projects", "*", "agent-transcripts", "*", "*.jsonl")):
            try:
                s.transcript_marks[path] = os.path.getsize(path)
            except OSError:
                pass
        if not args.dry_run:
            try:                        # tells the hook this prompt came from us, not a human typing
                import gc_gate
                gc_gate.mark_send(ctx["data_home"](), prompt)
            except Exception:
                pass
        if mode == "headless":
            if args.to or args.new:
                raise SendAbort("target", "--to/--new don't apply to --headless (it always starts a new CLI chat)")
            s.run_headless()
        elif mode == "cdp":
            s.run_cdp()
        elif SLASH_RE.match(prompt):
            # ponytail: slash commands need the DOM menu; add an AX menu walk if CDP is ever unavailable.
            raise SendAbort("slash", "slash commands need CDP (start Cursor with --remote-debugging-port=9222)")
        else:
            s.run_ax()
        if args.dry_run:
            result = "dry-run"
        elif mode != "headless" and args.verify > 0:
            chat_id = s.chat_id or (s.target["id"] if s.target else None) or focused_chat_id()
            poll = None
            if args.cdp and chat_id:
                workspace = (s.target or {}).get("workspace_name")
                def poll(port=args.cdp, cid=chat_id, hint=args.repo, workspace_name=workspace):
                    return cdp_composer_status(port, cid, hint, workspace_name)
            cid, src = confirm_delivery(prompt, args.verify, chat_id, ctx["cursor_dir"](),
                                        s.status_before, poll, s.transcript_marks)
            if cid is None:
                raise SendAbort("unverified", "unconfirmed: after Return the chat did not go busy and then "
                                "back to idle, and this chat's transcript got no new user turn within %ds. "
                                "Return was pressed; check Cursor before resending." % args.verify)
            entry["conversation_id"] = cid
            s.step("delivered via %s%s" % (src, " (chat %s)" % cid[:8] if cid else ""))
            result = "delivered"
    except SendAbort as exc:
        s.restore_clipboard()
        if not args.dry_run:
            try:
                import gc_gate
                gc_gate.unmark_send(ctx["data_home"]())
            except Exception:
                pass
        code = exc.code
        result = {"unverified": "unverified", "failed": "failed"}.get(exc.key, "abort")
        entry["reason"] = exc.key
        for st in s.steps:
            sys.stderr.write("  " + st + "\n")
        label = {"unverified": "unconfirmed", "failed": "failed"}.get(exc.key, "ABORT")
        sys.stderr.write("grok-cursor send: %s [%s, exit %d]: %s\n" % (label, exc.key, exc.code, exc.msg))
        if s.pressed and exc.key not in ("unverified", "failed"):
            sys.stderr.write("note: Return WAS pressed before this error; check Cursor before resending\n")
    if s.pressed:
        s.switch_back()
    entry.update(result=result, code=code, steps=s.steps, pressed=s.pressed,
                 elapsed_s=round(time.time() - started, 2))
    send_log(ctx["data_home"](), entry)
    if code:
        return code
    out = sys.stderr if mode == "headless" else sys.stdout
    for st in s.steps:
        out.write("  " + st + "\n")
    if args.dry_run:
        out.write("DRY RUN OK: nothing was sent\n")
    elif mode == "headless" or args.verify <= 0:
        out.write("SENT\n")
    else:
        out.write("delivered\n")
    return 0


# ------------------------------------------------------------------ approval cards (pending / approve)

# Exact button labels per action. "Allow" is Cursor's Run label on read-tool cards.
# Anything starting with "Always" (Always Run, Always Allow, Always Run Selected) is never clicked.
CARD_ACTIONS = {"run": ["Run", "Allow"], "skip": ["Skip"], "accept": ["Accept"]}

def run_card_id(chat_id, command):
    """Shell card id. sha256(chat_id + command)[:16]. The DOM tool id is a different value."""
    return hashlib.sha256(((chat_id or "") + (command or "")).encode("utf-8")).hexdigest()[:16]

def _open_chat(args):
    """(target, page, ws, tab) for --to over CDP. Caller closes ws."""
    port = args.cdp or auto_cdp_port()
    if not port:
        raise SendAbort("cdp", "approval cards need Cursor's DevTools port (start Cursor with "
                        "--remote-debugging-port=9222)")
    args.cdp = port
    chat, err = resolve_target(args.to, list_targets())
    if err or not chat:
        raise SendAbort("target", err or "no chat matches %r" % args.to)
    page = cdp_page(port, None, chat.get("workspace_name"))
    ws = WS(page.get("webSocketDebuggerUrl"))
    hits = cdp_tab_hits(cdp_eval(ws, "tabs"), chat["title"], chat["id"])
    if len(hits) != 1:
        ws.close()
        raise SendAbort("target", "chat %s %r is not an open tab in %r" % (
            chat["id"][:8], chat["title"], page.get("title")))
    return chat, page, ws, hits[0]

def _read_cards(ws, chat_id, wait):
    """Cards in a chat. After a tab switch the transcript renders late, so poll up to `wait` s."""
    deadline = time.time() + wait
    while True:
        cards = (cdp_eval(ws, "cards", id=chat_id) or {}).get("cards") or []
        if cards or time.time() >= deadline:
            return cards
        time.sleep(0.2)

ACTIONABLE = {b for labels in CARD_ACTIONS.values() for b in labels}

def actionable(cards):
    """Cards `approve` can act on: a live Run/Allow/Skip/Accept button. Finished edit rows and
    plan cards have none and must not count ("14 approval cards; refusing to guess")."""
    return [c for c in cards or [] if isinstance(c, dict) and ACTIONABLE & set(c.get("buttons") or [])]

def _card_line(chat, card):
    out = ["chat: %s %r" % (chat["id"][:8], safe_title(chat["title"])),
           "kind: %s  card_id: %s" % (card.get("kind") or "?", card.get("card_id") or ""),
           "action: %s" % (card.get("command") or "(no text)"),
           "buttons: %s" % ", ".join(card.get("buttons") or [])]
    if card.get("options"):
        out.append("options: %s" % ", ".join(card["options"]))
    if card.get("note"):
        out.append("review note: %s" % card["note"])
    return out

def cmd_pending(args, ctx):
    """Read-only, except that a background tab is brought forward to read its card, then put back."""
    try:
        chat, page, ws, tab = _open_chat(args)
        try:
            prev, cards = None, None
            if not tab.get("selected"):
                if not tab.get("waiting"):
                    cards = []
                else:
                    prev = cdp_select_tab(ws, tab, chat["title"], chat["id"])
            if cards is None:
                cards = _read_cards(ws, chat["id"], 3 if prev else 0)
        finally:
            try:
                if prev:
                    cdp_select_tab(ws, prev, prev.get("text"), None)
            finally:
                ws.close()
    except SendAbort as exc:
        sys.stderr.write("grok-cursor pending: ABORT [%s, exit %d]: %s\n" % (exc.key, exc.code, exc.msg))
        return exc.code
    if args.json:
        print(json.dumps({"chat_id": chat["id"], "title": chat["title"], "window": page.get("title"),
                          "in_view": bool(tab.get("selected")), "cards": cards}, ensure_ascii=False, indent=2))
        return 0
    if not cards:
        print("no approval card in %s %r" % (chat["id"][:8], safe_title(chat["title"])))
        return 0
    for i, card in enumerate(cards):
        if i:
            print()
        print("\n".join(_card_line(chat, card)))
    return 0

def _match_window(home):
    """Seconds an allow decision stays clickable. config match_window, default 30."""
    import gc_gate
    try:
        window = float(gc_gate.load_config(home).get("match_window", 30))
    except (TypeError, ValueError):
        window = 30.0
    return window if window > 0 else 30.0

def _decision_for_match(home, req_id):
    """The allow decision --match may click for. SendAbort('card') on any mismatch, before a click."""
    import gc_gate
    try:
        obj = gc_gate.load_decision(gc_gate.decision_path(home, req_id))
    except (OSError, ValueError):
        raise SendAbort("card", "no decision %s" % req_id)
    if obj.get("decided") != "allow":
        raise SendAbort("card", "decision %s is not allow" % req_id)
    try:
        when = datetime.datetime.fromisoformat(obj.get("decided_at") or "")
    except (TypeError, ValueError):
        raise SendAbort("card", "decision %s has no decided_at" % req_id)
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
    age = (datetime.datetime.now().astimezone() - when).total_seconds()
    window = _match_window(home)
    if age >= window:
        raise SendAbort("card", "decision %s is %.0fs old; window is %.0fs" % (req_id, age, window))
    return obj

def cmd_approve(args, ctx):
    """Click exactly one button (run, skip or accept) on the target chat's only approval card.
    --match REQ_ID clicks Run only when that allow decision is fresh and the single run card's
    command still matches. Never clicks an "Always ..." button. Verifies the card is gone afterwards."""
    if getattr(args, "match", None):
        if args.action not in (None, "run"):
            sys.stderr.write("grok-cursor approve: --match only clicks Run\n")
            return 33
        args.action = "run"
    if args.action not in CARD_ACTIONS:
        sys.stderr.write("grok-cursor approve: --action is required\n")
        return 2
    labels = CARD_ACTIONS[args.action]
    entry = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), "mode": "approve",
             "to": args.to, "action": args.action, "dry_run": bool(args.dry_run),
             "match": getattr(args, "match", None) or ""}
    steps, code, result = [], 0, "approved"
    decision = None
    match_id = getattr(args, "match", None) or ""
    try:
        if match_id:
            decision = _decision_for_match(ctx["data_home"](), match_id)
            if not args.to:
                args.to = decision.get("conversation_id") or ""
            if not args.to:
                raise SendAbort("card", "decision %s has no chat" % match_id)
        chat, page, ws, tab = _open_chat(args)
        entry["to"] = chat["id"]
        steps.append("chat: %s %r in %r" % (chat["id"][:8], safe_title(chat["title"]), page.get("title")))
        prev = None
        try:
            if not tab.get("selected"):
                if args.dry_run and not tab.get("waiting"):
                    raise SendAbort("no_card", "background tab shows no approval card")
                prev = cdp_select_tab(ws, tab, chat["title"], chat["id"])
                steps.append("switched to tab: verified")
            cards = actionable(_read_cards(ws, chat["id"], 3 if prev else 0))
            if decision is not None and len(cards) != 1:
                raise SendAbort("card", "%d cards in this chat; need exactly one" % len(cards))
            if not cards:
                raise SendAbort("no_card", "no approval card in this chat")
            if len(cards) > 1:
                raise SendAbort("card", "%d approval cards in this chat; refusing to guess" % len(cards))
            card = cards[0]
            steps.extend(_card_line(chat, card))
            if decision is not None:
                if (decision.get("conversation_id") or "") != chat["id"]:
                    raise SendAbort("card", "decision %s is for a different chat" % match_id)
                if card.get("kind") != "run":
                    raise SendAbort("card", "card kind is %r; need run" % card.get("kind"))
                want, have = norm(decision.get("cmd")), norm(card.get("command"))
                if not want or want != have:
                    raise SendAbort("card", "card command does not match decision %s" % match_id)
            if args.expect and norm(args.expect).lower() not in norm(card.get("command")).lower():
                raise SendAbort("card", "the card's action does not contain %r; refusing" % args.expect)
            want = [b for b in card.get("buttons") or [] if b in labels]
            if len(want) != 1:
                raise SendAbort("card", "card buttons are %s; need exactly one of %s" % (
                    card.get("buttons"), labels))
            if args.dry_run:
                steps.append("would click %r" % want[0])
                result = "dry-run"
            else:
                res = cdp_eval(ws, "press_card", id=chat["id"], labels=labels) or {}
                if res.get("hits") != 1:
                    raise SendAbort("card", "the card changed before the click (%s); nothing clicked" % res)
                steps.append("clicked %r" % res.get("clicked"))
                entry["clicked"] = res.get("clicked")
                deadline = time.time() + max(args.verify, 1)
                while True:
                    snap = cdp_eval(ws, "cards", id=chat["id"]) or {}
                    if snap.get("found") and not snap.get("cards"):
                        steps.append("card gone; chat status: %s" % (snap.get("status") or "?"))
                        break
                    if time.time() >= deadline:
                        raise SendAbort("unverified", "clicked %r but the card is still there after %ds"
                                        % (res.get("clicked"), args.verify))
                    time.sleep(0.2)
        finally:
            if prev and (args.back or args.dry_run):
                try:
                    cdp_select_tab(ws, prev, prev.get("text"), None)
                    steps.append("switched back to tab %r" % prev.get("text"))
                except SendAbort as exc:
                    steps.append("could not switch back: %s" % exc.msg)
            ws.close()
    except SendAbort as exc:
        code, result = exc.code, "abort"
        entry["reason"] = exc.key
        for st in steps:
            sys.stderr.write("  " + st + "\n")
        sys.stderr.write("grok-cursor approve: ABORT [%s, exit %d]: %s\n" % (exc.key, exc.code, exc.msg))
    entry.update(result=result, code=code, steps=steps)
    send_log(ctx["data_home"](), entry)
    if code:
        return code
    for st in steps:
        print("  " + st)
    print("DRY RUN OK: nothing was clicked" if args.dry_run else "approved: %s" % entry.get("clicked"))
    return 0

# Question option, then Continue. Plan accept is Build. Reject is the exact label "Reject"
# when the card has it. 3.24.12's probed plan has View Plan / Build / Open menu and no Reject,
# so reject clicks nothing. Edit rows are never clicked. Never a label that starts with Always.
PLAN_CLICK = {"accept": "Build", "reject": "Reject"}

def _exact(have, want):
    hits = [item for item in have or [] if norm(item) == norm(want)]
    return hits[0] if len(hits) == 1 else None

def _of_kind(cards, kind):
    return [c for c in cards or [] if c.get("kind") == kind]

def _refuse_always(label):
    if re.match(r"(?i)^always", label or ""):
        raise SendAbort("card", "refusing a label that starts with Always")

def _run_cdp_action(args, ctx, mode, act):
    """Open --to, run act(chat, ws, steps), verify inside act, log, restore the tab."""
    entry = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), "mode": mode,
             "to": getattr(args, "to", None), "dry_run": bool(getattr(args, "dry_run", False))}
    steps, code, result = [], 0, mode
    try:
        if not getattr(args, "to", None):
            raise SendAbort("target", "%s needs --to" % mode)
        chat, page, ws, tab = _open_chat(args)
        entry["to"] = chat["id"]
        steps.append("chat: %s %r in %r" % (chat["id"][:8], safe_title(chat["title"]), page.get("title")))
        prev = None
        try:
            if not tab.get("selected"):
                if getattr(args, "dry_run", False) and not tab.get("waiting"):
                    raise SendAbort("no_card", "background tab shows no card")
                prev = cdp_select_tab(ws, tab, chat["title"], chat["id"])
                steps.append("switched to tab: verified")
            act(chat, ws, steps, entry)
        finally:
            if prev and (getattr(args, "back", False) or getattr(args, "dry_run", False)):
                try:
                    cdp_select_tab(ws, prev, prev.get("text"), None)
                    steps.append("switched back to tab %r" % prev.get("text"))
                except SendAbort as exc:
                    steps.append("could not switch back: %s" % exc.msg)
            ws.close()
    except SendAbort as exc:
        code, result = exc.code, "abort"
        entry["reason"] = exc.key
        for st in steps:
            sys.stderr.write("  " + st + "\n")
        sys.stderr.write("grok-cursor %s: ABORT [%s, exit %d]: %s\n" % (mode, exc.key, exc.code, exc.msg))
    entry.update(result=result, code=code, steps=steps)
    send_log(ctx["data_home"](), entry)
    if code:
        return code
    for st in steps:
        print("  " + st)
    if getattr(args, "dry_run", False):
        print("DRY RUN OK: nothing was clicked")
    else:
        print("%s: %s" % (mode, ", ".join(entry.get("clicked") or [])))
    return 0

def _wait_cards(ws, chat_id, seconds, pred, what):
    deadline = time.time() + max(seconds, 1)
    while True:
        snap = cdp_eval(ws, "cards", id=chat_id) or {}
        if pred(snap):
            return snap
        if time.time() >= deadline:
            raise SendAbort("unverified", what)
        time.sleep(0.2)

def cmd_answer(args, ctx):
    """Click one question option by exact label, then Continue once data-click-ready is true."""
    label = getattr(args, "option", None) or ""
    verify = getattr(args, "verify", 10)

    def act(chat, ws, steps, entry):
        _refuse_always(label)
        cards = _read_cards(ws, chat["id"], 0)
        hits = _of_kind(cards, "question")
        if len(hits) != 1:
            raise SendAbort("card", "%d question cards; need exactly one" % len(hits))
        exact = _exact(hits[0].get("options"), label)
        if not exact:
            raise SendAbort("card", "option %r is not on the question card" % label)
        steps.extend(_card_line(chat, hits[0]))
        if getattr(args, "dry_run", False):
            steps.append("would click %r then 'Continue'" % exact)
            return
        res = cdp_eval(ws, "press_option", id=chat["id"], label=exact) or {}
        if res.get("hits") != 1 or res.get("clicked") != exact:
            raise SendAbort("card", "the question changed before the option click (%s); nothing clicked" % res)
        clicked = [exact]
        snap = _wait_cards(
            ws, chat["id"], verify,
            lambda s: any(c.get("kind") == "question" and (c.get("ready") or {}).get("Continue") is True
                          for c in (s.get("cards") or [])),
            "clicked %r but Continue stayed disabled" % exact)
        again = _of_kind(snap.get("cards"), "question")
        if len(again) != 1 or (again[0].get("ready") or {}).get("Continue") is not True:
            raise SendAbort("card", "Continue was not ready on the re-read; not clicked")
        res = cdp_eval(ws, "press_continue", id=chat["id"]) or {}
        if res.get("hits") != 1 or res.get("clicked") != "Continue":
            raise SendAbort("card", "Continue was not ready at the click (%s)" % res)
        clicked.append("Continue")
        entry["clicked"] = clicked
        _wait_cards(ws, chat["id"], verify,
                    lambda s: not _of_kind(s.get("cards"), "question"),
                    "clicked Continue but the question is still there")
        steps.append("clicked %r then 'Continue'; question gone" % exact)

    return _run_cdp_action(args, ctx, "answer", act)

def cmd_plan(args, ctx):
    """accept clicks Build. reject clicks an exact Reject label, or clicks nothing if it is absent."""
    action = getattr(args, "action", None)
    if action not in PLAN_CLICK:
        sys.stderr.write("grok-cursor plan: action must be accept or reject\n")
        return 2
    label = PLAN_CLICK[action]
    verify = getattr(args, "verify", 10)

    def act(chat, ws, steps, entry):
        _refuse_always(label)
        cards = _read_cards(ws, chat["id"], 0)
        hits = _of_kind(cards, "plan")
        if len(hits) != 1:
            raise SendAbort("card", "%d plan cards; need exactly one" % len(hits))
        card = hits[0]
        steps.extend(_card_line(chat, card))
        exact = _exact(card.get("buttons"), label)
        if not exact:
            raise SendAbort("card", "plan buttons are %s; %s needs the exact label %r" % (
                card.get("buttons"), action, label))
        card_id = card.get("card_id") or ""
        if getattr(args, "dry_run", False):
            steps.append("would click %r" % exact)
            return
        fresh = _of_kind(_read_cards(ws, chat["id"], 0), "plan")
        if len(fresh) != 1 or (fresh[0].get("card_id") or "") != card_id or not _exact(fresh[0].get("buttons"), exact):
            raise SendAbort("card", "the plan changed before the click; nothing clicked")
        res = cdp_eval(ws, "press_plan", id=chat["id"], label=exact, card_id=card_id) or {}
        if res.get("hits") != 1 or res.get("clicked") != exact:
            raise SendAbort("card", "the plan changed before the click (%s); nothing clicked" % res)
        entry["clicked"] = [exact]
        def changed(snap):
            left = [c for c in _of_kind(snap.get("cards"), "plan") if (c.get("card_id") or "") == card_id]
            if not left:
                return True
            return _exact(left[0].get("buttons"), exact) is None
        _wait_cards(ws, chat["id"], verify, changed, "clicked %r but the plan did not change" % exact)
        steps.append("clicked %r; plan changed" % exact)

    return _run_cdp_action(args, ctx, "plan", act)

# ------------------------------------------------------------------ targets / ax-dump / doctor --send

def cmd_targets(args, ctx):
    rows = list_targets()
    if args.workspace:
        rows = [r for r in rows if args.workspace.lower() in (r["workspace"] or "").lower()]
    if not args.all:
        rows = [r for r in rows if r["open"]] or rows
    rows = rows[: args.limit]
    on_screen = []
    if args.ax:
        be, pid = ax_backend(), cursor_pid()
        if be and pid and be.trusted():
            cur = CursorAX(be, pid)
            for w in cur.windows():
                for t in cur.tabs(w):
                    on_screen.append({"window": safe_title(w.title), "text": safe_title(t.text), "role": t.role,
                                      "selected": t.selected})
        else:
            sys.stderr.write("(AX unavailable: needs macOS, Cursor running, Accessibility permission)\n")
    if args.json:
        print(json.dumps({"chats": rows, "on_screen": on_screen}, indent=2))
        return 0
    if not rows:
        print("no chats found in %s (Cursor's state DB layout may have changed)" % cursor_user_dir())
    for r in rows:
        flag = "*" if r["focused"] else ("o" if r["open"] else " ")
        when = (datetime.datetime.fromtimestamp(r["updated"] / 1000).strftime("%Y-%m-%d %H:%M")
                if r["updated"] else "")
        print("%s %s  %-44s %-16s  %s" % (flag, r["id"][:8], (r["title"] or "(untitled)")[:44], when,
                                         os.path.basename(r["workspace"] or "")))
    if rows:
        print("\n* = last focused, o = open tab (from Cursor's state DB; may lag the UI).")
        print('send with: grok-cursor send --to <id-prefix|title> "..."')
    if on_screen:
        print("\non screen (AX):")
        for t in on_screen:
            print("  %s %-14s %s  [%s]" % ("*" if t["selected"] else " ", t["role"], t["text"][:60], t["window"]))
    return 0


def cmd_ax_dump(args, ctx):
    be, pid = ax_backend(), cursor_pid()
    if not be or not pid:
        sys.stderr.write("needs macOS and a running Cursor\n")
        return EXIT["no_accessibility"]
    if not be.trusted():
        sys.stderr.write("not trusted for Accessibility (see `grok-cursor doctor --send`)\n")
        return EXIT["no_accessibility"]
    cur = CursorAX(be, pid)
    rx = re.compile(args.grep, re.I) if args.grep else None
    for w in cur.windows():
        print("WINDOW %r" % w.title)
        for n in w.walk(limit=args.limit):
            d = n.describe()
            if rx and not rx.search(d):
                continue
            depth = sum(1 for _ in n.ancestors())
            print("  " * min(depth, 30) + d)
    return 0


def doctor_send(ctx, w):
    """Readiness report for `send`. Returns the number of blocking problems."""
    bad = [0]

    def line(state, label, detail=""):
        if state is False:
            bad[0] += 1
        w("  [%s] %s%s\n" % ({True: "ok", False: "!!", None: "--"}[state], label, (": " + detail) if detail else ""))

    w("\nsend readiness (macOS UI automation)\n")
    plat = os.environ.get("GROK_CURSOR_PLATFORM", sys.platform)
    line(plat == "darwin", "macOS (needed for UI send)", plat)
    if plat != "darwin":
        w("  UI send is macOS-only here; --headless may still work.\n")
    p = run(["sw_vers", "-productVersion"])
    if ok(p):
        line(None, "macOS version", p.stdout.strip())
    app_path = None
    for cand in ("/Applications/%s.app" % app_name(), os.path.expanduser("~/Applications/%s.app" % app_name())):
        if os.path.isdir(cand):
            app_path = cand
    if app_path:
        p = run(["defaults", "read", os.path.join(app_path, "Contents", "Info"), "CFBundleShortVersionString"])
        line(True, "Cursor installed", "%s (%s)" % (app_path, p.stdout.strip() if ok(p) else "version ?"))
    elif plat == "darwin":
        line(None, "Cursor.app not found in /Applications")
    if plat == "darwin":
        pid = cursor_pid()
        line(bool(pid), "Cursor running", str(pid or "no"))
        cs = console_state()
        line(cs["on_console"] is not False, "console session", "user %s" % (cs["user"] or "?"))
        line(cs["locked"] is False, "screen unlocked", {True: "LOCKED", None: "unknown"}.get(cs["locked"], "yes"))
        be = ax_backend()
        trusted = bool(be and be.trusted())
        line(trusted, "Accessibility trusted for this process chain")
        p = osa('tell application "System Events" to get name of first application process whose frontmost is true')
        line(ok(p), "Automation: System Events", p.stdout.strip() if ok(p) else
             ((p.stderr.strip()[:120] if p else "osascript missing") + " (grant under Privacy > Automation)"))
        chain, pp = [], os.getpid()
        for _ in range(15):
            q = run(["ps", "-o", "ppid=,comm=", "-p", str(pp)])
            if not ok(q) or not q.stdout.strip():
                break
            ppid, _, comm = q.stdout.strip().partition(" ")
            chain.append(comm.strip())
            if not ppid.strip().isdigit() or int(ppid) <= 1:
                break
            pp = int(ppid)
        if chain:
            line(None, "process chain (the .app nearest the top usually needs Accessibility)", " <- ".join(chain))
        if trusted and pid:
            try:
                cur = CursorAX(be, pid)
                win = cur.window()
                line(None, "AXManualAccessibility", "acknowledged" if cur.manual else "not acknowledged (tree may "
                     "still be exposed)")
                ins = cur.inputs(win)
                line(bool(ins), "chat input visible via AX", "%d found" % len(ins))
                tabs = cur.tabs(win)
                line(None, "tabs visible via AX", ", ".join(("*" if t.selected else "") + t.text[:30]
                                                            for t in tabs[:8]) or "none")
            except SendAbort as exc:
                line(False, "AX tree", exc.msg)
        p = run(["pmset", "-g"])
        if ok(p):
            m = re.search(r"^\s*sleep\s+(\d+)", p.stdout, re.M)
            d = re.search(r"^\s*displaysleep\s+(\d+)", p.stdout, re.M)
            line(None, "pmset", "sleep %s min, displaysleep %s min" % (m.group(1) if m else "?",
                                                                       d.group(1) if d else "?"))
    import shutil
    agent = shutil.which("agent") or shutil.which("cursor-agent")
    if agent:
        p = run([agent, "--version"], timeout=10)
        line(True, "Cursor CLI (--headless)", "%s %s" % (agent, p.stdout.strip() if ok(p) else ""))
    else:
        line(None, "Cursor CLI not installed (--headless unavailable)")
    port = int(os.environ.get("GROK_CURSOR_CDP_PORT", "9222"))
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        line(None, "CDP port %d is OPEN: any local process can drive Cursor through it" % port)
    except OSError:
        line(None, "CDP port %d closed (fine; --cdp is opt-in)" % port)
    tx = list_targets()
    line(None, "chats in Cursor state DB", "%d (%d open tabs)" % (len(tx), sum(1 for t in tx if t["open"])))
    return bad[0]
