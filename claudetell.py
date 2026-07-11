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
  claudetell.py serve [--host 127.0.0.1] [--port 7717]   browser version
  claudetell.py json [--all]          print live sessions as JSON (used over SSH)
  claudetell.py focus <query>         focus a session's window/pane (bind to a key)
  claudetell.py hook                  hook entrypoint (stdin JSON from Claude Code)
  claudetell.py install               register hooks in ~/.claude/settings.json
  claudetell.py uninstall             remove them

Lights:
  green   idle, waiting for next user prompt
  amber   busy (working)                       [pulses]
  blue    agents or a live background shell running (count shown as a badge)
  mauve   loop scheduled (ScheduleWakeup) while otherwise idle
  orange  waiting for user input (permissions / ask menu)
  red     error (usage limit, API error)       [pulses]
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    / "claudetell"
)
DEFAULT_PORT = 7717
LOOP_GRACE = 120  # ponytail: mauve lingers this long past a scheduled wakeup
AGENT_TTL = 2 * 3600  # ponytail: SubagentStop can be missed on crash; TTL self-heals
BUSY_STALE = 300  # "busy" + no transcript write this long → check CPU (tree_busy)
WORKFLOW_TTL = 1800  # ponytail: no completion hook — badge/detail only, never a color
SHELL_COMMS = {"bash", "sh", "dash", "zsh", "ksh", "fish"}
REMOTES_CFG = STATE_DIR / "remotes.json"
REMOTE_INTERVAL = 5  # seconds between SSH polls of each remote host


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


def session_pid(session_id: str) -> int | None:
    """Live pid owning this session, from the session files."""
    for path in (CLAUDE_DIR / "sessions").glob("*.json"):
        try:
            d = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("sessionId") == session_id and pid_alive(d.get("pid", 0)):
            return d.get("pid")
    return None


