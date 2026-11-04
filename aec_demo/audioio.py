"""音频读写与重采样：优先 soundfile，WAV 兜底用标准库，其他格式尝试 ffmpeg."""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path
from typing import Optional, Tuple

import numpy as np


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """单声道 float32 重采样；soxr 优先，scipy 兜底."""
    x = np.asarray(x, dtype=np.float32).ravel()
    if sr_in == sr_out or x.size == 0:
        return x
    try:
        import soxr

        return np.asarray(soxr.resample(x, sr_in, sr_out, quality="HQ"), dtype=np.float32)
    except ImportError:
        from scipy.signal import resample_poly
        from math import gcd

        g = gcd(sr_in, sr_out)
        return resample_poly(x, sr_out // g, sr_in // g).astype(np.float32)


def to_mono(data: np.ndarray) -> np.ndarray:
    data = np.asarray(data)
    if data.ndim == 1:
        return data.astype(np.float32)
    return data.mean(axis=1, dtype=np.float32)


def _read_wave_stdlib(path: Path) -> Tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        x = v.astype(np.float32) / float(1 << 23)
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"不支持的 WAV 位宽: {width * 8}bit")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x.astype(np.float32), sr


def _read_ffmpeg(path: Path) -> Tuple[np.ndarray, int]:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("当前环境无法解码该格式：未安装 soundfile/ffmpeg")
    sr = 48000
    cmd = [
        exe, "-v", "error", "-i", str(path),
        "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", str(sr), "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 解码失败: {proc.stderr.decode(errors='ignore')[:400]}")
    return np.frombuffer(proc.stdout, dtype="<f4").astype(np.float32), sr


def read_audio(path: str | Path, target_rate: int = 16000) -> np.ndarray:
    """读取任意音频为单声道 float32，并重采样到 target_rate."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"音频文件不存在: {path}")
    data: Optional[np.ndarray] = None
    sr = target_rate
    try:
        import soundfile as sf

        data, sr = sf.read(str(path), dtype="float32", always_2d=False)
        data = to_mono(data)
    except Exception:
        try:
            data, sr = _read_wave_stdlib(path)
        except Exception:
            data, sr = _read_ffmpeg(path)
    return resample(data, int(sr), target_rate)


def write_wav(path: str | Path, data: np.ndarray, rate: int) -> None:
    """写 16bit PCM WAV；二维数组按多声道写出（列优先）."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.asarray(data)
    if arr.ndim == 1:
        channels = 1
        interleaved = arr
    else:
        channels = arr.shape[1]
        interleaved = arr.reshape(-1)
    pcm = np.clip(np.asarray(interleaved, dtype=np.float32) * 32768.0, -32768, 32767)
    pcm = pcm.astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(int(rate))
        wf.writeframes(pcm.tobytes())


def duration_s(path: str | Path) -> float:
    path = Path(path)
    try:
        import soundfile as sf

        info = sf.info(str(path))
        return float(info.frames) / float(info.samplerate)
    except Exception:
        try:
            with wave.open(str(path), "rb") as wf:
                return wf.getnframes() / float(wf.getframerate())
        except Exception:
            return 0.0
