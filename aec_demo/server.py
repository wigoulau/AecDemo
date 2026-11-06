"""FastAPI 服务：REST 接口 + 静态 WebUI."""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import apm
from .audioio import duration_s, resample, write_wav
from .config import (
    CACHE_DIR,
    OUTPUT_DIR,
    PRESETS,
    ROOT,
    STATIC_DIR,
    SUPPORTED_RATES,
    AecSettings,
    RunOptions,
    SimulateSettings,
)
from .runner import JOBS, run_compare, run_device, run_files, run_files_stereo, run_simulation

app = FastAPI(title="AEC Demo", version="1.0.0")

UPLOAD_DIR = CACHE_DIR / "uploads"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# 请求体
# --------------------------------------------------------------------------
class SimRequest(BaseModel):
    simulate: Dict[str, Any] = Field(default_factory=dict)
    aec: Dict[str, Any] = Field(default_factory=dict)
    options: Dict[str, Any] = Field(default_factory=dict)


class FileRequest(BaseModel):
    """文件模式请求：两种写法二选一.

    - 双文件：`mic_file` + `ref_file`
    - 单文件：`file`（多声道，默认 ch0=mic / ch1=ref），可选 `mic_channel`/`ref_channel`
    """

    mic_file: str = ""
    ref_file: str = ""
    file: str = ""
    mic_channel: int = 0
    ref_channel: int = 1
    aec: Dict[str, Any] = Field(default_factory=dict)
    options: Dict[str, Any] = Field(default_factory=dict)
    mic_rate: Optional[int] = None
    ref_rate: Optional[int] = None


class DeviceRequest(BaseModel):
    aec: Dict[str, Any] = Field(default_factory=dict)
    options: Dict[str, Any] = Field(default_factory=dict)
    simulate: Dict[str, Any] = Field(default_factory=dict)
    input_device: Optional[int] = None
    output_device: Optional[int] = None
    device_rate: int = 48000
    play: bool = False
    duration_s: float = 6.0
    play_gain: float = 1.0
    input_channels: int = 1


class CompareRequest(BaseModel):
    simulate: Dict[str, Any] = Field(default_factory=dict)
    aec: Dict[str, Any] = Field(default_factory=dict)
    presets: List[str] = Field(default_factory=lambda: ["off", "aec_only", "voice_assistant"])
    delays: List[int] = Field(default_factory=lambda: [0, 30, 60, 120, 240])
    options: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _merge(base, patch: Dict[str, Any]):
    allowed = set(base.__dataclass_fields__.keys())
    data = {k: v for k, v in (patch or {}).items() if k in allowed}
    return type(base)(**{**base.__dict__, **data})


def _aec(patch: Dict[str, Any]) -> AecSettings:
    s = _merge(AecSettings(), patch)
    if s.rate not in SUPPORTED_RATES:
        raise HTTPException(400, f"不支持的采样率 {s.rate}，可选 {SUPPORTED_RATES}")
    if s.ns_level not in (0, 1, 2, 3):
        raise HTTPException(400, "ns_level 需为 0-3")
    return s


def _upload_path(file_id: str) -> Path:
    if not file_id.startswith("up_"):
        raise HTTPException(400, "非法文件标识")
    p = (UPLOAD_DIR / file_id).resolve()
    if UPLOAD_DIR.resolve() not in p.parents or not p.exists():
        raise HTTPException(404, "文件不存在")
    return p


_CHANNEL_CACHE: Dict[str, int] = {}


def _probe_channels(path: Path) -> Optional[int]:
    """探测音频声道数（缓存，只读文件头，不整段解码）."""
    key = str(path)
    if key in _CHANNEL_CACHE:
        return _CHANNEL_CACHE[key]
    ch: Optional[int] = None
    try:
        import soundfile as sf

        ch = int(sf.info(str(path)).channels)
    except Exception:
        try:
            import wave

            with wave.open(str(path), "rb") as wf:
                ch = int(wf.getnchannels())
        except Exception:
            ch = None
    if ch is not None:
        _CHANNEL_CACHE[key] = ch
    return ch


