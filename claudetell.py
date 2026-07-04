#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
#
# [tool.uv]
# # gi (PyGObject) comes from the distro and must match the system python ABI
# python-preference = "only-system"
# ///
"""claudetell — traffic-light overlay for running Claude Code sessions.

Usage:
  claudetell.py [overlay]             native always-on-top overlay (default)
  claudetell.py serve [--port 7717]   browser version (http://127.0.0.1:7717)
  claudetell.py hook                  hook entrypoint (stdin JSON from Claude Code)
  claudetell.py install               register hooks in ~/.claude/settings.json
  claudetell.py uninstall             remove them

Lights:
  green   idle, waiting for next user prompt
  amber   busy (working)                       [pulses]
  blue    agents or background shells running
  mauve   loop scheduled/running (ScheduleWakeup)
  orange  waiting for user input (permissions / ask menu)
  red     error (usage limit, API error)       [pulses]
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "claudetell"
DEFAULT_PORT = 7717
LOOP_GRACE = 120  # ponytail: mauve lingers this long past a scheduled wakeup
AGENT_TTL = 2 * 3600  # ponytail: SubagentStop can be missed on crash; TTL self-heals
SHELL_TTL = 4 * 3600


# -- shared helpers ---------------------------------------------------------

def pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def proc_starttime(pid: int) -> str | None:
    """starttime (clock ticks) from /proc/<pid>/stat, field 22."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def state_path(session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return STATE_DIR / f"{safe}.json"


def read_state(session_id: str) -> dict:
    try:
        return json.loads(state_path(session_id).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


# -- hook entrypoint --------------------------------------------------------

def cmd_hook() -> None:
    """Update per-session state from a Claude Code hook event. Never fails."""
    try:
        payload = json.load(sys.stdin)
        sid = payload.get("session_id")
        if not sid:
            return
        event = payload.get("hook_event_name", "")
        path = state_path(sid)
        STATE_DIR.mkdir(parents=True, exist_ok=True)

        if event == "SessionEnd":
            path.unlink(missing_ok=True)
            return

        # flock: parallel tool calls fire hooks concurrently
        lock = open(STATE_DIR / ".lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            st = read_state(sid)
            now = time.time()
            agents = st.get("agents", [])
            shells = st.get("shells", [])

            if event == "PreToolUse":
                tool = payload.get("tool_name", "")
                ti = payload.get("tool_input") or {}
                if tool in ("ScheduleWakeup", "CronCreate"):
                    delay = float(ti.get("delaySeconds") or ti.get("interval") or 600)
                    st["loop_until"] = now + min(delay, 3600) + LOOP_GRACE
                elif tool == "Bash" and ti.get("run_in_background"):
                    shells.append(now)
            elif event == "SubagentStart":
                agents.append(now)
            elif event == "SubagentStop" and agents:
                agents.pop(0)
            elif event == "StopFailure":
                st["error"] = {
                    "type": payload.get("error_type", "unknown"),
                    "msg": payload.get("error_message", ""),
                    "ts": now,
                }
            elif event in ("UserPromptSubmit", "Stop"):
                st.pop("error", None)  # turn completed / user retried → red clears

            st["agents"] = [t for t in agents if now - t < AGENT_TTL]
            st["shells"] = [t for t in shells if now - t < SHELL_TTL]
            st["updated"] = now
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(st))
            os.replace(tmp, path)
        finally:
            lock.close()
    except Exception:
        pass  # never break the user's session over a status light


# -- session scanning -------------------------------------------------------

_transcript_cache: dict[str, Path] = {}
_error_cache: dict[str, tuple[float, int, str | None]] = {}  # sid -> (mtime, size, err)


def find_transcript(session_id: str) -> Path | None:
    p = _transcript_cache.get(session_id)
    if p and p.exists():
        return p
    for hit in (CLAUDE_DIR / "projects").glob(f"*/{session_id}.jsonl"):
        _transcript_cache[session_id] = hit
        return hit
    return None


ERROR_MARKERS = ("usage limit", "rate limit", "api error", "overloaded",
                 "credit balance", "oauth token has expired")


def transcript_error(session_id: str) -> str | None:
    """Error text if the transcript's last meaningful entry is an API error."""
    path = find_transcript(session_id)
    if not path:
        return None
    try:
        stat = path.stat()
        cached = _error_cache.get(session_id)
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2]
        with open(path, "rb") as f:
            f.seek(max(0, stat.st_size - 65536))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    err = None
    for line in reversed(tail.splitlines()):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = entry.get("type")
        if etype not in ("assistant", "user", "system"):
            continue
        if etype == "assistant" and entry.get("isApiErrorMessage"):
            content = (entry.get("message") or {}).get("content") or []
            texts = [c.get("text", "") for c in content if isinstance(c, dict)]
            err = " ".join(texts).strip() or "API error"
        elif etype == "system" and entry.get("level") == "error":
            err = entry.get("content") or "error"
        else:
            low = line.lower()
            if etype == "system" and any(m in low for m in ERROR_MARKERS):
                err = entry.get("content") or "limit reached"
        break  # only the LAST meaningful entry counts; anything newer clears red
    _error_cache[session_id] = (stat.st_mtime, stat.st_size, err)
    return err


