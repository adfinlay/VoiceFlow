# Linux control socket

VoiceFlow can be driven from a compositor keybind (i3, Sway, Hyprland, etc.)
without going through its built-in global hotkey listener. This lets you
drop your `input` group membership: VoiceFlow no longer needs direct access
to `/dev/input/event*`.

## How it works

When running on Linux, VoiceFlow listens on a Unix domain socket at:

```
$XDG_RUNTIME_DIR/voiceflow/control.sock
```

(typically `/run/user/<UID>/voiceflow/control.sock`).

Socket permissions: parent directory `0700`, socket file `0600`. Only your
user can connect.

## Setup

1. Install `socat` (`sudo apt install socat`, `sudo dnf install socat`, etc.).
2. Launch VoiceFlow and open settings. Under **Behavior › Linux control
   socket**, turn off **Use built-in global hotkey**. VoiceFlow will stop
   opening `/dev/input/event*` and you can safely remove yourself from the
   `input` group (`sudo gpasswd -d $USER input`, then log out + back in).
3. Add the appropriate compositor bindings below.
4. Optional: enable the systemd user unit so VoiceFlow auto-starts at
   login. See [systemd user unit](#systemd-user-unit) below.

## i3 / Sway

```text
# Tap-toggle (press to start, press again to stop — one binding)
bindsym $mod+r exec --no-startup-id \
    sh -c 'echo toggle | socat -u - UNIX:$XDG_RUNTIME_DIR/voiceflow/control.sock'

# True push-to-talk (press starts, release stops)
bindsym           $mod+r exec --no-startup-id \
    sh -c 'echo start | socat -u - UNIX:$XDG_RUNTIME_DIR/voiceflow/control.sock'
bindsym --release $mod+r exec --no-startup-id \
    sh -c 'echo stop  | socat -u - UNIX:$XDG_RUNTIME_DIR/voiceflow/control.sock'
```

`exec` runs through `/bin/sh -c`, which on Debian/Ubuntu is `dash` — the
`sh -c '...'` wrapper keeps the pipe portable.

## Hyprland

```text
# Tap-toggle
bind  = SUPER, R, exec, sh -c 'echo toggle | socat -u - UNIX:$XDG_RUNTIME_DIR/voiceflow/control.sock'

# Push-to-talk (bind on press, bindr on release)
bind  = SUPER, R, exec, sh -c 'echo start | socat -u - UNIX:$XDG_RUNTIME_DIR/voiceflow/control.sock'
bindr = SUPER, R, exec, sh -c 'echo stop  | socat -u - UNIX:$XDG_RUNTIME_DIR/voiceflow/control.sock'
```

## Protocol

One request per connection, one line in, one JSON line out.

Recognised verbs: `start`, `stop`, `toggle`.

Request — either a bare verb or a JSON object:

```text
toggle
```

```json
{"cmd":"toggle"}
```

Response — always JSON:

```json
{"ok":true,"verb":"toggle","recording":true,"changed":true}
```

On error:

```json
{"ok":false,"error":"unknown_command","detail":"got 'spam', expected ['start','stop','toggle']"}
```

For interactive debugging, drop `socat`'s `-u` flag to see the response.

## systemd user unit

A ready-to-edit unit lives at `installer/voiceflow.service.example`. Copy
it into place, edit the `ExecStart` path to match where you installed
VoiceFlow, then enable it:

```bash
cp installer/voiceflow.service.example ~/.config/systemd/user/voiceflow.service
$EDITOR ~/.config/systemd/user/voiceflow.service     # set ExecStart=
systemctl --user daemon-reload
systemctl --user enable --now voiceflow.service
```

## Troubleshooting

**`socat: ... connection refused`** — VoiceFlow is not running. Launch it,
or enable the systemd user unit.

**The keybind fires but nothing happens** — open
`~/.VoiceFlow/VoiceFlow.log` and look for entries under the `ipc` domain.
Drop the `-u` flag from `socat` to surface the response body, which
includes any dispatch error (e.g. `onboarding_active`).

**Stale socket after a crash** — VoiceFlow unlinks the socket on shutdown
and again on next startup. If you ever see `bind: Address already in use`
in the log, delete `$XDG_RUNTIME_DIR/voiceflow/control.sock` manually and
relaunch.