def _resolve_sim_files(sim: SimulateSettings) -> SimulateSettings:
    """把前端传来的上传 id 换成真实磁盘路径（仿真与设备模式共用）."""
    if sim.ref_source == "file" and sim.ref_file:
        sim.ref_file = str(_upload_path(sim.ref_file))
    if sim.mic_source == "file" and sim.mic_file:
        sim.mic_file = str(_upload_path(sim.mic_file))
    return sim


@app.get("/api/health")
def health() -> Dict[str, Any]:
    ok, msg = apm.check_library()
    return {
        "ok": True,
        "apm": {"available": ok, "message": msg, "platform": apm.platform_info()},
        "presets": PRESETS,
        "rates": list(SUPPORTED_RATES),
        "server_time": time.time(),
    }


@app.get("/api/devices")
def devices(all: bool = False) -> Dict[str, Any]:
    try:
        import sounddevice as sd

        raw = list(sd.query_devices())
        hostapis = [{"index": i, "name": h["name"]} for i, h in enumerate(sd.query_hostapis())]
        default_in, default_out = None, None
        try:
            di, do = sd.default.device
            default_in, default_out = di, do
        except Exception:
            pass

        items = []
        for idx, d in enumerate(raw):
            api = hostapis[d["hostapi"]]["name"] if d["hostapi"] < len(hostapis) else ""
            items.append({
                "index": idx,
                "name": _clean_dev_name(d["name"]),
                "raw_name": d["name"],
                "hostapi": d["hostapi"],
                "hostapi_name": api,
                "max_input_channels": d["max_input_channels"],
                "max_output_channels": d["max_output_channels"],
                "default_samplerate": d["default_samplerate"],
            })

        if all:
            ins = [x for x in items if x["max_input_channels"] > 0]
            outs = [x for x in items if x["max_output_channels"] > 0]
        else:
            ins = _dedupe_devices([x for x in items if x["max_input_channels"] > 0], input_side=True)
            outs = _dedupe_devices([x for x in items if x["max_output_channels"] > 0], input_side=False)

        return {"available": True, "inputs": ins, "outputs": outs,
                "default_input": _match_default(items, ins, default_in),
                "default_output": _match_default(items, outs, default_out),
                "hostapis": hostapis, "raw_count": len(items)}
    except Exception as exc:
        return {"available": False, "error": str(exc), "inputs": [], "outputs": []}


# PortAudio 在 Windows 上会把同一台物理设备按后端各列一遍（MME / DirectSound /
# WASAPI / WDM-KS）：35 个条目里其实只有 3 个麦克风 + 3 个扬声器。WDM-KS 还会
# 带出 `麦克风 ()`、`Speakers ()` 这类没有名字的幽灵设备，以及断连蓝牙耳机的
# 残留条目。下面按后端优先级去重、清理名字、丢掉别名与空条目。
_HOSTAPI_PRIORITY = (
    ("wasapi", 0), ("core audio", 0), ("alsa", 0), ("pulse", 0), ("jack", 0),
    ("directsound", 1), ("mme", 2), ("wdm-ks", 3),
)
# 这些是「默认设备」的别名，不是独立硬件
_ALIAS_NAMES = (
    "microsoft sound mapper", "primary sound capture driver", "primary sound driver",
    "主声音捕获驱动程序", "主声音驱动程序", "默认设备", "default",
)
# WDM-KS 的蓝牙设备名形如 `耳机 (@System32\drivers\bthhfenum.sys,#2;%1 Hands-Free AG Audio%0;(TWS-Q6.))`
_JUNK_RE = re.compile(r"@[^;]*;\s*%1\s*")