def proc_descendants(pid: int) -> bool:
    """Does the claude process have any live child processes (shells)?"""
    kids: dict[int, list[int]] = {}
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                stat = open(f"/proc/{name}/stat", "rb").read().decode(errors="replace")
                ppid = int(stat.rsplit(")", 1)[1].split()[1])
                kids.setdefault(ppid, []).append(int(name))
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return False
    stack = [pid]
    while stack:
        for child in kids.get(stack.pop(), []):
            return True
    return False


def compute_light(base: str, st: dict, err: str | None, pid: int) -> tuple[str, str]:
    now = time.time()
    agents = st.get("agents", [])
    shells = [t for t in st.get("shells", []) if now - t < SHELL_TTL]
    # bg shells have no completion hook — validate against live child processes
    if shells and not proc_descendants(pid):
        shells = []
    if err:
        return "red", err[:300]
    if base == "waiting":
        return "orange", "waiting for your input"
    if st.get("loop_until", 0) > now:
        return "mauve", "loop running"
    if agents or shells:
        parts = []
        if agents:
            parts.append(f"{len(agents)} agent{'s' if len(agents) > 1 else ''}")
        if shells:
            parts.append(f"{len(shells)} background shell{'s' if len(shells) > 1 else ''}")
        return "blue", " + ".join(parts)
    if base == "busy":
        return "busy", "working"
    if base == "idle":
        return "green", "waiting for next prompt"
    return "gray", base or "unknown"


def scan_sessions(show_all: bool = False) -> list[dict]:
    sessions_dir = CLAUDE_DIR / "sessions"
    best: dict[str, dict] = {}
    if not sessions_dir.is_dir():
        return []
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid = data.get("pid", 0)
        if not pid_alive(pid):
            continue
        # pid reuse: an unrelated process recycled a stale session file's pid
        ps = data.get("procStart")
        if ps is not None and proc_starttime(pid) != str(ps):
            continue
        if not show_all and data.get("kind") != "interactive":
            continue
        sid = data.get("sessionId", "")
        st = read_state(sid)
        hook_err = st.get("error")
        if hook_err:
            err = f"{hook_err.get('type', 'error')}: {hook_err.get('msg', '')}".strip(": ")
        else:
            # fallback: sessions started before hooks were installed
            err = transcript_error(sid)
        light, detail = compute_light(data.get("status") or "", st, err, pid)
        entry = {
            "id": sid,
            "pid": pid,
            "name": data.get("name") or Path(data.get("cwd", "?")).name,
            "cwd": data.get("cwd", ""),
            "kind": data.get("kind", ""),
            "version": data.get("version", ""),
            "startedAt": data.get("startedAt", 0),
            "updatedAt": data.get("statusUpdatedAt") or data.get("updatedAt", 0),
            "status": data.get("status") or "unknown",
            "light": light,
            "detail": detail,
        }
        # one session can own several pid files (e.g. resume); keep the freshest
        cur = best.get(sid)
        if cur is None or entry["updatedAt"] >= cur["updatedAt"]:
            best[sid] = entry
    return sorted(best.values(), key=lambda s: s["startedAt"])


# -- server -----------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    show_all = False

    def do_GET(self) -> None:
        if self.path.startswith("/api/sessions"):
            body = json.dumps({"now": time.time() * 1000,
                               "sessions": scan_sessions(self.show_all)}).encode()
            ctype = "application/json"
        elif self.path == "/" or self.path.startswith("/index"):
            body = HTML.encode()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def cmd_serve(port: int, show_all: bool) -> None:
    Handler.show_all = show_all
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"claudetell overlay: http://127.0.0.1:{port}")
    print("tip: open it in Chrome/Chromium and hit the ⧉ button for an "
          "always-on-top picture-in-picture overlay")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# -- native overlay (GTK3) ----------------------------------------------------

