"""libs/webrtc_apm 的 ctypes 绑定（跨平台，相对本项目根目录加载）.

只做库加载与接口封装，不含项目内部日志/资源查找依赖，
并附带「库是否可用」的探测。
"""

from __future__ import annotations

import ctypes
import platform
from enum import IntEnum
from pathlib import Path
from typing import Optional, Tuple

from .config import LIBS_DIR

_LIB: Optional[ctypes.CDLL] = None
_LIB_PATH: Optional[str] = None


class DownmixMethod(IntEnum):
    AVERAGE_CHANNELS = 0
    USE_FIRST_CHANNEL = 1


class NoiseSuppressionLevel(IntEnum):
    LOW = 0
    MODERATE = 1
    HIGH = 2
    VERY_HIGH = 3


class GainController1Mode(IntEnum):
    ADAPTIVE_ANALOG = 0
    ADAPTIVE_DIGITAL = 1
    FIXED_DIGITAL = 2


class ClippingPredictorMode(IntEnum):
    CLIPPING_EVENT_PREDICTION = 0
    ADAPTIVE_STEP_CLIPPING_PEAK_PREDICTION = 1
    FIXED_STEP_CLIPPING_PEAK_PREDICTION = 2


class Pipeline(ctypes.Structure):
    _fields_ = [
        ("maximum_internal_processing_rate", ctypes.c_int),
        ("multi_channel_render", ctypes.c_bool),
        ("multi_channel_capture", ctypes.c_bool),
        ("capture_downmix_method", ctypes.c_int),
    ]


class PreAmplifier(ctypes.Structure):
    _fields_ = [
        ("enabled", ctypes.c_bool),
        ("fixed_gain_factor", ctypes.c_float),
    ]


class AnalogMicGainEmulation(ctypes.Structure):
    _fields_ = [
        ("enabled", ctypes.c_bool),
        ("initial_level", ctypes.c_int),
    ]


class CaptureLevelAdjustment(ctypes.Structure):
    _fields_ = [
        ("enabled", ctypes.c_bool),
        ("pre_gain_factor", ctypes.c_float),
        ("post_gain_factor", ctypes.c_float),
        ("mic_gain_emulation", AnalogMicGainEmulation),
    ]


class HighPassFilter(ctypes.Structure):
    _fields_ = [
        ("enabled", ctypes.c_bool),
        ("apply_in_full_band", ctypes.c_bool),
    ]


class EchoCanceller(ctypes.Structure):
    _fields_ = [
        ("enabled", ctypes.c_bool),
        ("mobile_mode", ctypes.c_bool),
        ("export_linear_aec_output", ctypes.c_bool),
        ("enforce_high_pass_filtering", ctypes.c_bool),
    ]


class NoiseSuppression(ctypes.Structure):
    _fields_ = [
        ("enabled", ctypes.c_bool),
        ("noise_level", ctypes.c_int),
        ("analyze_linear_aec_output_when_available", ctypes.c_bool),
    ]


class TransientSuppression(ctypes.Structure):
    _fields_ = [("enabled", ctypes.c_bool)]


class ClippingPredictor(ctypes.Structure):
    _fields_ = [
        ("enabled", ctypes.c_bool),
        ("predictor_mode", ctypes.c_int),
        ("window_length", ctypes.c_int),
        ("reference_window_length", ctypes.c_int),
        ("reference_window_delay", ctypes.c_int),
        ("clipping_threshold", ctypes.c_float),
        ("crest_factor_margin", ctypes.c_float),
        ("use_predicted_step", ctypes.c_bool),
    ]


class AnalogGainController(ctypes.Structure):
    _fields_ = [
        ("enabled", ctypes.c_bool),
        ("startup_min_volume", ctypes.c_int),
        ("clipped_level_min", ctypes.c_int),
        ("enable_digital_adaptive", ctypes.c_bool),
        ("clipped_level_step", ctypes.c_int),
        ("clipped_ratio_threshold", ctypes.c_float),
        ("clipped_wait_frames", ctypes.c_int),
        ("predictor", ClippingPredictor),
    ]