def _clean_dev_name(name: str) -> str:
    """清理 PortAudio 设备名：去换行、去驱动路径前缀、只留真正的型号."""
    s = " ".join(str(name).split())
    s = _JUNK_RE.sub("", s)
    for junk in ("%0;", "%0", "%1"):
        s = s.replace(junk, " ")
    s = " ".join(s.split())
    # `耳机 (Hands-Free AG Audio (TWS-Q6.))` → `耳机 (TWS-Q6)`
    head = s.split("(", 1)[0].strip()
    groups = re.findall(r"\(([^()]*)\)", s)
    if head and groups:
        tail = groups[-1].strip().rstrip(".").strip()
        if tail:
            return f"{head} ({tail})"
        return head
    return s


def _dev_priority(hostapi_name: str) -> int:
    low = hostapi_name.lower()
    for key, prio in _HOSTAPI_PRIORITY:
        if key in low:
            return prio
    return 4


def _norm_key(name: str) -> str:
    return re.sub(r"[^0-9a-z一-鿿]+", "", name.lower())


def _is_junk(dev: Dict[str, Any]) -> bool:
    name = dev["name"]
    if not _norm_key(name):
        return True                       # 空名 / 纯符号
    if re.search(r"\(\s*\)", dev.get("raw_name", name)):
        return True                       # `麦克风 ()` / `Speakers ()` 幽灵条目
    low = name.lower()
    if any(alias in low for alias in _ALIAS_NAMES):
        return True                       # 「主声音驱动」等默认设备别名
    # WDM-KS 还会列出没有型号的裸设备（`麦克风` / `Speakers` / `耳机`），
    # 无法判断是哪台硬件，且与 WASAPI 条目功能重复
    if _dev_priority(dev["hostapi_name"]) >= 3 and not _distinct_tokens(name):
        return True
    return False


_GENERIC_TOKENS = {
    "麦克风", "扬声器", "耳机", "立体声", "混音", "声音", "驱动", "程序", "设备", "默认",
    "speakers", "speaker", "microphone", "mic", "headphones", "headphone", "headset",
    "stereo", "mix", "input", "output", "audio", "high", "definition", "primary",
    "driver", "default", "mapper", "sound", "virtual", "handsfree",
}


def _distinct_tokens(name: str) -> set:
    """取出能标识硬件的词：Realtek / MK03 / ToDesk / AirPods …（滤掉通用词）."""
    toks = re.findall(r"[0-9a-z]+|[一-鿿]+", name.lower())
    out = set()
    for t in toks:
        if len(t) < 3 or t in _GENERIC_TOKENS:
            continue
        # 蓝牙名里的 Hands-Free / AG 之类是描述，不是型号
        if t in ("hands", "free", "ag"):
            continue
        out.add(t)
    return out


