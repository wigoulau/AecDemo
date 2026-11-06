"""最小可运行验证：不碰麦克风/扬声器，全部离线."""

import numpy as np
import pytest

from aec_demo import apm
from aec_demo.aec import AecCore, estimate_delay_ms
from aec_demo.analysis import compute_metrics, erle_curve
from aec_demo.audioio import read_audio, read_audio_multi, split_channels, write_wav
from aec_demo.config import AecSettings, RunOptions, SimulateSettings
from aec_demo.analysis import ratio_db
from aec_demo.runner import Job, run_files, run_files_stereo
from aec_demo.simulate import make_room_ir, make_speech_like, simulate


APM_OK = apm.check_library()[0]
needs_apm = pytest.mark.skipif(not APM_OK, reason="本机 WebRTC APM 动态库不可用")


def test_simulation_shape_and_segments():
    r = simulate(SimulateSettings(rate=16000, duration_s=4))
    assert r.rate == 16000
    assert len(r.far) == len(r.mic) == len(r.echo) == len(r.near_speech)
    assert np.max(np.abs(r.mic)) <= 1.0
    seg = r.segments
    assert seg["echo_only"][1] > seg["echo_only"][0] > 0
    assert seg["double_talk"][1] > seg["double_talk"][0]
    assert seg["near_only"][1] > seg["near_only"][0]


def test_simulation_is_deterministic():
    a = simulate(SimulateSettings(duration_s=2))
    b = simulate(SimulateSettings(duration_s=2))
    assert np.array_equal(a.mic, b.mic)


def test_silence_mic_has_no_near_segment():
    r = simulate(SimulateSettings(duration_s=3, mic_source="silence"))
    assert r.segments["double_talk"] == (0.0, 0.0)
    assert float(np.max(np.abs(r.near_speech))) == 0.0


def test_delay_estimation_finds_known_delay():
    r = simulate(SimulateSettings(duration_s=5, echo_delay_ms=80, room_rt60=0.0))
    est = estimate_delay_ms(r.mic, r.far, r.rate, 300)
    assert abs(est - 80) <= 15, est


def test_room_ir_energy_is_normalized():
    ir = make_room_ir(16000, 0.2, 50)
    assert abs(float(np.sqrt(np.sum(ir**2))) - 1.0) < 1e-5


def test_metrics_are_sane_for_identity_passthrough():
    """输出 = 输入时 ERLE 应为 0 dB."""
    r = simulate(SimulateSettings(duration_s=3))
    m = compute_metrics(r.rate, r.far, r.mic, r.mic, r.near_speech, r.segments, delay_ms=0)
    assert abs(m["segments"]["echo_only"]["erle_db"]) < 0.1
    # near_only 段还含底噪，允许 1dB 偏差
    assert abs(m["segments"]["near_only"]["near_retention_db"]) < 1.0


def test_metrics_detect_attenuation():
    r = simulate(SimulateSettings(duration_s=3))
    half = r.mic * 0.1
    m = compute_metrics(r.rate, r.far, r.mic, half, r.near_speech, r.segments, delay_ms=0)
    assert abs(m["segments"]["echo_only"]["erle_db"] - 20.0) < 0.3


def test_erle_curve_skips_silence():
    x = np.zeros(16000, dtype=np.float32)
    x[8000:] = 0.1
    c = erle_curve(x, x * 0.1, 16000, 50)
    assert len(c["t"]) < 13
    assert all(abs(v - 20.0) < 0.2 for v in c["erle_db"])


@needs_apm
def test_aec_reduces_echo_on_simulation():
    r = simulate(SimulateSettings(duration_s=5, ref_source="speech", mic_source="speech",
                                  echo_delay_ms=60, noise_level=0.0))
    s = AecSettings(rate=16000, preset="aec_only", delay_ms=60)
    with AecCore(s, input_rate=16000) as core:
        out = core.process_arrays(r.mic, r.far, input_rate=16000)
    assert len(out) == len(r.mic)
    m = compute_metrics(r.rate, r.far, r.mic, out, r.near_speech, r.segments, delay_ms=60)
    assert m["segments"]["echo_only"]["erle_db"] > 15.0, m["segments"]["echo_only"]
    assert out.dtype == np.float32
    assert np.max(np.abs(out)) <= 1.0


