"""MeetingsController — backend orchestration for the Meetings feature.

Holds the `RecordingService`, the transcribe/summarize job queues, and the LLM
provider factory. Lives on `AppController.meetings`. Every method here is
called from `server.py` RPC handlers and returns camelCase DTOs ready for the
frontend.

Job queues are serial — one transcribe worker and one summarize worker — to
keep GPU/CPU contention bounded. Cancellation flows through CancelToken.
"""

from __future__ import annotations

import json
import queue
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from services.database import DatabaseService
from services.logger import get_logger
from services.recording.audio_source import ParecAudioSource, SoundDeviceAudioSource
from services.recording.export import UnknownExportFormatError, export_recording
from services.recording.llm import (
    LLMConnectionError,
    OpenAICompatibleProvider,
)
from services.recording.loopback_linux import _filter_linux_loopback
from services.recording.loopback_pulse import (
    PULSE_ID_BASE,
    list_pulse_monitor_sources,
)
from services.recording.loopback_windows import _filter_wasapi_loopback
from services.recording.recorder import (
    NoAudioSourcesError,
    RecorderAlreadyStartedError,
    RecorderNotRunningError,
    RecordingService,
    SAMPLE_RATE,
)
from services.recording.recovery import recover_unfinished_recordings
from services.recording.secrets import (
    delete_api_key,
    get_api_key,
    has_api_key,
    set_api_key,
)
from services.recording.summary import SummaryService
from services.recording.title import generate_title, is_default_title
from services.settings import (
    DEFAULT_LLM_PROMPT,
    LLM_PRESETS,
    SettingsService,
)
from services.transcription import (
    CancelToken,
    TranscriptionCancelled,
    TranscriptionService,
)

log = get_logger("meeting")


# ─────────────────────────────────────────────────────────────────────────────
# Job-queue plumbing
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Job:
    recording_id: int
    cancel_token: CancelToken


# ─────────────────────────────────────────────────────────────────────────────
# Pre-record source preview — opens the picked source(s) without writing to
# disk so the recorder page can show live level meters before the user commits.
# ─────────────────────────────────────────────────────────────────────────────

class _SourcePreview:
    _PEAK_FLOOR_DB = -60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mic = None
        self._loop = None
        self._mic_peak_db: Optional[float] = None
        self._loop_peak_db: Optional[float] = None

    def is_active(self) -> bool:
        with self._lock:
            return self._mic is not None or self._loop is not None

    def start(self, mic, loop) -> None:
        # Idempotent — replace any previous preview pair.
        self.stop()
        if mic is None and loop is None:
            return
        with self._lock:
            self._mic = mic
            self._loop = loop
            self._mic_peak_db = None
            self._loop_peak_db = None
        try:
            if mic is not None:
                mic.start(self._on_mic)
            if loop is not None:
                loop.start(self._on_loop)
        except Exception:
            # If either source fails to start, tear the whole preview down
            # so we don't leave a half-running stream.
            self.stop()
            raise

    def stop(self) -> None:
        with self._lock:
            mic, loop = self._mic, self._loop
            self._mic = None
            self._loop = None
            self._mic_peak_db = None
            self._loop_peak_db = None
        for src in (mic, loop):
            if src is None:
                continue
            try:
                src.stop()
            except Exception:
                log.exception("preview source stop failed")

    def state(self) -> dict:
        with self._lock:
            return {
                "active": self._mic is not None or self._loop is not None,
                "hasMic": self._mic is not None,
                "hasLoopback": self._loop is not None,
                "micPeakDb": self._mic_peak_db,
                "loopbackPeakDb": self._loop_peak_db,
            }

    def _on_mic(self, frames) -> None:
        peak = self._compute_peak(frames)
        with self._lock:
            self._mic_peak_db = peak

    def _on_loop(self, frames) -> None:
        peak = self._compute_peak(frames)
        with self._lock:
            self._loop_peak_db = peak

    @classmethod
    def _compute_peak(cls, frames) -> float:
        import numpy as np
        if frames.size == 0:
            return cls._PEAK_FLOOR_DB
        peak = float(np.abs(frames).max())
        if peak < 1e-6:
            return cls._PEAK_FLOOR_DB
        db = 20.0 * float(np.log10(peak))
        return max(cls._PEAK_FLOOR_DB, db)


