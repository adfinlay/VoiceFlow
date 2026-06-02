"""External control channel over a Unix domain socket.

Lets other processes (typically a compositor keybind invoking `socat`) tell
VoiceFlow to start, stop, or toggle recording without going through the
in-process global hotkey listener. Lets the user drop their `input` group
membership and bind whatever key combo their compositor allows.

Two patterns share the same socket:

1. One-shot commands. Client sends a single line and receives a JSON reply.
   Request: bare verb (`toggle\n`) or JSON (`{"cmd":"toggle"}\n`).
   Verbs: `start`, `stop`, `toggle`.
   Reply:  `{"ok":true,"verb":"toggle","recording":true}` or
           `{"ok":false,"error":"unknown_command"}`.

2. Subscription. Client sends `subscribe\n` (or `{"cmd":"subscribe"}\n`) and
   the server keeps the connection open, pushing UTF-8 text lines whenever
   VoiceFlow's recording state changes. No JSON ack is sent — the wire is
   pure display text so a `polybar tail` module can stream it verbatim. The
   most recent line is replayed to new subscribers so a status bar starting
   mid-session sees the correct state immediately.

Each connection runs in its own daemon thread so a long-lived subscriber
doesn't block the accept loop or one-shot dispatches. The dispatch callable
must tolerate background-thread invocation (true for VoiceFlow today; the
Linux evdev hotkey already drives `AppController.manual_*` from a worker).
"""
from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Callable, Optional

from services.logger import get_logger

log = get_logger("ipc")

VERBS = {"start", "stop", "toggle", "subscribe"}
_MAX_REQUEST_BYTES = 4096
_CLIENT_TIMEOUT_SECS = 2.0
_ACCEPT_POLL_SECS = 0.5


