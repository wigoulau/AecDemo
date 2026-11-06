"""统一配置：采样率常量、帧长换算、档位预设."""

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
LIBS_DIR = ROOT / "libs"
OUTPUT_DIR = ROOT / "output"
CACHE_DIR = ROOT / "cache"
STATIC_DIR = ROOT / "webui"

# WebRTC APM 支持的采样率（必须是 10ms 帧的整数倍）
SUPPORTED_RATES = (8000, 16000, 32000, 48000)
# 设备流可以开在 44100（WASAPI 共享模式下虚拟声卡的常见原生率），
# 进 AEC 之前会重采样到最近的 APM 采样率。
DEVICE_RATES = (8000, 16000, 32000, 44100, 48000)
DEFAULT_RATE = 16000
FRAME_MS = 10


def frame_samples(rate: int) -> int:
    """10ms 对应的样本数，APM 只接受这个粒度."""
    return int(rate * FRAME_MS / 1000)


def supported_channels(rate: int) -> int:
    return 2 if rate == 48000 else 1


def nearest_apm_rate(rate: int) -> int:
    """把任意采样率映射到 APM 支持的最近采样率（44100 → 48000）."""
    rate = int(rate)
    if rate in SUPPORTED_RATES:
        return rate
    return min(SUPPORTED_RATES, key=lambda r: abs(r - rate))


PRESETS: Dict[str, Dict[str, Any]] = {
    # 关闭所有处理：观察原始回采信号，作为基线
    "off": {
        "name": "全部关闭（基线）",
        "echo": False,
        "preset": "off",
    },
    # 语音助手类应用的典型线上配置：
    # AEC + 高通 + 噪声抑制 MODERATE
    "voice_assistant": {
        "name": "语音助手常用（AEC+NS+高通）",
        "echo": True,
        "preprocess": True,
        "ns_level": 1,
    },
    # 只做 AEC，方便判断残余回声是否来自 NS
    "aec_only": {
        "name": "仅 AEC",
        "echo": True,
        "preprocess": False,
    },
    # 桌面端 WebRTC 常用组合
    "desktop": {
        "name": "Issues 调试 / 桌面端常用",
        "echo": True,
        "mobile_mode": False,
        "ns_level": 2,
        "highpass": True,
        "agc1": True,
        "agc1_mode": 1,
        "agc1_target_dbfs": 3,
        "agc1_compression_db": 9,
    },
    # 手机/嵌入式常用：mobile AEC 更省 CPU，但对非线性失真容错差
    "mobile": {
        "name": "移动端模式",
        "echo": True,
        "mobile_mode": True,
        "preprocess": True,
        "ns_level": 2,
    },
}


@dataclass
class AecSettings:
    """一次处理的全部可调参数."""

    rate: int = DEFAULT_RATE
    preset: str = "voice_assistant"
    # 回声消除
    echo: bool = True
    mobile_mode: bool = False
    enforce_highpass: bool = True
    export_linear_aec: bool = False
    # 前级
    highpass: bool = True
    ns_enabled: bool = True
    ns_level: int = 1
    transient: bool = False
    # AGC1
    agc1: bool = False
    agc1_mode: int = 1  # 0 自适应模拟 1 自适应数字 2 固定数字
    agc1_target_dbfs: int = 3
    agc1_compression_db: int = 9
    agc1_limiter: bool = True
    # AGC2
    agc2: bool = False
    # 延迟
    delay_ms: int = 60
    delay_auto: bool = False
    delay_search_max: int = 500

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SimulateSettings:
    """离线仿真参数（构造 mic/ref 两路信号）."""

    rate: int = DEFAULT_RATE
    duration_s: float = 6.0
    ref_source: str = "speech"  # speech | chirp | sweep | file | silence
    ref_file: str = ""
    mic_source: str = "speech"  # tone | speech | noise | file | silence
    mic_file: str = ""
    snr_db: float = 20.0
    speech_scale: float = 0.35
    noise_level: float = 0.004
    echo_delay_ms: int = 50
    echo_gain: float = 0.6
    echo_gain2: float = 0.15
    room_rt60: float = 0.15
    nonlinear: float = 0.15
    noise_gate: bool = True
    far_clipping: float = 0.9
    baseline_ms: int = 300
    tail_ms: int = 300

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RunOptions:
    """一次运行的附加控制."""

    save_aec_in: bool = True
    suppress_mic_during_far: bool = False
    reset_between_trials: bool = True
    label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