LIGHT_RGB = {
    "green": (0x4A, 0xDE, 0x80), "busy": (0xFB, 0xBF, 0x24),
    "blue": (0x60, 0xA5, 0xFA), "mauve": (0xCB, 0xA6, 0xF7),
    "orange": (0xFB, 0x92, 0x3C), "red": (0xF8, 0x71, 0x71),
    "gray": (0x58, 0x5B, 0x70),
}
PULSE_PERIOD = {"busy": 1.6, "blue": 2.2, "mauve": 2.2, "orange": 1.1, "red": 0.8}
LIGHT_PX = 24
OVERLAY_CFG = STATE_DIR / "overlay.json"


def _overlay_css() -> bytes:
    """GTK CSS for the whole overlay — avoids cairo (python3-gi-cairo often absent)."""
    rules = [
        "window.claudetell { background: rgba(30,30,46,0.88);"
        " border-radius: 13px; border: 1px solid rgba(69,71,90,0.9); }",
        ".light { border-radius: 999px; border: 1px solid rgba(255,255,255,0.18); }",
        "@keyframes ctpulse { 0% { opacity: 1; } 50% { opacity: 0.4; }"
        " 100% { opacity: 1; } }",
    ]
    for name, (r, g, b) in LIGHT_RGB.items():
        col = f"#{r:02x}{g:02x}{b:02x}"
        anim = (f" animation: ctpulse {PULSE_PERIOD[name]}s ease-in-out infinite;"
                if name in PULSE_PERIOD else "")
        rules.append(
            f".light.{name} {{ background-color: {col};"
            f" box-shadow: 0 0 8px 1px alpha({col}, 0.55),"
            f" inset 0 -2px 4px rgba(0,0,0,0.3);{anim} }}")
    return "\n".join(rules).encode()


def _fmt_age(ms: float) -> str:
    s = max(0, (time.time() * 1000 - ms) / 1000)
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.0f}m"
    if s < 86400:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


