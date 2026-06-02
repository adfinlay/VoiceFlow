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

The socket carries two patterns: one-shot commands and a subscription
stream. Both go through the same socket — the verb on the first line
decides which.

### One-shot commands

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
{"ok":false,"error":"unknown_command","detail":"got 'spam', expected ['start','stop','subscribe','toggle']"}
```

For interactive debugging, drop `socat`'s `-u` flag to see the response.

### Subscription stream

Send `subscribe\n` (or `{"cmd":"subscribe"}\n`) and keep the connection
open. VoiceFlow pushes one **plain-text line per state update** —
no JSON envelope — designed for `polybar tail` modules. The most recent
line is replayed on connect so a status bar starting after VoiceFlow
sees the current state immediately.

Lines look like:

| State          | Example                  |
| -------------- | ------------------------ |
| Idle           | `—`                      |
| Recording      | `● turbo ▁▂▃▄▅`          |
| Transcribing   | `● turbo …`              |

The dot pulses on a 1 s wall-clock cycle (`●` half the cycle, `○` the
other half), so the indicator visibly animates even during silence. The
amplitude bars are a rolling 5-sample window updated at ~12.5 Hz.

## polybar integration

Add a `custom/script` module with `tail = true` and a wrapper that
subscribes to the socket. Drop this in `~/.config/polybar/voiceflow.sh`:

```sh
#!/bin/sh
# Tail VoiceFlow status for a polybar `tail = true` module.
SOCK="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/voiceflow/control.sock"
while true; do
    printf -- '—\n'    # disconnected placeholder
    if [ -S "$SOCK" ]; then
        python3 -u -c '
import os, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    s.connect(os.environ["SOCK"])
    s.sendall(b"subscribe\n")
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        sys.stdout.write(chunk.decode("utf-8", errors="replace"))
        sys.stdout.flush()
except OSError:
    pass
' 2>/dev/null
    fi
    sleep 1
done
```

```bash
chmod +x ~/.config/polybar/voiceflow.sh
```

Then in your polybar config:

```ini
[module/voiceflow]
type = custom/script
exec = ~/.config/polybar/voiceflow.sh
tail = true
format-padding = 1
```

Place it in `modules-center = voiceflow` (or wherever you want it).
The script:

* Prints `—` whenever VoiceFlow is down so the slot doesn't go blank.
* Reconnects every second after disconnect (VoiceFlow restart, suspend, etc.).
* Uses only `python3` and standard libraries — already installed on any
  system that can run polybar.

If you'd rather not depend on Python, `socat` can do it with a hack to
keep stdin open after sending `subscribe`:

```sh
(printf 'subscribe\n'; exec sleep infinity) \
    | socat -t inf - "UNIX-CONNECT:$SOCK"
```

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
