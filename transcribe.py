#!/usr/bin/env python3
"""Whisper CLI wrapper with cross-platform GPU acceleration.

Backend priority:
  macOS Apple Silicon : MLX → CPU → MPS
  Linux / Windows     : CUDA → CPU

Usage:
  python transcribe.py <audio> [--model large] [--language vi] [--device cuda|cpu|mps] ...
  python transcribe.py --mic [--input-device N] [--language vi] [--model large] [--output file.txt]
"""
import sys
import platform


def _detect_best_device() -> str:
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def _patch_mps():
    # ponytail: sparse alignment_heads not supported on MPS — densify before .to(device)
    import whisper.model
    original = whisper.model.Whisper.set_alignment_heads
    def _patched(self, dump: bytes):
        original(self, dump)
        if getattr(self, "alignment_heads", None) is not None and self.alignment_heads.is_sparse:
            self.register_buffer("alignment_heads", self.alignment_heads.to_dense(), persistent=False)
    whisper.model.Whisper.set_alignment_heads = _patched


def _try_mlx(audio_input: str, extra_args: list) -> bool:
    """Transcribe with mlx-whisper (Apple Silicon only). Returns True on success."""
    if not _is_apple_silicon():
        return False
    try:
        import mlx_whisper
    except ImportError:
        return False

    import argparse
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--language", default=None)
    p.add_argument("--model", default="large")
    p.add_argument("--output_dir", default=".")
    known, _ = p.parse_known_args(extra_args)

    model_map = {
        "tiny":     "mlx-community/whisper-tiny-mlx",
        "base":     "mlx-community/whisper-base-mlx",
        "small":    "mlx-community/whisper-small-mlx",
        "medium":   "mlx-community/whisper-medium-mlx",
        "large":    "mlx-community/whisper-large-v3-mlx",
        "large-v2": "mlx-community/whisper-large-v2-mlx",
        "large-v3": "mlx-community/whisper-large-v3-mlx",
    }
    hf_repo = model_map.get(known.model, "mlx-community/whisper-large-v3-mlx")

    print(f"[whisper_acc] Backend: MLX ({hf_repo})", file=sys.stderr)
    result = mlx_whisper.transcribe(
        audio_input,
        path_or_hf_repo=hf_repo,
        language=known.language,
        verbose=True,
    )

    from pathlib import Path
    out = Path(known.output_dir) / (Path(audio_input).stem + ".txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result["text"], encoding="utf-8")
    print(f"[whisper_acc] Saved: {out}", file=sys.stderr)
    return True


def _run_whisper_cli(device: str):
    """Patch sparse-MPS bug and invoke the standard whisper CLI."""
    _patch_mps()

    if "--device" not in sys.argv:
        sys.argv.insert(1, device)
        sys.argv.insert(1, "--device")

    print(f"[whisper_acc] Backend: {device.upper()}", file=sys.stderr)
    from whisper.transcribe import cli
    cli()


def _mic_realtime(input_device, extra_args: list):
    """Realtime transcription from microphone using VAD-based chunking.

    Apple Silicon: uses mlx_whisper directly (numpy array input, ModelHolder caching,
    no PyTorch/MPS signal-blocking issues, plain KeyboardInterrupt works).
    Other platforms: loads openai-whisper once, transcribes in thread (CPU signal-safe).
    """
    try:
        import sounddevice as sd
    except ImportError:
        print("[mic] Missing dependency: pip install sounddevice", file=sys.stderr)
        sys.exit(1)
    import numpy as np
    import wave, tempfile, os, queue, signal, threading, contextlib
    from collections import deque

    @contextlib.contextmanager
    def _quiet():
        """Suppress stderr at fd level — catches tqdm, C-ext prints, HF hub bars."""
        devnull = os.open(os.devnull, os.O_WRONLY)
        saved = os.dup(2)
        os.dup2(devnull, 2)
        os.close(devnull)
        try:
            yield
        finally:
            os.dup2(saved, 2)
            os.close(saved)

    RATE          = 16000
    SILENCE_SECS  = 0.8
    MAX_SECS      = 15
    PRE_ROLL_SECS = 0.3
    CHECK_MS      = 100

    all_devs = sd.query_devices()
    inputs = [(i, d) for i, d in enumerate(all_devs) if d['max_input_channels'] > 0]
    if not inputs:
        print("[mic] No input devices found", file=sys.stderr)
        sys.exit(1)
    if input_device is None:
        if len(inputs) > 1:
            print("Available input devices:")
            for i, d in inputs:
                print(f"  [{i}] {d['name']}")
            raw = input(f"Select device index [{inputs[0][0]}]: ").strip()
            input_device = int(raw) if raw else inputs[0][0]
        else:
            input_device = inputs[0][0]
    print(f"[mic] Device: {all_devs[input_device]['name']}", file=sys.stderr)

    import argparse
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--language", default=None)
    p.add_argument("--model", default="large")
    p.add_argument("--output", default=None)
    p.add_argument("--threshold", type=float, default=None)
    p.add_argument("--device", default=None)
    known, _ = p.parse_known_args(extra_args)

    # Auto-calibrate silence threshold from 1s of ambient noise
    if known.threshold is None:
        print("[mic] Calibrating ambient noise (1s, stay quiet)...", file=sys.stderr)
        sample = sd.rec(RATE, samplerate=RATE, channels=1, dtype='float32', device=input_device)
        sd.wait()
        ambient = float(np.abs(sample).mean())
        # ponytail: 4× ambient gives headroom; clamp so it's never absurdly low or high
        SILENCE_THRESH = max(0.003, min(ambient * 4, 0.05))
        print(f"[mic] Ambient: {ambient:.4f} → threshold: {SILENCE_THRESH:.4f}", file=sys.stderr)
    else:
        SILENCE_THRESH = known.threshold
        print(f"[mic] Threshold: {SILENCE_THRESH:.4f}", file=sys.stderr)

    lang_display = known.language or "auto-detect"
    transcript_lines = []
    record_buf = []  # full raw audio for saving alongside transcript

    import time as _time
    _session_start = None  # set when recording actually begins (after model load)

    def _fmt_time(secs):
        m, s = divmod(int(secs), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    audio_q: queue.Queue = queue.Queue()
    def callback(indata, frames, time_info, status):
        chunk = indata[:, 0].copy()
        audio_q.put(chunk)
        record_buf.append(chunk)

    speech_buf = []
    pre_roll   = deque(maxlen=int(RATE * PRE_ROLL_SECS))
    vad        = {'silent_frames': 0, 'active': False}

    def _drain():
        try:
            while True:
                chunk = audio_q.get_nowait()
                if np.abs(chunk).mean() >= SILENCE_THRESH:
                    if not vad['active']:
                        speech_buf.extend(pre_roll)
                        pre_roll.clear()
                        vad['active'] = True
                    speech_buf.extend(chunk)
                    vad['silent_frames'] = 0
                else:
                    if vad['active']:
                        speech_buf.extend(chunk)
                        vad['silent_frames'] += len(chunk)
                    else:
                        pre_roll.extend(chunk)
        except queue.Empty:
            pass

    def _should_flush():
        return vad['active'] and (
            vad['silent_frames'] >= RATE * SILENCE_SECS or
            len(speech_buf) >= RATE * MAX_SECS
        )

    AGC_TARGET_RMS = 0.1  # normalize all chunks to this RMS before transcribing

    def _agc(audio_np):
        """Boost quiet speakers up to target RMS; leave normal/loud speakers untouched."""
        rms = np.sqrt(np.mean(audio_np ** 2))
        if rms < 1e-6:
            return audio_np
        gain = AGC_TARGET_RMS / rms
        if gain <= 1.0:
            return audio_np  # already loud enough, don't reduce
        return np.clip(audio_np * gain, -1.0, 1.0)

    # Apple Silicon: use MLX — no PyTorch/MPS, KeyboardInterrupt works normally
    # mlx_whisper.transcribe() accepts np.ndarray and caches model via ModelHolder
    use_mlx = _is_apple_silicon() and known.device != "cpu"
    if use_mlx:
        try:
            import mlx_whisper
        except ImportError:
            use_mlx = False

    if use_mlx:
        model_map = {
            "tiny":     "mlx-community/whisper-tiny-mlx",
            "base":     "mlx-community/whisper-base-mlx",
            "small":    "mlx-community/whisper-small-mlx",
            "medium":   "mlx-community/whisper-medium-mlx",
            "large":    "mlx-community/whisper-large-v3-mlx",
            "large-v2": "mlx-community/whisper-large-v2-mlx",
            "large-v3": "mlx-community/whisper-large-v3-mlx",
        }
        hf_repo = model_map.get(known.model, "mlx-community/whisper-large-v3-mlx")

        def _transcribe(audio_np):
            with _quiet():
                result = mlx_whisper.transcribe(
                    audio_np.astype(np.float32),
                    path_or_hf_repo=hf_repo,
                    language=known.language,
                    verbose=False,
                )
            return result["text"].strip()

        with sd.InputStream(samplerate=RATE, channels=1, dtype='float32',
                            device=input_device, callback=callback):
            print(f"[mic] Loading MLX model '{known.model}'... (recording in background)", file=sys.stderr)
            # Warm-up: populates ModelHolder cache so loop calls are fast
            with _quiet():
                mlx_whisper.transcribe(np.zeros(RATE, dtype=np.float32),
                                       path_or_hf_repo=hf_repo, verbose=False)
            print(f"[mic] Ready — language: {lang_display} | Ctrl+C to stop\n", file=sys.stderr)
            _session_start = _time.time()
            try:
                while True:
                    sd.sleep(CHECK_MS)
                    _drain()
                    if not _should_flush():
                        continue
                    chunk = np.array(speech_buf, dtype=np.float32)
                    speech_buf.clear()
                    vad['silent_frames'] = 0
                    vad['active'] = False
                    text = _transcribe(_agc(chunk))
                    if text:
                        marker = _fmt_time(_time.time() - _session_start)
                        print(f"[{marker}] {text}")
                        transcript_lines.append(f"[{marker}] {text}")
            except KeyboardInterrupt:
                print("\n[mic] Stopped.", file=sys.stderr)

    else:
        # Non-Apple-Silicon (or --device cpu): openai-whisper, model loaded once,
        # transcription in thread so t.join(0.1) releases GIL for signal delivery
        _patch_mps()
        device = known.device or _detect_best_device()
        import whisper

        _stop = [False]
        def _sigint(sig, frame):
            _stop[0] = True
            print("\n[mic] Stopping...", file=sys.stderr)
        signal.signal(signal.SIGINT, _sigint)

        def _transcribe_raw(audio_np):
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
                tmp = f.name
            pcm = (np.clip(audio_np, -1, 1) * 32767).astype(np.int16)
            with wave.open(tmp, 'wb') as wf:
                wf.setnchannels(1); wf.setsampwidth(2)
                wf.setframerate(RATE); wf.writeframes(pcm.tobytes())
            try:
                return model.transcribe(tmp, language=known.language, fp16=(device != "cpu"))["text"].strip()
            finally:
                os.unlink(tmp)

        def _transcribe(audio_np):
            box = [None]
            t = threading.Thread(target=lambda: box.__setitem__(0, _transcribe_raw(audio_np)), daemon=True)
            t.start()
            while t.is_alive():
                t.join(0.1)
            return box[0]

        with sd.InputStream(samplerate=RATE, channels=1, dtype='float32',
                            device=input_device, callback=callback):
            print(f"[mic] Loading model '{known.model}' on {device}... (recording in background)", file=sys.stderr)
            model = whisper.load_model(known.model, device=device)
            print(f"[mic] Ready — language: {lang_display} | Ctrl+C to stop\n", file=sys.stderr)
            _session_start = _time.time()
            while not _stop[0]:
                sd.sleep(CHECK_MS)
                _drain()
                if not _should_flush():
                    continue
                chunk = np.array(speech_buf, dtype=np.float32)
                speech_buf.clear()
                vad['silent_frames'] = 0
                vad['active'] = False
                text = _transcribe(_agc(chunk))
                if text:
                    marker = _fmt_time(_time.time() - _session_start)
                    print(f"[{marker}] {text}")
                    transcript_lines.append(f"[{marker}] {text}")

    # Flush remaining buffer (both paths share _transcribe and speech_buf)
    _drain()
    if speech_buf:
        text = _transcribe(_agc(np.array(speech_buf, dtype=np.float32)))
        if text:
            marker = _fmt_time(_time.time() - _session_start) if _session_start else "00:00"
            print(f"[{marker}] {text}")
            transcript_lines.append(f"[{marker}] {text}")

    from pathlib import Path
    from datetime import datetime
    base = known.output.rsplit('.', 1)[0] if known.output else f"transcript_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    if transcript_lines:
        txt_path = Path(base + ".txt")
        txt_path.write_text("\n".join(transcript_lines), encoding="utf-8")
        print(f"[mic] Transcript: {txt_path.resolve()}", file=sys.stderr)

    if record_buf:
        wav_path = Path(base + ".wav")
        pcm = (np.clip(np.concatenate(record_buf), -1, 1) * 32767).astype(np.int16)
        with wave.open(str(wav_path), 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(RATE)
            wf.writeframes(pcm.tobytes())
        print(f"[mic] Audio:      {wav_path.resolve()}", file=sys.stderr)


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--mic" in args:
        remaining = [a for a in args if a != "--mic"]
        input_device = None
        if "--input-device" in remaining:
            idx = remaining.index("--input-device")
            input_device = int(remaining[idx + 1])
            remaining = remaining[:idx] + remaining[idx + 2:]
        _mic_realtime(input_device, remaining)
        sys.exit(0)

    if not args or args[0].startswith("-"):
        print("Usage:")
        print("  python transcribe.py <audio> [--model large] [--language vi] [--device cuda|cpu|mps] ...")
        print("  python transcribe.py --mic [--input-device N] [--language vi] [--model large] [--output file.txt]")
        sys.exit(1)

    audio_input = args[0]
    extra_args = args[1:]

    if "--device" in extra_args:
        _run_whisper_cli(device="")
        sys.exit(0)

    if _is_apple_silicon():
        if _try_mlx(audio_input, extra_args):
            sys.exit(0)
        _run_whisper_cli("cpu")
    else:
        _run_whisper_cli(_detect_best_device())