def cmd_overlay() -> None:
    # XWayland: Mutter honors keep-above for X11 windows; native Wayland
    # clients can't request it on GNOME.
    os.environ.setdefault("GDK_BACKEND", "x11")
    try:
        try:
            import gi
        except ImportError:
            # uv script envs are isolated; gi is distro-packaged, not on PyPI-with-wheels
            sys.path.append("/usr/lib/python3/dist-packages")
            import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk, Gdk, GLib
    except (ImportError, ValueError):
        sys.exit("GTK3 not available (apt install python3-gi gir1.2-gtk-3.0) — "
                 "or use: claudetell.py serve")

    # single instance — a second overlay just shows every light twice
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    instance_lock = open(STATE_DIR / "overlay.lock", "w")
    try:
        fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("claudetell overlay is already running")

    try:
        cfg = json.loads(OVERLAY_CFG.read_text())
    except (OSError, json.JSONDecodeError):
        cfg = {}

    win = Gtk.Window(title="claudetell", decorated=False, resizable=False)
    win.set_keep_above(True)
    win.stick()
    win.set_skip_taskbar_hint(True)
    win.set_skip_pager_hint(True)
    win.set_accept_focus(False)
    screen = win.get_screen()
    rgba = screen.get_rgba_visual()
    if rgba:
        win.set_visual(rgba)

    provider = Gtk.CssProvider()
    provider.load_from_data(_overlay_css())
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    win.get_style_context().add_class("claudetell")

    flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                       orientation=Gtk.Orientation.HORIZONTAL,
                       column_spacing=8, row_spacing=8, homogeneous=True)
    box = Gtk.Box(margin=10)
    box.add(flow)
    win.add(box)

    lights: dict[str, Gtk.Widget] = {}
    state = {"empty": None}

    def set_empty() -> None:
        if lights and state["empty"]:
            state["empty"].destroy()  # destroys the FlowBoxChild wrapper + label
            state["empty"] = None
        elif not lights and not state["empty"]:
            lbl = Gtk.Label()
            lbl.set_markup("<span foreground='#7f849c'>no sessions</span>")
            flow.add(lbl)
            state["empty"] = lbl.get_parent()

    def apply_layout(name: str) -> None:
        n = max(1, len(lights))
        per_line = {"h": n, "v": 1, "grid": min(4, n)}.get(name, n)
        flow.set_min_children_per_line(per_line)
        flow.set_max_children_per_line(per_line)
        cfg["layout"] = name
        win.resize(1, 1)  # shrink-wrap to new content

    def place(w: int | None = None, h: int | None = None) -> None:
        anchor = cfg.get("anchor", "tr")
        if anchor == "free":
            return
        disp = Gdk.Display.get_default()
        mon = disp.get_primary_monitor() or disp.get_monitor(0)
        wa = mon.get_workarea()
        if w is None:
            w, h = win.get_size()
        margin = 12
        x = wa.x + margin if "l" in anchor else wa.x + wa.width - w - margin
        y = wa.y + margin if anchor[0] == "t" else wa.y + wa.height - h - margin
        win.move(x, y)

    def save_cfg() -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            OVERLAY_CFG.write_text(json.dumps(cfg))
        except OSError:
            pass

    def set_light_class(ebox, light: str) -> None:
        ctx = ebox.get_child().get_style_context()
        for name in LIGHT_RGB:
            ctx.remove_class(name)
        ctx.add_class(light if light in LIGHT_RGB else "gray")

    def make_light(s: dict) -> Gtk.EventBox:
        dot = Gtk.Box()
        dot.set_size_request(LIGHT_PX, LIGHT_PX)
        dot.get_style_context().add_class("light")
        ebox = Gtk.EventBox(visible_window=False)
        ebox.add(dot)
        ebox.set_has_tooltip(True)
        ebox._s = s

        def tooltip(_w, _x, _y, _kb, tip):
            sess = ebox._s
            esc = GLib.markup_escape_text
            lines = [f"<b>{esc(sess['name'])}</b>",
                     f"<tt>{esc(sess['cwd'])}</tt>",
                     f"{esc(sess['light'])} · {esc(sess['status'])}",
                     f"pid {sess['pid']} · up {_fmt_age(sess['startedAt'])}"
                     f" · changed {_fmt_age(sess['updatedAt'])} ago"]
            if sess.get("detail"):
                lines.append(esc(sess["detail"]))
            tip.set_markup("\n".join(lines))
            return True

        ebox.connect("query-tooltip", tooltip)

        # click on a light = show its info; drag (>4px) still moves the window
        press = [None]

        def light_press(_w, event):
            if event.button == 1:
                press[0] = (event.x_root, event.y_root, event.time)
                return True
            return on_press(_w, event)

        def light_motion(_w, event):
            if press[0] and (abs(event.x_root - press[0][0]) > 4
                             or abs(event.y_root - press[0][1]) > 4):
                x, y, t = press[0]
                press[0] = None
                anchor_items["free"].set_active(True)
                win.begin_move_drag(1, int(x), int(y), t)
            return True

        def light_release(_w, _event):
            if press[0]:
                press[0] = None
                ebox.trigger_tooltip_query()
            return True

        ebox.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                        | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.BUTTON1_MOTION_MASK)
        ebox.connect("button-press-event", light_press)
        ebox.connect("motion-notify-event", light_motion)
        ebox.connect("button-release-event", light_release)
        return ebox

    def refresh() -> bool:
        sessions = scan_sessions()
        seen = set()
        changed = False
        for s in sessions:
            seen.add(s["id"])
            el = lights.get(s["id"])
            if el is None:
                el = make_light(s)
                lights[s["id"]] = el
                flow.add(el)
                changed = True
            el._s = s
            set_light_class(el, s["light"])
        for sid in list(lights):
            if sid not in seen:
                lights.pop(sid).get_parent().destroy()
                changed = True
        set_empty()
        if changed:
            apply_layout(cfg.get("layout", "v"))
        flow.show_all()
        return True

    menu = Gtk.Menu()

    def add_radios(pairs, current, on_pick) -> dict:
        group = None
        items = {}
        for key, label in pairs:
            item = Gtk.RadioMenuItem(label=label)
            if group:
                item.join_group(group)
            else:
                group = item
            item.set_active(current == key)
            item.connect("activate",
                         lambda it, k=key: it.get_active() and on_pick(k))
            menu.append(item)
            items[key] = item
        return items

    def pick_layout(k: str) -> None:
        apply_layout(k)
        save_cfg()

    def pick_anchor(k: str) -> None:
        cfg["anchor"] = k
        place()
        save_cfg()

    add_radios((("h", "Horizontal"), ("v", "Vertical"), ("grid", "Grid")),
               cfg.get("layout", "v"), pick_layout)
    menu.append(Gtk.SeparatorMenuItem())
    anchor_items = add_radios(
        (("tl", "Top left"), ("tr", "Top right"),
         ("bl", "Bottom left"), ("br", "Bottom right"),
         ("free", "Free (drag)")),
        cfg.get("anchor", "tr"), pick_anchor)
    menu.append(Gtk.SeparatorMenuItem())
    quit_item = Gtk.MenuItem(label="Quit claudetell")
    quit_item.connect("activate", Gtk.main_quit)
    menu.append(quit_item)
    menu.show_all()

    def on_press(_w, event):
        if event.button == 3:
            menu.popup_at_pointer(event)
            return True
        if event.button == 1:
            anchor_items["free"].set_active(True)  # drag implies free placement
            win.begin_move_drag(1, int(event.x_root), int(event.y_root),
                                event.time)
            return True
        return False

    win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
    win.connect("button-press-event", on_press)
    win.connect("destroy", Gtk.main_quit)

    def on_configure(_w, _e):
        cfg["pos"] = list(win.get_position())
        return False

    win.connect("configure-event", on_configure)

    refresh()
    GLib.timeout_add(1000, refresh)
    # re-anchor whenever content size changes (lights added/removed, layout)
    win.connect("size-allocate", lambda _w, a: place(a.width, a.height))
    win.show_all()
    if cfg.get("anchor", "tr") == "free" and isinstance(cfg.get("pos"), list) \
            and len(cfg["pos"]) == 2:
        win.move(*cfg["pos"])
    else:
        place()
    try:
        Gtk.main()
    finally:
        save_cfg()


