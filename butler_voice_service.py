#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pi-side always-on voice: Friday → Vosk → butler chat → BT TTS.

Key fixes vs earlier versions:
- Keep reading mic while TTS plays (no capture overflow)
- Hardware mic boost + software gain
- Reject weak/garbage STT (stops Gemini "你想問我什麼嗎" loops)
- Cached "在" for faster ack
- Reset chat on each wake
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parent
WAKE_DIR = ROOT / "wake"
CACHE_DIR = ROOT / "static" / "voice_cache"
MODEL_PATH = Path(os.environ.get("BUTLER_FRIDAY_MODEL", str(WAKE_DIR / "Friday.onnx")))
VOSK_MODEL_DIR = Path(os.environ.get("BUTLER_VOSK_MODEL", str(WAKE_DIR / "vosk-model-small-cn-0.22")))
BUTLER_BASE = os.environ.get("BUTLER_BASE_URL", "http://127.0.0.1:8788").rstrip("/")
WAKE_THRESHOLD = float(os.environ.get("BUTLER_WAKE_THRESHOLD", "0.45"))
SAMPLE_RATE = 16000
CHUNK = 1280
COOLDOWN_SEC = float(os.environ.get("BUTLER_WAKE_COOLDOWN", "2.0"))
CONVERSE_SEC = float(os.environ.get("BUTLER_CONVERSE_SEC", "20"))
LISTEN_MAX_SEC = float(os.environ.get("BUTLER_LISTEN_MAX_SEC", "10"))
SILENCE_END_SEC = float(os.environ.get("BUTLER_SILENCE_END_SEC", "1.0"))
EMPTY_LISTEN_RETRIES = int(os.environ.get("BUTLER_EMPTY_LISTEN_RETRIES", "2"))
MIC_GAIN = float(os.environ.get("BUTLER_MIC_GAIN", "12.0"))
MIN_SPEECH_RMS = float(os.environ.get("BUTLER_MIN_SPEECH_RMS", "0.012"))
INPUT_DEVICE = os.environ.get("BUTLER_WAKE_DEVICE", "hw:3,0")
OUTPUT_DEVICE = os.environ.get("BUTLER_PLAY_DEVICE")
HA_MEDIA_ENTITY = os.environ.get("BUTLER_HA_MEDIA_ENTITY", "media_player.nesthubc902").strip()
HA_URL = os.environ.get("BUTLER_HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("BUTLER_HA_TOKEN", "").strip()
PUBLIC_BASE = os.environ.get("BUTLER_PUBLIC_BASE", "http://192.168.1.107:8788").rstrip("/")
BT_MAC = os.environ.get("BUTLER_BT_MAC", "D8:EB:46:C5:23:27").strip()
BT_SINK_HINT = os.environ.get("BUTLER_BT_SINK", "bluez_sink.D8_EB_46_C5_23_27.a2dp_sink").strip()
TTS_ROUTE = os.environ.get("BUTLER_TTS_ROUTE", "bt").strip().lower()
# Temporary kill-switch: keep BT/TTS code paths, but do not connect Nest Hub / play audio
BT_ENABLED = os.environ.get("BUTLER_BT_ENABLED", "1").strip().lower() not in ("0", "false", "off", "no")
BT_VOLUME = os.environ.get("BUTLER_BT_VOLUME", "55%").strip() or "55%"
POST_TTS_IGNORE_SEC = float(os.environ.get("BUTLER_POST_TTS_IGNORE_SEC", "1.5"))
PRE_LISTEN_DRAIN_SEC = float(os.environ.get("BUTLER_PRE_LISTEN_DRAIN_SEC", "0.35"))
ALSA_CARD = os.environ.get("BUTLER_ALSA_CARD", "3")

END_PHRASES = ("没事了", "沒事了", "没有了", "沒有了", "结束", "結束", "再见", "再見", "掰掰", "先这样", "先這樣")
# Vosk often invents these from silence / low-level noise
FILLER_TOKENS = set("啊阿那来來吧对對嗯唔哦喔额呃法的了呢吗嗎呀哈喂嗯哼啦嘛咦欸诶欸")

_state = "idle"
_last_wake = 0.0
_converse_until = 0.0
_ignore_wake_until = 0.0
_lock = threading.Lock()
_empty_listens = 0
_volume_applied = False
_pending_tts: str | None = None
_phrase_wav: dict[str, Path] = {}


def log(msg: str) -> None:
    print(f"[butler-voice] {msg}", flush=True)


def post_state(state: str, detail: str = "") -> None:
    """Tell Homelab UI: idle | listen | thinking | speaking | ack."""
    threading.Thread(
        target=lambda: post_json("/api/voice/state", {"state": state, "detail": detail}),
        daemon=True,
    ).start()


def post_json(path: str, payload: dict | None = None) -> dict:
    try:
        with httpx.Client(timeout=90.0) as client:
            r = client.post(f"{BUTLER_BASE}{path}", json=payload or {})
            if r.status_code >= 400:
                return {"error": f"HTTP {r.status_code}", "body": r.text[:300]}
            return r.json() if r.content else {}
    except Exception as e:
        return {"error": str(e)}


def _pulse_env() -> dict:
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def boost_mic_hardware() -> None:
    """Seiren Mini Capture Volume defaults mid-range; push near max."""
    try:
        r = subprocess.run(
            ["amixer", "-c", str(ALSA_CARD), "sset", "Mic", "37"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        log(f"mic hardware volume → max (amixer exit={r.returncode})")
    except Exception as e:
        log(f"mic boost skipped: {e}")


def ensure_pulse() -> bool:
    env = _pulse_env()
    try:
        if subprocess.call(["pactl", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env) == 0:
            return True
    except Exception:
        pass
    try:
        subprocess.Popen(
            ["pulseaudio", "--start", "--exit-idle-time=-1"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.0)
        return subprocess.call(["pactl", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env) == 0
    except Exception as e:
        log(f"pulse start failed: {e}")
        return False


def bt_connected() -> bool:
    if not BT_MAC:
        return False
    try:
        r = subprocess.run(["bluetoothctl", "info", BT_MAC], capture_output=True, text=True, timeout=8)
        return "Connected: yes" in (r.stdout or "")
    except Exception:
        return False


def ensure_bt_connected() -> bool:
    if not BT_ENABLED:
        return False
    if not BT_MAC:
        return False
    if bt_connected():
        return True
    try:
        r = subprocess.run(["bluetoothctl", "connect", BT_MAC], capture_output=True, text=True, timeout=20)
        log(f"BT connect: {((r.stdout or '') + (r.stderr or '')).strip()[:160]}")
        time.sleep(1.0)
        return bt_connected()
    except Exception as e:
        log(f"BT connect error: {e}")
        return False


def find_bt_sink() -> str | None:
    if not BT_ENABLED:
        return None
    if not ensure_pulse():
        return None
    env = _pulse_env()
    try:
        r = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=8, env=env)
        sinks = [ln.split()[1] for ln in (r.stdout or "").splitlines() if len(ln.split()) >= 2]
    except Exception as e:
        log(f"pactl sinks failed: {e}")
        return None
    if BT_SINK_HINT and BT_SINK_HINT in sinks:
        return BT_SINK_HINT
    for s in sinks:
        if "bluez" in s and "a2dp" in s:
            return s
    for s in sinks:
        if "bluez" in s:
            return s
    return None


def prefer_bt_sink(sink: str, set_volume: bool = False) -> None:
    global _volume_applied
    if not BT_ENABLED:
        return
    env = _pulse_env()
    try:
        subprocess.run(["pactl", "set-default-sink", sink], check=False, timeout=5, env=env)
        subprocess.run(["pactl", "set-sink-mute", sink, "0"], check=False, timeout=5, env=env)
        if set_volume or not _volume_applied:
            subprocess.run(["pactl", "set-sink-volume", sink, BT_VOLUME], check=False, timeout=5, env=env)
            _volume_applied = True
            log(f"BT volume set once → {BT_VOLUME}")
    except Exception as e:
        log(f"prefer sink failed: {e}")


def mark_post_tts_guard() -> None:
    global _ignore_wake_until
    with _lock:
        _ignore_wake_until = time.monotonic() + POST_TTS_IGNORE_SEC


def wake_blocked() -> bool:
    with _lock:
        return time.monotonic() < _ignore_wake_until


def drain_mic(stream, seconds: float) -> None:
    end = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < end:
        try:
            stream.read(CHUNK)
        except Exception:
            break


def to_int16_pcm(audio_chunk, gain: float = 1.0) -> np.ndarray:
    audio_f = np.asarray(audio_chunk).flatten().astype(np.float32) * float(gain)
    np.clip(audio_f, -1.0, 1.0, out=audio_f)
    return (audio_f * 32767.0).astype(np.int16)


def normalize_zh(text: str) -> str:
    return "".join(str(text or "").split())


def is_weak_command(text: str, max_rms: float) -> bool:
    """Reject silence-hallucinations that made Gemini say 你想問我什麼嗎."""
    t = normalize_zh(text)
    if not t:
        return True
    if max_rms < MIN_SPEECH_RMS:
        return True
    if len(t) < 2:
        return True
    if t in FILLER_TOKENS:
        return True
    # all chars are fillers / punctuation-ish
    if all(ch in FILLER_TOKENS for ch in t):
        return True
    return False


def fetch_tts_mp3(text: str) -> bytes | None:
    try:
        with httpx.Client(timeout=60.0) as client:
            r = client.get(f"{BUTLER_BASE}/api/tts", params={"text": text[:500]})
            if r.status_code == 200 and len(r.content) >= 64:
                return r.content
    except Exception as e:
        log(f"tts fetch error: {e}")
    return None


def mp3_to_wav(mp3_bytes: bytes, wav_path: Path, volume: str = "1.15") -> bool:
    fd, mp3_path = tempfile.mkstemp(prefix="butler_tts_", suffix=".mp3")
    os.close(fd)
    try:
        Path(mp3_path).write_bytes(mp3_bytes)
        r = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", mp3_path,
                "-ac", "2", "-ar", "44100",
                "-filter:a", f"volume={volume}",
                str(wav_path),
            ],
            check=False,
            timeout=30,
        )
        return r.returncode == 0 and wav_path.is_file()
    finally:
        try:
            os.unlink(mp3_path)
        except Exception:
            pass


def ensure_phrase_wav(phrase: str, filename: str) -> Path | None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    wav = CACHE_DIR / filename
    if wav.is_file() and wav.stat().st_size > 1000:
        return wav
    mp3 = fetch_tts_mp3(phrase)
    if not mp3:
        return None
    if mp3_to_wav(mp3, wav):
        log(f"cached phrase wav: {filename}")
        return wav
    return None


def warmup_phrase_cache() -> None:
    mapping = {
        "在": "ack_zai.wav",
        "没听清，请再说一次": "retry_once.wav",
        "好的，有需要再叫我": "bye.wav",
    }
    for phrase, fn in mapping.items():
        path = ensure_phrase_wav(phrase, fn)
        if path:
            _phrase_wav[phrase] = path
    # Non-speech cues so user hears listen vs thinking without waiting TTS
    listen_cue = ensure_cue_wav("cue_listen.wav", [(880.0, 0.07), (0.0, 0.04), (1175.0, 0.09)])
    think_cue = ensure_cue_wav("cue_think.wav", [(523.0, 0.12)])
    if listen_cue:
        _phrase_wav["__cue_listen__"] = listen_cue
    if think_cue:
        _phrase_wav["__cue_think__"] = think_cue


def ensure_cue_wav(filename: str, tones: list[tuple[float, float]]) -> Path | None:
    """Build short stereo cue WAV (freq_hz, duration_sec); freq 0 = silence."""
    import wave

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    wav = CACHE_DIR / filename
    if wav.is_file() and wav.stat().st_size > 200:
        return wav
    sr = 44100
    chunks: list[np.ndarray] = []
    for freq, dur in tones:
        n = max(1, int(sr * dur))
        t = np.arange(n, dtype=np.float32) / sr
        if freq <= 0:
            mono = np.zeros(n, dtype=np.float32)
        else:
            env = np.ones(n, dtype=np.float32)
            fade = min(int(0.008 * sr), n // 3)
            if fade > 0:
                env[:fade] = np.linspace(0, 1, fade, dtype=np.float32)
                env[-fade:] = np.linspace(1, 0, fade, dtype=np.float32)
            mono = (0.22 * np.sin(2 * np.pi * freq * t) * env).astype(np.float32)
        chunks.append(mono)
    mono = np.concatenate(chunks) if chunks else np.zeros(int(0.05 * sr), dtype=np.float32)
    stereo = np.column_stack([mono, mono])
    pcm = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype(np.int16)
    try:
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())
        log(f"cached cue wav: {filename}")
        return wav
    except Exception as e:
        log(f"cue wav failed: {e}")
        return None


def play_wav_on_bt(wav_path: Path) -> bool:
    if not ensure_pulse():
        return False
    if not ensure_bt_connected():
        return False
    sink = None
    for _ in range(6):
        sink = find_bt_sink()
        if sink:
            break
        time.sleep(0.3)
    if not sink:
        return False
    prefer_bt_sink(sink, set_volume=False)
    env = _pulse_env()
    env["PULSE_SINK"] = sink
    log(f"BT WAV → {sink} ({wav_path.name})")
    r = subprocess.run(["paplay", str(wav_path)], check=False, timeout=60, env=env)
    return r.returncode == 0


def _load_ha_creds() -> tuple[str, str]:
    url, token = HA_URL, HA_TOKEN
    if url and token:
        return url, token
    try:
        cfg = json.loads((ROOT / "butler_config.json").read_text(encoding="utf-8"))
        url = url or str(cfg.get("ha_url") or "").rstrip("/")
        token = token or str(cfg.get("ha_token") or "").strip()
    except Exception:
        pass
    return url, token


def play_tts_on_ha(mp3_bytes: bytes) -> bool:
    if not HA_MEDIA_ENTITY:
        return False
    ha_url, ha_token = _load_ha_creds()
    if not ha_url or not ha_token:
        return False
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "tts.mp3").write_bytes(mp3_bytes)
    media_url = f"{PUBLIC_BASE}/static/voice_cache/tts.mp3?t={int(time.time())}"
    headers = {"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=20.0, headers=headers) as client:
            try:
                client.post(f"{ha_url}/api/services/media_player/turn_off", json={"entity_id": HA_MEDIA_ENTITY})
            except Exception:
                pass
            r = client.post(
                f"{ha_url}/api/services/media_player/play_media",
                json={
                    "entity_id": HA_MEDIA_ENTITY,
                    "media_content_id": media_url,
                    "media_content_type": "music",
                },
            )
            if r.status_code >= 400:
                return False
            # Don't wait full playback — Nest will finish; we only need start
            time.sleep(0.8)
            return True
    except Exception as e:
        log(f"HA TTS error: {e}")
        return False


def play_tts_on_bt_mp3(mp3_bytes: bytes) -> bool:
    if not ensure_pulse() or not ensure_bt_connected():
        return False
    sink = find_bt_sink()
    if not sink:
        for _ in range(5):
            time.sleep(0.3)
            sink = find_bt_sink()
            if sink:
                break
    if not sink:
        return False
    prefer_bt_sink(sink, set_volume=False)
    env = _pulse_env()
    env["PULSE_SINK"] = sink
    fd, mp3_path = tempfile.mkstemp(prefix="butler_tts_", suffix=".mp3")
    os.close(fd)
    wav_path = mp3_path + ".wav"
    try:
        Path(mp3_path).write_bytes(mp3_bytes)
        if not mp3_to_wav(mp3_bytes, Path(wav_path)):
            r = subprocess.run(["mpg123", "-q", "-o", "pulse", mp3_path], check=False, timeout=90, env=env)
            return r.returncode == 0
        log(f"BT TTS → {sink}")
        r = subprocess.run(["paplay", wav_path], check=False, timeout=90, env=env)
        return r.returncode == 0
    except Exception as e:
        log(f"BT TTS error: {e}")
        return False
    finally:
        for p in (mp3_path, wav_path):
            try:
                os.unlink(p)
            except Exception:
                pass


def play_tts(text: str) -> None:
    text = " ".join(str(text or "").split()).strip()
    if not text:
        return
    if TTS_ROUTE in ("off", "none", "mute", "0", "false"):
        log(f"TTS muted (route={TTS_ROUTE}): {text[:60]}")
        return
    if not BT_ENABLED and TTS_ROUTE in ("bt", "auto", "both"):
        log(f"BT disabled — skip Nest TTS: {text[:60]}")
        return
    # Fast path: cached short phrases
    cached = _phrase_wav.get(text) or (
        _phrase_wav.get("在") if text == "在" else None
    )
    if text in _phrase_wav:
        if play_wav_on_bt(_phrase_wav[text]):
            return
    if text == "在" and "在" not in _phrase_wav:
        path = ensure_phrase_wav("在", "ack_zai.wav")
        if path:
            _phrase_wav["在"] = path
            if play_wav_on_bt(path):
                return
    mp3 = fetch_tts_mp3(text)
    if not mp3:
        log("tts failed")
        return
    if BT_ENABLED and TTS_ROUTE in ("bt", "auto", "both"):
        if play_tts_on_bt_mp3(mp3):
            return
        log("BT TTS failed → fallback")
    if TTS_ROUTE in ("ha", "auto", "both") or (TTS_ROUTE == "bt" and BT_ENABLED):
        if play_tts_on_ha(mp3):
            return
    if TTS_ROUTE in ("local", "mpg123"):
        fd, path = tempfile.mkstemp(prefix="butler_tts_", suffix=".mp3")
        os.close(fd)
        try:
            Path(path).write_bytes(mp3)
            subprocess.run(["mpg123", "-q", path], check=False, timeout=60)
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass
        return
    log(f"TTS skipped (no enabled route): {text[:40]}")


def play_tts_draining(stream, text: str) -> None:
    text = " ".join(str(text or "").split()).strip()
    if not text:
        return
    done = threading.Event()

    def _worker():
        try:
            play_tts(text)
        finally:
            done.set()

    threading.Thread(target=_worker, daemon=True).start()
    while not done.is_set():
        try:
            stream.read(CHUNK)
        except Exception as e:
            log(f"drain during TTS failed: {e}")
            break
        done.wait(0.01)
    drain_mic(stream, PRE_LISTEN_DRAIN_SEC)
    mark_post_tts_guard()


def play_cue_draining(stream, cue_key: str) -> None:
    if not BT_ENABLED or TTS_ROUTE in ("off", "none", "mute", "0", "false"):
        return
    path = _phrase_wav.get(cue_key)
    if not path or not path.is_file():
        return
    done = threading.Event()

    def _worker():
        try:
            play_wav_on_bt(path)
        finally:
            done.set()

    threading.Thread(target=_worker, daemon=True).start()
    while not done.is_set():
        try:
            stream.read(CHUNK)
        except Exception:
            break
        done.wait(0.01)
    drain_mic(stream, 0.12)
    mark_post_tts_guard()


def signal_listening(stream) -> None:
    post_state("listen", "正在聽你說")
    play_cue_draining(stream, "__cue_listen__")


def signal_thinking(stream) -> None:
    post_state("thinking", "思考中")
    play_cue_draining(stream, "__cue_think__")


def is_end_phrase(text: str) -> bool:
    t = normalize_zh(text)
    return any(p in t for p in END_PHRASES)


def listen_on_stream(vosk_model, stream) -> tuple[str, float]:
    from vosk import KaldiRecognizer

    rec = KaldiRecognizer(vosk_model, SAMPLE_RATE)
    rec.SetWords(False)
    text_parts: list[str] = []
    started = time.monotonic()
    last_voice = started
    spoke = False
    max_rms = 0.0
    energy_frames = 0

    log("listening…（請靠近 Razer 麥克風說完整句子）")
    while True:
        now = time.monotonic()
        if now - started > LISTEN_MAX_SEC:
            break
        if spoke and (now - last_voice) > SILENCE_END_SEC:
            break
        audio_chunk, _ = stream.read(CHUNK)
        audio_f = np.asarray(audio_chunk).flatten().astype(np.float32)
        rms = float(np.sqrt(np.mean(np.square(audio_f)))) if audio_f.size else 0.0
        if rms > max_rms:
            max_rms = rms
        energetic = rms >= (MIN_SPEECH_RMS * 0.45)
        if energetic:
            energy_frames += 1

        audio_int16 = to_int16_pcm(audio_chunk, MIC_GAIN)
        buf = audio_int16.tobytes()
        if rec.AcceptWaveform(buf):
            try:
                j = json.loads(rec.Result() or "{}")
            except Exception:
                j = {}
            piece = (j.get("text") or "").strip()
            # Only accept finals if this window had real energy
            if piece and energetic:
                text_parts.append(piece)
                spoke = True
                last_voice = now
                log(f"stt: {piece}")
        else:
            try:
                j = json.loads(rec.PartialResult() or "{}")
            except Exception:
                j = {}
            if (j.get("partial") or "").strip() and energetic:
                spoke = True
                last_voice = now

    try:
        j = json.loads(rec.FinalResult() or "{}")
        piece = (j.get("text") or "").strip()
        if piece and energy_frames >= 3:
            text_parts.append(piece)
    except Exception:
        pass

    out: list[str] = []
    for p in text_parts:
        if not out or out[-1] != p:
            out.append(p)
    heard = " ".join(out).strip()
    log(f"stt done text={heard!r} raw_rms={max_rms:.4f} energy_frames={energy_frames}")
    return heard, max_rms


def handle_utterance(text: str, stream, max_rms: float = 0.0) -> str:
    """Returns next state hint: idle|converse."""
    global _state, _converse_until, _empty_listens
    text = text.strip()

    if (not text) or is_weak_command(text, max_rms):
        _empty_listens += 1
        log(f"reject weak stt={text!r} rms={max_rms:.4f} try={_empty_listens}")
        if _empty_listens >= EMPTY_LISTEN_RETRIES:
            # one short cached nudge, then idle — do NOT call Gemini
            post_state("speaking", "没听清")
            play_tts_draining(stream, "没听清，请再说一次")
            _empty_listens = 0
            with _lock:
                _state = "idle"
                _converse_until = 0.0
            post_state("idle", "待命")
            return "idle"
        # silent retry listen
        with _lock:
            _state = "converse"
            _converse_until = time.monotonic() + CONVERSE_SEC
        return "converse"

    _empty_listens = 0
    log(f"heard: {text}")
    post_json("/api/voice/utterance", {"role": "user", "text": text})

    if is_end_phrase(text):
        post_state("speaking", "結束")
        play_tts_draining(stream, "好的，有需要再叫我")
        post_json("/api/voice/utterance", {"role": "assistant", "text": "好的，有需要再叫我。"})
        with _lock:
            _state = "idle"
            _converse_until = 0.0
        post_state("idle", "待命")
        return "idle"

    with _lock:
        _state = "busy"
    signal_thinking(stream)
    resp = post_json("/api/chat", {"message": text})
    reply = (resp.get("reply") or resp.get("error") or "好的。").strip()
    # Avoid endless clarification loops from prior garbage
    if any(k in reply for k in ("想問我什麼", "想问我什么", "你想問什麼", "多一點資訊", "多一点资讯")):
        reply = "我没听清楚你的指令，请再说一次完整一点。"
    log(f"reply: {reply[:120]}")
    post_json("/api/voice/utterance", {"role": "assistant", "text": reply})
    post_state("speaking", "回答中")
    play_tts_draining(stream, reply)
    with _lock:
        _converse_until = time.monotonic() + CONVERSE_SEC
        _state = "converse"
    return "converse"


def on_wake(score: float) -> None:
    global _state, _last_wake, _empty_listens, _pending_tts
    now = time.monotonic()
    if wake_blocked():
        return
    with _lock:
        if now - _last_wake < COOLDOWN_SEC:
            return
        if _state in ("busy", "ack", "listen", "speak"):
            return
        _last_wake = now
        _state = "speak"
        _empty_listens = 0
        _pending_tts = "在"
    log(f"WAKE Friday score={score:.3f}")
    post_state("ack", "已喚醒")
    post_json("/api/voice/wake", {"word": "Friday", "score": float(score)})
    # Clear confused Gemini history from prior garbage STT
    threading.Thread(target=lambda: post_json("/api/chat/reset", {}), daemon=True).start()


def query_devices_safe():
    try:
        return sd.query_devices()
    except Exception as e:
        log(f"query_devices failed: {e}")
        return []


def resolve_input_device():
    if INPUT_DEVICE not in (None, ""):
        return int(INPUT_DEVICE) if str(INPUT_DEVICE).isdigit() else INPUT_DEVICE
    devices = query_devices_safe()
    for i, d in enumerate(devices):
        if int(d.get("max_input_channels") or 0) <= 0:
            continue
        name = str(d.get("name") or "")
        if any(k.lower() in name.lower() for k in ("Seiren", "Razer", "USB Audio")):
            return i
    for i, d in enumerate(devices):
        if int(d.get("max_input_channels") or 0) > 0:
            return i
    return None


def wait_for_input_device(poll_sec: float = 5.0):
    while True:
        device = resolve_input_device()
        if device is not None:
            return device
        log("no capture device yet — retrying…")
        time.sleep(poll_sec)


def main() -> None:
    global _state, _pending_tts, _converse_until

    if not MODEL_PATH.is_file():
        raise SystemExit(f"Friday model missing: {MODEL_PATH}")
    if not VOSK_MODEL_DIR.is_dir():
        raise SystemExit(f"Vosk model missing: {VOSK_MODEL_DIR}")

    from openwakeword.model import Model
    from vosk import Model as VoskModel

    boost_mic_hardware()
    ensure_pulse()
    warmup_phrase_cache()

    log(f"loading Friday: {MODEL_PATH}")
    oww = Model(wakeword_models=[str(MODEL_PATH)], inference_framework="onnx")
    log(f"loading Vosk: {VOSK_MODEL_DIR}")
    vosk_model = VoskModel(str(VOSK_MODEL_DIR))

    def _audio_warmup():
        if not BT_ENABLED:
            log("BT disabled (Nest Hub muted) — skip connect")
            return
        if ensure_bt_connected():
            sink = find_bt_sink()
            if sink:
                prefer_bt_sink(sink, set_volume=True)
                log(f"BT sink ready: {sink}")
        else:
            log("BT not connected yet")

    threading.Thread(target=_audio_warmup, daemon=True).start()

    while True:
        device = wait_for_input_device()
        log(
            f"idle listen Friday thr={WAKE_THRESHOLD} device={device} "
            f"gain={MIC_GAIN} min_rms={MIN_SPEECH_RMS}"
        )
        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=CHUNK,
                device=device,
            ) as stream:
                while True:
                    with _lock:
                        st = _state
                        until = _converse_until
                        pending = _pending_tts

                    if st == "speak" and pending:
                        with _lock:
                            _pending_tts = None
                            _state = "ack"
                        post_state("speaking", "回應喚醒")
                        play_tts_draining(stream, pending)
                        with _lock:
                            _converse_until = time.monotonic() + CONVERSE_SEC
                            _state = "converse"
                        continue

                    now = time.monotonic()
                    if st == "converse" and now < until:
                        with _lock:
                            _state = "listen"
                        try:
                            signal_listening(stream)
                            uttered, rms = listen_on_stream(vosk_model, stream)
                            handle_utterance(uttered, stream, rms)
                        except Exception as e:
                            log(f"listen error: {e}")
                            with _lock:
                                _state = "idle"
                            post_state("idle", "待命")
                        continue

                    with _lock:
                        if _state == "converse" and time.monotonic() >= _converse_until:
                            _state = "idle"
                            log("converse timeout → idle")
                            post_state("idle", "待命")

                    if wake_blocked():
                        stream.read(CHUNK)
                        continue

                    audio_chunk, _ = stream.read(CHUNK)
                    audio_int16 = to_int16_pcm(audio_chunk, 1.0)
                    pred = oww.predict(audio_int16)
                    for _name, score in (pred or {}).items():
                        if float(score) >= WAKE_THRESHOLD:
                            on_wake(float(score))
                            break
        except Exception as e:
            log(f"input stream error: {e}; retry")
            time.sleep(2.0)


if __name__ == "__main__":
    main()
