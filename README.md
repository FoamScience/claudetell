# claudetell

Traffic-light overlay for running Claude Code sessions. One light per session,
hover for details (name, folder, pid, uptime, state).

| light | meaning |
|---|---|
| 🟢 green | idle — waiting for your next prompt |
| 🟡 amber (pulsing) | busy — working |
| 🔵 blue | agents or a live background shell running (count shown as a badge) |
| 🟣 mauve | loop scheduled (`/loop`, ScheduleWakeup, cron) while otherwise idle |
| 🟠 orange | waiting for your input (permission / ask menu / btw) |
| 🔴 red (pulsing) | error — usage limit, API error |
| ⚪ gray | unknown state |

## Setup

```sh
uv run claudetell.py install    # registers hooks in ~/.claude/settings.json (backed up)
uv run claudetell.py            # native always-on-top overlay (or just ./claudetell.py)
```

Restart running claude sessions so they pick up the hooks. Sessions started
before that still get green/amber/orange/red (from session files + transcript),
just not blue/mauve.

## Overlay

Native frameless GTK3 window: always-on-top, on all workspaces, no taskbar
entry, single instance. Runs via XWayland (`GDK_BACKEND=x11`) because GNOME
Wayland doesn't let clients request keep-above. Hover a light for details;
right-click for layout (horizontal / vertical / grid), position (screen
corners — the panel re-anchors as lights come and go — or free) and quit.
Left-drag moves it anywhere and switches position to free. Defaults: vertical,
top right. All choices persist.

Browser version also exists — `uv run claudetell.py serve` →
http://127.0.0.1:7717, with a ⧉ button for a Chrome picture-in-picture
always-on-top window.

PEP 723 script, zero dependencies. `[tool.uv] python-preference = "only-system"`
keeps the script on the distro python whose ABI matches the distro-packaged
PyGObject (`gi` isn't wheel-installable). Hooks are registered as plain
`python3 … hook` — stdlib-only and called on every tool use, so they skip the
uv indirection.

**This does not consume any API tokens**

## How it works

- Base state from `~/.claude/sessions/{pid}.json` (`busy|idle|waiting`, written
  by Claude Code itself; dead pids filtered).
- Hooks (`PreToolUse ScheduleWakeup|CronCreate|Workflow`, `PostToolUse Bash`,
  `SubagentStart/Stop`, `StopFailure`, `UserPromptSubmit`, `Stop`, `SessionEnd`)
  write per-session state to `~/.local/state/claudetell/`.
- No light outlives its cause: a background shell is pid-tracked (blue clears
  the instant it exits, not on a timer), red self-heals once the session reports
  a newer status, and mauve yields to real work. Running agents/shells/workflows
  show as a count badge; `Workflow` has no completion hook, so its badge (never
  its color) self-heals on a TTL. Re-run `install` after upgrading for the hooks.
- Red also falls back to scanning the transcript tail for API-error entries,
  so it works without hooks; it clears itself on the next turn.
- No dependencies, stdlib only. Server binds 127.0.0.1.

`python3 claudetell.py uninstall` removes the hooks again.