# -- hook install/uninstall -------------------------------------------------

HOOK_TAG = "claudetell.py hook"


def hook_entries() -> dict[str, list[dict]]:
    cmd = f"python3 {Path(__file__).resolve()} hook"
    hook = {"type": "command", "command": cmd, "timeout": 10}
    rule = [{"hooks": [hook]}]
    return {
        "PreToolUse": [{"matcher": "Bash|ScheduleWakeup|CronCreate", "hooks": [hook]}],
        "SubagentStart": rule,
        "SubagentStop": rule,
        "StopFailure": rule,
        "UserPromptSubmit": rule,
        "Stop": rule,
        "SessionEnd": rule,
    }


def _is_ours(rule: dict) -> bool:
    return any(HOOK_TAG in h.get("command", "") for h in rule.get("hooks", []))


def cmd_install() -> None:
    settings_file = CLAUDE_DIR / "settings.json"
    settings = {}
    if settings_file.exists():
        settings = json.loads(settings_file.read_text())
        backup = settings_file.with_suffix(".json.claudetell.bak")
        if not backup.exists():
            backup.write_text(json.dumps(settings, indent=2))
    hooks = settings.setdefault("hooks", {})
    for event, rules in hook_entries().items():
        existing = hooks.setdefault(event, [])
        existing[:] = [r for r in existing if not _is_ours(r)]
        existing.extend(rules)
    settings_file.write_text(json.dumps(settings, indent=2))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"hooks installed in {settings_file} (backup: *.claudetell.bak)")
    print("restart running claude sessions (or /hooks reload) to pick them up")


def cmd_uninstall() -> None:
    settings_file = CLAUDE_DIR / "settings.json"
    if not settings_file.exists():
        return
    settings = json.loads(settings_file.read_text())
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [r for r in hooks[event] if not _is_ours(r)]
        if not hooks[event]:
            del hooks[event]
    settings_file.write_text(json.dumps(settings, indent=2))
    print("claudetell hooks removed")


