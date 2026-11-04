"""离线信号仿真：构造 far（参考/扬声器信号）与 mic（回采+人声+噪声）.

用途：无需真实设备即可验证 AEC —— mic 里已知真实回声与近端人声，
因此能做 ERLE、近端保留率等确定性指标。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

import numpy as np

from .audioio import read_audio
from .config import SimulateSettings


@dataclass
class SimResult:
    rate: int
    far: np.ndarray
    mic: np.ndarray
    echo: np.ndarray
    near_speech: np.ndarray
    noise: np.ndarray
    segments: Dict[str, Tuple[float, float]] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return len(self.mic) / float(self.rate)


def _envelope(n: int, rate: int, seed: int) -> np.ndarray:
    """音节包络：4Hz 调制的升余弦包."""
    t = np.arange(n) / rate
    rng = np.random.default_rng(seed)
    env = np.zeros(n, dtype=np.float32)
    pos = 0
    while pos < n:
        syl = int(rate * rng.uniform(0.12, 0.32))
        gap = int(rate * rng.uniform(0.02, 0.12))
        seg = min(syl, n - pos)
        if seg > 0:
            w = np.hanning(seg + 2)[1:-1].astype(np.float32)
            env[pos:pos + seg] = w * rng.uniform(0.5, 1.0)
        pos += syl + gap
    return env


def make_speech_like(n: int, rate: int, seed: int = 1) -> np.ndarray:
    """合成类语音信号：基频+共振峰谐波+摩擦噪声，做 AEC 时比纯音更接近真实."""
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=np.float32) / rate
    f0 = 120.0 * (1.0 + 0.03 * np.sin(2 * np.pi * 2.3 * t))
    phase = 2 * np.pi * np.cumsum(f0) / rate
    sig = np.zeros(n, dtype=np.float32)
    formants = [(500, 1.0), (1500, 0.6), (2500, 0.35), (3500, 0.2)]
    for k in range(1, 40):
        fk = k * f0.mean()
        if fk > rate / 2 - 500:
            break
        gain = 0.0
        for fc, amp in formants:
            gain += amp * np.exp(-0.5 * ((fk - fc) / 180.0) ** 2)
        gain += 0.04
        sig += (gain / k ** 0.3) * np.sin(k * phase).astype(np.float32)
    # 摩擦音/气声：用包络调制白噪声
    fric = rng.standard_normal(n).astype(np.float32) * 0.12
    fric *= (0.5 + 0.5 * np.sin(2 * np.pi * 1.7 * t + 1.0)).astype(np.float32)
    sig = sig + fric
    sig *= _envelope(n, rate, seed + 7)
    peak = float(np.max(np.abs(sig))) or 1.0
    return (sig / peak).astype(np.float32)


def make_sweep(n: int, rate: int, f_start: float = 100.0, f_end: float = 7000.0) -> np.ndarray:
    """对数扫频（单段）.

    注意：连续长扫频会让 WebRTC AEC 持续重收敛，ERLE 看起来很差，
    更适合用 make_chirp_bursts / 类语音信号做效果验证。
    """
    n = max(1, int(n))
    t = np.arange(n, dtype=np.float64) / rate
    dur = max(n / rate, 1e-6)
    if abs(f_end - f_start) < 1.0:
        return (np.sin(2 * np.pi * f_start * t) * 0.9).astype(np.float32)
    k = (f_end / f_start) ** (1.0 / dur)
    phase = 2 * np.pi * f_start * (k**t - 1.0) / np.log(k)
    x = np.sin(phase)
    fade = int(rate * 0.02)
    if len(x) > 2 * fade > 0:
        x[:fade] *= np.linspace(0, 1, fade)
        x[-fade:] *= np.linspace(1, 0, fade)
    return (x * 0.9).astype(np.float32)


def make_chirp_bursts(
    n: int, rate: int, burst_s: float = 0.5, gap_s: float = 0.4,
    f0: float = 300.0, f1: float = 5000.0,
) -> np.ndarray:
    """短扫频突发 + 间隔：宽带激励同时给 AEC 留静音段收敛/跟踪."""
    n = max(1, int(n))
    out = np.zeros(n, dtype=np.float32)
    pos = 0
    burst = max(int(rate * burst_s), int(rate * 0.05))
    gap = max(int(rate * gap_s), 0)
    fade = max(int(rate * 0.005), 1)
    while pos < n:
        seg = make_sweep(burst, rate, f0, f1)
        if len(seg) > 2 * fade:
            seg[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
            seg[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
        take = min(burst, n - pos)
        out[pos:pos + take] = seg[:take]
        pos += burst + gap
    return out


def make_room_ir(rate: int, rt60: float, delay_ms: int, seed: int = 3) -> np.ndarray:
    """合成房间脉冲响应：直达声 + 指数衰减尾部 + 少量早期反射."""
    rng = np.random.default_rng(seed)
    d = int(rate * delay_ms / 1000.0)
    tail = int(rate * max(rt60, 0.02) * 1.2)
    ir = np.zeros(d + tail + 1, dtype=np.float32)
    if d > 0:
        # 扬声器-麦克风直达路径
        ir[d] = 1.0
        for _ in range(6):
            pos = d + int(rate * rng.uniform(0.0005, 0.02))
            if pos < len(ir):
                ir[pos] += rng.uniform(-0.4, 0.4)
    t = np.arange(tail, dtype=np.float32) / rate
    decay = np.exp(-6.9 * t / max(rt60, 1e-3))
    ir[d:d + tail] += (rng.standard_normal(tail).astype(np.float32) * decay * 0.05)
    norm = float(np.sqrt(np.sum(ir ** 2))) or 1.0
    return (ir / norm).astype(np.float32)


def _mix_to_rms(x: np.ndarray, target_rms: float) -> np.ndarray:
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) or 1e-9
    return (x * (target_rms / rms)).astype(np.float32)


def simulate(settings: SimulateSettings) -> SimResult:
    """时间轴（五段）:

    baseline: 静音，只有底噪
    echo_only: 只有 far（扬声器在放），mic 里是纯回声
    double_talk: far + 近端人声同时存在
    near_only: far 已停，只剩近端人声（AEC 应原样放行）
    tail: 全部静音，检查是否有残留拖尾/近端被误抑制
    """
    rate = int(settings.rate)
    total = max(1.0, float(settings.duration_s))
    n_total = int(rate * total)

    base = int(rate * max(0.0, settings.baseline_ms) / 1000)
    tail = int(rate * max(0.0, settings.tail_ms) / 1000)
    body = max(n_total - base - tail, int(rate * 1.0))
    n = base + body + tail

    # 主体三等分：纯回声 / 双讲 / 仅近端
    echo_len = int(body * 0.34)
    dt_len = int(body * 0.38)
    near_len = body - echo_len - dt_len
    i_echo = base
    i_dt = base + echo_len
    i_near = i_dt + dt_len
    i_end = i_near + near_len

    rng = np.random.default_rng(20240501)

    far = np.zeros(n, dtype=np.float32)
    if settings.ref_source == "file" and settings.ref_file:
        src = read_audio(settings.ref_file, rate)
    elif settings.ref_source == "silence":
        src = np.zeros(0, dtype=np.float32)
    elif settings.ref_source == "sweep":
        src = make_sweep(i_near - i_echo, rate)
    elif settings.ref_source == "chirp":
        src = make_chirp_bursts(i_near - i_echo, rate)
    else:
        # 默认类语音参考：回声路径稳定，AEC 能收敛，最接近真实 TTS 播放
        src = make_speech_like(i_near - i_echo, rate, seed=5) * 0.9
    if len(src):
        need = i_near - i_echo
        if len(src) < need:
            src = np.tile(src, int(np.ceil(need / len(src))))
        far[i_echo:i_near] = src[:need]
    far = np.clip(far * 0.9, -1.0, 1.0)

    # 近端人声：双讲段 + 仅近端段
    near = np.zeros(n, dtype=np.float32)
    need_near = i_end - i_dt
    if settings.mic_source == "file" and settings.mic_file:
        nsrc = read_audio(settings.mic_file, rate)
    elif settings.mic_source == "tone":
        tt = np.arange(need_near, dtype=np.float32) / rate
        nsrc = (0.5 * np.sin(2 * np.pi * 440 * tt) * (tt > 0.2)).astype(np.float32)
    elif settings.mic_source == "noise":
        nsrc = rng.standard_normal(need_near).astype(np.float32) * 0.2
    elif settings.mic_source == "silence":
        nsrc = np.zeros(0, dtype=np.float32)
    else:
        nsrc = make_speech_like(need_near, rate, seed=11)
    if len(nsrc):
        if len(nsrc) < need_near:
            nsrc = np.tile(nsrc, int(np.ceil(need_near / len(nsrc))))
        near[i_dt:i_end] = nsrc[:need_near]
    if np.any(near):
        near = _mix_to_rms(near, settings.speech_scale * 0.12)

    # ---- 回声路径：延迟 + 房间响应 + 扬声器非线性 ----
    echo_gain = float(settings.echo_gain)
    if settings.room_rt60 > 0:
        ir = make_room_ir(rate, settings.room_rt60, settings.echo_delay_ms)
        echo = np.convolve(far, ir, mode="full")[:n] * echo_gain
    else:
        d = int(rate * settings.echo_delay_ms / 1000.0)
        echo = np.zeros(n, dtype=np.float32)
        if d < n:
            echo[d:] = far[: n - d] * echo_gain
    if settings.echo_gain2 > 0:
        d2 = int(rate * (settings.echo_delay_ms + 37) / 1000.0)
        if d2 < n:
            echo[d2:] += far[: n - d2] * settings.echo_gain2
    if settings.nonlinear > 0:
        # 功放/扬声器削波失真，AEC 线性自适应滤波最怕这个
        echo = np.tanh(echo * (1.0 + 2.0 * settings.nonlinear)) / (1.0 + settings.nonlinear)

    noise = rng.standard_normal(n).astype(np.float32)
    noise = _mix_to_rms(noise, settings.noise_level) if settings.noise_level > 0 \
        else np.zeros(n, dtype=np.float32)

    mic = (echo + near + noise).astype(np.float32)
    peak = float(np.max(np.abs(mic)))
    if peak > 1.0:
        mic = (mic / peak * 0.98).astype(np.float32)
    far_peak = float(np.max(np.abs(far))) or 1.0
    if far_peak > settings.far_clipping:
        far = (far / far_peak * settings.far_clipping).astype(np.float32)

    seg = {
        "baseline": (0.0, base / rate),
        "echo_only": (i_echo / rate, i_dt / rate),
        "double_talk": (i_dt / rate, i_near / rate),
        "near_only": (i_near / rate, i_end / rate),
        "tail": (i_end / rate, n / rate),
    }
    if settings.ref_source == "silence":
        seg["echo_only"] = (0.0, 0.0)
    if mic_source_silence(settings):
        seg["double_talk"] = (0.0, 0.0)
        seg["near_only"] = (0.0, 0.0)

    return SimResult(
        rate=rate, far=far, mic=mic, echo=echo, near_speech=near,
        noise=noise, segments={k: (float(a), float(b)) for k, (a, b) in seg.items()},
    )


def mic_source_silence(settings: SimulateSettings) -> bool:
    return settings.mic_source == "silence"


def ref_audio_for_device(settings: SimulateSettings) -> np.ndarray:
    """设备测试的参考音（未加首尾静音，也未按 duration 补零）.

    file 模式返回文件本身的完整内容，由调用方决定播放/录音长度；
    其余模式按 duration_s 生成长度固定的信号。
    """
    rate = int(settings.rate)
    if settings.ref_source == "file" and settings.ref_file:
        x = read_audio(settings.ref_file, rate)
        if len(x) == 0:
            raise ValueError(f"参考音文件为空: {settings.ref_file}")
        return x.astype(np.float32)

    n = int(rate * max(1.0, float(settings.duration_s)))
    if settings.ref_source == "silence":
        return np.zeros(n, dtype=np.float32)
    if settings.ref_source == "sweep":
        return make_sweep(n, rate)
    if settings.ref_source == "chirp":
        return make_chirp_bursts(n, rate)
    # 默认：类语音（TTS 风格的连续语音），设备实测播放它最能反映真实表现
    return (make_speech_like(n, rate, seed=5) * 0.9).astype(np.float32)
