"""External control channel over a Unix domain socket.

Lets other processes (typically a compositor keybind invoking `socat`) tell
VoiceFlow to start, stop, or toggle recording without going through the
in-process global hotkey listener. Lets the user drop their `input` group
membership and bind whatever key combo their compositor allows.

Protocol: one request per connection, one line in, one JSON line out.

Request is either a bare verb (`toggle\n`) or a JSON object
(`{"cmd":"toggle"}\n`). Recognised verbs: `start`, `stop`, `toggle`.

Response is always JSON, e.g. `{"ok":true,"verb":"toggle","recording":true}`
or `{"ok":false,"error":"unknown_command"}`.

The accept loop runs in a daemon thread and calls the dispatch callable
synchronously per-connection. The dispatch is expected to be tolerant of
background-thread invocation; for VoiceFlow that's true today because the
Linux evdev hotkey already drives `AppController.manual_*` methods from a
background thread.
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

VERBS = {"start", "stop", "toggle"}
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

    @property
    def path(self) -> Optional[Path]:
        return self._path

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
            try:
                self._handle_connection(conn)
            except Exception:  # noqa: BLE001
                log.exception("Unhandled error in control connection handler")
            finally:
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