def _proc_table() -> dict[int, tuple[int, str, str]]:
    """pid -> (ppid, comm, starttime) in one /proc pass."""
    tab: dict[int, tuple[int, str, str]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            data = open(f"/proc/{name}/stat").read()
            comm = data[data.index("(") + 1 : data.rindex(")")]
            rest = data[data.rindex(")") + 1 :].split()
            tab[int(name)] = (int(rest[1]), comm, rest[19])
        except (OSError, ValueError, IndexError):
            continue
    return tab


def _descendants(root: int, tab: dict) -> list[int]:
    kids: dict[int, list[int]] = {}
    for pid, (ppid, _, _) in tab.items():
        kids.setdefault(ppid, []).append(pid)
    out, stack = [], list(kids.get(root, []))
    while stack:
        p = stack.pop()
        out.append(p)
        stack.extend(kids.get(p, []))
    return out


CPU_BUSY_TPS = 8  # process-tree CPU ticks/sec above this ⇒ actively computing
_cpu_cache: dict[int, tuple[float, int]] = {}  # pid -> (monotonic, tree ticks)


def _tree_cpu_ticks(pid: int) -> int:
    """utime+stime summed over the claude process and all its descendants."""
    tab = _proc_table()
    total = 0
    for p in [pid] + _descendants(pid, tab):
        try:
            f = open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()
            total += int(f[11]) + int(f[12])
        except (OSError, IndexError, ValueError):
            pass
    return total


def tree_busy(pid: int) -> bool:
    """Is the session's process tree actively burning CPU? Lets a long silent
    tool (build/sim writing nothing to the transcript) stay 'busy'. Compares CPU
    ticks between scans; assumes busy until it has two samples (avoids a flicker
    to idle on the first stale scan). Idle MCP servers sit well under the rate."""
    now, ticks = time.monotonic(), _tree_cpu_ticks(pid)
    prev = _cpu_cache.get(pid)
    _cpu_cache[pid] = (now, ticks)
    if not prev or now - prev[0] < 0.5:
        return True
    return (ticks - prev[1]) / (now - prev[0]) > CPU_BUSY_TPS


def _ancestors(pid: int, tab: dict) -> set[int]:
    seen: set[int] = set()
    while pid > 1 and pid in tab and pid not in seen:
        seen.add(pid)
        pid = tab[pid][0]
    return seen


def capture_bg_shell(claude_pid: int) -> list | None:
    """[pid, starttime] of the background shell a Bash tool just launched.

    Runs inside the PostToolUse hook (itself a descendant of claude). The shell
    spawned *before* the hook did, so after excluding this hook's own process
    chain, the newest shell descendant of claude is that shell. Precise liveness
    (pid+starttime) later means the light clears the instant the shell exits —
    no TTL guessing. ponytail: parallel bg launches in one batch may mis-pick;
    self-heals as the wrong pid dies.
    """
    tab = _proc_table()
    mine = _ancestors(os.getpid(), tab) | {os.getpid()}
    best = None
    for p in _descendants(claude_pid, tab):
        if p in mine:
            continue
        _ppid, comm, start = tab[p]
        if comm in SHELL_COMMS and (best is None or int(start) > best[0]):
            best = (int(start), p, start)
    return [best[1], best[2]] if best else None


def shell_alive(entry) -> bool:
    return (
        isinstance(entry, (list, tuple))
        and len(entry) == 2
        and pid_alive(entry[0])
        and proc_starttime(entry[0]) == entry[1]
    )


# -- session identity + focus ------------------------------------------------

# shape = per-cwd bucket (same folder → same shape), cycled so distinct projects
# get distinct shapes. Silhouettes are per-corner border-radius (top-left,
# top-right, bottom-right, bottom-left) — both GTK and the web view render these
# natively, so ~12 clearly-distinct shapes with no SVG polygons or cairo.
SHAPE_RADIUS = {
    "circle": "999px",
    "square": "2px",
    "rounded": "8px",
    "arch": "999px 999px 2px 2px",
    "keystone": "2px 2px 999px 999px",
    "d-left": "999px 2px 2px 999px",
    "d-right": "2px 999px 999px 2px",
    "drop": "999px 2px 999px 999px",
    "drop-alt": "999px 999px 999px 2px",
    "fan": "2px 999px 2px 2px",
    "leaf": "999px 2px 999px 2px",
    "leaf-alt": "2px 999px 2px 999px",
}
SHAPES = tuple(SHAPE_RADIUS)


def assign_shapes(keys: list[str]) -> list[str]:
    """Cycle shapes across distinct keys (cwds): same key → same shape, new keys
    get the next shape round-robin. Collides only past len(SHAPES) distinct keys."""
    order: dict[str, int] = {}
    for k in keys:
        order.setdefault(k, len(order))
    return [SHAPES[order[k] % len(SHAPES)] for k in keys]


def session_letter(name: str) -> str:
    for ch in name or "":
        if ch.isalnum():
            return ch.upper()
    return "?"


_env_cache: dict[int, tuple[str | None, str | None, str | None]] = {}


def session_tmux(pid: int) -> tuple[str | None, str | None]:
    """(TMUX_PANE, tmux socket) from the session process's environ, cached.
    environ is fixed at exec, so (pid, starttime) is a safe cache key."""
    st = proc_starttime(pid)
    cached = _env_cache.get(pid)
    if cached and cached[0] == st:
        return cached[1], cached[2]
    pane = sock = None
    try:
        raw = open(f"/proc/{pid}/environ", "rb").read()
        for kv in raw.split(b"\0"):
            k, _, v = kv.partition(b"=")
            if k == b"TMUX_PANE":
                pane = v.decode("utf-8", "replace")
            elif k == b"TMUX":  # <socket>,<server-pid>,<session-n>
                sock = v.decode("utf-8", "replace").split(",")[0] or None
    except OSError:
        pass
    _env_cache[pid] = (st, pane, sock)
    return pane, sock


def _ancestor_chain(pid: int) -> list[int]:
    """[pid, parent, grandparent, ...] up to init — nearest first."""
    tab = _proc_table()
    chain, seen = [], set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        chain.append(pid)
        if pid not in tab:
            break
        pid = tab[pid][0]
    return chain


def _tmux_client_pids(sock: str | None, pane: str) -> list[int]:
    """PIDs of terminals with a client attached to this pane's session — those
    are the GUI windows worth focusing (a detached session has none)."""
    base = ["tmux"] + (["-S", sock] if sock else [])
    try:
        sess = subprocess.run(
            base + ["display-message", "-p", "-t", pane, "#{session_id}"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        out = subprocess.run(
            base + ["list-clients", "-t", sess, "-F", "#{client_pid}"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
        return [int(x) for x in out.split() if x.isdigit()]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def _kitty_focus(pids: set[int]) -> bool:
    """Focus the kitty window whose process tree contains one of pids, via
    kitty's remote-control IPC (works on Wayland, unlike xdotool)."""
    # every kitty instance has its own socket; a session can live in any of
    # them, so check them all (own socket first), not just KITTY_LISTEN_ON.
    socks = list(
        dict.fromkeys(
            (
                [os.environ["KITTY_LISTEN_ON"]]
                if os.environ.get("KITTY_LISTEN_ON")
                else []
            )
            + [
                "unix:" + p
                for p in glob.glob("/tmp/kitty-*")
                if not p.endswith(".lock")
            ]
        )
    )
    for sock in socks:
        try:
            ls = subprocess.run(
                ["kitten", "@", "--to", sock, "ls"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            data = json.loads(ls.stdout)
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            continue
        for osw in data:
            for tab in osw.get("tabs", []):
                for w in tab.get("windows", []):
                    tree = {w.get("pid")} | {
                        p.get("pid") for p in w.get("foreground_processes", [])
                    }
                    if tree & pids:
                        subprocess.run(
                            [
                                "kitten",
                                "@",
                                "--to",
                                sock,
                                "focus-window",
                                "--match",
                                f"id:{w['id']}",
                            ],
                            timeout=2,
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        return True
    return False


def _xdotool_focus(pids: list[int]) -> None:
    """X11 fallback for non-kitty terminals. ponytail: no-op on Wayland."""
    if not os.environ.get("DISPLAY"):
        return
    for anc in pids:  # nearest ancestor with a window wins
        try:
            out = subprocess.run(
                ["xdotool", "search", "--pid", str(anc)],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return  # no xdotool — give up silently
        wins = out.stdout.split()
        if wins:
            try:
                subprocess.run(
                    ["xdotool", "windowactivate", wins[-1]],
                    timeout=2,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            return


def focus_session(entry: dict) -> None:
    """Focus the tmux pane + terminal GUI window hosting a session (same
    machine). tmux switches the pane; kitty IPC (or xdotool) raises the window."""
    if entry.get("remote"):
        return  # pane/pid live on another machine — its pid may collide locally
    pane, sock = entry.get("pane"), entry.get("tmux_sock")
    pids = []
    if pane:
        base = ["tmux"] + (["-S", sock] if sock else [])

        def tmux_out(args):
            try:
                return subprocess.run(
                    base + args, capture_output=True, text=True, timeout=2
                ).stdout.split()
            except (OSError, subprocess.SubprocessError):
                return []

        # If a client already displays this pane's session, just position the
        # window/pane there — never touch clients viewing OTHER sessions (that
        # would yank every terminal to the clicked session). Only when the
        # session is detached do we switch a single client to show it.
        sess = " ".join(
            tmux_out(["display-message", "-p", "-t", pane, "#{session_id}"])
        )
        cmds = [["select-window", "-t", pane], ["select-pane", "-t", pane]]
        if sess and not tmux_out(["list-clients", "-t", sess, "-F", "#{client_name}"]):
            spare = tmux_out(["list-clients", "-F", "#{client_name}"])
            if spare:
                cmds.insert(0, ["switch-client", "-c", spare[0], "-t", pane])
        for sub in cmds:
            try:
                subprocess.run(
                    base + sub,
                    timeout=2,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        for cp in _tmux_client_pids(sock, pane):  # client → its kitty window shell
            pids += _ancestor_chain(cp)
    pid = entry.get("pid", 0)
    if pid:
        pids += _ancestor_chain(pid)
    if pids and not _kitty_focus(set(pids)):
        _xdotool_focus(pids)


def cmd_focus(query: str) -> None:
    """Focus a session by letter, name, or cwd substring — bind this to a key
    via your desktop's keyboard settings (Wayland won't let apps grab one)."""
    q = query.strip().lower()
    if not q:
        sys.exit("usage: claudetell.py focus <letter|name|cwd-substring>")
    for s in scan_sessions(show_all=True):
        if q == s["letter"].lower() or q in s["name"].lower() or q in s["cwd"].lower():
            focus_session(s)
            return
    sys.exit(f"no live session matching {query!r}")


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
            workflows = st.get("workflows", [])

            if event == "PreToolUse":
                tool = payload.get("tool_name", "")
                ti = payload.get("tool_input") or {}
                if tool in ("ScheduleWakeup", "CronCreate"):
                    delay = float(ti.get("delaySeconds") or ti.get("interval") or 600)
                    st["loop_until"] = now + min(delay, 3600) + LOOP_GRACE
                elif tool == "Workflow":
                    workflows.append(now)
            elif event == "PostToolUse":
                # shell exists only after the tool returns — capture its pid now
                ti = payload.get("tool_input") or {}
                if payload.get("tool_name") == "Bash" and ti.get("run_in_background"):
                    cp = session_pid(sid)
                    sh = capture_bg_shell(cp) if cp else None
                    if sh:
                        shells.append(sh)
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
                if event == "UserPromptSubmit":
                    st.pop("loop_until", None)  # user took over → not a loop anymore

            st["agents"] = [t for t in agents if now - t < AGENT_TTL]
            st["shells"] = [s for s in shells if shell_alive(s)]
            st["workflows"] = [t for t in workflows if now - t < WORKFLOW_TTL]
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


ERROR_MARKERS = (
    "usage limit",
    "rate limit",
    "session limit",
    "api error",
    "overloaded",
    "credit balance",
    "oauth token has expired",
)


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
        # null system markers (no level, no content) trail real entries — they
        # aren't messages, so skip them and keep looking back for the real one.
        if etype == "system" and not entry.get("level") and not entry.get("content"):
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


def compute_light(
    base: str, st: dict, err: str | None, pid: int
) -> tuple[str, str, dict]:
    """Map ground-truth state to a light. Every ephemeral state is re-validated
    against something we can verify *right now* (a live pid, a fresh status
    timestamp, an unexpired hook), so no light survives its cause.
    """
    now = time.time()
    # re-prune on read: state may be scanned long after the last hook wrote it
    agents = [t for t in st.get("agents", []) if now - t < AGENT_TTL]
    workflows = [t for t in st.get("workflows", []) if now - t < WORKFLOW_TTL]
    # shells are pid-tracked: gone the instant the process dies (no TTL guess)
    shells = [s for s in st.get("shells", []) if shell_alive(s)]
    counts = {"agents": len(agents), "shells": len(shells), "workflows": len(workflows)}

    def parts() -> str:
        p = []
        if agents:
            p.append(f"{len(agents)} agent{'s' if len(agents) > 1 else ''}")
        if workflows:
            p.append(f"{len(workflows)} workflow{'s' if len(workflows) > 1 else ''}")
        if shells:
            p.append(f"{len(shells)} background shell{'s' if len(shells) > 1 else ''}")
        return " + ".join(p)

    if err:
        return "red", err[:300], counts
    if base == "waiting":
        return "orange", "waiting for your input", counts
    # blue only for things we can prove are alive — agents (SubagentStop) and
    # pid-tracked shells. Workflows have no completion signal, so they inform the
    # detail/badge but never drive the color (else they'd stick after finishing).
    if agents or shells:
        return "blue", parts(), counts
    if base == "busy":
        return "busy", parts() or "working", counts
    if st.get("loop_until", 0) > now:  # idle, but a wakeup is scheduled
        return "mauve", parts() or "loop scheduled", counts
    if base == "idle":
        return "green", parts() or "waiting for next prompt", counts
    return "gray", base or "unknown", counts


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
        # self-heal red: if the session reported a fresh status after the error,
        # it recovered — don't wait on a UserPromptSubmit/Stop hook that may miss.
        status_ts = (data.get("statusUpdatedAt") or 0) / 1000
        if hook_err and status_ts > hook_err.get("ts", 0) + 1:
            hook_err = None
        if hook_err:
            err = f"{hook_err.get('type', 'error')}: {hook_err.get('msg', '')}".strip(
                ": "
            )
        else:
            # fallback: sessions started before hooks were installed
            err = transcript_error(sid)
        status = data.get("status") or ""
        # statusUpdatedAt only stamps the busy transition, NOT each turn step, so
        # a long turn looks "stale" by it. A live turn writes to the transcript
        # every few seconds; no write for BUSY_STALE means the session is stuck
        # (killed/suspended UI, crash mid-turn) — treat it as idle, not amber.
        # A long tool (build/sim) can run >BUSY_STALE writing nothing, so before
        # demoting a stale-transcript session we check its process tree is still
        # burning CPU; only a genuinely quiet one falls back to idle.
        if status == "busy":
            tp = find_transcript(sid)
            try:
                fresh = bool(tp) and time.time() - tp.stat().st_mtime < BUSY_STALE
            except OSError:
                fresh = False
            if not fresh and not tree_busy(pid):
                status = "idle"
        light, detail, counts = compute_light(status, st, err, pid)
        pane, tmux_sock = session_tmux(pid)
        name = data.get("name") or Path(data.get("cwd", "?")).name
        entry = {
            "id": sid,
            "pid": pid,
            "name": name,
            "cwd": data.get("cwd", ""),
            "letter": session_letter(name),
            "pane": pane,
            "tmux_sock": tmux_sock,
            "kind": data.get("kind", ""),
            "version": data.get("version", ""),
            "startedAt": data.get("startedAt", 0),
            "updatedAt": data.get("statusUpdatedAt") or data.get("updatedAt", 0),
            "status": status or "unknown",
            "light": light,
            "detail": detail,
            "agents": counts["agents"],
            "workflows": counts["workflows"],
            "shells": counts["shells"],
            "bg": counts["agents"] + counts["workflows"] + counts["shells"],
        }
        # one session can own several pid files (e.g. resume); keep the freshest
        cur = best.get(sid)
        if cur is None or entry["updatedAt"] >= cur["updatedAt"]:
            best[sid] = entry
    result = sorted(best.values(), key=lambda s: s["startedAt"])
    for s, shape in zip(result, assign_shapes([s["cwd"] or s["id"] for s in result])):
        s["shape"] = shape
    return result


# -- remote sessions (over SSH) ---------------------------------------------


def load_remotes() -> list[dict]:
    """Configurable hosts to poll over SSH. remotes.json is a JSON list of
    {"name": "devbox", "ssh": "user@host"} — optional "cmd" (run instead of
    piping this script), "ssh_opts" (list), "timeout" (seconds)."""
    try:
        data = json.loads(REMOTES_CFG.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def fetch_remote(remote: dict, src: str) -> list[dict]:
    """SSH to a host and run claudetell's json scan there; tag the sessions with
    the host. The remote computes its own lights (it owns the /proc + transcripts
    + hook state), so by default we just pipe THIS script to its python3 — no
    install or version-sync needed. Set "cmd" to run an installed copy instead."""
    name = remote.get("name") or remote.get("ssh") or "remote"
    target = remote.get("ssh")
    if not target:
        return []
    opts = remote.get("ssh_opts") or ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    cmd = remote.get("cmd")
    argv = ["ssh", *opts, target] + (cmd.split() if cmd else ["python3", "-", "json"])
    try:
        out = subprocess.run(
            argv,
            input=None if cmd else src,
            capture_output=True,
            text=True,
            timeout=remote.get("timeout", 12),
        )
        data = json.loads(out.stdout)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return []
    sessions = data.get("sessions", []) if isinstance(data, dict) else data
    for s in sessions:
        s["remote"] = True
        s["host"] = name
        s["id"] = f"{name}:{s.get('id', '')}"  # namespace so ids never collide
    return sessions if isinstance(sessions, list) else []


# -- server -----------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    show_all = False

    def do_GET(self) -> None:
        if self.path.startswith("/api/sessions"):
            body = json.dumps(
                {"now": time.time() * 1000, "sessions": scan_sessions(self.show_all)}
            ).encode()
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

    def do_POST(self) -> None:
        # only the session id crosses the wire; the pane/socket handed to tmux
        # come from server-side scan data, never from the client — no shell.
        if self.path.startswith("/api/focus"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                sid = json.loads(self.rfile.read(length) or b"{}").get("id")
            except (ValueError, json.JSONDecodeError):
                sid = None
            hit = (
                next((s for s in scan_sessions(self.show_all) if s["id"] == sid), None)
                if sid
                else None
            )
            if hit:
                focus_session(hit)
            self.send_response(204 if hit else 404)
            self.end_headers()
            return
        self.send_error(404)

    def log_message(self, *args) -> None:
        pass


def cmd_serve(port: int, show_all: bool, host: str = "127.0.0.1") -> None:
    Handler.show_all = show_all
    server = ThreadingHTTPServer((host, port), Handler)
    shown = host if host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    print(f"claudetell overlay: http://{shown}:{port}")
    if host not in ("127.0.0.1", "::1", "localhost"):
        print(
            "warning: bound to a non-loopback address — the page has no auth "
            "and exposes session names, cwds and pids to anyone who can reach "
            "it. Prefer 127.0.0.1 + an SSH tunnel (ssh -L)."
        )
    print(
        "tip: open it in Chrome/Chromium and hit the ⧉ button for an "
        "always-on-top picture-in-picture overlay"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# -- native overlay (GTK3) ----------------------------------------------------

LIGHT_RGB = {
    "green": (0x4A, 0xDE, 0x80),
    "busy": (0xFB, 0xBF, 0x24),
    "blue": (0x60, 0xA5, 0xFA),
    "mauve": (0xCB, 0xA6, 0xF7),
    "orange": (0xFB, 0x92, 0x3C),
    "red": (0xF8, 0x71, 0x71),
    "gray": (0x58, 0x5B, 0x70),
}
PULSE_PERIOD = {"busy": 1.6, "blue": 2.2, "mauve": 2.2, "orange": 1.1, "red": 0.8}
LIGHT_PX = 24
OVERLAY_CFG = STATE_DIR / "overlay.json"


def _overlay_css(alpha: float = 0.6) -> bytes:
    """GTK CSS for the whole overlay — avoids cairo (python3-gi-cairo often absent).
    alpha = panel background opacity (lights stay solid so you can see through the
    frame to what's behind it)."""
    rules = [
        f"window.claudetell {{ background: rgba(30,30,46,{alpha:.2f});"
        " border-radius: 13px; border: 1px solid rgba(69,71,90,0.9); }",
        ".light { border-radius: 999px; border: 1px solid rgba(255,255,255,0.18); }",
        # per-cwd shape = per-corner border-radius silhouette (see SHAPE_RADIUS).
        *(
            f".light.shape-{name} {{ border-radius: {r}; }}"
            for name, r in SHAPE_RADIUS.items()
        ),
        ".ltr { color: rgba(0,0,0,0.72); font-weight: 700; font-size: 10px;"
        " text-shadow: 0 1px 1px rgba(255,255,255,0.25); }",
        ".name { color: #cdd6f4; font-size: 11px; }",
        ".badge { background: rgba(30,30,46,0.95); color: #cdd6f4;"
        " font-size: 8px; font-weight: 700; padding: 0 2px; border-radius: 999px;"
        " border: 1px solid rgba(69,71,90,0.9); margin: 0 -2px -2px 0; }",
        "@keyframes ctpulse { 0% { opacity: 1; } 50% { opacity: 0.4; }"
        " 100% { opacity: 1; } }",
    ]
    for name, (r, g, b) in LIGHT_RGB.items():
        col = f"#{r:02x}{g:02x}{b:02x}"
        anim = (
            f" animation: ctpulse {PULSE_PERIOD[name]}s ease-in-out infinite;"
            if name in PULSE_PERIOD
            else ""
        )
        rules.append(
            f".light.{name} {{ background-color: {col};"
            f" box-shadow: 0 0 8px 1px alpha({col}, 0.55),"
            f" inset 0 -2px 4px rgba(0,0,0,0.3);{anim} }}"
        )
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
        from gi.repository import Gtk, Gdk, GLib, Pango
    except (ImportError, ValueError):
        sys.exit(
            "GTK3 not available (apt install python3-gi gir1.2-gtk-3.0) — "
            "or use: claudetell.py serve"
        )

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
    provider.load_from_data(_overlay_css(cfg.get("opacity", 0.6)))
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    win.get_style_context().add_class("claudetell")

    flow = Gtk.FlowBox(
        selection_mode=Gtk.SelectionMode.NONE,
        orientation=Gtk.Orientation.HORIZONTAL,
        column_spacing=8,
        row_spacing=8,
        homogeneous=True,
    )
    box = Gtk.Box(margin=10)
    box.add(flow)
    win.add(box)

    lights: dict[str, Gtk.Widget] = {}
    state = {"empty": None}

    # poll configured remote hosts over SSH off the GTK thread; refresh() merges
    # the latest cached result. One writer, one reader, atomic list swap → no lock.
    remote_cache = {"sessions": []}
    remotes = load_remotes()
    if remotes:
        src = Path(__file__).read_text()

        def poll_remotes() -> None:
            while True:
                merged = []
                for r in remotes:
                    merged += fetch_remote(r, src)
                remote_cache["sessions"] = merged
                time.sleep(REMOTE_INTERVAL)

        threading.Thread(target=poll_remotes, daemon=True).start()

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
        ctx = ebox._dot.get_style_context()
        for name in LIGHT_RGB:
            ctx.remove_class(name)
        ctx.add_class(light if light in LIGHT_RGB else "gray")

    def set_shape(ebox, shape: str) -> None:
        ctx = ebox._dot.get_style_context()
        for name in SHAPE_RADIUS:
            ctx.remove_class(f"shape-{name}")
        ctx.add_class(f"shape-{shape if shape in SHAPE_RADIUS else 'circle'}")

    def set_badge(ebox, s: dict) -> None:
        bg = s.get("bg", 0)
        if bg > 0:
            ebox._badge.set_text(str(bg))
            ebox._badge.show()
        else:
            ebox._badge.hide()

    def make_light(s: dict) -> Gtk.EventBox:
        dot = Gtk.Box()
        dot.set_size_request(LIGHT_PX, LIGHT_PX)
        dot.get_style_context().add_class("light")
        letter = Gtk.Label()
        letter.get_style_context().add_class("ltr")
        letter.set_halign(Gtk.Align.CENTER)
        letter.set_valign(Gtk.Align.CENTER)
        badge = Gtk.Label()
        badge.get_style_context().add_class("badge")
        badge.set_halign(Gtk.Align.END)
        badge.set_valign(Gtk.Align.END)
        badge.set_no_show_all(True)  # show_all() must not force it visible
        overlay = Gtk.Overlay()
        overlay.add(dot)
        overlay.add_overlay(letter)
        overlay.set_overlay_pass_through(letter, True)  # clicks fall through
        overlay.add_overlay(badge)
        overlay.set_overlay_pass_through(badge, True)  # clicks fall through to ebox
        # name label beside the dot (hidden unless "Show names"): a single letter
        # can't tell 15 similarly-named sessions apart; a few chars of the name can.
        name = Gtk.Label(xalign=0)
        name.get_style_context().add_class("name")
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(20)
        name.set_no_show_all(True)  # visibility driven by cfg["labels"], not show_all
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        row.pack_start(overlay, False, False, 0)
        row.pack_start(name, False, False, 0)
        ebox = Gtk.EventBox(visible_window=False)
        ebox.add(row)
        ebox.set_has_tooltip(True)
        ebox._s = s
        ebox._dot = dot
        ebox._letter = letter
        ebox._name = name
        ebox._badge = badge

        def tooltip(_w, _x, _y, _kb, tip):
            sess = ebox._s
            esc = GLib.markup_escape_text
            host = f" <i>@{esc(sess['host'])}</i>" if sess.get("host") else ""
            lines = [
                f"<b>{esc(sess['name'])}</b>{host}",
                f"<tt>{esc(sess['cwd'])}</tt>",
                f"{esc(sess['light'])} · {esc(sess['status'])}",
                f"pid {sess['pid']} · up {_fmt_age(sess['startedAt'])}"
                f" · changed {_fmt_age(sess['updatedAt'])} ago",
            ]
            if sess.get("detail"):
                lines.append(esc(sess["detail"]))
            tip.set_markup("\n".join(lines))
            return True

        ebox.connect("query-tooltip", tooltip)

        # click on a light = focus its pane/terminal; drag (>4px) moves the
        # window; info still shows on hover via the tooltip.
        press = [None]

        def light_press(_w, event):
            if event.button == 1:
                press[0] = (event.x_root, event.y_root, event.time)
                return True
            return on_press(_w, event)

        def light_motion(_w, event):
            if press[0] and (
                abs(event.x_root - press[0][0]) > 4
                or abs(event.y_root - press[0][1]) > 4
            ):
                x, y, t = press[0]
                press[0] = None
                anchor_items["free"].set_active(True)
                win.begin_move_drag(1, int(x), int(y), t)
            return True

        def light_release(_w, _event):
            if press[0]:
                press[0] = None
                focus_session(ebox._s)
            return True

        ebox.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.BUTTON1_MOTION_MASK
        )
        ebox.connect("button-press-event", light_press)
        ebox.connect("motion-notify-event", light_motion)
        ebox.connect("button-release-event", light_release)
        return ebox

    def refresh() -> bool:
        sessions = scan_sessions() + remote_cache["sessions"]
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
            set_shape(el, s.get("shape", "circle"))
            el._letter.set_text(s.get("letter", "?"))
            el._name.set_text(s.get("name", ""))
            el._name.set_visible(cfg.get("labels", True))
            set_badge(el, s)
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
            item.connect("activate", lambda it, k=key: it.get_active() and on_pick(k))
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

    def pick_opacity(v: float) -> None:
        cfg["opacity"] = v
        provider.load_from_data(_overlay_css(v))  # live: re-styles in place
        save_cfg()

    add_radios(
        (("h", "Horizontal"), ("v", "Vertical"), ("grid", "Grid")),
        cfg.get("layout", "v"),
        pick_layout,
    )
    menu.append(Gtk.SeparatorMenuItem())
    anchor_items = add_radios(
        (
            ("tl", "Top left"),
            ("tr", "Top right"),
            ("bl", "Bottom left"),
            ("br", "Bottom right"),
            ("free", "Free (drag)"),
        ),
        cfg.get("anchor", "tr"),
        pick_anchor,
    )
    menu.append(Gtk.SeparatorMenuItem())
    add_radios(
        ((0.3, "Transparent"), (0.6, "Medium"), (0.85, "Opaque")),
        cfg.get("opacity", 0.6),
        pick_opacity,
    )
    menu.append(Gtk.SeparatorMenuItem())

    def toggle_labels(item) -> None:
        cfg["labels"] = item.get_active()
        for el in lights.values():
            el._name.set_visible(cfg["labels"])
        win.resize(1, 1)  # shrink-wrap to the new width
        save_cfg()

    labels_item = Gtk.CheckMenuItem(label="Show names")
    labels_item.set_active(cfg.get("labels", True))
    labels_item.connect("toggled", toggle_labels)
    menu.append(labels_item)
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
            win.begin_move_drag(1, int(event.x_root), int(event.y_root), event.time)
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
    if (
        cfg.get("anchor", "tr") == "free"
        and isinstance(cfg.get("pos"), list)
        and len(cfg["pos"]) == 2
    ):
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
        "PreToolUse": [
            {"matcher": "ScheduleWakeup|CronCreate|Workflow", "hooks": [hook]}
        ],
        "PostToolUse": [{"matcher": "Bash", "hooks": [hook]}],
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
    position: relative;
    width: 22px; height: 22px; cursor: pointer;
    border: 1px solid rgba(255,255,255,.15);
    background: radial-gradient(circle at 35% 30%,
      color-mix(in srgb, var(--c) 70%, white) 0%, var(--c) 55%,
      color-mix(in srgb, var(--c) 60%, black) 100%);
    box-shadow: 0 0 8px 1px color-mix(in srgb, var(--c) 60%, transparent),
                inset 0 -2px 4px rgba(0,0,0,.25);
    transition: transform .15s;
  }
  /* per-cwd shape silhouettes (per-corner border-radius), injected below */
SHAPE_CSS
  .light .ltr {
    position: absolute; inset: 0; display: flex;
    align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700; color: rgba(0,0,0,.68);
    text-shadow: 0 1px 1px rgba(255,255,255,.28); pointer-events: none; }
  .light:hover { transform: scale(1.18); }
  .light.green  { --c: var(--green); }
  .light.busy   { --c: var(--busy);   animation: pulse 1.6s ease-in-out infinite; }
  .light.blue   { --c: var(--blue);   animation: pulse 2.2s ease-in-out infinite; }
  .light.mauve  { --c: var(--mauve);  animation: pulse 2.2s ease-in-out infinite; }
  .light.orange { --c: var(--orange); animation: pulse 1.1s ease-in-out infinite; }
  .light.red    { --c: var(--red);    animation: pulse .8s ease-in-out infinite; }
  .light.gray   { --c: var(--gray); }
  .light .badge {
    position: absolute; bottom: -4px; right: -4px; min-width: 13px; height: 13px;
    padding: 0 3px; border-radius: 999px; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); font-size: 9px; font-weight: 700;
    line-height: 11px; text-align: center;
  }
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

async function focusSession(id) {
  try {
    await fetch(ORIGIN + "/api/focus", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
  } catch (e) { /* server gone or not same-machine */ }
}

function render(data) {
  sessions = data.sessions;
  document.getElementById("count").textContent = sessions.length || "";
  const seen = new Set();
  for (const s of sessions) {
    seen.add(s.id);
    let el = nodes.get(s.id);
    if (!el) {
      el = document.createElement("div");
      el.innerHTML = `<span class="ltr">${esc(s.letter)}</span>`;
      el.addEventListener("mouseenter", () => showTip(el, el._s));
      el.addEventListener("click", () => focusSession(el._s.id));
      el.addEventListener("mouseleave", () => tip.style.display = "none");
      nodes.set(s.id, el);
    }
    el._s = s;
    el.className = "light shape-" + s.shape + " " + s.light;
    let badge = el.querySelector(".badge");
    if (s.bg > 0) {
      if (!badge) { badge = document.createElement("span"); badge.className = "badge"; el.appendChild(badge); }
      badge.textContent = s.bg;
    } else if (badge) badge.remove();
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

HTML = HTML.replace(
    "SHAPE_CSS",
    "\n".join(
        f"  .light.shape-{name} {{ border-radius: {r}; }}"
        for name, r in SHAPE_RADIUS.items()
    ),
)


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "overlay"
    if cmd == "hook":
        cmd_hook()
    elif cmd == "focus":
        cmd_focus(args[1] if len(args) > 1 else "")
    elif cmd == "overlay":
        cmd_overlay()
    elif cmd == "serve":
        port = int(args[args.index("--port") + 1]) if "--port" in args else DEFAULT_PORT
        host = args[args.index("--host") + 1] if "--host" in args else "127.0.0.1"
        cmd_serve(port, "--all" in args, host)
    elif cmd == "json":
        print(json.dumps({"sessions": scan_sessions("--all" in args)}))
    elif cmd == "install":
        cmd_install()
    elif cmd == "uninstall":
        cmd_uninstall()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