def _same_hardware(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """两个条目是否指向同一台物理设备（名字相同或共享型号词）."""
    ka, kb = _norm_key(a["name"]), _norm_key(b["name"])
    if ka == kb:
        return True
    if min(len(ka), len(kb)) >= 6 and (ka.startswith(kb) or kb.startswith(ka)):
        return True                       # MME 截断名 vs 完整名
    return bool(_distinct_tokens(a["name"]) & _distinct_tokens(b["name"]))


def _dedupe_devices(items: List[Dict[str, Any]], input_side: bool) -> List[Dict[str, Any]]:
    """同一台设备只保留质量最好的那个后端；只在某后端独占时才保留.

    WASAPI > DirectSound > MME > WDM-KS。WDM-KS 独有的设备（例如只在这里
    出现的蓝牙耳机）会保留，WDM-KS 与 WASAPI 重复的则丢弃。
    """
    live = [d for d in items if not _is_junk(d)]
    live.sort(key=lambda d: (_dev_priority(d["hostapi_name"]), d["index"]))
    kept: List[Dict[str, Any]] = []
    for d in live:
        prio = _dev_priority(d["hostapi_name"])
        dup = False
        for k in kept:
            if _dev_priority(k["hostapi_name"]) < prio or (
                _dev_priority(k["hostapi_name"]) == prio and _norm_key(k["name"]) == _norm_key(d["name"])
            ):
                if _same_hardware(d, k):
                    dup = True
                    break
        if not dup:
            kept.append(d)
    if input_side:
        kept.sort(key=lambda d: d["index"])
    return kept


def _match_default(items: List[Dict[str, Any]], shown: List[Dict[str, Any]],
                   default_index: Optional[int]) -> Optional[int]:
    """把原始默认设备索引映射到去重后仍在列表里的那个索引."""
    if default_index is None or default_index < 0 or default_index >= len(items):
        return None
    shown_idx = {d["index"] for d in shown}
    if default_index in shown_idx:
        return default_index
    key = _norm_key(items[default_index]["name"])
    best = None
    for d in shown:
        k = _norm_key(d["name"])
        if k == key or (min(len(k), len(key)) >= 6 and (k.startswith(key) or key.startswith(k))):
            if best is None or _dev_priority(d["hostapi_name"]) < _dev_priority(best["hostapi_name"]):
                best = d
    return best["index"] if best else None


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> Dict[str, Any]:
    ext = Path(file.filename or "audio.wav").suffix.lower() or ".wav"
    if ext not in (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".webm", ".aif", ".aiff"):
        raise HTTPException(400, f"不支持的音频格式: {ext}")
    dest = UPLOAD_DIR / f"up_{uuid.uuid4().hex[:10]}{ext}"
    size = 0
    with dest.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > 200 * 1024 * 1024:
                fh.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "文件超过 200MB")
            fh.write(chunk)
    info: Dict[str, Any] = {"file_id": dest.name, "name": file.filename, "size": size}
    try:
        from .audioio import read_audio_multi

        raw = read_audio_multi(dest, 16000)
        x = raw if raw.ndim == 1 else raw.mean(axis=1, dtype=np.float32)
        channels = 1 if raw.ndim == 1 else int(raw.shape[1])
        info.update({"duration_s": round(len(x) / 16000, 2), "sampled_at_16k": len(x),
                     "channels": channels,
                     "peak": round(float(np.max(np.abs(x))) if len(x) else 0.0, 4),
                     "rms_dbfs": round(float(20 * np.log10(max(float(np.sqrt(np.mean(x**2))), 1e-6))), 1)})
    except Exception as exc:
        info["decode_error"] = str(exc)
    return info


@app.get("/api/uploads")
def list_uploads() -> Dict[str, Any]:
    items = []
    for p in sorted(UPLOAD_DIR.glob("up_*"), key=lambda x: -x.stat().st_mtime):
        items.append({"file_id": p.name, "name": p.name,
                      "duration_s": round(duration_s(p), 2),
                      "size": p.stat().st_size,
                      "channels": _probe_channels(p)})
    return {"items": items}


@app.post("/api/simulate")
def start_simulate(req: SimRequest) -> Dict[str, Any]:
    sim = _resolve_sim_files(_merge(SimulateSettings(), req.simulate))
    aec = _aec(req.aec)
    opts = _merge(RunOptions(), req.options)
    job = JOBS.submit(
        "simulate",
        f"仿真 @{aec.rate}Hz preset={aec.preset}",
        lambda j: run_simulation(j, sim, aec, opts),
    )
    return job.to_dict()


@app.post("/api/files")
def start_files(req: FileRequest) -> Dict[str, Any]:
    aec = _aec(req.aec)
    opts = _merge(RunOptions(), req.options)

    if req.file:
        # 单文件模式：一个多声道文件，按声道拆 mic/ref
        path = str(_upload_path(req.file))
        if req.mic_channel == req.ref_channel:
            raise HTTPException(400, "mic 与 ref 不能是同一个声道")
        if req.mic_channel < 0 or req.ref_channel < 0:
            raise HTTPException(400, "声道索引不能为负")
        job = JOBS.submit(
            "files",
            f"文件 {Path(path).name} ch{req.mic_channel + 1}=mic / ch{req.ref_channel + 1}=ref",
            lambda j: run_files_stereo(j, path, aec, opts, req.mic_channel, req.ref_channel),
        )
        return job.to_dict()

    if not req.mic_file or not req.ref_file:
        raise HTTPException(400, "请提供「一个多声道文件」或「mic + ref 两个文件」")
    mic = str(_upload_path(req.mic_file))
    ref = str(_upload_path(req.ref_file))
    job = JOBS.submit(
        "files", f"文件 {Path(mic).name} + {Path(ref).name}",
        lambda j: run_files(j, mic, ref, aec, opts, req.mic_rate, req.ref_rate),
    )
    return job.to_dict()