class GainController1(ctypes.Structure):
    _fields_ = [
        ("enabled", ctypes.c_bool),
        ("controller_mode", ctypes.c_int),
        ("target_level_dbfs", ctypes.c_int),
        ("compression_gain_db", ctypes.c_int),
        ("enable_limiter", ctypes.c_bool),
        ("analog_controller", AnalogGainController),
    ]


class InputVolumeController(ctypes.Structure):
    _fields_ = [("enabled", ctypes.c_bool)]


class AdaptiveDigital(ctypes.Structure):
    _fields_ = [
        ("enabled", ctypes.c_bool),
        ("headroom_db", ctypes.c_float),
        ("max_gain_db", ctypes.c_float),
        ("initial_gain_db", ctypes.c_float),
        ("max_gain_change_db_per_second", ctypes.c_float),
        ("max_output_noise_level_dbfs", ctypes.c_float),
    ]


class FixedDigital(ctypes.Structure):
    _fields_ = [("gain_db", ctypes.c_float)]


class GainController2(ctypes.Structure):
    _fields_ = [
        ("enabled", ctypes.c_bool),
        ("volume_controller", InputVolumeController),
        ("adaptive_controller", AdaptiveDigital),
        ("fixed_controller", FixedDigital),
    ]


class Config(ctypes.Structure):
    _fields_ = [
        ("pipeline_config", Pipeline),
        ("pre_amp", PreAmplifier),
        ("level_adjustment", CaptureLevelAdjustment),
        ("high_pass", HighPassFilter),
        ("echo", EchoCanceller),
        ("noise_suppress", NoiseSuppression),
        ("transient_suppress", TransientSuppression),
        ("gain_control1", GainController1),
        ("gain_control2", GainController2),
    ]


def _normalize_arch(arch: str) -> str:
    arch = arch.lower()
    if arch in ("x86_64", "amd64"):
        return "x64"
    if arch in ("aarch64", "arm64"):
        return "arm64"
    if arch in ("i386", "i686", "x86"):
        return "x86"
    return arch


def library_path() -> Path:
    """按当前平台定位动态库；树莓派等 arm64 Linux 走 linux/arm64."""
    system = platform.system().lower()
    arch = _normalize_arch(platform.machine())
    if system == "linux":
        name, sub = "libwebrtc_apm.so", "linux"
    elif system == "darwin":
        name, sub = "libwebrtc_apm.dylib", "macos"
    elif system == "windows":
        name, sub = "libwebrtc_apm.dll", "windows"
    else:
        raise RuntimeError(f"不支持的平台: {system}")
    return LIBS_DIR / "webrtc_apm" / sub / arch / name


def platform_info() -> dict:
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "arch": _normalize_arch(platform.machine()),
        "lib_path": str(library_path()),
        "lib_exists": library_path().exists(),
        "python": platform.python_version(),
    }


def _init_function_signatures(lib: ctypes.CDLL) -> None:
    lib.WebRTC_APM_Create.argtypes = []
    lib.WebRTC_APM_Create.restype = ctypes.c_void_p

    lib.WebRTC_APM_Destroy.argtypes = [ctypes.c_void_p]
    lib.WebRTC_APM_Destroy.restype = None

    lib.WebRTC_APM_CreateStreamConfig.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.WebRTC_APM_CreateStreamConfig.restype = ctypes.c_void_p

    lib.WebRTC_APM_DestroyStreamConfig.argtypes = [ctypes.c_void_p]
    lib.WebRTC_APM_DestroyStreamConfig.restype = ctypes.c_void_p

    lib.WebRTC_APM_ApplyConfig.argtypes = [ctypes.c_void_p, ctypes.POINTER(Config)]
    lib.WebRTC_APM_ApplyConfig.restype = ctypes.c_int

    lib.WebRTC_APM_ProcessReverseStream.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_short),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_short),
    ]
    lib.WebRTC_APM_ProcessReverseStream.restype = ctypes.c_int

    lib.WebRTC_APM_ProcessStream.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_short),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_short),
    ]
    lib.WebRTC_APM_ProcessStream.restype = ctypes.c_int

    lib.WebRTC_APM_SetStreamDelayMs.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.WebRTC_APM_SetStreamDelayMs.restype = None


