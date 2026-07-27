# claudetell

Traffic-light overlay for running Claude Code sessions. One light per session,
hover for details (name, folder, pid, uptime, state). Click a light to jump to
that session's terminal ([Focus](#focus-a-session)).

**Colour** is the state (below). Identity is the name, shape, and a letter:

- **Name** — the GTK overlay shows the full session name beside each light (on
  by default; right-click → *Show names* to toggle). A single letter can't tell
  15 similarly-named sessions apart; the name can.
- **Shape** — shared by all sessions in the same folder (cwd) and cycled so
  distinct folders get distinct shapes. Twelve per-corner-radius silhouettes
  (circle, square, leaf, arch, keystone, D-left/right, teardrop, …) rendered
  identically in the overlay and browser; wraps only past twelve folders.
- **Letter** — the session's initial, centred in the light; the compact identity
  when names are hidden and in the browser view. Full name is always on hover.
- **Context bar** — a thin bar beside each light showing how full that session's
  context window is (green → amber at 70% → red at 90%); exact numbers on hover.
  Right-click → *Show context usage* to toggle. See
  [Context usage](#context-usage) for where the number comes from.

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
corners — the panel re-anchors as lights come and go — or free), names
(stem-stripped session name beside each light, on by default), panel opacity
(transparent / medium / opaque — the frame goes translucent so you can see what's
behind it, lights stay solid) and quit.
Left-drag moves it anywhere and switches position to free. Defaults: vertical,
top right. All choices persist.

Browser version also exists — `uv run claudetell.py serve` →
http://127.0.0.1:7717, with a ⧉ button for a Chrome picture-in-picture
always-on-top window. `--host` and `--port` override the bind address
(default loopback; it has no auth, so keep it on 127.0.0.1 and reach it over an
SSH tunnel — `ssh -L 7717:127.0.0.1:7717` — rather than binding 0.0.0.0).

## Context usage

Claude Code tells you how full a session's context window is in exactly one
place: the JSON it pipes to your **statusline**. The transcript records tokens
used per message but never the window size, and the model id there is always
plain (`claude-opus-4-8`) — never the `[1m]` variant that distinguishes a 1M
model from its 200k twin. Guessing therefore gets it badly wrong: 147k tokens is
74% of a 200k window but 15% of a 1M one.

So `install` puts claudetell *in front of* your statusline. It reads the
payload, records `context_window` to `~/.local/state/claudetell/ctx/<id>.json`,
then runs your original statusline command with the same stdin and prints its
output verbatim. Your statusline is unchanged — claudetell just reads the mail.
The displaced command is stored next to it in `settings.json` as
`_claudetell_wrapped`, and `uninstall` puts it back.

```jsonc
"statusLine": {
  "type": "command",
  "command": "python3 /path/to/claudetell.py statusline",
  "_claudetell_wrapped": "<your original command, verbatim>"
}
```

Every failure in the tee is swallowed and the wrapped command still runs — a
missing bar is cosmetic, a broken statusline is not. If you have no statusline
configured, claudetell does **not** install itself as one; it falls back to the
transcript's token count against a guessed window tier (200k, then 1M), and the
hover text marks those as `(est.)`. Same fallback for a session that hasn't
rendered its statusline yet, and for remote hosts without the tee installed.

## Focus a session

Jump to the terminal a session is running in — its tmux pane is selected and
the terminal window is raised.

- **Click** a light (overlay or browser). Left-drag still moves the overlay; a
  click that doesn't drag focuses.
- **Remote sessions** (over SSH) can't be reached by their remote pane/pid, so
  clicking one jumps to the *local* window hosting its `ssh`. It's matched by the
  ssh host (from the process cmdline) plus the **remote tmux session** the pane
  belongs to (the remote reports it as `tmux_session`; the local ssh window shows
  it in its title, e.g. `host: tmux a -t 0`). Works when many claude sessions
  share one remote tmux session. kitty windows are raised via kitty IPC; if the
  ssh instead runs in a *local* tmux pane, that pane (named after the session) is
  the fallback. Requires the **remote host to run this updated script** so the
  `tmux_session` field crosses the wire.
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
