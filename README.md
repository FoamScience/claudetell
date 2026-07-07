# claudetell

Traffic-light overlay for running Claude Code sessions. One light per session,
hover for details (name, folder, pid, uptime, state). Click a light to jump to
that session's terminal ([Focus](#focus-a-session)).

**Colour** is the state (below). **Shape + letter** is the identity, so you can
tell sessions apart at a glance and keep track of which is which:

- **Letter** — the session's initial (folder name), centred in the light. This
  is the stable identity, identical in the overlay and the browser view.
- **Shape** — a per-project bucket derived from the folder path, so a project
  always looks the same. The browser draws six (circle, square, triangle,
  diamond, pentagon, hexagon); the GTK overlay has no polygons in CSS, so it
  buckets those into three rounded forms — the letter carries the fine detail.

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
always-on-top window. `--host` and `--port` override the bind address
(default loopback; it has no auth, so keep it on 127.0.0.1 and reach it over an
SSH tunnel — `ssh -L 7717:127.0.0.1:7717` — rather than binding 0.0.0.0).

## Focus a session

Jump to the terminal a session is running in — its tmux pane is selected and
the terminal window is raised.

- **Click** a light (overlay or browser). Left-drag still moves the overlay; a
  click that doesn't drag focuses.
- **Keyboard shortcut** — `claudetell.py focus <query>`, where `<query>` matches
  a session's letter, name, or folder substring (first live match wins):

  ```sh
  uv run claudetell.py focus C            # by letter
  uv run claudetell.py focus centripump   # by name / folder
  ```

  Wayland won't let an app grab a global hotkey (the compositor owns them), so
  bind the command yourself: **GNOME Settings → Keyboard → Keyboard Shortcuts →
  Custom Shortcuts**, command `python3 /path/to/claudetell.py focus C`, and pick
  a key. Because letters are stable, `Super+C` → the centripump session reads
  naturally. (Other desktops: use their custom-shortcut tool the same way.)

How it raises the window, in order:

1. **tmux** — `select-window` + `select-pane` on the pane (captured from the
   session process's `TMUX_PANE`). This alone is enough when that terminal is
   already visible.
2. **kitty** — via kitty's remote-control IPC (`kitten @ focus-window`), which
   works on Wayland. Requires kitty remote control enabled
   (`allow_remote_control yes` + a `listen_on` socket in `kitty.conf`).
3. **xdotool** — X11 fallback for other terminals; a no-op on Wayland.

Same-machine only — the overlay/server must run where the sessions and display
are (so it's a no-op reaching a `serve` instance over an SSH tunnel).

**GNOME Wayland caveat.** Mutter's focus-stealing prevention refuses to raise a
window that isn't already focused: the tmux window/pane still switches
underneath, but you get a "window is ready" notification instead of the terminal
coming forward. It bites hardest when the target lives in a *different* kitty
instance than the one you're looking at. There is no code or `gsettings` fix (no
activation token is available to hand the terminal) — the raise must be unblocked
on the GNOME side. Options:

- **Install a "steal focus" extension** (the real fix). It turns the
  window-demands-attention signal into an immediate raise. A GNOME 45–46+ one:
  [steal-my-focus-window](https://github.com/v-dimitrov/gnome-shell-extension-stealmyfocus)
  — clone into `~/.local/share/gnome-shell/extensions/` under its exact uuid
  folder `steal-my-focus-window@steal-my-focus-window`, log out and back in
  (Wayland needs a full session restart to load a new extension), then
  `gnome-extensions enable steal-my-focus-window@steal-my-focus-window`.
- **Click the "is ready" notification** — it raises the window, zero install.

Clicking works for every session state (idle, busy, error, …) and switches the
tmux window/pane regardless; only the GUI raise is subject to this policy.

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