def ensure_library_loaded() -> ctypes.CDLL:
    global _LIB, _LIB_PATH
    if _LIB is not None:
        return _LIB
    path = library_path()
    if not path.exists():
        raise FileNotFoundError(f"未找到 WebRTC APM 动态库: {path}")
    _LIB_PATH = str(path)
    _LIB = ctypes.CDLL(_LIB_PATH)
    _init_function_signatures(_LIB)
    return _LIB


class WebRTCAudioProcessing:
    """APM 实例封装，生命周期由调用方管理."""

    def __init__(self) -> None:
        lib = ensure_library_loaded()
        self._lib = lib
        self._handle = lib.WebRTC_APM_Create()
        if not self._handle:
            raise RuntimeError("WebRTC_APM_Create 失败")
        self._destroyed = False

    def __del__(self) -> None:
        self.close()

    def create_stream_config(self, sample_rate: int, num_channels: int) -> int:
        handle = self._lib.WebRTC_APM_CreateStreamConfig(sample_rate, num_channels)
        if not handle:
            raise RuntimeError("创建流配置失败")
        return handle

    def destroy_stream_config(self, config_handle: int) -> None:
        self._lib.WebRTC_APM_DestroyStreamConfig(config_handle)

    def apply_config(self, config: Config) -> int:
        return self._lib.WebRTC_APM_ApplyConfig(self._handle, ctypes.byref(config))

    def process_reverse_stream(self, src, src_config, dest_config, dest) -> int:
        return self._lib.WebRTC_APM_ProcessReverseStream(
            self._handle, src, src_config, dest_config, dest
        )

    def process_stream(self, src, src_config, dest_config, dest) -> int:
        return self._lib.WebRTC_APM_ProcessStream(
            self._handle, src, src_config, dest_config, dest
        )

    def set_stream_delay_ms(self, delay_ms: int) -> None:
        self._lib.WebRTC_APM_SetStreamDelayMs(self._handle, int(delay_ms))

    def close(self) -> None:
        if self._destroyed or not getattr(self, "_handle", None):
            return
        self._destroyed = True
        try:
            self._lib.WebRTC_APM_Destroy(self._handle)
        except Exception:
            pass
        self._handle = None