@app.post("/api/device")
def start_device(req: DeviceRequest) -> Dict[str, Any]:
    if req.device_rate not in SUPPORTED_RATES:
        raise HTTPException(400, f"device_rate 需为 {SUPPORTED_RATES}")
    aec = _aec(req.aec)
    opts = _merge(RunOptions(), req.options)
    sim = SimulateSettings(**{**SimulateSettings().to_dict(), "rate": req.device_rate,
                              **{k: v for k, v in req.simulate.items()
                                 if k in SimulateSettings.__dataclass_fields__}})
    # 参考音若来自上传，前端传的是文件 id，这里换成真实路径
    sim = _resolve_sim_files(sim)
    job = JOBS.submit(
        "device",
        f"设备实测 {'播放参考音' if req.play else '仅录音'} @{req.device_rate}Hz",
        lambda j: run_device(j, aec, opts, sim, req.input_device, req.output_device,
                             req.device_rate, req.play, req.duration_s, req.play_gain,
                             req.input_channels),
    )
    return job.to_dict()


@app.post("/api/compare")
def start_compare(req: CompareRequest) -> Dict[str, Any]:
    sim = _merge(SimulateSettings(), req.simulate)
    aec = _aec(req.aec)
    opts = _merge(RunOptions(), req.options)
    presets = [p for p in req.presets if p in PRESETS] or ["off", "voice_assistant"]
    delays = sorted({int(max(0, min(500, d))) for d in req.delays}) or [0, 60]
    job = JOBS.submit(
        "compare", f"对比 {len(presets)} 配置 × {len(delays)} 延迟",
        lambda j: run_compare(j, sim, aec, presets, delays, opts),
    )
    return job.to_dict()


@app.get("/api/jobs")
def list_jobs() -> Dict[str, Any]:
    return {"jobs": JOBS.list()}


@app.get("/api/jobs/{jid}")
def get_job(jid: str) -> Dict[str, Any]:
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job.to_dict()


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str) -> Dict[str, Any]:
    return {"ok": JOBS.cancel(jid)}


@app.get("/api/results")
def list_results() -> Dict[str, Any]:
    items = []
    for d in sorted(OUTPUT_DIR.iterdir(), reverse=True) if OUTPUT_DIR.exists() else []:
        if not d.is_dir():
            continue
        files = sorted(p.name for p in d.iterdir() if p.is_file())
        if not files:
            continue  # 失败任务留下的空目录不展示
        meta = d / "metrics.json"
        summary = None
        if meta.exists():
            try:
                summary = json.loads(meta.read_text(encoding="utf-8")).get("summary")
            except Exception:
                pass
        if (d / "compare.json").exists():
            summary = {"type": "compare"}
        items.append({
            "dir": d.name,
            "mtime": d.stat().st_mtime,
            "summary": summary,
            "files": files,
        })
    return {"items": items}


@app.get("/files/{dir_name}/{file_name}")
def get_file(dir_name: str, file_name: str) -> FileResponse:
    base = (OUTPUT_DIR / dir_name).resolve()
    if OUTPUT_DIR.resolve() not in base.parents:
        raise HTTPException(400, "非法路径")
    target = (base / file_name).resolve()
    if base not in target.parents or not target.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target, filename=file_name)


@app.get("/uploads/{file_id}")
def get_upload(file_id: str) -> FileResponse:
    return FileResponse(_upload_path(file_id), filename=file_id)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


if (STATIC_DIR).exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