# -- frontend ---------------------------------------------------------------

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>claudetell</title>
<style>
  :root {
    --bg: #1e1e2e; --surface: #313244; --overlay: #45475a;
    --text: #cdd6f4; --muted: #7f849c; --border: #45475a;
    --green: #4ade80; --busy: #fbbf24; --blue: #60a5fa; --mauve: #cba6f7;
    --orange: #fb923c; --red: #f87171; --gray: #585b70;
  }
  * { box-sizing: border-box; margin: 0; }
  html, body { height: 100%; }
  body {
    background: var(--bg); color: var(--text);
    font: 13px/1.45 system-ui, -apple-system, sans-serif;
    display: flex; align-items: flex-start; justify-content: flex-start;
  }
  #app { padding: 10px; width: 100%; }
  .panel {
    background: color-mix(in srgb, var(--surface) 55%, transparent);
    border: 1px solid var(--border); border-radius: 14px;
    padding: 10px 12px; backdrop-filter: blur(12px);
    display: inline-flex; flex-direction: column; gap: 8px; min-width: 150px;
  }
  header { display: flex; align-items: center; gap: 8px; user-select: none; }
  header .title { font-weight: 600; font-size: 12px; letter-spacing: .04em;
    color: var(--muted); text-transform: uppercase; }
  header .count { font-size: 11px; color: var(--muted); margin-right: auto; }
  .btn {
    background: none; border: 1px solid transparent; border-radius: 7px;
    color: var(--muted); cursor: pointer; font-size: 13px; line-height: 1;
    padding: 4px 6px;
  }
  .btn:hover { background: var(--overlay); color: var(--text); }
  .btn.active { color: var(--text); border-color: var(--border); background: var(--overlay); }

  #lights { display: flex; gap: 10px; align-items: center; }
  #lights.v { flex-direction: column; }
  #lights.grid { display: grid; grid-template-columns: repeat(4, 1fr); }
  .light {
    width: 22px; height: 22px; border-radius: 50%; cursor: default;
    border: 1px solid rgba(255,255,255,.15);
    background: radial-gradient(circle at 35% 30%,
      color-mix(in srgb, var(--c) 70%, white) 0%, var(--c) 55%,
      color-mix(in srgb, var(--c) 60%, black) 100%);
    box-shadow: 0 0 8px 1px color-mix(in srgb, var(--c) 60%, transparent),
                inset 0 -2px 4px rgba(0,0,0,.25);
    transition: transform .15s;
  }
  .light:hover { transform: scale(1.18); }
  .light.green  { --c: var(--green); }
  .light.busy   { --c: var(--busy);   animation: pulse 1.6s ease-in-out infinite; }
  .light.blue   { --c: var(--blue);   animation: pulse 2.2s ease-in-out infinite; }
  .light.mauve  { --c: var(--mauve);  animation: pulse 2.2s ease-in-out infinite; }
  .light.orange { --c: var(--orange); animation: pulse 1.1s ease-in-out infinite; }
  .light.red    { --c: var(--red);    animation: pulse .8s ease-in-out infinite; }
  .light.gray   { --c: var(--gray); }
  @keyframes pulse {
    0%, 100% { filter: brightness(1); }
    50% { filter: brightness(.55); }
  }
  .empty { color: var(--muted); font-size: 12px; padding: 2px 0; }

  #tip {
    position: fixed; z-index: 10; display: none; max-width: 300px;
    background: var(--bg); border: 1px solid var(--border); border-radius: 10px;
    padding: 8px 10px; box-shadow: 0 6px 24px rgba(0,0,0,.5); pointer-events: none;
  }
  #tip .name { font-weight: 600; display: flex; align-items: center; gap: 6px; }
  #tip .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--c); }
  #tip .cwd { color: var(--muted); font-family: ui-monospace, monospace;
    font-size: 11px; word-break: break-all; margin: 2px 0 6px; }
  #tip .row { display: flex; justify-content: space-between; gap: 12px; font-size: 11px; }
  #tip .row span:first-child { color: var(--muted); }
  #tip .detail { font-size: 11px; margin-top: 4px; padding-top: 4px;
    border-top: 1px solid var(--border); }
</style>
</head>
<body>
<div id="app">
  <div class="panel">
    <header>
      <span class="title">claudetell</span>
      <span class="count" id="count"></span>
      <button class="btn lay" data-l="h" title="horizontal">──</button>
      <button class="btn lay" data-l="v" title="vertical">︙</button>
      <button class="btn lay" data-l="grid" title="grid">▦</button>
      <button class="btn" id="pip" title="pop out (always on top)" hidden>⧉</button>
    </header>
    <div id="lights"></div>
    <div id="tip"></div>
  </div>
</div>
<script>
const ORIGIN = location.origin;
const lightsEl = document.getElementById("lights");
const tip = document.getElementById("tip");
const nodes = new Map();
let sessions = [];

