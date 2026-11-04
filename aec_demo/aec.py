"""AEC 处理内核：WebRTC APM 的 near/far 双流封装.

关键约束：
- APM 只吃 10ms 帧
- 每个 near 帧之前必须先喂入对应时刻的 far（reverse）帧
- far 与 near 采样率不同时用 soxr 流式重采样
- 失败即旁路，不中断链路

额外能力：可读的线性 AEC 输出（export_linear_aec_output）、
延迟估计、逐帧统计，便于 demo 观察。
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from . import apm
from .audioio import resample
from .config import AecSettings, frame_samples


@dataclass
class FrameStats:
    frames: int = 0
    failures: int = 0
    proc_time_s: float = 0.0
    far_dropped: int = 0

    @property
    def realtime_factor(self) -> float:
        """处理耗时 / 音频时长；<1 表示能满足实时."""
        if self.proc_time_s <= 0:
            return 0.0
        return self.proc_time_s / max(self.frames * 0.01, 1e-9)


def build_apm_config(s: AecSettings) -> apm.Config:
    cfg = apm.create_default_config()
    cfg.pipeline_config.maximum_internal_processing_rate = 48000 if s.rate == 48000 else s.rate
    cfg.echo.enabled = bool(s.echo)
    cfg.echo.mobile_mode = bool(s.mobile_mode)
    cfg.echo.enforce_high_pass_filtering = bool(s.enforce_highpass)
    cfg.echo.export_linear_aec_output = bool(s.export_linear_aec)
    cfg.high_pass.enabled = bool(s.highpass)
    cfg.noise_suppress.enabled = bool(s.ns_enabled)
    cfg.noise_suppress.noise_level = int(s.ns_level)
    cfg.transient_suppress.enabled = bool(s.transient)
    cfg.gain_control1.enabled = bool(s.agc1)
    cfg.gain_control1.controller_mode = int(s.agc1_mode)
    cfg.gain_control1.target_level_dbfs = int(s.agc1_target_dbfs)
    cfg.gain_control1.compression_gain_db = int(s.agc1_compression_db)
    cfg.gain_control1.enable_limiter = bool(s.agc1_limiter)
    cfg.gain_control2.enabled = bool(s.agc2)
    return cfg


def apply_preset(s: AecSettings) -> AecSettings:
    """把 preset 字段展开成具体开关（保留手工改动的非预设字段）."""
    from .config import PRESETS

    p = PRESETS.get(s.preset)
    if not p:
        return s
    s.echo = p.get("echo", s.echo)
    s.mobile_mode = p.get("mobile_mode", False)
    if "preprocess" in p:
        s.highpass = p["preprocess"]
        s.ns_enabled = p["preprocess"]
    s.ns_level = p.get("ns_level", s.ns_level)
    s.agc1 = p.get("agc1", False)
    s.agc1_mode = p.get("agc1_mode", s.agc1_mode)
    s.agc1_target_dbfs = p.get("agc1_target_dbfs", s.agc1_target_dbfs)
    s.agc1_compression_db = p.get("agc1_compression_db", s.agc1_compression_db)
    return s


def _to_i16(x: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(x, dtype=np.float32) * 32768.0, -32768.0, 32767.0).astype(np.int16)


def _to_f32(x: np.ndarray) -> np.ndarray:
    return np.asarray(x, dtype=np.int16).astype(np.float32) / 32768.0


def _make_resampler(src: int, dst: int):
    if src == dst:
        return None
    import soxr

    return soxr.ResampleStream(src, dst, num_channels=1, dtype="float32", quality="QQ")


class AecCore:
    """一次 AEC 会话（一次运行 = 一个实例），线程内使用."""

    def __init__(self, settings: AecSettings, input_rate: Optional[int] = None):
        # 复制一份：preset 展开与 mobile 降级都不应改到调用方的对象
        settings = AecSettings(**settings.to_dict())
        apply_preset(settings)
        self.settings = settings
        self.rate = int(settings.rate)
        self.frame = frame_samples(self.rate)
        self.input_rate = int(input_rate or self.rate)
        self.errors: List[str] = []
        self.stats = FrameStats()
        # mobile AEC 在部分 APM 构建里不可用（process_stream 返回 -11），
        # 这里先探测，不可用则自动降级为桌面 AEC，并在结果里标注。
        self.mobile_downgraded = False
        if settings.mobile_mode and not apm.mobile_mode_supported():
            settings.mobile_mode = False
            self.mobile_downgraded = True
            self.errors.append("当前 WebRTC APM 构建不支持 mobile AEC（返回 -11），已自动降级为桌面 AEC")

        self._apm = apm.WebRTCAudioProcessing()
        cfg = build_apm_config(settings)
        ret = self._apm.apply_config(cfg)
        if ret != 0:
            raise RuntimeError(f"apply_config 失败，返回 {ret}")
        self._cfg = self._apm.create_stream_config(self.rate, 1)
        self._delay_ms = int(settings.delay_ms)
        self._apm.set_stream_delay_ms(self._delay_ms)

        self._in = (ctypes.c_short * self.frame)()
        self._out = (ctypes.c_short * self.frame)()
        self._rin = (ctypes.c_short * self.frame)()
        self._rout = (ctypes.c_short * self.frame)()

        self._far_rs = _make_resampler(self.input_rate, self.rate)
        self._near_rs = _make_resampler(self.input_rate, self.rate)
        self._far_buf = np.empty(0, dtype=np.float32)
        self._near_buf = np.empty(0, dtype=np.float32)
        self._closed = False
        self.last_delay_ms = self._delay_ms

    # ---- 配置 ----
    def set_delay_ms(self, delay_ms: int) -> None:
        self._delay_ms = int(max(0, min(delay_ms, 500)))
        self.last_delay_ms = self._delay_ms
        if not self._closed:
            self._apm.set_stream_delay_ms(self._delay_ms)

    # ---- 离线：两路等长、已对齐的整段信号 ----
    def process_arrays(self, mic: np.ndarray, far: np.ndarray, input_rate: Optional[int] = None) -> np.ndarray:
        """输入可为任意采样率；返回 APM 采样率下的输出，长度与输入（重采样后）一致."""
        in_rate = int(input_rate or self.input_rate)
        mic = np.asarray(mic, dtype=np.float32).ravel()
        far = np.asarray(far, dtype=np.float32).ravel()
        if in_rate != self.rate:
            mic = resample(mic, in_rate, self.rate)
            far = resample(far, in_rate, self.rate)
        n = min(len(mic), len(far))
        mic, far = mic[:n], far[:n]
        usable = (n // self.frame) * self.frame
        if usable == 0:
            return np.zeros(0, dtype=np.float32)
        mic, far = mic[:usable], far[:usable]
        out = np.empty(usable, dtype=np.float32)
        mic16, far16 = _to_i16(mic), _to_i16(far)
        t0 = time.perf_counter()
        for off in range(0, usable, self.frame):
            self._feed_far(far16[off:off + self.frame])
            ctypes.memmove(self._in, mic16[off:off + self.frame].ctypes.data, self.frame * 2)
            ret = self._apm.process_stream(self._in, self._cfg, self._cfg, self._out)
            if ret != 0:
                raise RuntimeError(f"process_stream 返回 {ret}")
            out[off:off + self.frame] = _to_f32(np.frombuffer(self._out, dtype=np.int16))
            self.stats.frames += 1
        self.stats.proc_time_s += time.perf_counter() - t0
        return out

    def _feed_far(self, far_i16: np.ndarray) -> None:
        ctypes.memmove(self._rin, far_i16.ctypes.data, self.frame * 2)
        ret = self._apm.process_reverse_stream(self._rin, self._cfg, self._cfg, self._rout)
        if ret != 0:
            raise RuntimeError(f"process_reverse_stream 返回 {ret}")

    # ---- 实时：任意长度块，输出与输入块等长（设备模式使用）----
    def process_chunk(self, mic: np.ndarray, far: np.ndarray) -> np.ndarray:
        """mic/far 同为 input_rate 采样率的等长块；返回同采样率下的输出."""
        mic = np.asarray(mic, dtype=np.float32).ravel()
        far = np.asarray(far, dtype=np.float32).ravel()
        if self._far_rs is not None:
            far = np.asarray(self._far_rs.resample_chunk(far, last=False), dtype=np.float32)
        if self._near_rs is not None:
            mic = np.asarray(self._near_rs.resample_chunk(mic, last=False), dtype=np.float32)
        self._far_buf = np.concatenate((self._far_buf, far))
        self._near_buf = np.concatenate((self._near_buf, mic))

        n_frames = min(len(self._far_buf), len(self._near_buf)) // self.frame
        if n_frames == 0:
            return np.zeros(0, dtype=np.float32)
        usable = n_frames * self.frame
        far_f, near_f = self._far_buf[:usable], self._near_buf[:usable]
        self._far_buf = self._far_buf[usable:]
        self._near_buf = self._near_buf[usable:]

        out = np.empty(usable, dtype=np.float32)
        far16, near16 = _to_i16(far_f), _to_i16(near_f)
        t0 = time.perf_counter()
        for off in range(0, usable, self.frame):
            self._feed_far(far16[off:off + self.frame])
            ctypes.memmove(self._in, near16[off:off + self.frame].ctypes.data, self.frame * 2)
            ret = self._apm.process_stream(self._in, self._cfg, self._cfg, self._out)
            if ret != 0:
                raise RuntimeError(f"process_stream 返回 {ret}")
            out[off:off + self.frame] = _to_f32(np.frombuffer(self._out, dtype=np.int16))
            self.stats.frames += 1
        self.stats.proc_time_s += time.perf_counter() - t0
        if self.input_rate != self.rate:
            return resample(out, self.rate, self.input_rate)
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._apm.destroy_stream_config(self._cfg)
        except Exception:
            pass
        try:
            self._apm.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# --------------------------------------------------------------------------
# 延迟估计
# --------------------------------------------------------------------------
def estimate_delay_ms(
    mic: np.ndarray, far: np.ndarray, rate: int, max_ms: int = 500, prefer_positive: bool = True
) -> float:
    """用包络互相关估计「far 播出 → mic 收到」的延迟（ms）.

    包络而不是原始波形：对增益/非线性/谱形变化更稳健，且计算量小。
    """
    mic = np.asarray(mic, dtype=np.float64).ravel()
    far = np.asarray(far, dtype=np.float64).ravel()
    n = min(len(mic), len(far))
    if n < rate // 10:
        return 0.0
    mic, far = mic[:n], far[:n]
    hop = max(1, int(rate / 1000))  # 1ms
    def env(x):
        seg = (n // hop) * hop
        return np.sqrt(np.mean(x[:seg].reshape(-1, hop) ** 2, axis=1) + 1e-12)
    em, ef = env(mic), env(far)
    em -= em.mean()
    ef -= ef.mean()
    if np.allclose(em, 0) or np.allclose(ef, 0):
        return 0.0
    # FFT 互相关，等价于滑窗 multiply-accumulate
    size = 1 << int(np.ceil(np.log2(2 * len(em))))
    corr = np.fft.irfft(np.fft.rfft(em, size) * np.conj(np.fft.rfft(ef, size)), size)
    corr = corr[: len(em)]
    max_lag = int(max_ms)
    if prefer_positive:
        search = corr[: max_lag + 1]
        lag = int(np.argmax(search))
        # 能量比太低则认为无回声
        if search[lag] <= 0 or search[lag] < 3.0 * float(np.std(corr[1:])):
            return 0.0
    else:
        half = len(corr) // 2
        lag = int(np.argmax(corr[: min(half, max_lag)]))
    return float(lag)