class _SerialJobQueue:
    """One worker thread, one queue. Lets transcribe and summarize each have
    their own dedicated lane without blocking the other or fighting for GPU."""

    def __init__(self, name: str, runner: Callable[[_Job], None]) -> None:
        self._name = name
        self._runner = runner
        self._queue: "queue.Queue[_Job]" = queue.Queue()
        self._cancels: dict[int, CancelToken] = {}
        self._lock = threading.Lock()
        self._stopped = False
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"meetings-{name}")
        self._thread.start()

    def enqueue(self, recording_id: int) -> CancelToken:
        token = CancelToken()
        with self._lock:
            self._cancels[recording_id] = token
        self._queue.put(_Job(recording_id=recording_id, cancel_token=token))
        return token

    def cancel(self, recording_id: int) -> bool:
        with self._lock:
            token = self._cancels.get(recording_id)
        if token is None:
            return False
        token.cancel()
        return True

    def shutdown(self) -> None:
        self._stopped = True
        # Put a sentinel so the worker wakes up and exits.
        self._queue.put(_Job(recording_id=-1, cancel_token=CancelToken()))

    def _loop(self) -> None:
        while not self._stopped:
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job.recording_id < 0:
                return  # shutdown sentinel
            try:
                self._runner(job)
            except Exception:
                log.exception("job runner crashed", queue=self._name, recording_id=job.recording_id)
            finally:
                with self._lock:
                    self._cancels.pop(job.recording_id, None)


# ─────────────────────────────────────────────────────────────────────────────
# Controller
# ─────────────────────────────────────────────────────────────────────────────