class ControlSocketService:
    """Unix-socket control channel for external triggers."""

    def __init__(self, dispatch: Callable[[str], dict]):
        self._dispatch = dispatch
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._path: Optional[Path] = None
        # Subscribers receive a UTF-8 text line every time broadcast() is
        # called. The lock guards the set against concurrent broadcasts,
        # subscribes, and disconnect cleanup; it also serialises socket
        # writes so two broadcasts can't interleave bytes on one client.
        self._subscribers: set[socket.socket] = set()
        self._sub_lock = threading.Lock()
        # Snapshot of the most recent broadcast line, replayed to new
        # subscribers so polybar starting after VoiceFlow sees current
        # state immediately rather than blank until the next transition.
        self._last_line: Optional[bytes] = None

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def broadcast(self, line: str) -> None:
        """Push a UTF-8 text line to every subscriber and remember it as
        the replay snapshot for future subscribers. Safe to call from any
        thread; no-op when there are no subscribers."""
        encoded = (line + "\n").encode("utf-8")
        with self._sub_lock:
            self._last_line = encoded
            if not self._subscribers:
                return
            dead = []
            for sub in self._subscribers:
                try:
                    sub.sendall(encoded)
                except (BrokenPipeError, OSError):
                    dead.append(sub)
            for d in dead:
                self._subscribers.discard(d)
                try:
                    d.close()
                except OSError:
                    pass

    def start(self, path: Optional[Path] = None) -> Optional[Path]:
        """Bind the socket and start the accept loop. Returns the bound path,
        or None if binding failed (in which case the service stays inert)."""
        if self._thread is not None:
            log.warning("Control socket already started", path=str(self._path))
            return self._path

        if path is None:
            path = default_socket_path()

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Belt-and-suspenders: XDG_RUNTIME_DIR is already 0700 on systemd,
            # but we own the subdir and the /tmp fallback parent.
            os.chmod(path.parent, 0o700)
        except OSError as exc:
            log.warning("Failed to prepare socket directory",
                        dir=str(path.parent), error=str(exc))

        # Stale socket from a prior unclean shutdown — unlink before bind.
        if path.exists() or path.is_symlink():
            try:
                path.unlink()
                log.info("Removed stale control socket", path=str(path))
            except OSError as exc:
                log.error("Failed to unlink stale socket",
                          path=str(path), error=str(exc))
                return None

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(path))
        except OSError as exc:
            log.error("Failed to bind control socket",
                      path=str(path), error=str(exc))
            sock.close()
            return None

        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            log.warning("Failed to chmod socket",
                        path=str(path), error=str(exc))

        sock.listen(4)
        sock.settimeout(_ACCEPT_POLL_SECS)

        self._sock = sock
        self._path = path
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="control-socket")
        self._thread.start()
        log.info("Control socket listening", path=str(path))
        return path

    def stop(self) -> None:
        """Stop the accept loop, close the socket, unlink the path."""
        if self._thread is None:
            return
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2)
        # Close out any active subscribers so their threads exit cleanly.
        with self._sub_lock:
            for sub in list(self._subscribers):
                try:
                    sub.close()
                except OSError:
                    pass
            self._subscribers.clear()
        if self._path is not None:
            try:
                if self._path.exists():
                    self._path.unlink()
            except OSError as exc:
                log.warning("Failed to unlink socket on shutdown",
                            path=str(self._path), error=str(exc))
        log.info("Control socket stopped")

    # --- internals ---

    def _accept_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                # Socket closed during shutdown — normal exit path.
                if not self._stop.is_set():
                    log.exception("Accept loop terminated unexpectedly")
                return
            # One thread per connection. One-shot dispatches return
            # quickly; subscribers block until the client disconnects.
            # Either way the accept loop stays responsive to new work.
            threading.Thread(
                target=self._handle_connection_thread,
                args=(conn,),
                daemon=True,
                name="control-conn",
            ).start()

    def _handle_connection_thread(self, conn: socket.socket) -> None:
        try:
            self._handle_connection(conn)
        except Exception:  # noqa: BLE001
            log.exception("Unhandled error in control connection handler")
        finally:
            with self._sub_lock:
                self._subscribers.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _handle_connection(self, conn: socket.socket) -> None:
        conn.settimeout(_CLIENT_TIMEOUT_SECS)
        buf = bytearray()
        while b"\n" not in buf:
            try:
                chunk = conn.recv(1024)
            except socket.timeout:
                _send_error(conn, "client_timeout")
                return
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > _MAX_REQUEST_BYTES:
                _send_error(conn, "payload_too_large")
                return

        line = bytes(buf).split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
        if not line:
            _send_error(conn, "empty_request")
            return

        verb, parse_err = _parse_verb(line)
        if parse_err is not None:
            _send_error(conn, parse_err)
            return
        if verb not in VERBS:
            _send_error(conn, "unknown_command",
                        detail=f"got {verb!r}, expected one of {sorted(VERBS)}")
            return

        if verb == "subscribe":
            self._handle_subscribe(conn)
            return

        try:
            result = self._dispatch(verb) or {}
        except Exception as exc:  # noqa: BLE001
            log.exception("Dispatch raised", verb=verb)
            _send_error(conn, "dispatch_failed", detail=str(exc))
            return

        # Only log dispatches that actually changed state — X11 auto-repeat
        # fires the press binding ~30x/sec while a key is held, and we don't
        # want to flood the log with "Dispatching" lines for no-op repeats.
        if result.get("changed"):
            log.debug("Dispatched control command", verb=verb, **result)

        _send(conn, {"ok": True, "verb": verb, **result})

    def _handle_subscribe(self, conn: socket.socket) -> None:
        """Add the connection to the subscriber set and block until it
        disconnects. The wire is plain text (no JSON ack) so polybar's
        `tail = true` module can stream it verbatim."""
        with self._sub_lock:
            last = self._last_line
            self._subscribers.add(conn)
        log.debug("Status subscriber attached", count=len(self._subscribers))
        # Replay the latest broadcast so the new subscriber sees current
        # state without having to wait for the next transition.
        if last is not None:
            try:
                conn.sendall(last)
            except (BrokenPipeError, OSError):
                return
        # Block until the client closes. We don't expect more data on this
        # socket — subscribers are passive readers — so anything they send
        # is discarded. recv returns b'' on clean close, raises on error.
        conn.settimeout(None)
        try:
            while True:
                chunk = conn.recv(64)
                if not chunk:
                    return
        except OSError:
            return


def default_socket_path() -> Path:
    """Resolve the canonical control socket path.

    Prefers $XDG_RUNTIME_DIR (mode 0700 on systemd) so the socket inherits
    user-only access. Falls back to /tmp/voiceflow-$UID/ when XDG_RUNTIME_DIR
    is unset, with explicit 0700 + 0600 perms applied in start().
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        return Path(runtime) / "voiceflow" / "control.sock"
    return Path(f"/tmp/voiceflow-{os.getuid()}") / "control.sock"


def _parse_verb(line: str) -> tuple[Optional[str], Optional[str]]:
    """Return (verb, error_code). Accepts plain-text verb or JSON object."""
    if line.startswith("{"):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None, "invalid_json"
        if not isinstance(obj, dict):
            return None, "invalid_json_shape"
        verb = obj.get("cmd")
        if not isinstance(verb, str):
            return None, "missing_cmd"
        return verb, None
    return line, None


def _send(conn: socket.socket, payload: dict) -> None:
    try:
        conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
    except BrokenPipeError:
        # Expected when the client used `socat -u` (send-only) and closed
        # before reading the response. Not an error worth logging.
        pass
    except OSError as exc:
        log.debug("Failed to send response", error=str(exc))


def _send_error(conn: socket.socket, code: str, *, detail: str = "") -> None:
    payload: dict = {"ok": False, "error": code}
    if detail:
        payload["detail"] = detail
    _send(conn, payload)
