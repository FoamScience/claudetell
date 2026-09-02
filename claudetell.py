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
  claudetell.py json [--all]          print live sessions + rate limits as JSON (over SSH)
  claudetell.py focus <query>         focus a session's window/pane (bind to a key)
  claudetell.py hook                  hook entrypoint (stdin JSON from Claude Code)
  claudetell.py install               register hooks
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
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime
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
HUD_CTX_DIR = CLAUDE_DIR / "plugins" / "claude-hud" / "context-cache"
LIMITS_STALE = 3600  # no cswap measurement this long → the numbers are guesswork
CSWAP_DIR = (
    Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    / "claude-swap"
)
# Fallback only, for sessions claude-hud hasn't cached yet (or hosts running
# without it): the transcript states tokens used but never the window size, so
# guess the smallest tier that fits. A 1M session reads against 200k until it
# grows past it — that's what hud's cache spares us.
CONTEXT_TIERS = (200_000, 1_000_000)


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


def tmux_session_name(sock: str | None, pane: str | None) -> str | None:
    """tmux session name owning a pane (e.g. '0', 'aporia'). For a remote
    session this rides across the wire so a local `ssh … tmux a -t <name>`
    window can be matched to it — many claude sessions can share one tmux
    session, and the remote session *name* never appears in the local ssh."""
    if not pane:
        return None
    base = ["tmux"] + (["-S", sock] if sock else [])
    try:
        out = subprocess.run(
            base + ["display-message", "-p", "-t", pane, "#{session_name}"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


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


def _kitty_sockets() -> list[str]:
    """Every kitty remote-control socket (own one first). A session can live in
    any kitty instance, so callers must check them all, not just KITTY_LISTEN_ON."""
    return list(
        dict.fromkeys(
            ([os.environ["KITTY_LISTEN_ON"]] if os.environ.get("KITTY_LISTEN_ON") else [])
            + ["unix:" + p for p in glob.glob("/tmp/kitty-*") if not p.endswith(".lock")]
        )
    )


def _kitty_windows() -> list[dict]:
    """All kitty windows across all sockets: {sock, id, title, cmd} where cmd is
    the joined foreground-process cmdlines (holds the ssh target) and title holds
    what the remote set (e.g. 'host: tmux a -t 0')."""
    wins = []
    for sock in _kitty_sockets():
        try:
            data = json.loads(subprocess.run(
                ["kitten", "@", "--to", sock, "ls"],
                capture_output=True, text=True, timeout=2).stdout)
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            continue
        for osw in data:
            for tab in osw.get("tabs", []):
                for w in tab.get("windows", []):
                    cmd = " ".join(
                        " ".join(p.get("cmdline", []))
                        for p in w.get("foreground_processes", []))
                    title = f"{w.get('title') or ''} {tab.get('title') or ''}"
                    wins.append({"sock": sock, "id": w.get("id"),
                                 "title": title, "cmd": cmd})
    return wins


def _remote_ssh_target(host_name: str) -> str | None:
    for r in load_remotes():
        if (r.get("name") or r.get("ssh")) == host_name:
            return r.get("ssh")
    return None


def _find_ssh_kitty_window(host: str | None, tmux_session: str | None,
                           name: str | None) -> dict | None:
    """Local kitty window hosting a remote session's ssh. Narrow by ssh host in
    the cmdline, then pick the window whose title shows `tmux … -t <session>`
    (the reliable key — the claude session name isn't in the local ssh). Falls
    back to the session name in the title, then the sole ssh window to that host."""
    wins = _kitty_windows()
    cands = [w for w in wins if host and host in w["cmd"]]
    if not cands:  # ssh alias not in cmdline → last resort, host string in title
        cands = [w for w in wins if host and host in w["title"]]
    if tmux_session:
        pat = re.compile(r"-t\s+" + re.escape(tmux_session) + r"(?:\s|$)")
        hit = [w for w in cands if pat.search(w["title"])]
        if hit:
            return hit[0]
    if name:
        hit = [w for w in cands if name.lower() in w["title"].lower()]
        if hit:
            return hit[0]
    return cands[0] if len(cands) == 1 else None


def _kitty_focus(pids: set[int]) -> bool:
    """Focus the kitty window whose process tree contains one of pids, via
    kitty's remote-control IPC (works on Wayland, unlike xdotool)."""
    for sock in _kitty_sockets():
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


def _local_pane_by_name(name: str) -> str | None:
    """Local tmux pane whose window name or title matches a (remote) session
    name — the handle for jumping to a session that lives over SSH but is open
    in a local `ssh` pane. Searches the default tmux server only.
    ponytail: default socket covers the usual setup; add -S scanning if needed."""
    q = (name or "").strip().lower()
    if not q:
        return None
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{window_name}\t#{pane_title}"],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows.append((parts[0], parts[1].lower(), parts[2].lower()))
    for pid_, wn, pt in rows:  # exact window/title match wins
        if q == wn or q == pt:
            return pid_
    for pid_, wn, pt in rows:  # then a substring match
        if q in wn or q in pt:
            return pid_
    return None


def _remote_select_window(entry: dict) -> None:
    """SSH to the host and switch its tmux to the window/pane hosting this
    session. Raising the local ssh window alone lands on whatever window that
    remote tmux session last showed — so sessions sharing one tmux session (each
    in its own window) all resolve to the same kitty window and never switch.
    This selects the right one by its remote pane id."""
    pane = entry.get("pane")
    remote = next(
        (r for r in load_remotes()
         if (r.get("name") or r.get("ssh")) == entry.get("host", "")), None)
    target = remote and remote.get("ssh")
    if not (pane and target):
        return
    opts = remote.get("ssh_opts") or ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    sock = entry.get("tmux_sock")
    tmux = "tmux" + (f" -S {shlex.quote(sock)}" if sock else "")
    p = shlex.quote(pane)
    rcmd = f"{tmux} select-window -t {p} && {tmux} select-pane -t {p}"
    try:
        subprocess.run(
            ["ssh", *opts, target, rcmd], timeout=6, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def focus_session(entry: dict) -> None:
    """Focus the tmux pane + terminal GUI window hosting a session. Local
    sessions use their own pane/pid; a remote session (over SSH) is matched to
    the LOCAL tmux pane that hosts it by window/pane name. tmux switches the
    pane; kitty IPC (or xdotool) raises the window."""
    if entry.get("remote"):
        # remote pane/pid live on another machine. The session is open locally
        # in an `ssh … tmux a -t <sess>` window; find that kitty window (ssh host
        # from the cmdline, remote tmux session from the title) and raise it.
        target = _remote_ssh_target(entry.get("host", ""))
        host = target.split("@")[-1] if target else None
        win = _find_ssh_kitty_window(host, entry.get("tmux_session"), entry.get("name"))
        if win:
            _remote_select_window(entry)  # switch remote tmux to the exact window
            try:
                subprocess.run(
                    ["kitten", "@", "--to", win["sock"], "focus-window",
                     "--match", f"id:{win['id']}"],
                    timeout=2, check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                pass
            return
        # fallback: the ssh runs inside a LOCAL tmux pane named after the session
        pane, sock, local_pid = _local_pane_by_name(entry.get("name") or ""), None, 0
        if not pane:
            return
    else:
        pane, sock, local_pid = (
            entry.get("pane"), entry.get("tmux_sock"), entry.get("pid", 0))
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
    if local_pid:
        pids += _ancestor_chain(local_pid)
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


_ctx_cache: dict[str, tuple[float, int, int]] = {}  # sid -> (mtime, size, tokens)


def context_used(session_id: str) -> int:
    """Tokens in the session's context = the last main-chain assistant message's
    prompt size (input + both cache buckets — they're disjoint slices of the same
    prompt). Sidechain messages are a subagent's own window, not this one's."""
    path = find_transcript(session_id)
    if not path:
        return 0
    try:
        stat = path.stat()
        cached = _ctx_cache.get(session_id)
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2]
        with open(path, "rb") as f:
            # a wider tail than transcript_error's: one fat tool result can sit
            # between the end of the file and the last assistant message
            f.seek(max(0, stat.st_size - 262144))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return 0

    used = 0
    for line in reversed(tail.splitlines()):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        usage = (entry.get("message") or {}).get("usage") or {}
        used = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        if used:
            break
    _ctx_cache[session_id] = (stat.st_mtime, stat.st_size, used)
    return used


def context_limit(used: int) -> int:
    """Smallest window tier that fits what the session used (see CONTEXT_TIERS)."""
    return next((t for t in CONTEXT_TIERS if used <= t), CONTEXT_TIERS[-1])


def session_context(session_id: str) -> tuple[int, int, float | None]:
    """(used, limit, pct) for a session. claude-hud's cached reading is exact and
    wins; without it we fall back to the transcript plus a guessed tier."""
    rec = read_context(session_id) or {}
    if rec.get("used") and rec.get("pct") is not None:
        return rec["used"], rec.get("limit", 0), rec["pct"]
    used = context_used(session_id)
    # a record with only the window size still beats guessing the tier
    limit = rec.get("limit") or 0
    if limit:
        return used, limit, 100.0 * used / limit
    return used, context_limit(used), None


def ctx_fraction(s: dict, override: int | None = None) -> float | None:
    """How full a session's context window is, or None when unknown (no
    transcript yet, or a session that hasn't had its first turn)."""
    if not override and s.get("ctx_pct") is not None:
        return min(1.0, s["ctx_pct"] / 100)
    used = s.get("ctx") or 0
    if not used:
        return None
    limit = override or s.get("ctx_limit") or context_limit(used)
    return min(1.0, used / limit)


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
        used, limit, pct = session_context(sid)
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
            "tmux_session": tmux_session_name(tmux_sock, pane),
            "kind": data.get("kind", ""),
            "version": data.get("version", ""),
            "startedAt": data.get("startedAt", 0),
            "updatedAt": data.get("statusUpdatedAt") or data.get("updatedAt", 0),
            "status": status or "unknown",
            "light": light,
            "detail": detail,
            "ctx": used,
            "ctx_limit": limit,
            "ctx_pct": pct,
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


def fetch_remote(remote: dict, src: str) -> dict:
    """SSH to a host and run claudetell's json scan there; tag its sessions and
    rate-limit accounts with the host. The remote computes its own lights (it owns the /proc + transcripts
    + hook state), so by default we just pipe THIS script to its python3 — no
    install or version-sync needed. Set "cmd" to run an installed copy instead."""
    name = remote.get("name") or remote.get("ssh") or "remote"
    target = remote.get("ssh")
    if not target:
        return {"sessions": [], "limits": []}
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
        return {"sessions": [], "limits": []}
    sessions = data.get("sessions", []) if isinstance(data, dict) else data
    limits = data.get("limits", []) if isinstance(data, dict) else []
    if not isinstance(sessions, list):
        sessions = []
    if not isinstance(limits, list):
        limits = []
    for s in sessions:
        s["remote"] = True
        s["host"] = name
        s["id"] = f"{name}:{s.get('id', '')}"  # namespace so ids never collide
    for a in limits:
        a["remote"] = True
        a["host"] = name
        a["id"] = f"{name}:{a.get('id', '')}"
        a["label"] = f"{name}:{a.get('label', '')}".rstrip(":")
    return {"sessions": sessions, "limits": limits}


# -- server -----------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    show_all = False

    def do_GET(self) -> None:
        if self.path.startswith("/api/sessions"):
            body = json.dumps(
                {
                    "now": time.time() * 1000,
                    "sessions": scan_sessions(self.show_all),
                    "limits": read_limits(),
                }
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
TRAY_ICONS = STATE_DIR / "icons"
# worst-first: the tray dot shows the single most urgent session
LIGHT_PRIORITY = ("red", "orange", "busy", "mauve", "blue", "green", "gray")


def tray_icon(light: str) -> str:
    """Path to the tray dot for a light colour. SNI's icon name may be an
    absolute path (gnome-shell's appindicator and KDE both take one), which
    saves installing an icon theme."""
    p = TRAY_ICONS / f"{light}.svg"
    if not p.exists():
        TRAY_ICONS.mkdir(parents=True, exist_ok=True)
        r, g, b = LIGHT_RGB[light]
        p.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22">'
            f'<circle cx="11" cy="11" r="8" fill="#{r:02x}{g:02x}{b:02x}"/></svg>'
        )
    return str(p)


def _overlay_css(alpha: float = 0.6) -> bytes:
    """GTK CSS for the whole overlay — avoids cairo (python3-gi-cairo often absent).
    alpha = panel background opacity (lights stay solid so you can see through the
    frame to what's behind it)."""
    rules = [
        f"window.claudetell {{ background: rgba(30,30,46,{alpha:.2f});"
        " border-radius: 13px; border: 1px solid rgba(69,71,90,0.9); }",
        ".light { border-radius: 999px; border: 1px solid rgba(255,255,255,0.18); }",
        # group divider between local and each remote host (see make_separator)
        "separator.ctsep { background-color: rgba(255,255,255,0.18);"
        " min-width: 1px; min-height: 1px; margin: 3px; }",
        # per-cwd shape = per-corner border-radius silhouette (see SHAPE_RADIUS).
        *(
            f".light.shape-{name} {{ border-radius: {r}; }}"
            for name, r in SHAPE_RADIUS.items()
        ),
        ".ltr { color: rgba(0,0,0,0.72); font-weight: 700; font-size: 10px;"
        " text-shadow: 0 1px 1px rgba(255,255,255,0.25); }",
        ".name { color: #cdd6f4; font-size: 11px; }",
        ".ulbl { color: #a6adc8; font-size: 9px; font-weight: 700; }",
        ".ulbl.cur { color: #a6e3a1; }",  # the account you're on, here
        ".ulbl.curem { color: #89b4fa; }",  # ...on a remote host
        # context-usage bar: GTK3 progressbars style through their trough/progress
        # sub-nodes, so min-height has to land on both or the bar keeps its default.
        "progressbar.ctx trough { min-height: 4px; min-width: 26px;"
        " background-color: rgba(255,255,255,0.14); border: none; border-radius: 999px; }",
        "progressbar.ctx progress { min-height: 4px; border: none;"
        " border-radius: 999px; background-color: #4ade80; }",
        "progressbar.ctx.warn progress { background-color: #fbbf24; }",
        "progressbar.ctx.full progress { background-color: #f87171; }",
        # an account cswap isn't currently on: same colours, quieter
        "progressbar.ctx.idle progress { opacity: 0.45; }",
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
        from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Pango
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
        homogeneous=False,  # a spanning separator can't share a 24px light cell
    )
    # order lights + group separators by an _order int stamped in refresh()
    flow.set_sort_func(
        lambda a, b: getattr(a.get_child(), "_order", 0)
        - getattr(b.get_child(), "_order", 0)
    )
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
    # rate-limit bars, above the lights: one row per account per window (cswap
    # knows every account, not only the one you're on — see read_limits). Same
    # look as the per-session context bars.
    usage_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    usage_box.set_no_show_all(True)  # visibility is driven by data + cfg["usage"]
    usage_rows: dict[str, Gtk.Box] = {}
    box.pack_start(usage_box, False, False, 0)
    box.pack_start(flow, True, True, 0)
    win.add(box)

    WINDOWS = (("five_hour", "5h", "5 hour"), ("seven_day", "7d", "7 day"))

    def usage_tooltip(row, _x, _y, _kb, tip):
        acct = row._acct
        who = acct["email"] or "this account"
        if acct.get("host"):
            who = f"{acct['host']}: {who}"
        head = f"<b>{GLib.markup_escape_text(who)}</b>"
        if not acct["active"] and acct["email"]:
            head += " <i>(not active)</i>"
        lines = [head]
        for window, _short, long in WINDOWS:
            w = acct.get(window)
            if not w:
                continue
            line = f"{long} limit {w['pct']:.0f}% used"
            if w.get("resets_at"):
                left = max(0, w["resets_at"] - time.time())
                line += f" · resets in {left // 3600:.0f}h {left % 3600 // 60:.0f}m"
            lines.append(line)
        tip.set_markup("\n".join(lines))
        return True

    def usage_row(key: str) -> Gtk.Box:
        """One account: its number, then a labelled bar per window."""
        row = usage_rows.get(key)
        if row is None:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            num = Gtk.Label(xalign=0)
            num.get_style_context().add_class("ulbl")
            num.set_no_show_all(True)  # nothing to disambiguate on one account
            row.pack_start(num, False, False, 0)
            row._num, row._bars = num, {}
            for window, short, _long in WINDOWS:
                lbl = Gtk.Label(label=short, xalign=0)
                lbl.get_style_context().add_class("ulbl")
                bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER)
                bar.get_style_context().add_class("ctx")
                cell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
                cell.pack_start(lbl, False, False, 0)
                cell.pack_start(bar, True, True, 0)
                row.pack_start(cell, True, True, 0)
                row._bars[window] = (cell, bar)
            row.set_has_tooltip(True)
            row.connect("query-tooltip", usage_tooltip)
            usage_box.pack_start(row, False, False, 0)
            row.show_all()  # per-widget visibility is set below, not by show_all
            usage_rows[key] = row
        return row

    def dedup_accounts(accounts: list[dict]) -> list[dict]:
        """One row per account, not per host: the same login polled on two
        machines is one set of windows. Keyed on email (the only cross-host
        identity — the account *number* is per-machine), newest reading wins,
        and the key doubles as the row id so a swap can't churn the rows."""
        by_email: dict[str, dict] = {}
        out = []
        for a in accounts:
            a = dict(a, active_host=a.get("host") if a["active"] else None)
            email = a.get("email")
            if not email:
                out.append(a)
                continue
            a["id"] = email
            prev = by_email.get(email)
            if prev is None:
                by_email[email] = a
                out.append(a)
                continue
            # active anywhere → active: it's the same account either way, and
            # the machine you're looking at isn't necessarily the one on it.
            active = prev["active"] or a["active"]
            # local wins the "where" — that's the machine you're looking at
            hosts = [x["active_host"] for x in (prev, a) if x["active"]]
            host = None if not hosts or None in hosts else hosts[0]
            if a["ts"] > prev["ts"]:
                out[out.index(prev)] = a
                by_email[email] = a
                prev = a
            prev["active"] = active
            prev["active_host"] = host if active else None
        return out

    def set_usage() -> None:
        accounts = dedup_accounts(
            read_limits() + remote_cache["limits"] if cfg.get("usage", True) else []
        )
        seen = set()
        for pos, acct in enumerate(accounts):
            seen.add(acct["id"])
            row = usage_row(acct["id"])
            row._acct = acct
            row._num.set_text(f"#{acct['label']}")
            num_style = row._num.get_style_context()
            for name, on in (
                ("cur", acct["active"] and not acct["active_host"]),
                ("curem", bool(acct["active_host"])),  # active on a remote host
            ):
                (num_style.add_class if on else num_style.remove_class)(name)
            row._num.set_visible(len(accounts) > 1 and bool(acct["label"]))
            for window, (cell, bar) in row._bars.items():
                w = acct.get(window)
                cell.set_visible(bool(w))
                if not w:
                    continue
                frac = min(1.0, max(0.0, w["pct"] / 100.0))
                bar.set_fraction(frac)
                style = bar.get_style_context()
                for name in ("warn", "full", "idle"):
                    style.remove_class(name)
                if frac >= 0.9:
                    style.add_class("full")
                elif frac >= 0.7:
                    style.add_class("warn")
                if not acct["active"]:
                    style.add_class("idle")  # dimmed: an account you're not on
            usage_box.reorder_child(row, pos)
            row.show()
        for key in list(usage_rows):
            if key not in seen:
                usage_rows.pop(key).destroy()
        usage_box.set_visible(bool(seen))

    lights: dict[str, Gtk.Widget] = {}
    seps: list[Gtk.Widget] = []
    NO_GROUP = object()  # sentinel: distinct from any host (incl. local's None)
    state = {"empty": None}

    def make_separator() -> Gtk.Widget:
        # perpendicular to the flow: a rule across the column in vertical layout,
        # a divider between lights in horizontal. ponytail: grid gets the vertical
        # one too — a full-width break can't span flowbox cells.
        vertical = cfg.get("layout", "v") == "v"
        sep = Gtk.Separator(
            orientation=Gtk.Orientation.HORIZONTAL
            if vertical
            else Gtk.Orientation.VERTICAL
        )
        sep.set_hexpand(vertical)
        sep.set_vexpand(not vertical)
        sep.get_style_context().add_class("ctsep")
        return sep

    # poll configured remote hosts over SSH off the GTK thread; refresh() merges
    # the latest cached result. One writer, one reader, atomic list swap → no lock.
    remote_cache = {"sessions": [], "limits": []}
    remotes = load_remotes()
    if remotes:
        src = Path(__file__).read_text()

        def poll_remotes() -> None:
            while True:
                merged, accts = [], []
                for r in remotes:
                    got = fetch_remote(r, src)
                    merged += got["sessions"]
                    accts += got["limits"]
                remote_cache["sessions"] = merged
                remote_cache["limits"] = accts
                time.sleep(REMOTE_INTERVAL)

        threading.Thread(target=poll_remotes, daemon=True).start()

    tray_items: list[Gtk.Widget] = []

    def tray_row(s: dict) -> Gtk.MenuItem:
        who = f"{s['host']}: {s['name']}" if s.get("host") else s["name"]
        pct = s.get("ctx_pct")
        label = f"{who} — {s['status']}" + (f" · {pct}%" if pct else "")
        item = Gtk.ImageMenuItem(label=label)
        # dbusmenu carries the pixbuf, so the light's colour survives into the tray
        item.set_image(Gtk.Image.new_from_pixbuf(
            GdkPixbuf.Pixbuf.new_from_file_at_size(tray_icon(s["light"]), 16, 16)))
        item.set_always_show_image(True)
        item.connect("activate", lambda _i, e=s: focus_session(e))
        return item

    def set_tray(sessions: list[dict]) -> None:
        if not indicator:
            return
        worst = next(
            (c for c in LIGHT_PRIORITY if any(s["light"] == c for s in sessions)),
            "gray",
        )
        if worst != state.get("tray"):
            state["tray"] = worst
            indicator.set_icon_full(tray_icon(worst), worst)
        waiting = sum(1 for s in sessions if s["light"] in ("orange", "red"))
        indicator.set_label(str(waiting) if waiting else "", "99")
        # session rows on top of the menu; rebuilt only when one visibly changes
        sig = [(s["id"], s["light"], s["name"], s["status"], s.get("ctx_pct"))
               for s in sessions]
        if sig == state.get("tray_sig"):
            return
        state["tray_sig"] = sig
        for it in tray_items:
            it.destroy()
        tray_items.clear()
        rows = [tray_row(s) for s in sessions]
        if not rows:
            empty = Gtk.MenuItem(label="no sessions")
            empty.set_sensitive(False)
            rows = [empty]
        rows.append(Gtk.SeparatorMenuItem())
        for i, item in enumerate(rows):
            menu.insert(item, i)
            tray_items.append(item)
        menu.show_all()

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
        total = max(1, len(flow.get_children()))  # lights + group separators
        per_line = {"h": total, "v": 1, "grid": min(4, n)}.get(name, n)
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

    def set_ctx(ebox, s: dict) -> None:
        frac = ctx_fraction(s, cfg.get("context_limit"))
        if frac is None or not cfg.get("context", True):
            ebox._ctx.hide()
            return
        ebox._ctx.set_fraction(frac)
        style = ebox._ctx.get_style_context()
        for name in ("warn", "full"):
            style.remove_class(name)
        if frac >= 0.9:
            style.add_class("full")
        elif frac >= 0.7:
            style.add_class("warn")
        ebox._ctx.show()

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
        # context-usage bar: how full the session's window is. Sits after the
        # name so it stays put whether or not labels are shown.
        ctx = Gtk.ProgressBar(valign=Gtk.Align.CENTER)
        ctx.get_style_context().add_class("ctx")
        ctx.set_no_show_all(True)  # hidden until the session has a usage number
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        row.pack_start(overlay, False, False, 0)
        row.pack_start(name, False, False, 0)
        row.pack_start(ctx, False, False, 0)
        ebox = Gtk.EventBox(visible_window=False)
        ebox.add(row)
        ebox.set_has_tooltip(True)
        ebox._s = s
        ebox._dot = dot
        ebox._letter = letter
        ebox._name = name
        ebox._badge = badge
        ebox._ctx = ctx

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
            frac = ctx_fraction(sess, cfg.get("context_limit"))
            if frac is not None:
                limit = cfg.get("context_limit") or sess.get("ctx_limit", 0)
                size = f" / {limit / 1000:.0f}k" if limit else ""
                guess = "" if sess.get("ctx_pct") is not None else " (est.)"
                lines.append(
                    f"context {frac * 100:.0f}%{guess} · {sess['ctx'] / 1000:.0f}k{size}"
                )
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

    def regroup(sessions: list) -> None:
        # rebuild group separators and stamp the flow sort order: local first,
        # then each remote host, a divider whenever the host changes.
        for sep in seps:
            p = sep.get_parent()
            if p:
                p.destroy()
        seps.clear()
        order = 0
        prev = NO_GROUP
        for s in sessions:
            el = lights.get(s["id"])
            if el is None:
                continue
            grp = s.get("host")  # None → local
            if prev is not NO_GROUP and grp != prev:
                sep = make_separator()
                sep._order = order
                order += 1
                flow.add(sep)
                seps.append(sep)
            el._order = order
            order += 1
            prev = grp
        flow.invalidate_sort()

    def refresh() -> bool:
        sessions = scan_sessions() + remote_cache["sessions"]
        state["sessions"] = sessions
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
            set_ctx(el, s)
        for sid in list(lights):
            if sid not in seen:
                lights.pop(sid).get_parent().destroy()
                changed = True
        set_empty()
        set_usage()
        if changed:
            regroup(sessions)
            apply_layout(cfg.get("layout", "v"))
        flow.show_all()
        set_tray(sessions)
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
        cfg["layout"] = k  # make_separator reads this for divider orientation
        regroup(state.get("sessions", []))
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

    def toggle_ctx(item) -> None:
        cfg["context"] = item.get_active()
        for el in lights.values():
            set_ctx(el, el._s)
        win.resize(1, 1)
        save_cfg()

    ctx_item = Gtk.CheckMenuItem(label="Show context usage")
    ctx_item.set_active(cfg.get("context", True))
    ctx_item.connect("toggled", toggle_ctx)
    menu.append(ctx_item)

    def toggle_usage(item) -> None:
        cfg["usage"] = item.get_active()
        set_usage()
        win.resize(1, 1)
        save_cfg()

    usage_item = Gtk.CheckMenuItem(label="Show usage limits")
    usage_item.set_active(cfg.get("usage", True))
    usage_item.connect("toggled", toggle_usage)
    menu.append(usage_item)
    menu.append(Gtk.SeparatorMenuItem())
    quit_item = Gtk.MenuItem(label="Quit claudetell")
    quit_item.connect("activate", Gtk.main_quit)
    menu.append(quit_item)

    # tray icon (StatusNotifierItem) — the overlay's menu, plus one dot for the
    # worst live session, so claudetell can run without the overlay on screen.
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3

        indicator = AppIndicator3.Indicator.new(
            "claudetell", tray_icon("gray"),
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
    except (ImportError, ValueError):
        indicator = None  # no typelib (apt install gir1.2-appindicator3-0.1)

    if indicator:
        def toggle_overlay(item) -> None:
            cfg["overlay"] = item.get_active()
            win.set_visible(cfg["overlay"])
            save_cfg()

        overlay_item = Gtk.CheckMenuItem(label="Show overlay")
        overlay_item.set_active(cfg.get("overlay", False))
        overlay_item.connect("toggled", toggle_overlay)
        menu.insert(overlay_item, 0)
        menu.insert(Gtk.SeparatorMenuItem(), 1)

    menu.show_all()
    if indicator:
        indicator.set_menu(menu)

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
    if indicator and not cfg.get("overlay", False):
        win.hide()
    try:
        Gtk.main()
    finally:
        save_cfg()


# -- context usage ----------------------------------------------------------


def read_context(session_id: str) -> dict | None:
    """This session's context window, as claude-hud cached it.

    Claude Code states the window *size* in one place only: the JSON it pipes to
    the statusline. Session files don't carry it, and the transcript's model id
    never has the [1m] suffix that tells a 1M model from its 200k twin. claude-hud
    owns the statusline and writes each session's reading to disk, keyed by a
    sha256 of the transcript path — so we read its cache instead of sitting in
    front of it. A tee is not an option: Claude Code rewrites settings.json and
    drops the key the displaced command was kept in, leaving a statusline that
    renders nothing."""
    path = find_transcript(session_id)
    if not path:
        return None
    key = hashlib.sha256(os.path.abspath(path).encode()).hexdigest()
    try:
        rec = json.loads((HUD_CTX_DIR / f"{key}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    usage = rec.get("current_usage") or {}
    used = (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
    )
    return {
        "used": used,
        "limit": rec.get("context_window_size") or 0,
        # hud caches Claude Code's own percentage, which is authoritative: it
        # knows what it reserves for the output budget, our ratio doesn't.
        "pct": rec.get("used_percentage"),
        "ts": (rec.get("saved_at") or 0) / 1000,
    }


def _cswap_accounts() -> list[dict]:
    """Every account claude-swap knows, newest measurement each. It polls each
    account's usage directly, so a swap moves the bars at once, no session can
    report the window of an account that is no longer active, and the accounts
    you are *not* on are worth seeing — that's where the headroom is."""
    try:
        table = json.loads((CSWAP_DIR / "cache/usage.json").read_text())["accounts"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return []
    try:
        active = str(
            json.loads((CSWAP_DIR / "sequence.json").read_text())["activeAccountNumber"]
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        active = None
    out = []
    for num, acct in sorted(table.items()):
        rec = {
            "id": num,
            "label": num,
            "email": acct.get("email", ""),
            "active": num == active,
            "ts": acct.get("fetchedAt", 0),
        }
        good = acct.get("lastGood") or {}
        for key in ("five_hour", "seven_day"):
            w = good.get(key) or {}
            if w.get("pct") is None:
                continue
            try:
                resets = datetime.fromisoformat(w["resets_at"]).timestamp()
            except (KeyError, TypeError, ValueError):
                resets = None
            rec[key] = {"pct": w["pct"], "resets_at": resets}
        if "five_hour" in rec or "seven_day" in rec:
            out.append(rec)
    return out


def read_limits() -> list[dict]:
    """Rate-limit windows to draw, one entry per account. cswap polls them per
    account; nothing else on disk carries them (Claude Code states them in the
    statusline payload alone), so without cswap there are no bars to draw."""
    return [a for a in _cswap_accounts() if time.time() - a["ts"] <= LIMITS_STALE]


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
    ${s.ctx ? `<div class="row"><span>context</span><span>${Math.round(s.ctx_pct ?? 100 * s.ctx / s.ctx_limit)}%${s.ctx_pct == null ? " (est.)" : ""} · ${Math.round(s.ctx / 1000)}k${s.ctx_limit ? ` / ${Math.round(s.ctx_limit / 1000)}k` : ""}</span></div>` : ""}
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
        print(
            json.dumps(
                {"sessions": scan_sessions("--all" in args), "limits": read_limits()}
            )
        )
    elif cmd == "install":
        cmd_install()
    elif cmd == "uninstall":
        cmd_uninstall()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
