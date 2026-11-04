"""AEC 效果分析：分段指标、ERLE 曲线、频谱对比图."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

EPS = 1e-12


def dbfs(x: np.ndarray) -> float:
    if x is None or len(x) == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))
    return float(20 * np.log10(max(rms, 1e-6)))


def ratio_db(num: np.ndarray, den: np.ndarray) -> float:
    a = float(np.mean(np.asarray(num, dtype=np.float64) ** 2)) if len(num) else 0.0
    b = float(np.mean(np.asarray(den, dtype=np.float64) ** 2)) if len(den) else 0.0
    if b <= EPS or a <= EPS:
        return 0.0
    return float(10 * np.log10(a / b))


def _seg(x: np.ndarray, rate: int, span: Tuple[float, float]) -> np.ndarray:
    a = max(0, int(span[0] * rate))
    b = min(len(x), int(span[1] * rate))
    if b <= a:
        return np.zeros(0, dtype=np.float32)
    return x[a:b]


def _trim(a: np.ndarray, b: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    n = min(len(a), len(b))
    return a[:n], b[:n]


def erle_curve(mic: np.ndarray, out: np.ndarray, rate: int, win_ms: int = 50) -> Dict[str, list]:
    """逐窗口 ERLE，单位 dB；只在 mic 有足够能量的窗口计算."""
    win = max(1, int(rate * win_ms / 1000))
    n = min(len(mic), len(out)) // win
    t, erle, mic_db, out_db = [], [], [], []
    mic = mic.astype(np.float64)
    out = out.astype(np.float64)
    for i in range(n):
        m = mic[i * win:(i + 1) * win]
        o = out[i * win:(i + 1) * win]
        pm = float(np.mean(m**2))
        po = float(np.mean(o**2))
        if pm < 1e-9:
            continue
        t.append((i * win + win / 2) / rate)
        mic_db.append(10 * np.log10(max(pm, EPS)))
        out_db.append(10 * np.log10(max(po, EPS)))
        erle.append(float(10 * np.log10(max(pm, EPS) / max(po, EPS))))
    return {"t": t, "erle_db": erle, "mic_dbfs": mic_db, "out_dbfs": out_db}


def band_erle(mic: np.ndarray, out: np.ndarray, rate: int) -> Dict[str, list]:
    """宽带能量之外再看分频段抑制量（125Hz~8kHz 三分之一倍频程）."""
    mic, out = _trim(mic, out)
    n = 1 << int(np.ceil(np.log2(max(len(mic), 1024))))
    win = np.hanning(n).astype(np.float64)
    pm = np.abs(np.fft.rfft(mic.astype(np.float64) * win)) ** 2
    po = np.abs(np.fft.rfft(out.astype(np.float64) * win)) ** 2
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    centers = [125, 250, 500, 1000, 2000, 4000, 8000]
    bands, vals = [], []
    for fc in centers:
        if fc >= rate / 2:
            continue
        lo, hi = fc / 2 ** (1 / 6), fc * 2 ** (1 / 6)
        sel = (freqs >= lo) & (freqs < hi)
        if not np.any(sel):
            continue
        a = float(np.sum(pm[sel]))
        b = float(np.sum(po[sel]))
        if b <= EPS or a <= EPS:
            continue
        bands.append(fc)
        vals.append(float(10 * np.log10(a / b)))
    return {"bands_hz": bands, "erle_db": vals}


def compute_metrics(
    rate: int,
    ref: np.ndarray,
    mic: np.ndarray,
    out: np.ndarray,
    near_truth: Optional[np.ndarray] = None,
    segments: Optional[Dict[str, Tuple[float, float]]] = None,
    delay_ms: Optional[float] = None,
) -> Dict:
    """核心指标.

    - echo_return_loss: ref → mic 的声学耦合损耗（越大回声越小）
    - erle: mic → out 的抑制量，回声段（无双讲）才代表 AEC 真实能力
    - near_retention: 仅近端语音段落里，输出相对近端真值的保留量
    """
    n = min(len(ref), len(mic), len(out))
    ref, mic, out = ref[:n], mic[:n], out[:n]
    near_truth = near_truth[:n] if near_truth is not None and len(near_truth) >= n else None
    if segments is None:
        segments = {"all": (0.0, n / rate)}

    per_segment = {}
    for name, span in segments.items():
        if span[1] <= span[0]:
            continue
        r = _seg(ref, rate, span)
        m = _seg(mic, rate, span)
        o = _seg(out, rate, span)
        nt = _seg(near_truth, rate, span) if near_truth is not None else None
        item = {
            "start_s": round(span[0], 3),
            "end_s": round(span[1], 3),
            "ref_dbfs": round(dbfs(r), 2),
            "mic_dbfs": round(dbfs(m), 2),
            "out_dbfs": round(dbfs(o), 2),
            "erle_db": round(ratio_db(m, o), 2),
        }
        if nt is not None:
            item["near_dbfs"] = round(dbfs(nt), 2)
            # 近端保留：输出里近端成分 / 近端真值（不含回声时的参考）
            item["near_retention_db"] = round(ratio_db(o, nt), 2)
        per_segment[name] = item

    echo_seg = per_segment.get("echo_only") or per_segment.get("all")
    dt_seg = per_segment.get("double_talk")
    near_seg = per_segment.get("near_only")
    wide_span = segments.get("echo_only") or segments.get("all") or (0.0, n / rate)
    summary = {
        "erle_db": echo_seg["erle_db"] if echo_seg else None,
        "double_talk_erle_db": dt_seg["erle_db"] if dt_seg else None,
        "near_retention_db": near_seg.get("near_retention_db") if near_seg else None,
        "echo_return_loss_db": round(
            ratio_db(_seg(ref, rate, wide_span), _seg(mic, rate, wide_span)), 2
        ),
        "delay_ms": round(float(delay_ms), 1) if delay_ms is not None else None,
        "out_clipped": bool(np.max(np.abs(out)) >= 0.999),
        "out_peak_dbfs": round(float(20 * np.log10(max(np.max(np.abs(out)) if n else 0, 1e-6))), 2),
    }
    return {"summary": summary, "segments": per_segment}


# --------------------------------------------------------------------------
# 绘图
# --------------------------------------------------------------------------
_CJK_CANDIDATES = [
    "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Noto Sans SC",
    "Source Han Sans SC", "WenQuanYi Zen Hei", "PingFang SC", "Heiti SC",
    "Arial Unicode MS", "DejaVu Sans",
]
_cjk_ready = False


def _setup_cjk_font(font_manager, plt) -> None:
    """图里会写中文，找一个本机真正可用的 CJK 字体文件；找不到则退回英文标签."""
    global _cjk_ready
    if _cjk_ready:
        return
    _cjk_ready = True
    try:
        installed = {}
        for f in font_manager.fontManager.ttflist:
            installed.setdefault(f.name, f.fname)
        picked = [n for n in _CJK_CANDIDATES if n in installed and n != "DejaVu Sans"]
        if picked:
            # 显式注册首选字体的文件，避免同名/回退导致 findfont 仍拿到 DejaVu
            try:
                font_manager.fontManager.addfont(installed[picked[0]])
            except Exception:
                pass
            plt.rcParams["font.family"] = "sans-serif"
            plt.rcParams["font.sans-serif"] = picked + ["DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            _cjk_ok = True
        else:
            _cjk_ok = False
    except Exception:
        pass


def make_plot(
    path: Path,
    rate: int,
    ref: np.ndarray,
    mic: np.ndarray,
    out: np.ndarray,
    segments: Dict[str, Tuple[float, float]],
    metrics: Dict,
    title: str = "",
    near_truth: Optional[np.ndarray] = None,
) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except Exception:
        return False

    _setup_cjk_font(font_manager, plt)

    n = min(len(ref), len(mic), len(out))
    ref, mic, out = ref[:n], mic[:n], out[:n]
    t = np.arange(n) / rate
    fig, axes = plt.subplots(5, 1, figsize=(12, 12), sharex=True,
                             gridspec_kw={"height_ratios": [1, 1, 1, 1.1, 1.1]})
    colors = {"ref": "#4C78A8", "mic": "#E45756", "out": "#54A24B", "near": "#B279A2"}
    seg_colors = {"baseline": "#BBBBBB", "echo_only": "#F0C987",
                  "double_talk": "#9EC9E2", "near_only": "#C7E2B5", "tail": "#DDDDDD"}

    for name, (a, b) in segments.items():
        if b <= a:
            continue
        for ax in axes[:4]:
            ax.axvspan(a, b, alpha=0.16, color=seg_colors.get(name, "#888888"), lw=0)
    axes[0].plot(t, ref, lw=0.4, color=colors["ref"])
    axes[0].set_ylabel("ref (far)")
    axes[1].plot(t, mic, lw=0.4, color=colors["mic"])
    if near_truth is not None and len(near_truth) >= n:
        axes[1].plot(t, near_truth[:n], lw=0.4, color=colors["near"], alpha=0.6, label="near truth")
        axes[1].legend(loc="upper right", fontsize=8)
    axes[1].set_ylabel("mic (before AEC)")
    axes[2].plot(t, out, lw=0.4, color=colors["out"])
    axes[2].set_ylabel("out (after AEC)")

    # 段名标在顶部，避免与曲线重叠
    for name, (a, b) in segments.items():
        if b > a:
            axes[0].text((a + b) / 2, axes[0].get_ylim()[1], name, ha="center", va="bottom",
                         fontsize=7, color="#444444",
                         bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.7))

    curve = erle_curve(mic, out, rate, 50)
    if curve["t"]:
        mask = np.array(curve["mic_dbfs"]) > -55.0
        ts = np.array(curve["t"])
        vals = np.array(curve["erle_db"])
        axes[3].plot(ts[mask], vals[mask], lw=1.0, color="#F58518", label="ERLE / 50ms")
        axes[3].axhline(0, color="#999999", lw=0.6, ls="--")
        axes[3].set_ylabel("ERLE (dB)")
        axes[3].grid(alpha=0.25)
        hi = float(np.percentile(vals[mask], 95)) if np.any(mask) else 40.0
        axes[3].set_ylim(-12, min(70, max(30.0, hi + 5)))
        axes[3].legend(loc="upper left", fontsize=8)

    # 纯回声段的长时平均频谱：直观看到各频段被压掉多少
    span = segments.get("echo_only") or segments.get("all") or (0.0, n / rate)
    m_seg = _seg(mic, rate, span)
    o_seg = _seg(out, rate, span)
    if len(m_seg) > 64:
        nfft = 1 << int(np.ceil(np.log2(min(len(m_seg), 32768))))
        nfft = min(nfft, len(m_seg))
        win = np.hanning(nfft)
        freqs = np.fft.rfftfreq(nfft, 1 / rate)
        pm = np.abs(np.fft.rfft(m_seg[:nfft] * win)) ** 2
        po = np.abs(np.fft.rfft(o_seg[:nfft] * win)) ** 2
        to_db = lambda p: 10 * np.log10(np.maximum(p, 1e-14))
        axes[4].semilogx(freqs[1:], to_db(pm[1:]), lw=0.8, color=colors["mic"], label="mic")
        axes[4].semilogx(freqs[1:], to_db(po[1:]), lw=0.8, color=colors["out"], label="out")
        axes[4].set_ylabel("spectrum (dB, echo-only)")
        axes[4].set_xlabel("frequency (Hz)")
        axes[4].grid(alpha=0.25, which="both")
        axes[4].legend(loc="upper right", fontsize=8)

    summ = metrics.get("summary", {})
    subtitle = (
        f"ERLE(echo-only)={summ.get('erle_db')} dB  |  "
        f"ERLE(double-talk)={summ.get('double_talk_erle_db')} dB  |  "
        f"near-retention={summ.get('near_retention_db')} dB  |  "
        f"delay={summ.get('delay_ms')} ms"
    )

    summ = metrics.get("summary", {})
    subtitle = (
        f"ERLE(echo-only)={summ.get('erle_db')} dB  |  "
        f"ERLE(double-talk)={summ.get('double_talk_erle_db')} dB  |  "
        f"delay={summ.get('delay_ms')} ms"
    )
    fig.suptitle((title + "\n" if title else "") + subtitle, fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return True


def save_metrics(path: Path, metrics: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