class MeetingsController:
    """Lives on `AppController.meetings`. RPC handlers in server.py call into
    this. Construct after the AppController's existing services so we can
    reuse the shared `db`, `settings_service`, and `transcription_service`."""

    def __init__(
        self,
        db: DatabaseService,
        settings_service: SettingsService,
        transcription_service: TranscriptionService,
        data_root: Path,
        event_emitter: Optional[Callable[[str, dict], None]] = None,
    ) -> None:
        self.db = db
        self.settings = settings_service
        self.transcription = transcription_service
        self.data_root = data_root
        self.recordings_dir = data_root / "recordings"
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir = data_root / "exports"
        self.exports_dir.mkdir(parents=True, exist_ok=True)

        self._emit = event_emitter or (lambda _name, _payload: None)

        self.recorder = RecordingService()
        self.summary = SummaryService()
        # Pre-record source preview — see _SourcePreview for the why.
        self._preview = _SourcePreview()
        # Session cache: PULSE_ID_BASE+N → PipeWire monitor source name. Populated
        # by list_audio_sources() and read by start() so the user picks an int id
        # in the dropdown but we open a ParecAudioSource with the actual name.
        self._pulse_monitor_by_id: dict[int, str] = {}

        # Background queues.
        self._transcribe_q = _SerialJobQueue("transcribe", self._run_transcribe)
        self._summarize_q = _SerialJobQueue("summarize", self._run_summarize)

        # Server-side push ticker. Replaces the old 250 ms HTTP poll the
        # dashboard used to do: while a recording is active, fire `meeting-state`
        # events at TICK_INTERVAL_MS through `self._emit` so both popup and
        # dashboard windows receive duration + peak meters without going through
        # HTTP. Stops itself when state leaves recording/paused.
        import threading
        self._tick_stop = threading.Event()
        self._tick_thread: Optional[threading.Thread] = None

    # -------------------------------------------------------------- discovery

    def list_audio_sources(self) -> dict:
        """Enumerate available mic + loopback devices. Backend uses sounddevice
        which is platform-aware on import."""
        try:
            import sounddevice as sd
        except Exception:
            return {"mic": [], "loopback": []}

        try:
            devs = [{**dict(d), "index": i} for i, d in enumerate(sd.query_devices())]
            apis = [dict(a) for a in sd.query_hostapis()]
        except Exception as exc:
            log.warning("query_devices failed", error=str(exc))
            return {"mic": [], "loopback": []}

        # Pick loopback sources via the platform-specific filter, then fall
        # back to the OTHER platform's filter (in case PortAudio reports
        # something unexpected). The filters are conservative — they won't
        # double-list anything that doesn't look like loopback.
        loopback = _filter_linux_loopback(devs, apis) + _filter_wasapi_loopback(devs, apis)

        # PipeWire / PulseAudio monitor sources — pactl/parec path. This is
        # what actually works on Linux for capturing Chrome / Teams audio,
        # since PortAudio's JACK and ALSA backends both mangle monitor names.
        self._pulse_monitor_by_id = {}
        for entry in list_pulse_monitor_sources():
            self._pulse_monitor_by_id[entry["id"]] = entry["pulseSourceName"]
            loopback.append({
                "id": entry["id"],
                "name": entry["name"],
                "kind": entry["kind"],
                "hostApi": entry["hostApi"],
                "isDefault": entry["isDefault"],
            })

        loopback_ids = {item["id"] for item in loopback}

        # Mic = input devices that are NOT already classified as loopback.
        mics: list[dict] = []
        try:
            default_in = sd.default.device[0] if sd.default.device else None
        except Exception:
            default_in = None
        for d in devs:
            if d.get("max_input_channels", 0) <= 0:
                continue
            if d.get("index") in loopback_ids:
                continue
            host_index = d.get("hostapi", 0)
            host_name = apis[host_index].get("name", "") if 0 <= host_index < len(apis) else ""
            mics.append({
                "id": int(d.get("index", 0)),
                "name": str(d.get("name", "")),
                "kind": "mic",
                "hostApi": host_name,
                "isDefault": d.get("index") == default_in,
            })

        return {"mic": mics, "loopback": loopback}

    # -------------------------------------------------------------- recording

    # Server push cadence — matches what the old client poll was doing so the
    # UX is identical from the dashboard's perspective, just on a transport
    # (Qt WebChannel via `window.invoke`) that survives Chromium freezes.
    _TICK_INTERVAL_S = 0.25

    def _emit_meeting_state(self, state: str, duration_ms: Optional[int] = None) -> None:
        """Push a meeting-state event with the full recorder state. Routed in
        main.py to both popup (translated to `popup-state`) and dashboard
        (raw payload) via Qt WebChannel — never HTTP."""
        try:
            recorder_state = self.recorder.get_state()
        except Exception:
            recorder_state = {}
        if duration_ms is None:
            try:
                duration_ms = int(recorder_state.get("duration_ms", 0))
            except Exception:
                duration_ms = 0
        payload = {
            "state": state,
            "durationMs": duration_ms,
            "recordingId": recorder_state.get("recording_id"),
            "micPeakDb": recorder_state.get("mic_peak_db"),
            "loopbackPeakDb": recorder_state.get("loopback_peak_db"),
        }
        try:
            self._emit("meeting-state", payload)
        except Exception:
            log.exception("emit meeting-state failed", target_state=state)

    def _start_tick(self) -> None:
        """Kick off the background tick thread that pushes meeting-state every
        TICK_INTERVAL_S while the recorder is active. Idempotent."""
        import threading
        if self._tick_thread is not None and self._tick_thread.is_alive():
            return
        self._tick_stop.clear()
        t = threading.Thread(target=self._tick_loop, name="meeting-tick", daemon=True)
        self._tick_thread = t
        t.start()

    def _stop_tick(self) -> None:
        self._tick_stop.set()
        self._tick_thread = None

    def _tick_loop(self) -> None:
        """Push the current recorder state on a fixed cadence. Exits when
        the recorder transitions out of recording/paused or the stop event is
        set. Any exception is logged and re-checked next loop — never let this
        die silently."""
        while not self._tick_stop.is_set():
            try:
                st = self.recorder.get_state()
            except Exception:
                st = {}
            state = st.get("state", "idle")
            if state not in ("recording", "paused"):
                return
            self._emit_meeting_state(state)
            # `wait()` returns True if stopped — quick exit path.
            if self._tick_stop.wait(self._TICK_INTERVAL_S):
                return

    def start(
        self,
        title: str,
        mic_device_id: Optional[int],
        loopback_device_id: Optional[int],
    ) -> dict:
        if mic_device_id is None and loopback_device_id is None:
            raise NoAudioSourcesError("at least one of mic/loopback must be provided")

        sources: list[str] = []
        if mic_device_id is not None:
            sources.append("mic")
        if loopback_device_id is not None:
            sources.append("loopback")

        # Tear down any pre-record preview so it doesn't keep the audio device
        # busy when the recorder tries to open it.
        self._preview.stop()

        recording_id = self.db.create_recording(title=title or _default_title(), sources=sources)
        wav_rel = f"recordings/{recording_id}_{_slug(title)}.wav"
        wav_abs = self.data_root / wav_rel
        wav_abs.parent.mkdir(parents=True, exist_ok=True)

        mic_source = self._build_mic_source(mic_device_id)
        loop_source = self._build_loopback_source(loopback_device_id)

        try:
            self.recorder.start(
                recording_id=recording_id,
                file_path=wav_abs,
                mic=mic_source,
                loopback=loop_source,
            )
        except (RecorderAlreadyStartedError, RecorderNotRunningError, NoAudioSourcesError):
            self.db.delete_recording(recording_id)
            raise

        self.db.set_recording_recorder_state(recording_id, "recording")
        self.db.set_recording_audio(
            recording_id,
            audio_relpath=wav_rel,
            duration_ms=0,
            size_bytes=0,
            sample_rate=SAMPLE_RATE,
            channels=2 if (mic_source and loop_source) else 1,
        )
        self._emit_meeting_state("recording", duration_ms=0)
        self._start_tick()
        return {"recording_id": recording_id}

    def _build_mic_source(self, device_id: Optional[int]):
        if device_id is None:
            return None
        return SoundDeviceAudioSource(device_id=device_id, loopback=False)

    def _build_loopback_source(self, device_id: Optional[int]):
        if device_id is None:
            return None
        # Route PipeWire monitor sources through parec instead of PortAudio.
        pulse_name = self._pulse_monitor_by_id.get(device_id)
        if pulse_name is not None:
            return ParecAudioSource(source_name=pulse_name)
        return SoundDeviceAudioSource(
            device_id=device_id,
            # WASAPI loopback flag only matters on Windows; on Linux the
            # PortAudio device IS the monitor source, no flag needed.
            loopback=_is_windows(),
        )

    # -------------------------------------------------------------- pre-record preview

    def preview_start(
        self,
        mic_device_id: Optional[int],
        loopback_device_id: Optional[int],
    ) -> dict:
        """Open the picked source(s) without writing to disk. The recorder page
        polls `preview_state()` to drive the live level meters in the
        pre-record form. Conflicts with active recording — if the recorder is
        running, preview is a no-op."""
        if self.recorder.get_state()["state"] != "idle":
            return {"ok": False, "reason": "recording_in_progress"}
        mic = self._build_mic_source(mic_device_id)
        loop = self._build_loopback_source(loopback_device_id)
        try:
            self._preview.start(mic, loop)
        except Exception as exc:
            log.warning("preview start failed", error=str(exc))
            return {"ok": False, "reason": str(exc)}
        return {"ok": True}

    def preview_stop(self) -> dict:
        self._preview.stop()
        return {"ok": True}

    def preview_state(self) -> dict:
        return self._preview.state()

    def pause(self) -> dict:
        self.recorder.pause()
        state = self.recorder.get_state()
        rid = state.get("recording_id")
        if rid is not None:
            self.db.set_recording_recorder_state(rid, "paused")
        self._emit_meeting_state("paused")
        return {"ok": True}

    def resume(self) -> dict:
        self.recorder.resume()
        state = self.recorder.get_state()
        rid = state.get("recording_id")
        if rid is not None:
            self.db.set_recording_recorder_state(rid, "recording")
        self._emit_meeting_state("recording")
        return {"ok": True}

    def stop(self) -> dict:
        state = self.recorder.get_state()
        rid = state.get("recording_id")
        result = self.recorder.stop()
        self._stop_tick()
        self._emit_meeting_state("idle", duration_ms=result.get("duration_ms", 0))
        if rid is not None:
            self.db.set_recording_audio(
                rid,
                audio_relpath=self.db.get_recording(rid)["audio_relpath"],
                duration_ms=result["duration_ms"],
                size_bytes=result["size_bytes"],
                sample_rate=result["sample_rate"],
                channels=result["channels"],
            )
            self.db.set_recording_recorder_state(rid, None)

            # Auto-transcribe if enabled (default true).
            settings = self.settings.get_settings()
            if getattr(settings, "recordings_auto_transcribe", True):
                self._transcribe_q.enqueue(rid)
        return self._to_dto(self.db.get_recording(rid)) if rid is not None else {}

    def get_recorder_state(self) -> dict:
        st = self.recorder.get_state()
        dto = {
            "state": st["state"],
            "recordingId": st.get("recording_id"),
            "durationMs": int(st.get("duration_ms", 0)),
            "micPeakDb": st.get("mic_peak_db"),
            "loopbackPeakDb": st.get("loopback_peak_db"),
        }
        # Throttled diagnostic — log once every ~5s so we can confirm in the
        # field whether the frontend's polling is still firing. (The
        # stuck-at-0:33 freeze was the UI silently stopping its polling.)
        self._state_poll_count = getattr(self, "_state_poll_count", 0) + 1
        last_logged = getattr(self, "_state_poll_last_logged", 0.0)
        now = time.monotonic()
        if now - last_logged > 5.0:
            log.info(
                "get_recorder_state polled",
                state=dto["state"],
                durationMs=dto["durationMs"],
                polls_in_window=self._state_poll_count,
            )
            self._state_poll_last_logged = now
            self._state_poll_count = 0
        return dto

    # -------------------------------------------------------------- library CRUD

    def list_recordings(self, limit: int, offset: int, search: Optional[str]) -> list[dict]:
        rows = self.db.list_recordings(limit=limit, offset=offset, search=search)
        return [self._to_dto(r) for r in rows]

    def get_recording(self, recording_id: int) -> Optional[dict]:
        row = self.db.get_recording(recording_id, include_segments=True)
        if row is None:
            return None
        dto = self._to_dto(row)
        dto["segments"] = [
            {
                "id": s["id"],
                "recordingId": s["recording_id"],
                "startMs": s["start_ms"],
                "endMs": s["end_ms"],
                "text": s["text"],
            }
            for s in row.get("segments", [])
        ]
        return dto

    def update_recording(self, recording_id: int, fields: dict) -> dict:
        # camelCase → snake_case at the boundary.
        translated: dict[str, Any] = {}
        if "title" in fields: translated["title"] = fields["title"]
        if "summary" in fields: translated["summary"] = fields["summary"]
        if "notes" in fields: translated["notes"] = fields["notes"]
        if "tags" in fields: translated["tags"] = fields["tags"]
        if "language" in fields: translated["language"] = fields["language"]
        self.db.update_recording(recording_id, **translated)
        return self._to_dto(self.db.get_recording(recording_id))

    def delete_recording(self, recording_id: int) -> dict:
        row = self.db.get_recording(recording_id)
        if row and row.get("audio_relpath"):
            audio_path = (self.data_root / row["audio_relpath"]).resolve()
            try:
                audio_path.relative_to(self.data_root.resolve())
                if audio_path.exists():
                    audio_path.unlink()
            except (ValueError, OSError):
                pass
        self.db.delete_recording(recording_id)
        return {"ok": True}

    def import_file(self, file_path: str, title: Optional[str]) -> dict:
        src = Path(file_path).expanduser().resolve()
        if not src.exists() or not src.is_file():
            raise FileNotFoundError(file_path)
        rid = self.db.create_recording(title=title or src.stem, sources=[])
        ext = src.suffix.lower() or ".wav"
        rel = f"recordings/{rid}_{_slug(title or src.stem)}{ext}"
        dst = self.data_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)

        try:
            import soundfile as sf
            with sf.SoundFile(str(dst)) as snd:
                duration_ms = int(snd.frames * 1000 / snd.samplerate) if snd.samplerate else 0
                channels = snd.channels
                sample_rate = snd.samplerate
        except Exception as exc:
            log.warning("imported file metadata unreadable", error=str(exc))
            duration_ms = None
            channels = None
            sample_rate = None

        size_bytes = dst.stat().st_size
        self.db.set_recording_audio(
            rid, audio_relpath=rel, duration_ms=duration_ms,
            size_bytes=size_bytes, sample_rate=sample_rate, channels=channels,
        )

        settings = self.settings.get_settings()
        if getattr(settings, "recordings_auto_transcribe", True):
            self._transcribe_q.enqueue(rid)

        return {"recording_id": rid}

    def export(self, recording_id: int, fmt: str) -> dict:
        row = self.db.get_recording(recording_id, include_segments=True)
        if row is None:
            raise FileNotFoundError(f"recording {recording_id} not found")
        try:
            path = export_recording(row, fmt, self.exports_dir)
        except UnknownExportFormatError as exc:
            raise ValueError(str(exc)) from exc
        return {"path": str(path)}

    # -------------------------------------------------------------- jobs

    def transcribe(self, recording_id: int) -> dict:
        self._transcribe_q.enqueue(recording_id)
        self.db.update_transcript_status(recording_id, "pending", progress=0)
        return {"ok": True}

    def cancel_transcribe(self, recording_id: int) -> dict:
        ok = self._transcribe_q.cancel(recording_id)
        return {"ok": ok}

    def summarize(self, recording_id: int, prompt: Optional[str]) -> dict:
        # Store the override prompt (if any) on the recording's notes? No —
        # just hold it in the job. For v1 we always use the settings template
        # and ignore per-call overrides.
        self._summarize_q.enqueue(recording_id)
        self.db.update_summary_status(recording_id, "summarizing", progress=0)
        return {"ok": True}

    def cancel_summarize(self, recording_id: int) -> dict:
        ok = self._summarize_q.cancel(recording_id)
        return {"ok": ok}

    # -------------------------------------------------------------- LLM config

    def get_llm_config(self) -> dict:
        s = self.settings.get_settings()
        preset = getattr(s, "llm_preset", "ollama")
        return {
            "preset": preset,
            "endpoint": getattr(s, "llm_endpoint", ""),
            "model": getattr(s, "llm_model", ""),
            "hasApiKey": has_api_key(preset),
            "promptTemplate": getattr(s, "llm_prompt_template", DEFAULT_LLM_PROMPT),
        }

    def set_llm_config(
        self,
        *,
        preset: Optional[str] = None,
        endpoint: Optional[str] = None,
        model: Optional[str] = None,
        apiKey: Optional[str] = None,  # camelCase from RPC layer
        promptTemplate: Optional[str] = None,
        **_ignored,
    ) -> dict:
        if preset is not None and preset not in LLM_PRESETS:
            raise ValueError(f"unknown LLM preset: {preset}")
        kwargs: dict[str, Any] = {}
        if preset is not None:
            kwargs["llm_preset"] = preset
        if endpoint is not None:
            kwargs["llm_endpoint"] = endpoint
        if model is not None:
            kwargs["llm_model"] = model
        if promptTemplate is not None:
            kwargs["llm_prompt_template"] = promptTemplate
        if kwargs:
            self.settings.update_settings(**kwargs)
        if apiKey and preset is not None:
            set_api_key(preset, apiKey)
        return {"ok": True}

    def test_llm_connection(
        self, preset: str, endpoint: str, apiKey: Optional[str] = None,
    ) -> dict:
        key = apiKey or get_api_key(preset)
        provider = OpenAICompatibleProvider(
            endpoint=endpoint, api_key=key, default_model="probe",
        )
        try:
            models = provider.list_models()
            return {"ok": True, "error": None, "models": models}
        except LLMConnectionError as exc:
            return {"ok": False, "error": str(exc)}

    def list_llm_models(
        self, preset: str, endpoint: str, apiKey: Optional[str] = None,
    ) -> list[str]:
        key = apiKey or get_api_key(preset)
        provider = OpenAICompatibleProvider(
            endpoint=endpoint, api_key=key, default_model="probe",
        )
        return provider.list_models()

    # -------------------------------------------------------------- crash recovery

    def recover_unfinished(self) -> list[int]:
        """Called from AppController.initialize() — sweep any rows left in
        recorder_state=recording/paused and bring them back to life."""
        return recover_unfinished_recordings(
            self.db,
            data_root=self.data_root,
            on_recovered=lambda rid: self._transcribe_q.enqueue(rid),
        )

    # -------------------------------------------------------------- job runners

    def _run_transcribe(self, job: _Job) -> None:
        rid = job.recording_id
        row = self.db.get_recording(rid)
        if row is None or not row.get("audio_relpath"):
            self.db.update_transcript_status(rid, "error", error="audio not found")
            self._emit("recording-transcribe-complete", {"recordingId": rid, "success": False, "error": "audio not found"})
            return

        audio_path = self.data_root / row["audio_relpath"]
        if not audio_path.exists():
            self.db.update_transcript_status(rid, "error", error="audio missing on disk")
            self._emit("recording-transcribe-complete", {"recordingId": rid, "success": False, "error": "audio missing"})
            return

        self.db.update_transcript_status(rid, "transcribing", progress=0)
        try:
            settings = self.settings.get_settings()

            # Ensure a model is loaded — pick a sensible default if not.
            if self.transcription._model is None:  # type: ignore[attr-defined]
                self.transcription.load_model(
                    settings.model or "tiny",
                    settings.device or "auto",
                )

            def on_progress(p: float, text: str) -> None:
                self.db.update_transcript_status(rid, "transcribing", progress=p)
                self._emit("recording-transcribe-progress", {
                    "recordingId": rid, "progress": p, "currentText": text,
                })

            result = self.transcription.transcribe_file(
                str(audio_path),
                language=settings.language or "auto",
                on_progress=on_progress,
                cancel_token=job.cancel_token,
            )
        except TranscriptionCancelled:
            self.db.update_transcript_status(rid, "cancelled")
            self._emit("recording-transcribe-complete", {"recordingId": rid, "success": False, "error": "cancelled"})
            return
        except Exception as exc:
            log.exception("transcription failed", recording_id=rid)
            self.db.update_transcript_status(rid, "error", error=str(exc))
            self._emit("recording-transcribe-complete", {"recordingId": rid, "success": False, "error": str(exc)})
            return

        self.db.set_recording_transcript(rid, result["text"], language=result.get("language"))
        self.db.replace_recording_segments(rid, result["segments"])

        # Auto-rename the title from the transcript context. Best-effort —
        # runs while status is still "transcribing" so the detail page's
        # existing 1.5 s poll picks up the new title in the same refresh
        # that flips the status to done. Failure is logged and swallowed.
        self._maybe_auto_rename_title(rid, result["text"], job.cancel_token)

        self.db.update_transcript_status(rid, "done", progress=1.0)
        self._emit("recording-transcribe-complete", {"recordingId": rid, "success": True})

        # Auto-summarize if enabled.
        if getattr(self.settings.get_settings(), "recordings_auto_summarize", False):
            self._summarize_q.enqueue(rid)
            self.db.update_summary_status(rid, "summarizing", progress=0)

    def _maybe_auto_rename_title(
        self,
        rid: int,
        transcript: str,
        cancel_token: Optional[CancelToken],
    ) -> None:
        """Ask the LLM for a topic title and overwrite the default timestamp.

        No-ops when the setting is off, when the title has already been
        customised by the user, or when the LLM call fails — the goal is to
        never block transcription completion. Any errors are logged at warning
        and the original title is preserved."""
        settings = self.settings.get_settings()
        if not getattr(settings, "recordings_auto_rename_title", True):
            return

        row = self.db.get_recording(rid)
        if row is None:
            return
        current_title = row.get("title")
        if not is_default_title(current_title):
            log.debug(
                "auto-rename skipped — title already customized",
                recording_id=rid,
                title=current_title,
            )
            return

        try:
            config = self.get_llm_config()
            preset = config["preset"]
            provider = OpenAICompatibleProvider(
                endpoint=config["endpoint"],
                api_key=get_api_key(preset),
                default_model=config["model"],
            )
            new_title = generate_title(
                transcript, provider, cancel_token=cancel_token
            )
        except Exception as exc:
            log.warning(
                "auto-rename failed",
                recording_id=rid,
                error=str(exc),
            )
            return

        self.db.update_recording(rid, title=new_title)
        log.info(
            "auto-renamed recording",
            recording_id=rid,
            old_title=current_title,
            new_title=new_title,
        )

    def _run_summarize(self, job: _Job) -> None:
        rid = job.recording_id
        row = self.db.get_recording(rid, include_segments=True)
        if row is None or not row.get("transcript"):
            self.db.update_summary_status(rid, "error", error="no transcript yet")
            self._emit("recording-summarize-complete", {"recordingId": rid, "success": False, "error": "no transcript"})
            return

        config = self.get_llm_config()
        preset = config["preset"]
        api_key = get_api_key(preset)

        provider = OpenAICompatibleProvider(
            endpoint=config["endpoint"],
            api_key=api_key,
            default_model=config["model"],
        )

        try:
            def on_stream(token: str) -> None:
                self._emit("recording-summarize-progress", {
                    "recordingId": rid, "progress": 0.5, "partialText": token,
                })

            summary = self.summary.summarize(
                transcript=row["transcript"],
                segments=row.get("segments", []),
                provider=provider,
                prompt_template=config["promptTemplate"],
                on_stream=on_stream,
                cancel_token=job.cancel_token,
            )
        except Exception as exc:
            log.exception("summary failed", recording_id=rid)
            self.db.update_summary_status(rid, "error", error=str(exc))
            self._emit("recording-summarize-complete", {"recordingId": rid, "success": False, "error": str(exc)})
            return

        self.db.update_recording(rid, summary=summary)
        provider_label = f"{preset}:{config['model']}"
        self.db.update_summary_status(rid, "done", progress=1.0, provider=provider_label)
        self._emit("recording-summarize-complete", {"recordingId": rid, "success": True})

    # -------------------------------------------------------------- dto

    @staticmethod
    def _to_dto(rec: dict) -> dict:
        """DB snake_case → frontend camelCase."""
        return {
            "id": rec["id"],
            "title": rec["title"],
            "audioRelpath": rec.get("audio_relpath"),
            "audioDurationMs": rec.get("audio_duration_ms"),
            "audioSizeBytes": rec.get("audio_size_bytes"),
            "audioSampleRate": rec.get("audio_sample_rate"),
            "audioChannels": rec.get("audio_channels"),
            "sources": rec.get("sources", []),
            "language": rec.get("language"),
            "transcript": rec.get("transcript"),
            "transcriptStatus": rec.get("transcript_status", "pending"),
            "transcriptProgress": float(rec.get("transcript_progress", 0) or 0),
            "transcriptError": rec.get("transcript_error"),
            "summary": rec.get("summary"),
            "summaryProvider": rec.get("summary_provider"),
            "summaryStatus": rec.get("summary_status", "idle"),
            "summaryProgress": float(rec.get("summary_progress", 0) or 0),
            "summaryError": rec.get("summary_error"),
            "tags": rec.get("tags", []),
            "notes": rec.get("notes"),
            "recorderState": rec.get("recorder_state"),
            "createdAt": rec.get("created_at"),
            "updatedAt": rec.get("updated_at"),
        }


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_windows() -> bool:
    import sys
    return sys.platform.startswith("win")


def _slug(s: str) -> str:
    import re
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "-", (s or "rec").strip()).strip("-")
    return (clean or "rec")[:40]


def _default_title() -> str:
    return time.strftime("Meeting %a %b %d %H:%M")