def create_default_config() -> Config:
    """默认配置，与上游封装保持一致."""
    config = Config()

    config.pipeline_config.maximum_internal_processing_rate = 48000
    config.pipeline_config.multi_channel_render = False
    config.pipeline_config.multi_channel_capture = False
    config.pipeline_config.capture_downmix_method = DownmixMethod.AVERAGE_CHANNELS

    config.pre_amp.enabled = False
    config.pre_amp.fixed_gain_factor = 1.0

    config.level_adjustment.enabled = False
    config.level_adjustment.pre_gain_factor = 1.0
    config.level_adjustment.post_gain_factor = 1.0
    config.level_adjustment.mic_gain_emulation.enabled = False
    config.level_adjustment.mic_gain_emulation.initial_level = 255

    config.high_pass.enabled = False
    config.high_pass.apply_in_full_band = True

    config.echo.enabled = False
    config.echo.mobile_mode = False
    config.echo.export_linear_aec_output = False
    config.echo.enforce_high_pass_filtering = True

    config.noise_suppress.enabled = False
    config.noise_suppress.noise_level = NoiseSuppressionLevel.MODERATE
    config.noise_suppress.analyze_linear_aec_output_when_available = False

    config.transient_suppress.enabled = False

    config.gain_control1.enabled = False
    config.gain_control1.controller_mode = GainController1Mode.ADAPTIVE_ANALOG
    config.gain_control1.target_level_dbfs = 3
    config.gain_control1.compression_gain_db = 9
    config.gain_control1.enable_limiter = True

    config.gain_control1.analog_controller.enabled = True
    config.gain_control1.analog_controller.startup_min_volume = 0
    config.gain_control1.analog_controller.clipped_level_min = 70
    config.gain_control1.analog_controller.enable_digital_adaptive = True
    config.gain_control1.analog_controller.clipped_level_step = 15
    config.gain_control1.analog_controller.clipped_ratio_threshold = 0.1
    config.gain_control1.analog_controller.clipped_wait_frames = 300

    config.gain_control1.analog_controller.predictor.enabled = False
    config.gain_control1.analog_controller.predictor.predictor_mode = (
        ClippingPredictorMode.CLIPPING_EVENT_PREDICTION
    )
    config.gain_control1.analog_controller.predictor.window_length = 5
    config.gain_control1.analog_controller.predictor.reference_window_length = 5
    config.gain_control1.analog_controller.predictor.reference_window_delay = 5
    config.gain_control1.analog_controller.predictor.clipping_threshold = -1.0
    config.gain_control1.analog_controller.predictor.crest_factor_margin = 3.0
    config.gain_control1.analog_controller.predictor.use_predicted_step = True

    config.gain_control2.enabled = False
    config.gain_control2.volume_controller.enabled = False
    config.gain_control2.adaptive_controller.enabled = False
    config.gain_control2.adaptive_controller.headroom_db = 5.0
    config.gain_control2.adaptive_controller.max_gain_db = 50.0
    config.gain_control2.adaptive_controller.initial_gain_db = 15.0
    config.gain_control2.adaptive_controller.max_gain_change_db_per_second = 6.0
    config.gain_control2.adaptive_controller.max_output_noise_level_dbfs = -50.0
    config.gain_control2.fixed_controller.gain_db = 0.0
    return config


def check_library() -> Tuple[bool, str]:
    """自检：加载库 → 建实例 → 应用 AEC 配置 → 跑一帧，并检查 mobile AEC 是否可用."""
    try:
        import numpy as np

        apm = WebRTCAudioProcessing()
        config = create_default_config()
        config.echo.enabled = True
        ret = apm.apply_config(config)
        if ret != 0:
            return False, f"apply_config 返回 {ret}"
        cfg = apm.create_stream_config(16000, 1)
        apm.set_stream_delay_ms(50)
        frame = 160
        src = (ctypes.c_short * frame)()
        dst = (ctypes.c_short * frame)()
        r1 = apm.process_reverse_stream(src, cfg, cfg, dst)
        r2 = apm.process_stream(src, cfg, cfg, dst)
        apm.destroy_stream_config(cfg)
        apm.close()
        if r1 or r2:
            return False, f"process 返回 {r1}/{r2}"
        return True, "ok"
    except Exception as exc:  # pragma: no cover - 依赖本机动态库
        return False, str(exc)


_MOBILE_SUPPORT: Optional[bool] = None


def mobile_mode_supported() -> bool:
    """探测当前动态库是否支持 mobile AEC（部分构建上会返回 -11/参数非法）.

    探测结果缓存，避免每次运行都花 3 帧的成本。
    """
    global _MOBILE_SUPPORT
    if _MOBILE_SUPPORT is not None:
        return _MOBILE_SUPPORT
    try:
        apm_obj = WebRTCAudioProcessing()
        config = create_default_config()
        config.echo.enabled = True
        config.echo.mobile_mode = True
        if apm_obj.apply_config(config) != 0:
            _MOBILE_SUPPORT = False
            return False
        cfg = apm_obj.create_stream_config(16000, 1)
        frame = 160
        src = (ctypes.c_short * frame)()
        dst = (ctypes.c_short * frame)()
        ok = True
        for _ in range(5):
            if apm_obj.process_stream(src, cfg, cfg, dst) != 0:
                ok = False
                break
        apm_obj.destroy_stream_config(cfg)
        apm_obj.close()
        _MOBILE_SUPPORT = ok
    except Exception:
        _MOBILE_SUPPORT = False
    return bool(_MOBILE_SUPPORT)
