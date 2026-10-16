"""gc_awake: keep the Mac awake while Cursor agents work. Python 3 stdlib. Spec: docs/KEEP_AWAKE_SPEC.md.

State lives in <home>/awake.json (the session) and <home>/awake-lid.json (lid-sleep ownership flag).
A session is a small daemon (`grok-cursor awake daemon`) that holds `caffeinate -i [-d] -w <its pid>`,
so the power assertion dies with the daemon. Every 30 s the daemon runs `tick()`, a pure function
over (state, power readings, now) that decides whether the session ends and why.
Power reads and pmset writes go through a Power object so tests inject fakes and never touch pmset or sudo.
"""
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

STATE = "awake.json"
LID_FLAG = "awake-lid.json"
LAST = "awake-last.json"
AUTO = "awake-auto.json"
AUTO_SUPPRESS = "awake-auto-suppress.json"
LOG = "awake.log"
CHIPS = ("15m", "30m", "1h", "2h", "4h", "8h", "inf")
TICK_SECONDS = 30.0
LID_CAP_DEFAULT = 4 * 3600
LID_CAP_MAX = 12 * 3600
BATTERY_LIMIT_DEFAULT = 20
INF_RECONFIRM = 8 * 3600
END_REASONS = ("manual", "timer", "battery", "thermal", "cap", "auto_idle", "quit", "crash_recovered", "lid_failed")

# ---------------------------------------------------------------- durations

def parse_duration(text):
    """'15m', '1h', '1h30m', '90m', '2h', 'inf' -> seconds (None for inf). ValueError otherwise."""
    t = str(text or "").strip().lower()
    if t in ("inf", "infinite", "indefinite", "forever"):
        return None
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", t)
    if not m or not t or (m.group(1) is None and m.group(2) is None):
        raise ValueError("bad duration %r (use 15m, 1h, 1h30m, inf)" % text)
    secs = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
    if secs <= 0:
        raise ValueError("duration must be positive")
    return secs

def parse_until(text, now=None):
    """'18:30' or '6:30pm' -> the next such wall-clock time as a unix timestamp (tomorrow if already past)."""
    t = str(text or "").strip().lower()
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        raise ValueError("bad --until %r (use HH:MM or 6:30pm)" % text)
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if hour < 1 or hour > 12:
            raise ValueError("bad hour %r" % text)
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        raise ValueError("bad time %r" % text)
    base = datetime.datetime.fromtimestamp(time.time() if now is None else now)
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= base:
        target += datetime.timedelta(days=1)
    return target.timestamp()

def fmt_remaining(seconds):
    if seconds is None:
        return "indefinite"
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return ("%dh%02dm" % (h, m)) if h else ("%dm" % m if m else "%ds" % seconds)

# ---------------------------------------------------------------- files

def _path(home, name):
    return os.path.join(home, name)

def _read(home, name):
    try:
        with open(_path(home, name), encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None

def _write(home, name, obj):
    os.makedirs(home, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=home)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, _path(home, name))

def _remove(home, name):
    try:
        os.remove(_path(home, name))
    except OSError:
        pass

def _log(home, kind, **fields):
    try:
        import gc_events
        gc_events.append(kind, home=home, **fields)
    except Exception:
        pass

def _notify(title, text):
    try:
        import gc_outbox
        gc_outbox.notify(title, text)
    except Exception:
        pass

def load_state(home):
    return _read(home, STATE)

def lid_flag(home):
    return _read(home, LID_FLAG)

def config(home):
    try:
        import gc_gate
        cfg = gc_gate.load_config(home).get("awake")
    except Exception:
        cfg = None
    return cfg if isinstance(cfg, dict) else {}

def lid_cap(home):
    try:
        cap = int(config(home).get("lid_cap_seconds", LID_CAP_DEFAULT))
    except (TypeError, ValueError):
        cap = LID_CAP_DEFAULT
    return max(60, min(cap, LID_CAP_MAX))

