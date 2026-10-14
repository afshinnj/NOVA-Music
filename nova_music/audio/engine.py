"""Custom PCM playback engine.

FFmpeg decodes supported files to float32 PCM. sounddevice owns the output
stream, allowing the EQ DSP to affect the audible signal in real time.
FFmpeg must be installed and available on PATH.
"""
from __future__ import annotations
import math
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy.signal import butter, sosfilt

from PySide6.QtCore import QObject, Signal, QTimer

BAND_CENTERS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]


class AudioEngine(QObject):
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playbackStateChanged = Signal(bool)
    sourceChanged = Signal(str)
    spectrumReady = Signal(object)
    waveformReady = Signal(int, int, float)
    errorOccurred = Signal(str)
    trackEnded = Signal()
    trackFailed = Signal()
    crossfadeFinished = Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path = ""
        self.duration_ms = 0
        self.position_ms = 0
        self.sample_rate = 44100
        self.channels = 2
        self.volume = 0.75
        self._process = None
        self._stream = None
        self.output_device = None
        self.crossfade_ms = 0
        self._next_path = ""
        self._next_process = None
        self._next_buffer = np.zeros((0, 2), dtype=np.float32)
        self._crossfade_done = 0
        self._crossfade_total = 0
        self._playing = False
        self._paused = False
        self._lock = threading.RLock()
        self._buffer_condition = threading.Condition(self._lock)
        self._buffer_limit_frames = self.sample_rate * 8
        self._buffer = np.zeros((0, 2), dtype=np.float32)
        self._buffer_cursor = 0
        self._eq_enabled = True
        self._gains = [0.0] * 10
        self._filters = []
        self._filter_state = []
        self._last_emit = 0.0
        self._frames_played = 0
        self.output_frames_total = 0
        self._last_position_emit_frame = 0
        self._end_notified = False
        self._generation = 0
        self._spectrum_counter = 0
        self._build_filters()
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll_position)
        self._timer.start()

    def _ffmpeg(self):
        exe = shutil.which("ffmpeg")
        if not exe:
            bundled_root = Path(__file__).resolve().parents[1] / "bin"
            candidates = [bundled_root / "ffmpeg.exe"]
            candidates.extend(sorted(bundled_root.glob("*/bin/ffmpeg.exe")))
            exe = next((str(path) for path in candidates if path.is_file()), None)
        if not exe:
            raise RuntimeError(
                "FFmpeg پیدا نشد. آن را نصب کن و به PATH ویندوز اضافه کن؛ "
                "سپس ترمینال را دوباره باز کن."
            )
        return exe

    def set_source(self, path: str):
        self.stop()
        self.path = str(path)
        self.position_ms = 0
        self._frames_played = 0
        self._last_position_emit_frame = 0
        self._end_notified = False
        self.duration_ms = self._probe_duration(self.path)
        self.sourceChanged.emit(self.path)
        self.durationChanged.emit(self.duration_ms)

    def output_devices(self):
        result = [(None, "پیش‌فرض سیستم")]
        hostapis = sd.query_hostapis()
        for index, device in enumerate(sd.query_devices()):
            if int(device.get("max_output_channels", 0)) >= self.channels:
                hostapi = int(device.get("hostapi", -1))
                api_name = hostapis[hostapi]["name"] if 0 <= hostapi < len(hostapis) else ""
                result.append((index, f"{device['name']} ({api_name})" if api_name else device["name"]))
        return result

    def set_output_device(self, device_id):
        was_running = self._playing
        old_device = self.output_device
        if was_running: self._stop_stream()
        self.output_device = device_id
        try:
            if was_running: self._start_stream()
        except Exception:
            self.output_device = old_device
            if was_running: self._start_stream()
            raise

    def set_crossfade(self, milliseconds):
        self.crossfade_ms = max(0, min(12000, int(milliseconds)))
        if not self.crossfade_ms:
            self._stop_next_decoder()
            self._next_path = ""

    def set_next_source(self, path):
        self._stop_next_decoder()
        self._next_path = str(path or "")
        self._crossfade_done = self._crossfade_total = 0
        if not self._next_path or not self._playing or not self.crossfade_ms: return
        try:
            command = [self._ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", self._next_path,
                       "-vn", "-f", "f32le", "-acodec", "pcm_f32le", "-ar", str(self.sample_rate),
                       "-ac", str(self.channels), "pipe:1"]
            self._next_process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            threading.Thread(target=self._read_next_decoder, args=(self._next_process,), daemon=True, name="NOVA-FFmpeg-next").start()
        except Exception as exc:
            self._next_process = None
            self.errorOccurred.emit(f"آماده‌سازی Crossfade ناموفق بود: {exc}")

    def _stop_next_decoder(self):
        process, self._next_process = self._next_process, None
        if process:
            try:
                if process.poll() is None: process.terminate()
                process.wait(timeout=1)
            except Exception:
                try: process.kill()
                except Exception: pass
        with self._lock: self._next_buffer = np.zeros((0, self.channels), dtype=np.float32)

    def _read_next_decoder(self, process):
        if not process.stdout: return
        carry = b""; bpf = 4 * self.channels
        try:
            while True:
                chunk = process.stdout.read(16384)
                if not chunk: break
                data = carry + chunk; usable = len(data) - len(data) % bpf
                if usable:
                    samples = np.frombuffer(data[:usable], dtype="<f4").reshape(-1, self.channels).copy()
                    with self._buffer_condition:
                        if process is self._process:
                            room = max(0, self._buffer_limit_frames - len(self._buffer))
                            if room: self._buffer = np.concatenate((self._buffer, samples[:room]), axis=0)
                        else:
                            offset = 0
                            while offset < len(samples):
                                room = self._buffer_limit_frames - len(self._next_buffer)
                                while room <= 0 and process is self._next_process:
                                    self._buffer_condition.wait(timeout=0.1)
                                    room = self._buffer_limit_frames - len(self._next_buffer)
                                if process is not self._next_process: return
                                take = min(room, len(samples) - offset)
                                self._next_buffer = np.concatenate((self._next_buffer, samples[offset:offset + take]), axis=0)
                                offset += take
                    carry = data[usable:]
                else: carry = data
        except Exception: pass

    def _probe_duration(self, path):
        try:
            from mutagen import File as MutagenFile
            media = MutagenFile(path)
            return int(float(media.info.length) * 1000) if media and media.info else 0
        except Exception:
            return 0

    def play(self):
        if not self.path:
            return
        if self._playing:
            return
        try:
            self._ffmpeg()
            self._end_notified = False
            self._start_decoder(self.position_ms)
            self._playing = True
            self._paused = False
            self._start_stream()
            self._playing = True
            self._paused = False
            self.playbackStateChanged.emit(True)
        except Exception as exc:
            self._playing = False
            self._paused = False
            self._stop_stream()
            self._stop_decoder()
            self.errorOccurred.emit(str(exc))
            self.playbackStateChanged.emit(False)

    def pause(self):
        if not self._playing:
            return
        with self._lock:
            self._paused = True
            self._playing = False
        self._stop_stream()
        self._stop_decoder()
        self.playbackStateChanged.emit(False)

    def stop(self):
        was_active = self._playing or self._paused
        self._playing = False
        self._paused = False
        self._generation += 1
        self._stop_stream()
        self._stop_decoder()
        self._stop_next_decoder()
        with self._lock:
            self._buffer = np.zeros((0, self.channels), dtype=np.float32)
            self._buffer_cursor = 0
        self.position_ms = 0
        self._frames_played = 0
        self._last_position_emit_frame = 0
        self._end_notified = False
        if was_active:
            self.playbackStateChanged.emit(False)
        self.positionChanged.emit(0)

    def toggle(self):
        if self._playing:
            self.pause()
        else:
            self.play()

    def set_position(self, ms):
        was_playing = self._playing
        self.position_ms = max(0, min(int(ms), self.duration_ms or int(ms)))
        self._frames_played = int(self.position_ms * self.sample_rate / 1000)
        self._last_position_emit_frame = self._frames_played
        self._end_notified = False
        if self.path:
            self._stop_stream()
            self._stop_decoder()
            try:
                self._start_decoder(self.position_ms)
                with self._lock:
                    self._buffer = np.zeros((0, self.channels), dtype=np.float32)
                    self._buffer_cursor = 0
                    self._reset_filter_state()
                if was_playing:
                    self._start_stream()
                else:
                    self._stop_decoder()
            except Exception as exc:
                self.errorOccurred.emit(str(exc))
        self.positionChanged.emit(self.position_ms)

    def set_volume(self, value):
        self.volume = max(0.0, min(1.0, float(value) / 100.0))

    def set_eq_band(self, index, db):
        if 0 <= index < len(self._gains):
            self._gains[index] = max(-12.0, min(12.0, float(db)))

    def set_eq_enabled(self, enabled):
        self._eq_enabled = bool(enabled)

    def _build_filters(self):
        self._filters = []
        nyquist = self.sample_rate / 2
        for center in BAND_CENTERS:
            low = max(10.0, center / math.sqrt(2))
            high = min(nyquist * 0.95, center * math.sqrt(2))
            if center >= nyquist * 0.95 or low >= high:
                self._filters.append(None)
                continue
            self._filters.append(
                butter(2, [low, high], btype="bandpass", fs=self.sample_rate, output="sos")
            )
        self._reset_filter_state()

    def _reset_filter_state(self):
        self._filter_state = [None] * len(self._filters)

    def _start_decoder(self, start_ms):
        self._stop_decoder()
        self._generation += 1
        generation = self._generation
        command = [
            self._ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0, start_ms) / 1000:.3f}", "-i", self.path,
            "-vn", "-f", "f32le", "-acodec", "pcm_f32le",
            "-ar", str(self.sample_rate), "-ac", str(self.channels), "pipe:1"
        ]
        self._process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
        with self._lock:
            self._buffer = np.zeros((0, self.channels), dtype=np.float32)
            self._buffer_cursor = 0
        threading.Thread(
            target=self._read_decoder, args=(self._process, generation),
            daemon=True, name="NOVA-FFmpeg-reader"
        ).start()
        threading.Thread(
            target=self._read_decoder_stderr, args=(self._process, generation),
            daemon=True, name="NOVA-FFmpeg-errors"
        ).start()

    def _read_decoder_stderr(self, process, generation):
        """Drain FFmpeg diagnostics so a full stderr pipe cannot stall decoding."""
        if not process or not process.stderr:
            return
        tail = bytearray()
        try:
            while True:
                chunk = process.stderr.read(4096)
                if not chunk:
                    break
                tail.extend(chunk)
                if len(tail) > 8192:
                    del tail[:-8192]
            return_code = process.wait()
            # pause(), seek and stop intentionally terminate FFmpeg. _stop_decoder
            # clears the active process reference before terminating it, so only
            # report a non-zero exit while this is still the current decoder.
            if return_code and generation == self._generation and process is self._process:
                detail = tail.decode("utf-8", errors="replace").strip()
                message = f"خطای FFmpeg (کد {return_code})"
                if detail:
                    message += f": {detail[-2000:]}"
                self.errorOccurred.emit(message)
        except Exception:
            pass

    def _read_decoder(self, process, generation):
        if not process or not process.stdout:
            return
        bytes_per_frame = 4 * self.channels
        carry = b""
        try:
            while True:
                chunk = process.stdout.read(16384)
                if not chunk:
                    break
                data = carry + chunk
                usable = len(data) - (len(data) % bytes_per_frame)
                if usable:
                    samples = np.frombuffer(data[:usable], dtype="<f4").reshape(-1, self.channels)
                    offset = 0
                    while offset < len(samples):
                        with self._buffer_condition:
                            while (generation == self._generation
                                   and len(self._buffer) >= self._buffer_limit_frames):
                                self._buffer_condition.wait(timeout=0.1)
                            if generation != self._generation:
                                return
                            room = self._buffer_limit_frames - len(self._buffer)
                            take = min(room, len(samples) - offset)
                            self._buffer = np.concatenate(
                                (self._buffer, samples[offset:offset + take]), axis=0
                            )
                            offset += take
                    carry = data[usable:]
                else:
                    carry = data
        except Exception:
            pass

    def _start_stream(self):
        if self._stream:
            return
        self._stream = sd.OutputStream(
            samplerate=self.sample_rate, channels=self.channels,
            dtype="float32", blocksize=1024, callback=self._audio_callback,
            device=self.output_device
        )
        self._stream.start()

    def _stop_stream(self):
        stream, self._stream = self._stream, None
        if stream:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def _stop_decoder(self):
        process, self._process = self._process, None
        if process:
            try:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=1)
            except Exception:
                try: process.kill()
                except Exception: pass
            try:
                if process.stdout: process.stdout.close()
                if process.stderr: process.stderr.close()
            except Exception:
                pass

    def _audio_callback(self, outdata, frames, time_info, status):
        outdata.fill(0)
        if status:
            # Device under/overflows are surfaced in the status area without
            # raising from PortAudio's real-time callback.
            self.errorOccurred.emit(f"وضعیت دستگاه صوتی: {status}")
        if not self._playing or self._paused:
            return

        with self._lock:
            available = len(self._buffer)
            take = min(frames, available)
            if take:
                block = self._buffer[:take].copy()
                self._buffer = self._buffer[take:]
                self._buffer_condition.notify_all()
            else:
                block = np.zeros((0, self.channels), dtype=np.float32)
            process = self._process
            next_take = 0
            next_block = None
            fade_start = max(0, self.duration_ms * self.sample_rate // 1000 - self.crossfade_ms * self.sample_rate // 1000)
            if (take and self._next_process and self._next_path and self.crossfade_ms
                    and self._frames_played + take > fade_start):
                offset = max(0, fade_start - self._frames_played)
                fade_count = min(take - offset, max(0, self.crossfade_ms * self.sample_rate // 1000 - self._crossfade_done))
                next_take = min(fade_count, len(self._next_buffer))
                if next_take:
                    next_block = self._next_buffer[:next_take].copy()
                    self._next_buffer = self._next_buffer[next_take:]
                    self._buffer_condition.notify_all()
                    self._crossfade_total = self.crossfade_ms * self.sample_rate // 1000
                    self._crossfade_done += next_take
                    self._buffer_condition.notify_all()

        if take:
            processed = self._process_eq(block)
            outdata[:take] = processed * self.volume
            if next_take and next_block is not None:
                incoming = self._process_eq(next_block)
                start = max(0, fade_start - self._frames_played)
                weights = (self._crossfade_done - next_take + np.arange(1, next_take + 1)) / max(1, self._crossfade_total)
                outdata[start:start + next_take] = (processed[start:start + next_take] * (1.0 - weights[:, None]) + incoming * weights[:, None]) * self.volume
            self.output_frames_total += take

            # Position is based on frames actually handed to the output stream,
            # rather than wall-clock timer ticks.
            self._frames_played += take
            self.position_ms = int(self._frames_played * 1000 / self.sample_rate)
            if self.duration_ms:
                self.position_ms = min(self.position_ms, self.duration_ms)

            if self._frames_played - self._last_position_emit_frame >= int(self.sample_rate * 0.1):
                self._last_position_emit_frame = self._frames_played
                self.positionChanged.emit(self.position_ms)
                rms=float(np.sqrt(np.mean(np.square(outdata[:take])))) if take else 0.0
                self.waveformReady.emit(self.position_ms,self.duration_ms,min(1.0,math.sqrt(max(0.0,rms))*1.35))

            # Real-time 24-bin FFT spectrum. Keep it lightweight and emit every
            # few callback blocks to reduce UI/Qt signal overhead.
            self._spectrum_counter += 1
            if self._spectrum_counter >= 3:
                self._spectrum_counter = 0
                try:
                    mono = np.mean(block, axis=1)
                    windowed = mono * np.hanning(len(mono))
                    spectrum = np.abs(np.fft.rfft(windowed))
                    if spectrum.size > 2:
                        bins = np.array_split(spectrum[1:], 24)
                        levels = []
                        for part in bins:
                            val = float(np.mean(part)) if part.size else 0.0
                            levels.append(min(1.0, (val ** 0.5) * 0.16))
                        self.spectrumReady.emit(levels)
                except Exception:
                    pass

        # End only after FFmpeg has exited AND all decoded frames have drained.
        with self._lock:
            buffer_empty = len(self._buffer) == 0
        if (self._crossfade_total and self._crossfade_done >= self._crossfade_total and self._next_process):
            with self._lock:
                old = self._process
                self.path = self._next_path
                self.duration_ms = self._probe_duration(self.path)
                self.position_ms = self.crossfade_ms
                self._frames_played = self._crossfade_done
                self._last_position_emit_frame = self._frames_played
                self._process = self._next_process
                self._buffer = self._next_buffer
                self._next_buffer = np.zeros((0, self.channels), dtype=np.float32)
                self._next_process = None
                self._next_path = ""
                self._crossfade_done = self._crossfade_total = 0
                self._end_notified = False
            if old:
                try:
                    if old.poll() is None: old.terminate()
                except Exception: pass
            self.crossfadeFinished.emit(self.path, self.output_frames_total)
            self.sourceChanged.emit(self.path)
            self.durationChanged.emit(self.duration_ms)
        elif (take < frames and buffer_empty and process is not None
                and process.poll() is not None and not self._end_notified):
            self._end_notified = True
            self._playing = False
            self._paused = False
            if process.returncode == 0:
                self.trackEnded.emit()
            else:
                self.trackFailed.emit()
            self.playbackStateChanged.emit(False)

    def _process_eq(self, block):
        if not self._eq_enabled or not any(abs(g) > 0.01 for g in self._gains):
            return np.clip(block, -1.0, 1.0)
        source = block.astype(np.float64, copy=False)
        result = source.copy()
        for index, (sos, gain) in enumerate(zip(self._filters, self._gains)):
            if sos is None or abs(gain) < 0.01:
                continue
            band, state = self._filter_chunk(sos, source, self._filter_state[index])
            self._filter_state[index] = state
            result += band * (10 ** (gain / 20.0) - 1.0)
        peak = np.max(np.abs(result)) if result.size else 0.0
        if peak > 0.999:
            result *= 0.999 / peak
        return np.asarray(result, dtype=np.float32)

    @staticmethod
    def _filter_chunk(sos, source, state):
        from scipy.signal import sosfilt, sosfilt_zi
        if state is None:
            zi = np.zeros((sos.shape[0], 2, source.shape[1]), dtype=np.float64)
        else:
            zi = state
        return sosfilt(sos, source, axis=0, zi=zi)

    def _poll_position(self):
        # A UI heartbeat only; the position itself is driven by output frames.
        if self._playing:
            self.positionChanged.emit(self.position_ms)

    def close(self):
        self._timer.stop()
        self.stop()
