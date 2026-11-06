"""任务运行器：离线仿真 / 输入音频文件 / 设备实测，统一进度与产物管理."""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from .aec import AecCore, estimate_delay_ms
from .analysis import compute_metrics, make_plot, save_metrics
from .audioio import read_audio, read_audio_multi, resample, write_wav
from .config import (
    AecSettings,
    OUTPUT_DIR,
    PRESETS,
    RunOptions,
    SimulateSettings,
    frame_samples,
    nearest_apm_rate,
)
from .simulate import ref_audio_for_device, simulate


@dataclass
class Job:
    id: str
    kind: str
    title: str
    status: str = "pending"  # pending running done error cancelled
    progress: float = 0.0
    stage: str = "排队中"
    logs: List[str] = field(default_factory=list)
    result: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    cancel: bool = False

    def log(self, msg: str) -> None:
        self.logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(self.logs) > 400:
            del self.logs[:100]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("cancel", None)
        return d


class JobManager:
    """单线程串行执行：同一时刻只跑一个采集/处理任务，避免抢设备."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._queue: List[str] = []
        self._runners: Dict[str, Callable[[Job], None]] = {}

    def submit(self, kind: str, title: str, fn: Callable[[Job], None]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, title=title)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._queue.append(job.id)
            self._runners[job.id] = fn
            if len(self._order) > 60:
                old = self._order[:-60]
                self._order = self._order[-60:]
                for oid in old:
                    self._jobs.pop(oid, None)
                    self._runners.pop(oid, None)
        self._ensure_worker()
        return job

    def _ensure_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    return
                jid = self._queue.pop(0)
                job = self._jobs.get(jid)
                fn = self._runners.get(jid)
            if job is None or fn is None:
                continue
            if job.cancel:
                job.status = "cancelled"
                continue
            job.status = "running"
            job.started_at = time.time()
            try:
                fn(job)
                if job.cancel:
                    job.status = "cancelled"
                    job.stage = "已取消"
                else:
                    job.status = "done"
                    job.progress = 1.0
                    job.stage = "完成"
            except Exception as exc:  # noqa: BLE001 - 任务内异常统一回传前端
                job.status = "error"
                job.error = f"{exc}"
                job.stage = "失败"
                job.log(f"错误: {exc}")
                job.log(traceback.format_exc(limit=6))
            finally:
                job.finished_at = time.time()
                with self._lock:
                    self._runners.pop(jid, None)

    def get(self, jid: str) -> Optional[Job]:
        return self._jobs.get(jid)

    def cancel(self, jid: str) -> bool:
        job = self._jobs.get(jid)
        if not job:
            return False
        job.cancel = True
        if job.status == "pending":
            job.status = "cancelled"
        return True

    def list(self) -> List[Dict[str, Any]]:
        out = []
        for jid in reversed(self._order):
            job = self._jobs.get(jid)
            if job:
                d = job.to_dict()
                d.pop("logs", None)
                out.append(d)
        return out


JOBS = JobManager()


# --------------------------------------------------------------------------
# 产物
# --------------------------------------------------------------------------
def run_dir(job: Job) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(job.created_at))
    d = OUTPUT_DIR / f"{stamp}_{job.id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _artifact(path: Path, label: str, desc: str, kind: str = "wav") -> Dict[str, Any]:
    return {
        "name": path.name,
        "url": f"/files/{path.parent.name}/{path.name}",
        "label": label,
        "desc": desc,
        "kind": kind,
        "size": path.stat().st_size if path.exists() else 0,
    }


def _save_outputs(
    out_dir: Path,
    rate: int,
    mic: np.ndarray,
    ref: np.ndarray,
    out: np.ndarray,
    options: RunOptions,
    near_truth: Optional[np.ndarray] = None,
    extra: Optional[Dict[str, np.ndarray]] = None,
) -> List[Dict[str, Any]]:
    """按需求落盘：aec 前 mic+ref 合一个文件；aec 后单独文件."""
    arts: List[Dict[str, Any]] = []
    n = min(len(mic), len(ref), len(out))
    mic, ref, out = mic[:n], ref[:n], out[:n]

    if options.save_aec_in:
        stereo = np.stack([mic, ref], axis=1)
        p = out_dir / "aec_in.wav"
        write_wav(p, stereo, rate)
        arts.append(_artifact(p, "AEC 输入（L=mic / R=ref）", "进入 AEC 前的两路原始信号，同一时间轴"))

    p_mic = out_dir / "mic.wav"
    write_wav(p_mic, mic, rate)
    arts.append(_artifact(p_mic, "mic 原始", "AEC 前的麦克风采集（含回声+近端人声+噪声）"))

    p_ref = out_dir / "ref.wav"
    write_wav(p_ref, ref, rate)
    arts.append(_artifact(p_ref, "ref 参考", "AEC 前的扬声器参考信号（far-end）"))

    p_out = out_dir / "aec_out.wav"
    write_wav(p_out, out, rate)
    arts.append(_artifact(p_out, "AEC 输出", "回声消除后的上行信号"))

    if near_truth is not None and len(near_truth) >= n:
        p_near = out_dir / "near_truth.wav"
        write_wav(p_near, near_truth[:n], rate)
        arts.append(_artifact(p_near, "近端真值（仿真）", "仿真中的人声真值，用于计算近端保留率"))

    for name, data in (extra or {}).items():
        p = out_dir / f"{name}.wav"
        write_wav(p, data, rate)
        arts.append(_artifact(p, name, "附加信号"))
    return arts


def _stats_dict(core: AecCore) -> Dict[str, Any]:
    d = asdict(core.stats)
    d["realtime_factor"] = round(core.stats.realtime_factor, 6)
    d["proc_time_s"] = round(core.stats.proc_time_s, 4)
    d["mobile_downgraded"] = core.mobile_downgraded
    d["warnings"] = list(core.errors)
    return d


def _log_warnings(job: Job, stats: Dict[str, Any]) -> None:
    """把引擎的降级/兼容性提示写进任务日志，前端可见."""
    for w in stats.get("warnings") or []:
        job.log(w)


def _add_plot_and_metrics(
    out_dir: Path, rate: int, ref, mic, out, metrics, segments, title: str, near_truth=None
) -> List[Dict[str, Any]]:
    arts = []
    mp = out_dir / "metrics.json"
    save_metrics(mp, metrics)
    arts.append(_artifact(mp, "指标 JSON", "分段 ERLE / 延迟 / 电平", kind="json"))
    ip = out_dir / "plot.png"
    if make_plot(ip, rate, ref, mic, out, segments, metrics, title, near_truth):
        arts.append(_artifact(ip, "波形与 ERLE 图", "上三路波形 + 逐 50ms ERLE", kind="png"))
    return arts


# --------------------------------------------------------------------------
# 离线仿真
# --------------------------------------------------------------------------
def run_simulation(
    job: Job, sim: SimulateSettings, aec: AecSettings, options: RunOptions
) -> None:
    out_dir = run_dir(job)
    job.stage = "构造仿真信号"
    job.progress = 0.05
    job.log(f"仿真: {sim.duration_s:.1f}s @ {sim.rate}Hz, 回声延迟 {sim.echo_delay_ms}ms, "
            f"增益 {sim.echo_gain:.2f}, RT60 {sim.room_rt60:.2f}s, 非线性 {sim.nonlinear:.2f}")
    simres = simulate(sim)
    rate = simres.rate
    job.progress = 0.2

    job.stage = "估计回声延迟"
    delay_ms = float(aec.delay_ms)
    auto_note = ""
    if aec.delay_auto:
        est = estimate_delay_ms(simres.mic, simres.far, rate, aec.delay_search_max)
        delay_ms = est
        auto_note = f"（自动估计，搜索上限 {aec.delay_search_max}ms）"
        job.log(f"自动延迟估计: {est:.0f} ms{auto_note}")
    else:
        job.log(f"使用固定延迟: {delay_ms:.0f} ms")

    job.stage = "运行 WebRTC APM"
    job.progress = 0.3
    settings = AecSettings(**{**aec.to_dict(), "rate": rate, "delay_ms": int(round(delay_ms))})
    with AecCore(settings, input_rate=rate) as core:
        out = core.process_arrays(simres.mic, simres.far, input_rate=rate)
        stats = _stats_dict(core)
        _log_warnings(job, stats)

    job.progress = 0.7
    n = min(len(out), len(simres.mic))
    out, mic = out[:n], simres.mic[:n]
    ref, near = simres.far[:n], simres.near_speech[:n]

    job.stage = "计算指标"
    metrics = compute_metrics(rate, ref, mic, out, near, simres.segments, delay_ms=delay_ms)
    metrics["aec_stats"] = stats
    metrics["config"] = {"simulate": sim.to_dict(), "aec": settings.to_dict()}

    job.stage = "生成产物"
    job.progress = 0.85
    arts = _save_outputs(out_dir, rate, mic, ref, out, options, near, {
        "echo_only": simres.echo[:n],
        "noise_only": simres.noise[:n],
    })
    arts += _add_plot_and_metrics(
        out_dir, rate, ref, mic, out, metrics, simres.segments,
        f"[仿真] {PRESETS.get(aec.preset, {}).get('name', aec.preset)}", near,
    )
    job.result = {
        "dir": out_dir.name,
        "artifacts": arts,
        "metrics": metrics,
        "segments": simres.segments,
        "duration_s": simres.duration_s,
        "delay_ms": delay_ms,
        "preset_name": PRESETS.get(aec.preset, {}).get("name", aec.preset),
        "mode": "simulation",
    }
    job.log(f"完成：ERLE(echo-only)={metrics['summary']['erle_db']} dB, "
            f"双讲={metrics['summary']['double_talk_erle_db']} dB, "
            f"近端保留={metrics['summary']['near_retention_db']} dB, "
            f"实时因子={stats['realtime_factor']:.4f}")


# --------------------------------------------------------------------------
# 输入音频文件
# --------------------------------------------------------------------------
def run_files(
    job: Job,
    mic_path: str,
    ref_path: str,
    aec: AecSettings,
    options: RunOptions,
    mic_rate: Optional[int] = None,
    ref_rate: Optional[int] = None,
) -> None:
    out_dir = run_dir(job)
    job.stage = "读取音频"
    job.progress = 0.05
    rate = int(aec.rate)
    job.log(f"mic: {mic_path}")
    job.log(f"ref: {ref_path}")
    mic = read_audio(mic_path, rate)
    ref = read_audio(ref_path, rate)
    _run_files_core(job, out_dir, rate, mic, ref, aec, options,
                    src_desc=f"{Path(mic_path).name} + {Path(ref_path).name}")


def run_files_stereo(
    job: Job,
    path: str,
    aec: AecSettings,
    options: RunOptions,
    mic_channel: int = 0,
    ref_channel: int = 1,
) -> None:
    """单文件模式：一个多声道文件，按声道拆出 mic / ref.

    `aec_in.wav` 那种 L=mic / R=ref 的合轨文件可以直接喂进来。
    """
    out_dir = run_dir(job)
    job.stage = "读取音频"
    job.progress = 0.05
    rate = int(aec.rate)
    job.log(f"文件: {path}（第 {mic_channel + 1} 声道 = mic，第 {ref_channel + 1} 声道 = ref）")

    data = read_audio_multi(path, rate)
    channels = 1 if data.ndim == 1 else data.shape[1]
    job.log(f"检测到 {channels} 个声道")
    if channels < 2:
        raise ValueError(
            f"该文件只有 {channels} 个声道，无法同时提供 mic 与 ref。"
            "请用双声道文件（如 L=mic / R=ref），或改用「两个文件」模式。"
        )
    for name, idx in (("mic", mic_channel), ("ref", ref_channel)):
        if not 0 <= idx < channels:
            raise ValueError(f"{name} 声道索引 {idx} 超出范围（文件共 {channels} 声道）")
    if mic_channel == ref_channel:
        raise ValueError("mic 与 ref 不能是同一个声道，否则 AEC 拿到的两路信号相同")

    mic = np.ascontiguousarray(data[:, mic_channel], dtype=np.float32)
    ref = np.ascontiguousarray(data[:, ref_channel], dtype=np.float32)
    name = Path(path).name
    _run_files_core(job, out_dir, rate, mic, ref, aec, options,
                    src_desc=f"{name} [ch{mic_channel + 1}=mic / ch{ref_channel + 1}=ref]")


def _run_files_core(
    job: Job,
    out_dir: Path,
    rate: int,
    mic: np.ndarray,
    ref: np.ndarray,
    aec: AecSettings,
    options: RunOptions,
    src_desc: str,
) -> None:
    """文件模式的公共后半段：对齐 → 延迟 → AEC → 指标 → 产物."""
    job.log(f"时长: mic={len(mic)/rate*1000:.0f}ms, ref={len(ref)/rate*1000:.0f}ms")
    n = min(len(mic), len(ref))
    if n < frame_samples(rate) * 10:
        raise ValueError("音频过短（不足 100ms），无法评估 AEC")
    if abs(len(mic) - len(ref)) > rate * 0.05:
        job.log("提示：两路长度差异较大，按较短者对齐裁剪；如来源不是同一时刻请先对齐")
    mic, ref = mic[:n], ref[:n]
    job.progress = 0.25

    job.stage = "估计延迟"
    delay_ms = float(aec.delay_ms)
    if aec.delay_auto:
        delay_ms = estimate_delay_ms(mic, ref, rate, aec.delay_search_max)
        job.log(f"自动延迟估计: {delay_ms:.0f} ms")

    job.stage = "运行 WebRTC APM"
    job.progress = 0.35
    settings = AecSettings(**{**aec.to_dict(), "rate": rate, "delay_ms": int(round(delay_ms))})
    with AecCore(settings, input_rate=rate) as core:
        out = core.process_arrays(mic, ref, input_rate=rate)
        stats = _stats_dict(core)
        _log_warnings(job, stats)
    job.progress = 0.7

    out = out[:n]
    segments = {"all": (0.0, n / rate)}

    # 文件模式下没有真值：用 ref/out 的能量比 + 可选空白段推断
    job.stage = "计算指标"
    metrics = compute_metrics(rate, ref, mic, out, None, segments, delay_ms=delay_ms)
    mic_db, out_db = metrics["segments"]["all"]["mic_dbfs"], metrics["segments"]["all"]["out_dbfs"]
    metrics["summary"]["level_change_db"] = round(out_db - mic_db, 2)
    metrics["aec_stats"] = stats
    metrics["config"] = {"source": src_desc, "aec": settings.to_dict()}
    job.log("提示：文件模式无真值，ERLE 为 mic→out 的整体抑制量；"
            "若 mic 中含人声，ERLE 会被拉低")

    job.stage = "生成产物"
    job.progress = 0.85
    arts = _save_outputs(out_dir, rate, mic, ref, out, options)
    arts += _add_plot_and_metrics(
        out_dir, rate, ref, mic, out, metrics, segments,
        f"[文件] {src_desc}", None,
    )
    job.result = {
        "dir": out_dir.name,
        "artifacts": arts,
        "metrics": metrics,
        "segments": segments,
        "duration_s": n / rate,
        "delay_ms": delay_ms,
        "mode": "files",
    }
    job.log(f"完成：ERLE={metrics['summary']['erle_db']} dB, 实时因子={stats['realtime_factor']:.4f}")


# --------------------------------------------------------------------------
# 设备实测（会占用麦克风，扬声器仅在显式允许时打开）
# --------------------------------------------------------------------------
def _resolve_index(sd, device, side: str) -> Optional[int]:
    """把 None（=系统默认）解析成具体设备索引；side 为 'in' 或 'out'."""
    idx = device
    if idx is None:
        try:
            idx = sd.default.device[0] if side == "in" else sd.default.device[1]
        except Exception:
            return None
    try:
        idx = int(idx)
    except Exception:
        return None
    return idx if idx >= 0 else None


def _device_key(sd, idx) -> Optional[str]:
    """设备的稳定标识「后端名|设备名」. PortAudio 的数字索引会随插拔漂移."""
    try:
        d = sd.query_devices(int(idx))
        api = sd.query_hostapis(int(d["hostapi"]))["name"]
        return f"{api}|{d['name']}"
    except Exception:
        return None


def _resolve_device(sd, index, key: Optional[str], side: str) -> Optional[int]:
    """在当前设备枚举里重新定位设备：优先用 key，其次退回数字索引.

    前端页面打开后若插拔过设备，PortAudio 会重新编号，旧的数字索引可能指向
    另一台（甚至另一个后端的）设备——之前 WDM-KS 的 `WdmSyncIoctl` 报错
    就是这么来的。
    """
    try:
        all_devs = sd.query_devices()
    except Exception:
        return int(index) if index is not None else None
    want_in = side == "in"

    def usable(d):
        return d["max_input_channels"] > 0 if want_in else d["max_output_channels"] > 0

    if key:
        for i, d in enumerate(all_devs):
            if not usable(d):
                continue
            if _device_key(sd, i) == key:
                return i
    if index is not None:
        try:
            i = int(index)
            if 0 <= i < len(all_devs) and usable(all_devs[i]):
                return i
        except Exception:
            pass
    return None


def _hostapi_name(sd, idx) -> str:
    try:
        return sd.query_hostapis(int(sd.query_devices(int(idx))["hostapi"]))["name"]
    except Exception:
        return ""


def _norm_dev_name(name: str) -> str:
    """设备名归一化：去掉驱动路径 / 转义符，只留可比对的硬件型号."""
    s = str(name)
    # `耳机 (@System32\drivers\bthhfenum.sys,#2;%1 Hands-Free AG Audio%0;(TWS-Q6.))`
    s = re.sub(r"@[^;]*;", "", s)
    s = re.sub(r"%\d+", "", s)
    s = s.replace("\\", " ")
    s = " ".join(s.split())
    groups = re.findall(r"\(([^()]*)\)", s)
    head = s.split("(", 1)[0].strip()
    if head and groups:
        tail = groups[-1].strip().rstrip(".").strip()
        return f"{head} ({tail})" if tail else head
    return s


def _alt_backend_indices(sd, idx, side: str) -> List[int]:
    """同一硬件在其它后端下的索引（WDM-KS 在 Windows 上很不稳，需要备选）."""
    if idx is None:
        return []
    try:
        raw = sd.query_devices(int(idx))
        name = raw["name"]
        norm = _norm_dev_name(name)
        cur_api = _hostapi_name(sd, idx)
    except Exception:
        return []
    want_in = side == "in"
    out = []
    for i, d in enumerate(sd.query_devices()):
        if i == idx:
            continue
        if want_in and d["max_input_channels"] <= 0:
            continue
        if not want_in and d["max_output_channels"] <= 0:
            continue
        same = d["name"] == name or _norm_dev_name(d["name"]) == norm
        if not same:
            # WDM-KS 的蓝牙名带驱动路径，用「末段型号」再兜一层
            tail = re.findall(r"\(([^()]*)\)", str(d["name"]))
            core = tail[-1].strip(" .;") if tail else ""
            if not core or not norm.endswith(f"({core})"):
                continue
        api = _hostapi_name(sd, i)
        if api == cur_api or "wdm-ks" in api.lower():
            continue
        out.append(i)
    return out


def _dev_label(sd, idx) -> str:
    if idx is None:
        return ""
    try:
        d = sd.query_devices(int(idx))
        return f"{int(idx)}:{d['name']}"
    except Exception:
        return str(idx)


def _no_audio_hint(sd, input_device, output_device, rate) -> str:
    """流能打开却一帧数据都没有时，给出可操作的排查方向."""
    parts = [f"没有采集到音频（{rate}Hz）。"]
    for side, dev in (("输入", input_device), ("输出", output_device)):
        api = _hostapi_name(sd, dev) if dev is not None else ""
        if "wdm-ks" in api.lower():
            parts.append(f"{side}设备用的是 WDM-KS 后端（{_dev_label(sd, dev)}），"
                         f"该后端在 Windows 上经常能打开流但不来数据。"
                         f"请在设备下拉里改用同名设备的 WASAPI 条目。")
    if len(parts) == 1:
        parts.append("请确认设备未被其它程序独占、麦克风权限已开启，或换一台设备重试。")
    return "".join(parts)


def _short_err(exc: Optional[Exception], limit: int = 110) -> str:
    if exc is None:
        return "未知错误"
    msg = " ".join(str(exc).split())
    return msg if len(msg) <= limit else msg[: limit - 1] + "…"


def _stream_attempts(sd, input_device, output_device, rate, extra):
    """按成功率从高到低生成候选方案 (in, out, rate, extra, 说明).

    顺序：
    1. 用户选择（先用稳定的设备名重新定位，索引可能已漂移）
    2. 同一硬件换到非 WDM-KS 后端（WDM-KS 在 Windows 上 start() 常失败）
    3. 设备原生采样率
    4. 换后端 + 原生采样率
    """
    rin = _resolve_device(sd, input_device, None, "in")
    rout = _resolve_device(sd, output_device, None, "out")
    attempts = []

    def add(i, o, r, ex, why):
        attempts.append((i, o, r, ex, list(why)))

    add(rin, rout, rate, extra, [])
    native = _native_rate(sd, rout if rout is not None else rin, rate)

    # 后端替换：只针对当前指向 WDM-KS 的那一侧
    alt_in = _alt_backend_indices(sd, rin, "in") if "wdm-ks" in _hostapi_name(sd, rin).lower() else []
    alt_out = _alt_backend_indices(sd, rout, "out") if "wdm-ks" in _hostapi_name(sd, rout).lower() else []
    for ai in (alt_in or [rin]):
        for ao in (alt_out or [rout]):
            if (ai, ao) == (rin, rout):
                continue
            why = []
            if ai != rin:
                why.append(f"输入换到 {_hostapi_name(sd, ai)}")
            if ao != rout:
                why.append(f"输出换到 {_hostapi_name(sd, ao)}")
            add(ai, ao, rate, _wasapi_settings(sd, ai, ao, rate), why)

    if native and native != rate:
        add(rin, rout, native, _wasapi_settings(sd, rin, rout, native),
            [f"改用设备原生采样率 {native}Hz"])
        for ai in (alt_in or [rin]):
            for ao in (alt_out or [rout]):
                if (ai, ao) == (rin, rout):
                    continue
                add(ai, ao, native, _wasapi_settings(sd, ai, ao, native),
                    [f"输入/输出换后端", f"原生采样率 {native}Hz"])

    # 去重，保持顺序
    seen, out = set(), []
    for a in attempts:
        k = (a[0], a[1], a[2])
        if k in seen:
            continue
        seen.add(k)
        out.append(a)
    return out


def _uses_wasapi(sd, device, side: str = "in") -> bool:
    """指定设备（None 表示该系统侧的默认设备）是否走 WASAPI 后端."""
    try:
        idx = _resolve_index(sd, device, side)
        if idx is None:
            return False
        hostapi = int(sd.query_devices(idx)["hostapi"])
        return "wasapi" in sd.query_hostapis(hostapi)["name"].lower()
    except Exception:
        return False


def _wasapi_settings(sd, input_device=None, output_device=None, rate=None):
    """WASAPI 共享模式只接受设备原生采样率，且常只支持 2 声道.

    不开启 auto_convert 时，44.1k 虚拟声卡/16k 无法直接打开，会报
    `AUDCLNT_E_UNSUPPORTED_FORMAT`。开启后由 WASAPI 内部转换，任意采样率可用。
    输入/输出只要有一侧是 WASAPI 就返回配对设置；两侧都不是则返回 None。
    """
    in_wasapi = _uses_wasapi(sd, input_device, "in")
    out_wasapi = _uses_wasapi(sd, output_device, "out")
    if not (in_wasapi or out_wasapi):
        return None
    try:
        conv = sd.WasapiSettings(auto_convert=True)
    except Exception:
        return None
    return (conv if in_wasapi else None, conv if out_wasapi else None)


def _native_rate(sd, device, rate) -> Optional[int]:
    """设备的原生采样率候选（用于格式不支持时的降级重试），排除已试过的."""
    try:
        cands = []
        if device is not None:
            cands.append(int(round(float(sd.query_devices(int(device))["default_samplerate"]))))
        else:
            for idx in sd.default.device:
                if idx is not None and int(idx) >= 0:
                    cands.append(int(round(float(sd.query_devices(int(idx))["default_samplerate"]))))
        for r in cands:
            if r != rate:
                return r
    except Exception:
        pass
    return {48000: 44100, 44100: 48000}.get(rate)


def _check_stream_format(sd, rate: int, input_device, output_device,
                         input_channels: int, extra=None) -> bool:
    """只查能力不打开流：这对设备能否以 rate + extra 同时收发."""
    try:
        sd.check_input_settings(device=input_device, channels=max(1, int(input_channels)),
                                samplerate=rate, dtype="float32",
                                extra_settings=_split_extra(extra, "in"))
        sd.check_output_settings(device=output_device, channels=1,
                                 samplerate=rate, dtype="float32",
                                 extra_settings=_split_extra(extra, "out"))
        return True
    except Exception:
        return False


def _split_extra(extra, side: str):
    if extra is None:
        return None
    if isinstance(extra, tuple):
        return extra[0] if side == "in" else extra[1]
    return None


def _probe_stream_rate(sd, rate: int, input_device, output_device,
                       input_channels: int) -> Tuple[int, object]:
    """选出「输入+输出都能打开」的采样率与 WASAPI 设置.

    WASAPI 共享模式只接受设备原生采样率：默认输出若是 44.1k 虚拟声卡，
    请求 48000 会报 `AUDCLNT_E_UNSUPPORTED_FORMAT`。这里用 PortAudio 的
    能力查询（不打开流、不出声）逐个验证：先试用户请求的采样率（含 WASAPI
    auto_convert 兜底），不行再退到设备原生采样率。
    """
    candidates = [int(rate)]
    for dev in (output_device, input_device):
        r = _native_rate(sd, dev, rate)
        if r and r not in candidates:
            candidates.append(int(r))
    for r in (48000, 44100, 16000, 32000):
        if r not in candidates:
            candidates.append(r)

    for cand in candidates:
        combos = [None]
        wasapi = _wasapi_settings(sd, input_device, output_device, cand)
        if wasapi is not None:
            combos.append(wasapi)
        for extra in combos:
            if _check_stream_format(sd, cand, input_device, output_device, input_channels, extra):
                return int(cand), extra
    # 全都查不出结果就按用户选的来，交给打开时的错误提示
    return int(rate), _wasapi_settings(sd, input_device, output_device, rate)


def _stream_error_hint(exc: Exception, rate: int, input_device, output_device) -> str:
    msg = str(exc)
    if "AUDCLNT_E_UNSUPPORTED_FORMAT" in msg or "Unsupported" in msg.title() or "-9997" in msg:
        return (f"设备不支持 {rate}Hz 采集/播放（WASAPI 共享模式要求设备原生采样率）。"
                f"请在界面把采样率改成设备默认值后重试；或改用 MME/DirectSound 设备。"
                f"原始错误: {msg}")
    return f"打开音频设备失败: {msg}"


def run_device(
    job: Job,
    aec: AecSettings,
    options: RunOptions,
    sim: SimulateSettings,
    input_device: Optional[int] = None,
    output_device: Optional[int] = None,
    device_rate: int = 48000,
    play: bool = False,
    duration_s: float = 6.0,
    play_gain: float = 1.0,
    input_channels: int = 1,
    input_device_key: Optional[str] = None,
    output_device_key: Optional[str] = None,
) -> None:
    try:
        import sounddevice as sd
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"sounddevice 不可用: {exc}") from exc

    out_dir = run_dir(job)
    if not play:
        raise RuntimeError("设备模式需要显式允许播放参考音（本次测试已关闭播放）")

    # 页面打开后若插拔过设备，数字索引可能已经指向别处，先用稳定标识重新定位
    rin = _resolve_device(sd, input_device, input_device_key, "in")
    rout = _resolve_device(sd, output_device, output_device_key, "out")
    if input_device is not None and rin is None:
        raise RuntimeError(f"找不到所选输入设备（索引 {input_device}"
                           + (f"，{input_device_key}" if input_device_key else "") +
                           "）。设备可能已被拔出，请点「刷新设备列表」重新选择。")
    if output_device is not None and rout is None:
        raise RuntimeError(f"找不到所选输出设备（索引 {output_device}"
                           + (f"，{output_device_key}" if output_device_key else "") +
                           "）。设备可能已被拔出，请点「刷新设备列表」重新选择。")
    input_device, output_device = rin, rout
    if input_device_key or output_device_key:
        job.log(f"设备定位：输入 {_dev_label(sd, input_device) or '默认'}，"
                f"输出 {_dev_label(sd, output_device) or '默认'}")

    # 先确认这对设备能打开哪个采样率（只查能力，不打开流），再据此准备参考音，
    # 否则会把整段信号按错误的采样率生成。
    job.stage = "检查设备采样率"
    req_rate = int(device_rate)
    rate, stream_extra = _probe_stream_rate(
        sd, req_rate, input_device, output_device, input_channels)
    if rate != req_rate:
        job.log(f"所选设备不支持 {req_rate}Hz，改用 {rate}Hz（设备的可用采样率）")

    job.stage = "准备参考音"
    job.progress = 0.03
    sim_ref = SimulateSettings(**{**sim.to_dict(), "rate": rate})
    try:
        ref_play = ref_audio_for_device(sim_ref) * float(play_gain)
    except Exception as exc:
        raise RuntimeError(f"参考音加载失败: {exc}") from exc
    ref_secs = len(ref_play) / rate
    is_file = sim_ref.ref_source == "file" and bool(sim_ref.ref_file)
    if is_file:
        # 播放用户文件时以文件长度为准；duration_s 只体现「至少录多久」
        play_secs = max(ref_secs, float(duration_s))
        if ref_secs < float(duration_s):
            ref_play = np.pad(ref_play, (0, int(rate * (float(duration_s) - ref_secs))))
        job.log(f"参考音为音频文件：{Path(sim_ref.ref_file).name}，"
                f"有效内容 {ref_secs:.1f}s，总播放 {play_secs:.1f}s")
    else:
        total = int(rate * max(duration_s, ref_secs))
        if len(ref_play) < total:
            ref_play = np.pad(ref_play, (0, total - len(ref_play)))
        ref_play = ref_play[:total]
        job.log(f"合成参考音 {total/rate:.1f}s @ {rate}Hz（{sim_ref.ref_source}）")
    ref_play = ref_play.astype(np.float32)
    job.log(f"扬声器增益 {play_gain:.2f}")

    # 静音领头，便于看到纯底噪
    lead = int(rate * 0.3)
    ref_play = np.concatenate([np.zeros(lead, dtype=np.float32), ref_play])
    tail = int(rate * 0.4)
    ref_play = np.concatenate([ref_play, np.zeros(tail, dtype=np.float32)])
    total = len(ref_play)

    rec_mic: List[np.ndarray] = []
    played: List[np.ndarray] = []
    pos = 0
    step = frame_samples(rate)
    lock = threading.Lock()

    def callback(indata, outdata, frames, time_info, status):  # noqa: ANN001
        nonlocal pos
        if status:
            job.log(f"音频状态: {status}")
        with lock:
            mic = indata.mean(axis=1) if indata.ndim > 1 and indata.shape[1] > 1 else indata[:, 0]
            rec_mic.append(np.array(mic, dtype=np.float32))
            start = pos
            end = min(total, start + frames)
            chunk = ref_play[start:end]
            outdata[: len(chunk), 0] = chunk
            if len(chunk) < frames:
                outdata[len(chunk):, 0] = 0.0
            played.append(np.array(outdata[:frames, 0], dtype=np.float32))
            pos = end

    job.stage = "打开音频设备"
    blocksize = step
    extra = stream_extra
    in_name = _dev_label(sd, input_device) or "默认"
    out_name = _dev_label(sd, output_device) or "默认"
    job.log(f"输入设备={in_name}, 输出设备={out_name}, "
            f"块大小={blocksize} ({blocksize/rate*1000:.1f}ms)"
            + ("，WASAPI 自动重采样已开启" if extra else ""))

    # PortAudio 对 WDM-KS 后端经常在 start() 时才失败（WdmSyncIoctl / GLE 0x490），
    # 而且这些错误在构造 Stream 时不会出现，所以必须把「构造 + start」整体纳入重试。
    tried = []
    stream = None
    last_exc: Optional[Exception] = None
    for cand_in, cand_out, cand_rate, cand_extra, why in _stream_attempts(
            sd, input_device, output_device, rate, extra):
        tag = (cand_in, cand_out, cand_rate)
        if tag in tried:
            continue
        tried.append(tag)
        try:
            cand_stream = sd.Stream(
                samplerate=cand_rate,
                blocksize=frame_samples(cand_rate),
                dtype="float32",
                channels=(max(1, int(input_channels)), 1),
                device=(cand_in, cand_out),
                callback=callback,
                extra_settings=cand_extra,
            )
            cand_stream.start()
        except Exception as exc:
            last_exc = exc
            if any(cand_in == t[0] and cand_out == t[1] and cand_rate == t[2] for t in tried[:-1]):
                continue
            job.log(f"打开失败，换备选方案：{'；'.join(why) or '同参数重试'}（{_short_err(exc)}）")
            continue
        stream = cand_stream
        if why:
            job.log(f"已切换到备选方案：{'；'.join(why)}")
        if cand_rate != rate:
            job.log(f"采样率由 {rate}Hz 改为 {cand_rate}Hz")
            # 参考音是按旧采样率生成的，必须跟着换，否则播放速度和音高会变
            ref_play = resample(ref_play, rate, cand_rate)
            total = len(ref_play)
            rate = cand_rate
        if (cand_in, cand_out) != (input_device, output_device):
            job.log(f"设备由 {input_device}/{output_device} 重新定位到 "
                    f"{_dev_label(sd, cand_in)} / {_dev_label(sd, cand_out)}")
        input_device, output_device = cand_in, cand_out
        break

    if stream is None:
        hint = _stream_error_hint(last_exc, rate, input_device, output_device)
        alt = _native_rate(sd, output_device if output_device is not None else input_device, rate)
        if alt:
            hint += f"（本设备的原生采样率可能是 {alt}Hz）"
        raise RuntimeError(hint) from last_exc

    job.stage = "播放参考音并采集"
    job.log("已开始：请在参考音播放期间说几句话（双讲），结束后检查 AEC 输出")
    try:
        t0 = time.time()
        stalled_since = t0
        last_done = 0
        while True:
            time.sleep(0.1)
            with lock:
                done = pos
            job.progress = 0.05 + 0.55 * (min(done / total, 1.0) if total else 0.0)
            if done >= total or job.cancel:
                break
            if done != last_done:
                last_done, stalled_since = done, time.time()
            # 只有"长时间没有任何新数据"才算异常；正常播完会走上面 done>=total 分支
            if time.time() - stalled_since > 5.0:
                job.log(f"音频回调停止响应（{5.0:.0f}s 无数据），提前停止")
                break
            if time.time() - t0 > total / rate + 30:
                job.log("采集总耗时异常，提前停止")
                break
    finally:
        # 流已经手动 start() 过，这里只负责停与关（不能再用 with，会二次 start）
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    job.stage = "整理录音"
    mic = np.concatenate(rec_mic) if rec_mic else np.zeros(0, dtype=np.float32)
    ref = np.concatenate(played) if played else np.zeros(0, dtype=np.float32)
    n = min(len(mic), len(ref))
    if n < rate // 2:
        raise RuntimeError(_no_audio_hint(sd, input_device, output_device, rate))
    mic, ref = mic[:n], ref[:n]
    job.progress = 0.65

    job.stage = "运行 AEC"
    # 设备流可能是 44100Hz（WASAPI 虚拟声卡的原生率），APM 只接受 8/16/32/48k，
    # 所以处理前重采样到最近的 APM 采样率，输出再转回设备采样率。
    apm_rate = nearest_apm_rate(rate)
    if apm_rate != rate:
        job.log(f"设备采样率 {rate}Hz 非 APM 支持值，内部按 {apm_rate}Hz 处理")
    mic_p = resample(mic, rate, apm_rate) if apm_rate != rate else mic
    ref_p = resample(ref, rate, apm_rate) if apm_rate != rate else ref
    delay_ms = float(aec.delay_ms)
    if aec.delay_auto:
        est = estimate_delay_ms(mic_p, ref_p, apm_rate, aec.delay_search_max)
        # 系统自身的输入输出缓冲会贡献基础延迟，太小则取估计
        delay_ms = max(est, float(aec.delay_ms))
        job.log(f"自动延迟估计: {est:.0f} ms → 使用 {delay_ms:.0f} ms")
    settings = AecSettings(**{**aec.to_dict(), "rate": apm_rate, "delay_ms": int(round(delay_ms))})
    with AecCore(settings, input_rate=apm_rate) as core:
        out_p = core.process_arrays(mic_p, ref_p, input_rate=apm_rate)
        stats = _stats_dict(core)
        _log_warnings(job, stats)
    out = resample(out_p, apm_rate, rate) if apm_rate != rate else out_p
    job.progress = 0.85

    out = out[:n]
    segments = {"all": (0.0, n / rate)}
    metrics = compute_metrics(rate, ref, mic, out, None, segments, delay_ms=delay_ms)
    metrics["aec_stats"] = stats
    metrics["config"] = {"aec": settings.to_dict(), "device": {
        "input": input_device, "output": output_device, "rate": rate,
        "play": True, "duration_s": duration_s,
        "ref_source": sim_ref.ref_source,
        "ref_file": Path(sim_ref.ref_file).name if sim_ref.ref_file else "",
        "play_gain": play_gain}}
    job.log("说明：设备实测无近端真值；请对照 mic.wav 与 aec_out.wav 主观判断残余回声")

    arts = _save_outputs(out_dir, rate, mic, ref, out, options)
    # 扬声器实际输出的那一版参考音（含静音与增益），排查"播的和以为是的是否一致"
    p_played = out_dir / "ref_played.wav"
    write_wav(p_played, ref_play[:n], rate)
    arts.append(_artifact(p_played, "实际播放的参考音", "声卡实际输出的 far 信号（含增益与首尾静音）"))
    arts += _add_plot_and_metrics(
        out_dir, rate, ref, mic, out, metrics, segments, "[设备] 实测录音", None,
    )
    job.result = {
        "dir": out_dir.name,
        "artifacts": arts,
        "metrics": metrics,
        "segments": segments,
        "duration_s": n / rate,
        "delay_ms": delay_ms,
        "mode": "device",
    }
    job.log(f"完成：ERLE(整体)={metrics['summary']['erle_db']} dB")


# --------------------------------------------------------------------------
# 对比：同一输入，多个配置
# --------------------------------------------------------------------------
def run_compare(
    job: Job,
    sim: SimulateSettings,
    aec: AecSettings,
    presets: List[str],
    delays: List[int],
    options: RunOptions,
) -> None:
    out_dir = run_dir(job)
    job.stage = "构造仿真信号"
    job.progress = 0.03
    simres = simulate(sim)
    rate = simres.rate
    truth_delay = estimate_delay_ms(simres.mic, simres.far, rate, aec.delay_search_max)
    job.log(f"真实回声延迟估计 {truth_delay:.0f} ms（仿真设置 {sim.echo_delay_ms} ms）")

    combos = [(p, d) for p in presets for d in delays]
    rows = []
    n = len(simres.mic)
    for i, (preset, delay) in enumerate(combos):
        if job.cancel:
            break
        job.stage = f"配置 {i+1}/{len(combos)}: {preset} / {delay}ms"
        job.progress = 0.05 + 0.85 * i / max(len(combos), 1)
        settings = AecSettings(**{**aec.to_dict(), "rate": rate, "preset": preset, "delay_ms": delay})
        try:
            with AecCore(settings, input_rate=rate) as core:
                out = core.process_arrays(simres.mic, simres.far, input_rate=rate)
                stats = _stats_dict(core)
            _log_warnings(job, stats)
        except Exception as exc:
            job.log(f"{preset}/{delay}ms 失败: {exc}")
            continue
        out = out[:n]
        metrics = compute_metrics(rate, simres.far[:n], simres.mic[:n], out,
                                  simres.near_speech[:n], simres.segments, delay_ms=delay)
        s = metrics["summary"]
        rows.append({
            "preset": preset,
            "preset_name": PRESETS.get(preset, {}).get("name", preset),
            "delay_ms": delay,
            "erle_db": s["erle_db"],
            "double_talk_erle_db": s["double_talk_erle_db"],
            "near_retention_db": s["near_retention_db"],
            "rtf": round(stats["realtime_factor"], 4),
            "out_file": f"{preset}_{delay}ms.wav",
            "warnings": stats.get("warnings") or [],
        })
        write_wav(out_dir / f"{preset}_{delay}ms.wav", out, rate)
        job.log(f"{preset:10s} delay={delay:3d}ms  ERLE={s['erle_db']} dB  "
                f"双讲={s['double_talk_erle_db']} dB  近端={s['near_retention_db']} dB")

    job.stage = "汇总"
    job.progress = 0.92
    write_wav(out_dir / "mic.wav", simres.mic, rate)
    write_wav(out_dir / "ref.wav", simres.far, rate)
    write_wav(out_dir / "aec_in.wav", np.stack([simres.mic, simres.far], axis=1), rate)
    table = out_dir / "compare.json"
    table.write_text(json.dumps({"truth_delay_ms": truth_delay, "rows": rows,
                                 "simulate": sim.to_dict()}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    arts = [
        _artifact(out_dir / "aec_in.wav", "AEC 输入（L=mic / R=ref）", "对比用的同一份输入"),
        _artifact(table, "对比结果 JSON", "各配置的 ERLE / 近端保留 / 实时因子", kind="json"),
    ]
    for row in rows:
        p = out_dir / row["out_file"]
        if p.exists():
            arts.append(_artifact(p, f"AEC 输出 {row['preset']}@{row['delay_ms']}ms", "该配置下的输出"))
    job.result = {
        "dir": out_dir.name,
        "artifacts": arts,
        "compare": {"truth_delay_ms": truth_delay, "rows": rows},
        "mode": "compare",
    }