@needs_apm
def test_aec_passes_near_end_speech_when_far_silent():
    r = simulate(SimulateSettings(duration_s=4, ref_source="silence", mic_source="speech"))
    s = AecSettings(rate=16000, preset="aec_only", delay_ms=0)
    with AecCore(s, input_rate=16000) as core:
        out = core.process_arrays(r.mic, r.far, input_rate=16000)
    ratio = ratio_db(r.mic, out)
    assert abs(ratio) < 3.0, ratio


@needs_apm
def test_disabled_aec_is_near_passthrough():
    r = simulate(SimulateSettings(duration_s=3))
    s = AecSettings(rate=16000, preset="off")
    with AecCore(s, input_rate=16000) as core:
        out = core.process_arrays(r.mic, r.far, input_rate=16000)
    assert abs(ratio_db(r.mic, out)) < 1.0


@needs_apm
@pytest.mark.parametrize("rate", [8000, 16000, 32000, 48000])
def test_all_supported_rates_run(rate):
    r = simulate(SimulateSettings(rate=rate, duration_s=2))
    s = AecSettings(rate=rate, preset="voice_assistant", delay_ms=30)
    with AecCore(s, input_rate=rate) as core:
        out = core.process_arrays(r.mic, r.far, input_rate=rate)
    assert len(out) == (len(r.mic) // (rate // 100)) * (rate // 100)


@needs_apm
def test_resampling_path_48k_input_to_16k_apm():
    r = simulate(SimulateSettings(rate=48000, duration_s=2, echo_delay_ms=40))
    s = AecSettings(rate=16000, preset="aec_only", delay_ms=40)
    with AecCore(s, input_rate=48000) as core:
        out = core.process_arrays(r.mic, r.far, input_rate=48000)
    assert len(out) > 0
    assert not np.any(np.isnan(out))


@needs_apm
def test_chunk_api_matches_array_api_length():
    r = simulate(SimulateSettings(duration_s=2))
    s = AecSettings(rate=16000, preset="aec_only", delay_ms=60)
    with AecCore(s, input_rate=16000) as core:
        chunks = []
        block = 480  # 30ms，非 10ms 帧长，验证内部缓存
        for off in range(0, len(r.mic) - block, block):
            chunks.append(core.process_chunk(r.mic[off:off + block], r.far[off:off + block]))
        out = np.concatenate(chunks)
    assert len(out) == (len(out) // 160) * 160
    assert len(out) > 0


def test_mobile_downgrade_flag_never_raises():
    s = AecSettings(rate=16000, preset="mobile")
    if not APM_OK:
        pytest.skip("APM 不可用")
    with AecCore(s) as core:
        assert isinstance(core.mobile_downgraded, bool)


# ---------------------------------------------------------------------------
# 单文件（双声道）模式
# ---------------------------------------------------------------------------
def _write_stereo(tmp_path, mic, ref, rate):
    p = tmp_path / "rec_2ch.wav"
    write_wav(p, np.stack([mic, ref], axis=1), rate)
    return p


def test_read_audio_multi_keeps_channels(tmp_path):
    sr = 16000
    t = np.arange(sr) / sr
    left = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    right = (0.2 * np.sin(2 * np.pi * 880 * t)).astype(np.float32)
    p = _write_stereo(tmp_path, left, right, sr)

    data = read_audio_multi(p, sr)
    assert data.shape == (sr, 2)
    assert np.allclose(data[:, 0], left, atol=1e-4)
    assert np.allclose(data[:, 1], right, atol=1e-4)
    # read_audio 仍然是降混语义（两路平均），用于单路场景
    assert np.allclose(read_audio(p, sr), (left + right) / 2, atol=1e-4)


def test_read_audio_multi_mono_stays_1d(tmp_path):
    p = tmp_path / "mono.wav"
    write_wav(p, np.zeros(16000, dtype=np.float32) + 0.1, 16000)
    assert read_audio_multi(p, 16000).ndim == 1


def test_split_channels_picks_first_two(tmp_path):
    sr = 16000
    a = np.linspace(-0.5, 0.5, sr).astype(np.float32)
    b = np.linspace(0.5, -0.5, sr).astype(np.float32)
    c = np.zeros(sr, dtype=np.float32) + 0.3
    p = tmp_path / "tri.wav"
    write_wav(p, np.stack([a, b, c], axis=1), sr)

    mic, ref = split_channels(p, sr)
    assert np.allclose(mic, a, atol=1e-4)
    assert np.allclose(ref, b, atol=1e-4)


def test_split_channels_rejects_mono(tmp_path):
    p = tmp_path / "mono.wav"
    write_wav(p, np.zeros(16000, dtype=np.float32) + 0.1, 16000)
    with pytest.raises(ValueError, match="单声道"):
        split_channels(p, 16000)


@needs_apm
def test_stereo_file_mode_matches_two_file_mode(tmp_path):
    """同一个双声道文件 ↔ 拆成两个文件，两种走法结果必须一致."""
    r = simulate(SimulateSettings(duration_s=4.0, echo_delay_ms=50))
    p = _write_stereo(tmp_path, r.mic, r.far, r.rate)
    mic_p, ref_p = tmp_path / "mic.wav", tmp_path / "ref.wav"
    write_wav(mic_p, r.mic, r.rate)
    write_wav(ref_p, r.far, r.rate)

    aec = AecSettings(preset="aec_only", rate=r.rate, delay_ms=50)
    j_one = Job(id="one", kind="files", title="单文件")
    run_files_stereo(j_one, str(p), aec, RunOptions())
    j_two = Job(id="two", kind="files", title="双文件")
    run_files(j_two, str(mic_p), str(ref_p), aec, RunOptions())

    e1 = j_one.result["metrics"]["segments"]["all"]["erle_db"]
    e2 = j_two.result["metrics"]["segments"]["all"]["erle_db"]
    assert abs(e1 - e2) < 0.05, (e1, e2)
    assert j_one.result["metrics"]["config"]["source"].startswith("rec_2ch.wav")


@needs_apm
def test_stereo_file_mode_honours_channel_selection(tmp_path):
    """落盘的 mic.wav / ref.wav 必须严格对应所选声道（证明没有降混）."""
    from aec_demo.config import OUTPUT_DIR

    r = simulate(SimulateSettings(duration_s=3.0, echo_delay_ms=50))
    p = _write_stereo(tmp_path, r.mic, r.far, r.rate)
    aec = AecSettings(preset="aec_only", rate=r.rate, delay_ms=50)

    job = Job(id="sel", kind="files", title="声道选择")
    run_files_stereo(job, str(p), aec, RunOptions(), mic_channel=1, ref_channel=0)

    out_dir = OUTPUT_DIR / job.result["dir"]
    mic_saved = read_audio(out_dir / "mic.wav", r.rate)
    ref_saved = read_audio(out_dir / "ref.wav", r.rate)
    n = min(len(mic_saved), len(r.mic))

    # mic_channel=1 → 落盘 mic 应等于 far；ref_channel=0 → 落盘 ref 应等于 mic
    assert np.allclose(mic_saved[:n], r.far[:n], atol=2e-3)
    assert np.allclose(ref_saved[:n], r.mic[:n], atol=2e-3)
    # 两路绝不相同（若被降混，两者会一模一样）
    assert not np.allclose(mic_saved[:n], ref_saved[:n], atol=1e-3)


def test_stereo_file_mode_rejects_bad_channels(tmp_path):
    sr = 16000
    p = _write_stereo(tmp_path, np.zeros(sr, np.float32) + 0.1,
                      np.zeros(sr, np.float32) + 0.2, sr)
    aec = AecSettings(rate=sr)

    with pytest.raises(ValueError, match="同一个声道"):
        run_files_stereo(Job(id="a", kind="files", title="a"), str(p), aec, RunOptions(),
                         mic_channel=1, ref_channel=1)
    with pytest.raises(ValueError, match="超出范围"):
        run_files_stereo(Job(id="b", kind="files", title="b"), str(p), aec, RunOptions(),
                         mic_channel=0, ref_channel=5)


def test_stereo_file_mode_rejects_mono_file(tmp_path):
    sr = 16000
    p = tmp_path / "mono.wav"
    write_wav(p, np.zeros(sr, dtype=np.float32) + 0.1, sr)
    with pytest.raises(ValueError, match="声道"):
        run_files_stereo(Job(id="m", kind="files", title="m"), str(p),
                         AecSettings(rate=sr), RunOptions())