def battery_limit(home):
    try:
        return int(config(home).get("battery_limit", BATTERY_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        return BATTERY_LIMIT_DEFAULT

# ---------------------------------------------------------------- power layer

def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

class Power(object):
    """Real macOS readings. Every method is cheap and never raises."""
    def _run(self, argv, timeout=5):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def battery(self):
        """(on_battery, percent or None, low_power_mode)."""
        p = self._run(["pmset", "-g", "batt"])
        out = (p.stdout if p else "") or ""
        on_batt = "Battery Power" in out
        m = re.search(r"(\d+)%", out)
        pct = int(m.group(1)) if m else None
        p2 = self._run(["pmset", "-g"])
        low = bool(re.search(r"^\s*lowpowermode\s+1", (p2.stdout if p2 else "") or "", re.M))
        return on_batt, pct, low

    def thermal(self):
        """'nominal' | 'fair' | 'serious' | 'critical'. pmset reports CPU_Speed_Limit; <=50 is serious."""
        p = self._run(["pmset", "-g", "therm"])
        out = (p.stdout if p else "") or ""
        m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
        if not m:
            return "nominal"
        limit = int(m.group(1))
        return "critical" if limit <= 25 else "serious" if limit <= 50 else "fair" if limit < 100 else "nominal"

    def sleep_disabled(self):
        p = self._run(["pmset", "-g"])
        return bool(re.search(r"^\s*SleepDisabled\s+1", (p.stdout if p else "") or "", re.M))

    def sudo_forms(self):
        """pmset disablesleep forms a passwordless sudo rule already permits ('-a' or '')."""
        p = self._run(["sudo", "-n", "-l"])
        out = (p.stdout if p else "") or ""
        forms = []
        for m in re.finditer(r"NOPASSWD:[^\n]*", out):
            if re.search(r"/usr/bin/pmset -a disablesleep", m.group(0)):
                forms.append("-a")
            elif re.search(r"/usr/bin/pmset disablesleep", m.group(0)):
                forms.append("")
        return forms

    def set_disablesleep(self, on, form="-a"):
        """(ok, stderr). Non-interactive sudo only; never prompts."""
        argv = ["sudo", "-n", "/usr/bin/pmset"] + (["-a"] if form == "-a" else []) + ["disablesleep", "1" if on else "0"]
        p = self._run(argv, timeout=10)
        if p is None:
            return False, "pmset unavailable"
        return p.returncode == 0, (p.stderr or "").strip()[:200]

    def spawn_holder(self, pid, display_sleep):
        """caffeinate tied to `pid`: the assertion dies with the daemon. Returns the holder pid."""
        argv = ["caffeinate", "-i"] + ([] if display_sleep else ["-d"]) + ["-w", str(pid)]
        try:
            return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True).pid
        except OSError:
            return None

    def kill(self, pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, TypeError, ValueError):
            pass

    def alive(self, pid):
        return pid_alive(pid)

# ---------------------------------------------------------------- lid ownership

def lid_apply(home, power, want_on):
    """Set disablesleep with ownership bookkeeping. Returns (ok, detail).
    Only ever turns it off when we turned it on. Flag is written *before* 1 and cleared after a verified 0."""
    flag = lid_flag(home) or {}
    forms = power.sudo_forms()
    form = "-a" if "-a" in forms else ("" if "" in forms else "-a")
    if want_on:
        if power.sleep_disabled() and not flag.get("we_set"):
            return False, "SleepDisabled=1, not ours"
        _write(home, LID_FLAG, {"we_set": True, "ts": time.time(), "form": form})
        ok, err = power.set_disablesleep(True, form)
        if not ok:
            _remove(home, LID_FLAG)
            return False, err or "pmset disablesleep 1 failed"
        return True, "lid sleep disabled"
    if not flag.get("we_set"):
        return True, "not ours; untouched" if power.sleep_disabled() else "already 0"
    ok, err = power.set_disablesleep(False, flag.get("form", form))
    if ok and not power.sleep_disabled():
        _remove(home, LID_FLAG)
        return True, "lid sleep restored"
    return False, err or "pmset disablesleep 0 failed"

def recover(home, power, now=None):
    """Crash recovery: we own the lid flag but no live session -> restore it. Returns True if it acted."""
    flag = lid_flag(home)
    if not flag or not flag.get("we_set"):
        return False
    st = load_state(home)
    if st and power.alive(st.get("pid")):
        return False
    ok, detail = lid_apply(home, power, False)
    _log(home, "awake_ended", reason="crash_recovered", lid_restored=ok, detail=detail)
    _notify("grok-cursor awake", "lid sleep restored after a crash" if ok else "could not restore lid sleep: " + detail)
    if st:
        _remove(home, STATE)
        _write(home, LAST, {"reason": "crash_recovered", "at": time.time() if now is None else now})
    return True

# ---------------------------------------------------------------- state machine

def tick(st, power, home, now=None):
    """Pure decision for one daemon tick: '' to keep going, else an end reason."""
    now = time.time() if now is None else now
    if st.get("end") is not None and now >= float(st["end"]):
        return "timer"
    if st.get("lid"):
        if now - float(st.get("started") or now) >= lid_cap(home):
            return "cap"
        if power.thermal() in ("serious", "critical"):
            return "thermal"
    on_batt, pct, low = power.battery()
    if on_batt and pct is not None and pct <= battery_limit(home):
        return "battery"
    if on_batt and low and st.get("lid"):
        return "battery"
    return ""

def start(home, power, duration=None, until=None, lid=False, display_sleep=False, mode="manual",
          launch=None, now=None):
    """Start (or replace) a session. Returns (state, note). Chip toggle: same duration as the
    running manual session stops it instead. `launch(home)` starts the daemon and returns its pid."""
    now = time.time() if now is None else now
    recover(home, power, now)
    cur = load_state(home)
    if cur and power.alive(cur.get("pid")):
        same_chip = until is None and cur.get("duration") == duration and bool(cur.get("lid")) == bool(lid)
        if mode == "manual" and cur.get("mode") == "manual" and same_chip:
            stop(home, power, "manual")
            return None, "stopped (same duration chosen again)"
        if mode == "auto" and cur.get("mode") == "manual":
            return cur, "manual session running; auto does not override it"
        stop(home, power, "manual" if mode == "manual" else "auto_idle", quiet=True)
    if until is not None:
        end = float(until)
    elif duration is None:
        end = None
    else:
        secs = parse_duration(duration)
        end = None if secs is None else now + secs
    if lid:
        cap_end = now + lid_cap(home)
        end = cap_end if end is None or end > cap_end else end
    st = {"mode": mode, "started": now, "end": end, "lid": bool(lid), "display_sleep": bool(display_sleep),
          "duration": duration or ("until" if until else None), "pid": None, "holder_pid": None}
    if lid:
        ok, detail = lid_apply(home, power, True)
        if not ok:
            _log(home, "awake_lid_failed", detail=detail)
            if "not ours" in detail:
                _log(home, "awake_foreign_holder", detail=detail)
            st["lid"] = False
            st["lid_error"] = detail
    _write(home, STATE, st)
    pid = launch(home) if launch else None
    if pid:
        st["pid"] = pid
        _write(home, STATE, st)
    _log(home, "awake_started", mode=mode, duration=st["duration"] or "inf", lid=st["lid"],
         until=datetime.datetime.fromtimestamp(end).isoformat(timespec="minutes") if end else "")
    return st, "started"

def stop(home, power, reason="manual", quiet=False, now=None):
    """End the session: kill the daemon (its caffeinate follows), restore the lid if ours."""
    st = load_state(home)
    if not st:
        return False
    if st.get("lid"):
        ok, detail = lid_apply(home, power, False)
        if not ok:
            _log(home, "awake_lid_failed", detail=detail, during="stop")
    for key in ("holder_pid", "pid"):
        if st.get(key) and st[key] != os.getpid():
            power.kill(st[key])
    _remove(home, STATE)
    _write(home, LAST, {"reason": reason, "at": time.time() if now is None else now, "mode": st.get("mode")})
    if not quiet:
        _log(home, "awake_ended", reason=reason, mode=st.get("mode"))
    return True

def extend(home, minutes, now=None):
    st = load_state(home)
    if not st:
        return None
    now = time.time() if now is None else now
    base = float(st["end"]) if st.get("end") is not None else now
    st["end"] = max(base, now) + minutes * 60
    if st.get("lid"):
        st["end"] = min(st["end"], float(st["started"]) + lid_cap(home))
    _write(home, STATE, st)
    return st

def status(home, power, now=None):
    now = time.time() if now is None else now
    st = load_state(home)
    live = bool(st and power.alive(st.get("pid")))
    flag = lid_flag(home) or {}
    sd = power.sleep_disabled()
    lid_line = ("SleepDisabled=1, ours" if flag.get("we_set") else "SleepDisabled=1, not ours") if sd \
        else ("SleepDisabled=0" + (" (we own a stale flag)" if flag.get("we_set") else ""))
    out = {"active": live, "lid": lid_line, "sleep_disabled": sd, "lid_ours": bool(flag.get("we_set"))}
    if st and live:
        out.update(mode=st.get("mode"), started=st.get("started"), end=st.get("end"),
                   remaining=fmt_remaining(None if st.get("end") is None else float(st["end"]) - now),
                   display_sleep=st.get("display_sleep"), pid=st.get("pid"))
    elif st:
        out["stale"] = True
    last = _read(home, LAST)
    if last:
        out["last_end"] = last
    auto = _read(home, AUTO)
    if auto:
        out["auto"] = auto
    return out

# ---------------------------------------------------------------- daemon

class Daemon(object):
    def __init__(self, home, power, sleep=time.sleep, clock=time.time):
        self.home, self.power, self.sleep, self.clock = home, power, sleep, clock
        self.stop_reason = None

    def _signal(self, signum, _frame):
        self.stop_reason = "quit"

    def run(self, max_ticks=None):
        st = load_state(self.home)
        if not st:
            return 2
        st["pid"] = os.getpid()
        st["holder_pid"] = self.power.spawn_holder(os.getpid(), st.get("display_sleep"))
        _write(self.home, STATE, st)
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(s, self._signal)
            except (ValueError, OSError):
                pass
        ticks = 0
        reason = ""
        while True:
            st = load_state(self.home)
            if not st or st.get("pid") != os.getpid():
                reason = "manual"                       # someone else ended or replaced the session
                break
            if self.stop_reason:
                reason = self.stop_reason
                break
            reason = tick(st, self.power, self.home, self.clock())
            if reason:
                break
            if st.get("lid") and not self.power.sleep_disabled():     # macOS dropped it (plug/unplug): re-apply
                ok, detail = lid_apply(self.home, self.power, True)
                if not ok:
                    _log(self.home, "awake_lid_failed", detail=detail, during="reapply")
                    reason = "lid_failed"
                    break
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                return 0
            self.sleep(TICK_SECONDS)
        if reason != "manual" or load_state(self.home):
            stop(self.home, self.power, reason)
            if reason in ("battery", "thermal", "cap", "lid_failed"):
                _notify("grok-cursor awake", "session ended: %s" % reason)
        return 0

def launch_daemon(cli):
    """Start `grok-cursor awake daemon` detached. Returns its pid."""
    def go(home):
        logf = open(_path(home, LOG), "a")
        p = subprocess.Popen([sys.executable, cli, "awake", "daemon"], stdin=subprocess.DEVNULL, stdout=logf,
                             stderr=logf, start_new_session=True, env=dict(os.environ, GROK_CURSOR_HOME=home))
        return p.pid
    return go

# ---------------------------------------------------------------- CLI

def cmd_awake(args, ctx, power=None, launch=None):
    home = ctx["data_home"]()
    power = power or ctx.get("power") or Power()
    launch = launch or ctx.get("launch") or launch_daemon(ctx.get("cli") or os.path.realpath(sys.argv[0]))
    action = args.action
    if action == "daemon":
        return Daemon(home, power).run()
    if action == "status":
        out = status(home, power)
        if getattr(args, "json", False):
            print(json.dumps(out, indent=2))
            return 0
        if out["active"]:
            print("awake: on (%s, %s remaining%s)" % (out["mode"], out["remaining"],
                                                      ", display may sleep" if out.get("display_sleep") else ""))
        else:
            print("awake: off (Mac follows normal energy rules)")
        print("lid: " + out["lid"])
        if out.get("last_end"):
            print("last session ended: %s" % out["last_end"].get("reason"))
        if out.get("auto"):
            print("auto: %s" % ("on (idle %s)" % fmt_remaining(out["auto"].get("idle_seconds")) if out["auto"].get("enabled") else "off"))
        return 0
    if action == "off":
        if (_read(home, AUTO) or {}).get("enabled"):
            _write(home, AUTO_SUPPRESS, {"ts": time.time()})     # manual off wins until the next new task
        if not stop(home, power, "manual"):
            print("awake: already off")
        return 0
    if action == "extend":
        try:
            mins = parse_duration(args.duration or "30m") // 60
        except (ValueError, TypeError):
            sys.stderr.write("awake extend: bad duration\n")
            return 2
        st = extend(home, mins)
        if not st:
            sys.stderr.write("awake extend: no session running\n")
            return 1
        print("awake: extended, %s remaining" % fmt_remaining(float(st["end"]) - time.time()))
        return 0
    if action == "on":
        until = None
        if getattr(args, "until", None):
            try:
                until = parse_until(args.until)
            except ValueError as exc:
                sys.stderr.write("awake on: %s\n" % exc)
                return 2
        duration = args.duration or ("inf" if until is None else None)
        if duration:
            try:
                parse_duration(duration)
            except ValueError as exc:
                sys.stderr.write("awake on: %s\n" % exc)
                return 2
        st, note = start(home, power, duration=duration, until=until, lid=bool(getattr(args, "lid", False)),
                         display_sleep=bool(getattr(args, "display_sleep", False)), launch=launch)
        if st is None:
            print("awake: " + note)
            return 0
        print("awake: on (%s remaining)%s" % (fmt_remaining(None if st["end"] is None else float(st["end"]) - time.time()),
                                              ("; lid: " + st["lid_error"]) if st.get("lid_error") else (", lid sleep disabled" if st["lid"] else "")))
        return 0 if not st.get("lid_error") else 3
    if action == "auto":
        return cmd_auto(args, home)
    if action == "lid":
        import gc_awake_lid
        return gc_awake_lid.cmd_lid(args, home, power)
    sys.stderr.write("awake: unknown action %r\n" % action)
    return 2

def cmd_auto(args, home):
    sub = getattr(args, "duration", None) or "status"
    if sub == "on":
        try:
            idle = parse_duration(getattr(args, "idle", None) or "10m")
        except ValueError as exc:
            sys.stderr.write("awake auto: %s\n" % exc)
            return 2
        _write(home, AUTO, {"enabled": True, "idle_seconds": idle, "lid": bool(getattr(args, "lid", False))})
        _remove(home, AUTO_SUPPRESS)
        print("awake auto: on (release after %s idle%s)" % (fmt_remaining(idle), ", lid" if getattr(args, "lid", False) else ""))
        return 0
    if sub == "off":
        _write(home, AUTO, {"enabled": False})
        print("awake auto: off")
        return 0
    a = _read(home, AUTO) or {}
    print("awake auto: %s" % ("on" if a.get("enabled") else "off"))
    return 0