function fmtAge(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.round(s / 60) + "m";
  if (s < 86400) return (s / 3600).toFixed(1) + "h";
  return (s / 86400).toFixed(1) + "d";
}

function showTip(el, s) {
  const cs = getComputedStyle(el).getPropertyValue("--c");
  tip.innerHTML = `
    <div class="name" style="--c:${cs}"><span class="dot"></span>${esc(s.name)}</div>
    <div class="cwd">${esc(s.cwd)}</div>
    <div class="row"><span>state</span><span>${esc(s.light)} · ${esc(s.status)}</span></div>
    <div class="row"><span>pid</span><span>${s.pid}</span></div>
    <div class="row"><span>up</span><span>${fmtAge(Date.now() - s.startedAt)}</span></div>
    <div class="row"><span>changed</span><span>${fmtAge(Date.now() - s.updatedAt)} ago</span></div>
    ${s.detail ? `<div class="detail">${esc(s.detail)}</div>` : ""}`;
  tip.style.display = "block";
  const r = el.getBoundingClientRect(), w = el.ownerDocument.defaultView;
  const tr = tip.getBoundingClientRect();
  let x = Math.min(Math.max(4, r.left + r.width / 2 - tr.width / 2), w.innerWidth - tr.width - 4);
  let y = r.bottom + 8;
  if (y + tr.height > w.innerHeight - 4) y = r.top - tr.height - 8;
  tip.style.left = x + "px"; tip.style.top = Math.max(4, y) + "px";
}
function esc(t) { const d = document.createElement("i"); d.textContent = t ?? ""; return d.innerHTML; }

function render(data) {
  sessions = data.sessions;
  document.getElementById("count").textContent = sessions.length || "";
  const seen = new Set();
  for (const s of sessions) {
    seen.add(s.id);
    let el = nodes.get(s.id);
    if (!el) {
      el = document.createElement("div");
      el.addEventListener("mouseenter", () => showTip(el, el._s));
      el.addEventListener("click", () => showTip(el, el._s));
      el.addEventListener("mouseleave", () => tip.style.display = "none");
      nodes.set(s.id, el);
    }
    el._s = s;
    el.className = "light " + s.light;
    lightsEl.appendChild(el);
  }
  for (const [id, el] of nodes) if (!seen.has(id)) { el.remove(); nodes.delete(id); }
  lightsEl.querySelectorAll(".empty").forEach(e => e.remove());
  if (!sessions.length) {
    const e = document.createElement("div");
    e.className = "empty"; e.textContent = "no sessions";
    lightsEl.appendChild(e);
  }
}

async function tick() {
  try {
    const res = await fetch(ORIGIN + "/api/sessions");
    render(await res.json());
  } catch (e) { /* server gone; keep last state */ }
}

// layout
const saved = localStorage.getItem("layout") || "h";
function setLayout(l) {
  lightsEl.className = l === "h" ? "" : l;
  localStorage.setItem("layout", l);
  document.querySelectorAll(".lay").forEach(b => b.classList.toggle("active", b.dataset.l === l));
}
document.querySelectorAll(".lay").forEach(b => b.onclick = () => setLayout(b.dataset.l));
setLayout(saved);

// picture-in-picture overlay (Chromium: real always-on-top window)
let timer = setInterval(tick, 1000);
const pipBtn = document.getElementById("pip");
if ("documentPictureInPicture" in window) {
  pipBtn.hidden = false;
  pipBtn.onclick = async () => {
    const app = document.getElementById("app");
    const pip = await documentPictureInPicture.requestWindow({ width: 340, height: 110 });
    for (const st of document.querySelectorAll("style"))
      pip.document.head.appendChild(st.cloneNode(true));
    pip.document.body.appendChild(app);
    clearInterval(timer);
    timer = pip.setInterval(tick, 1000);  // background-tab timers are throttled
    pip.addEventListener("pagehide", () => {
      document.body.appendChild(app);
      timer = setInterval(tick, 1000);
    });
  };
}
tick();
</script>
</body>
</html>
"""


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "overlay"
    if cmd == "hook":
        cmd_hook()
    elif cmd == "overlay":
        cmd_overlay()
    elif cmd == "serve":
        port = int(args[args.index("--port") + 1]) if "--port" in args else DEFAULT_PORT
        cmd_serve(port, "--all" in args)
    elif cmd == "install":
        cmd_install()
    elif cmd == "uninstall":
        cmd_uninstall()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
