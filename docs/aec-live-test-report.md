# AEC 实测报告：`rec_20261103_203827.wav`

| 项 | 内容 |
|---|---|
| 被测文件 | `/Users/xpeng/facesync/sibon/logs/aec_live/rec_20261103_203827.wav` |
| 测音频 | 2026-11-03 |
| 测试代码 | aecdemo @ `4b83833`；结果于 2026-11-05 用当时的 `voice_assistant` 预设重跑确认（该预设此前名为 `xiaozhi`，内容相同） |
| 环境 | macOS arm64 / Python 3.12.11 / `libs/webrtc_apm/macos/arm64/libwebrtc_apm.dylib` |
| 结论 | AEC 链路正常，纯回声段 15–18 dB（`voice_assistant` 预设）；低值主要由**回声/底噪比不足**导致，非算法失效 |

---

## 1. 音频素材

### 1.1 格式

| 属性 | 值 |
|---|---|
| 采样率 | 16 000 Hz |
| 声道 | 2（ch1 = mic，ch2 = ref） |
| 时长 | 37.237 s（595 794 帧） |
| 位深 | PCM 16 bit |
| ch1 电平 | RMS −19.79 dBFS，峰值 1.000（**223 样本削顶**） |
| ch2 电平 | RMS −31.99 dBFS，峰值 0.161（无削顶） |

> 削顶全部发生在近端人声段（3.4–4.3 s、8.7–9.6 s、11.2 s、13.2 s、26.0–26.3 s），与远端播放无关，属于录音增益过大，不影响回声段结论，但会让该段的近端保留率偏乐观。

### 1.2 内容分段（按 20 ms 能量自动判定）

| 序号 | 时间段 | 场景 | 说明 |
|---|---|---|---|
| 1 | 0.0 – 2.9 s | 静音 | mic 底噪，ref 数字静音 |
| 2 | 2.9 – 5.6 s | 近端独讲 | ref 数字静音，mic 有人声 |
| 3 | 5.6 – 15.0 s | 双讲 / 回声 | ref 播放，mic 间歇人声 |
| 4 | 15.0 – 24.5 s | **纯回声 1** | ref 播放，mic 无近端人声 |
| 5 | 24.5 – 27.0 s | 近端独讲 | ref 数字静音 |
| 6 | 27.0 – 36.0 s | **纯回声 2** | ref 播放，mic 无近端人声 |

### 1.3 回声时延

用 GCC-PHAT 在 6 s 窗口上估计，全录音内稳定：

| 窗口 | 估计时延 | 归一化互相关 |
|---|---|---|
| 6–12 s | 202.9 ms | 0.05 |
| 12–18 s | 203.6 ms | 0.03 |
| 18–24 s | 203.6 ms | 0.51 |
| 24–30 s | 203.4 ms | 0.02 |
| 30–36 s | 203.4 ms | 0.53 |
| **全长** | **203.4 ms** | — |

即 **ref 比 mic 晚约 203.4 ms**（mic 中的回声出现在 ref 时间轴之后）。全录音无时钟漂移（首尾估计差 0.13 ms）。

### 1.4 回声耦合强度

| 指标 | 宽带 | 150–7 000 Hz |
|---|---|---|
| ref 电平（纯回声段） | −29 dBFS | −28.7 dBFS |
| mic 电平（纯回声段） | −43.5 dBFS | −44.8 dBFS |
| 耦合损耗 ERL | **−17 dB** | **−17.5 dB** |
| mic 底噪（ref 数字静音段） | −46.5 dBFS | −50.5 dBFS |

**关键：纯回声段 mic 中回声分量约 −46 dBFS，而底噪为 −46.5 dBFS，回声/底噪仅 +0.1 ~ +1.2 dB。** 宽带 ERLE 的物理上限约 3 dB，分频段（150–7 kHz）上限约 5.6 dB。

---

## 2. 测试方法

### 2.1 处理链路

```
rec_xxx.wav (2ch, 16k)
   ├─ ch0 → mic.wav  ┐
   └─ ch1 → ref.wav  ┘
        ↓
   AecCore.process_arrays(mic, ref, input_rate=16000)     # aec_demo/aec.py
        ↓  每 10ms 帧：先 process_reverse_stream(far)，再 process_stream(near)
   WebRTC APM（回声消除 / 高通 / 噪声抑制，按 preset 展开）
        ↓
   aec_out.wav + 分段指标
```

- 因 `read_audio()` 会把多声道降混为单声道（`audioio.py:31`），立体声文件**不能直接**送进 `run_files()`，需先拆成两路。本次拆分命令等价于：
  ```bash
  ffmpeg -i rec_20261103_203827.wav -map_channel 0.0.0 mic.wav -map_channel 0.0.1 ref.wav
  ```
- 处理采样率 16 kHz（与素材一致，无重采样）。
- **延迟参数取 203 ms**（实测值）。项目自带的自动估计在本文件上失效（见 §4.2），故手动指定。
- 帧数 3 723，无 `process_stream` 失败，RTF ≈ 0.012。

### 2.2 预设

| 预设 | AEC | 高通 | NS | 备注 |
|---|---|---|---|---|
| `aec_only` | ✅ | — | — | 观察 AEC 本身能力 |
| `voice_assistant`（默认） | ✅ | ✅ | ✅ level 1 | 语音助手线上配置，等同旧名 `xiaozhi` |

### 2.3 指标口径

- **ERLE** = `10·log10(Σmic² / Σout²)`，按时间段计算。纯回声段的 ERLE 才代表 AEC 能力；人声段的"ERLE"包含人声本身，不代表消除能力。
- **近端段**用 `out/mic` 的**电平变化**衡量（负值 = 人声被削弱）。
- 由于文件模式无近端真值，`near_retention_db` 无法计算（报告中记 null）。

---

## 3. 结果

### 3.1 分段 ERLE

| 分段 | mic | `aec_only` 输出 | `aec_only` ERLE | `voice_assistant` 输出 | `voice_assistant` ERLE |
|---|---|---|---|---|---|
| 纯回声 15.0–24.5 s | −43.8 dB | −49.9 dB | 6.0 dB | −59.0 dB | **15.2 dB** |
| 纯回声 27.0–36.0 s | −44.2 dB | −51.4 dB | 7.2 dB | −62.4 dB | **18.2 dB** |
| 双讲 7.5–12.5 s | −15.2 dB | −17.4 dB | 2.2 dB | −17.4 dB | 2.2 dB |
| 近端独讲 2.95–4.45 s | −10.6 dB | −12.9 dB | 2.4 dB | −12.9 dB | 2.4 dB |

### 3.2 分频段 ERLE（纯回声 27–36 s）

| 频段 | `aec_only` | `voice_assistant` |
|---|---|---|
| 20–150 Hz | 7.0 dB | 18.0 dB |
| 150–500 Hz | 5.7 dB | 16.6 dB |
| 500–2 000 Hz | 2.6 dB | 12.6 dB |
| 2 000–7 000 Hz | 4.6 dB | 13.7 dB |

### 3.3 近端人声损伤

| 场景 | `aec_only` | `voice_assistant` |
|---|---|---|
| 近端独讲（2.95–4.45 s），宽带 | −2.37 dB | −2.38 dB |
| ├ 20–150 Hz | −2.4 dB | −2.4 dB |
| ├ 150–500 Hz | −0.9 dB | −0.9 dB |
| ├ 500–2 000 Hz | −0.3 dB | −0.3 dB |
| └ 2 000–7 000 Hz | 0.0 dB | −0.1 dB |

近端损失集中在 150 Hz 以下，来自 APM 强制的 `echo.enforce_high_pass_filtering`（`aec.py:47`）。把 `enforce_highpass=False` 后近端段损失降为 **0.00 dB**，但纯回声段 ERLE 从 7.24 dB 降到 4.69 dB —— 高通在压低频底噪，对 ERLE 贡献明显。

### 3.4 噪声抑制能力

纯底噪段（0.1–2.0 s，ref 数字静音、无近端人声）：

| 配置 | mic | 输出 | 抑制量 |
|---|---|---|---|
| `aec_only` | −45.2 dB | −48.7 dB | +3.5 dB |
| 仅 NS（level 1） | −45.2 dB | −58.4 dB | +13.2 dB |
| `voice_assistant` | −45.2 dB | −58.6 dB | +13.4 dB |

---

## 4. 结果解读

### 4.1 为什么 ERLE 只有 15–18 dB 而不是 25–35 dB

**主因是录音的信噪比，不是 AEC。** 纯回声段里真正来自扬声器的分量约 −46 dBFS，而 mic 底噪是 −46.5 dBFS。即使 AEC 把回声完全消掉，输出也只能到 −46.5 dBFS 左右，宽带 ERLE 上限 ≈ 3 dB。

实测 `aec_only` 拿到 6–7 dB，**已超过这个"完美 AEC"上限** —— 多出来的部分来自与 ref 无关的固定处理（高通削低频底噪），不是回声消除。`voice_assistant` 的 15–18 dB 则是 AEC 消掉一部分回声后，NS 继续把残余压到底噪以下（输出 −62 dBFS，比原始底噪还低 16 dB）。

以下对照实验说明 **AEC 确实用到了 ref**：把 ref 换成与 mic 无关的信号后，ERLE 明显下降（降幅 ≈ AEC 本身的贡献）。

| 送入的 ref | 纯回声 15.0–24.5 s | 纯回声 27.0–36.0 s |
|---|---|---|
| **真实 ref** | **6.02 dB** | **7.23 dB** |
| 随机噪声（与 mic 无关） | 1.83 dB | 1.96 dB |
| 真实 ref 倒序 | 1.80 dB | 1.93 dB |

> 早期版本用 (15, 36) 合并窗口做过同一实验，得出"换成无关 ref 反而更好"的反向结论 —— 那是统计口径错误：该合并区间含 24.5–27 s 的近端人声，人声能量主导了 ERLE。按纯回声段分开算，结论与直觉一致。

按此推算：把 ref 换成无关信号后 `aec_only` 只剩 1.8–2.0 dB，说明这 6–7 dB 里**约 2 dB 来自与 ref 无关的固定处理**（高通削低频底噪 + 残余 NS），**AEC 本身贡献约 4–5 dB**。这个量级与数据上限吻合 —— 回声/底噪比只有 +0.1~+1.0 dB，AEC 再强也压不到底噪以下。

用 512 tap 最小二乘 FIR 在纯回声段拟合 `mic ≈ h · ref`，线性可预测增益同样只有 **1.7–2.9 dB**（各 2 s 窗口 1.68 ~ 2.92 dB），残差仍接近 mic 电平。三条独立证据指向同一结论：**限制来自录音，不是算法**。

### 4.2 自动延迟估计在本文件上失效

`estimate_delay_ms()`（`aec.py:241`）的两个限制：

1. 跑在 **10 ms 精度**的包络互相关上，且只在 `[0, max_ms]` 内搜索（默认 500 ms）。
2. 判定"无回声"的门限是 `peak < 3·std(corr)`。

本文件真实延迟 203.4 ms，但包络相关的全局峰在 **1.36 s**（前段近端人声与 ref 段错配），500 ms 窗口内又只有噪声峰。实测返回 **0 ms**。

`voice_assistant` / `aec_only` 在延迟 0～500 ms 范围内扫描，纯回声段 ERLE 只在 7.14–7.35 dB 之间波动 → 对本文件这份数据，延迟参数几乎不影响结果，但要得到"正确"的配置仍应手动填 203 ms。

### 4.3 双讲和近端段的表现

双讲段（7.5–12.5 s）和近端段（2.95–4.45 s）的 mic→out 差值为 +2.2 / +2.4 dB，属正常：AEC 在双讲时需要"少消"，NS 会把「人声 + 噪声」一起轻微压一点。分频段看人声主体（150 Hz–4 kHz）只损失 0.8–0.9 dB，人声基本完整。

---

## 5. 本次测试发现的仓库缺陷

以下问题在测试过程中确认，**均未修改源码**。各条均为 2026-11-05 当前代码状态复验的结果。

> 注意：仓库已于 2026-11-05 00:49 提交过一次（`4b83833`），其中预设 `xiaozhi` 更名为 `voice_assistant`；本节结论在新旧两版上均成立。

### 5.1 `off` 预设不是真正的基线 — `config.py:30` + `aec.py:62`

`PRESETS["off"]` 只有 `{"echo": False, "preset": "off"}`，没有 `preprocess` 键；`apply_preset()` 只在 `"preprocess" in p` 时才改写 `highpass` / `ns_enabled`（`aec.py:71`），因此从 `AecSettings()` 默认值继承下来的 `highpass=True, ns_enabled=True` **不会被清掉**。

实测：选择 `off` 预设时，生效配置为 `echo=False, highpass=True, ns_enabled=True`，纯回声段输出与单独开 HP+NS 时完全相同（27–36 s 均为 **5.08 dB**），而非透传；与真正启用 AEC 的 `voice_assistant`（18.20 dB）差距明显，说明这个"基线"其实仍在做大量处理。

额外问题：把 echo/hp/ns **全部关掉**后，APM 的 `process_stream()` 返回 0 但**输出全 0**（440 Hz 正弦输入验证过，output RMS = 0），不是直通。所以仓库里不存在可用的"透传"基线，只能用原始 `mic.wav` 代替。

### 5.2 文件模式的延迟自动估计不可靠 — `aec.py:241`

见 §4.2。建议改为 GCC-PHAT 或提高包络精度，并把搜索范围放宽到整段（不限 500 ms）。

### 5.3 `mic_rate` / `ref_rate` 是死参数 — `runner.py:324`

`run_files()` 接受这两个参数但函数体内从未使用；`read_audio()` 只用 `target_rate`（= AEC 处理率）并自动重采样。前端传了也没效果。要么实现，要么删掉以免误导。

### 5.4 立体声文件会被静默降混 — `audioio.py:31`

`to_mono()` 对多声道直接 `mean(axis=1)`。把双声道 mic/ref 文件直接传给文件模式，两路会被平均成一路，AEC 拿到的 `mic` 和 `ref` 是同一个混音信号，结果无意义且**不会报错**。建议 `read_audio` 增加显式的声道选择参数，或在上传时拒绝明显为 mic/ref 双声道结构的文件并提示拆分。

### 5.5 文件模式无分段指标 — `runner.py:360`

`segments = {"all": (0.0, n/rate)}`，因此 `metrics.json` 的 `erle_db` 是整段平均值。像本文件这种"前 15 s 有人声、后 21 s 纯回声"的素材，整体值只有 2.37 dB，与纯回声段的 18 dB 相差 7 倍，容易被误读为" AEC 无效"。本报告的分段数据是用 `compute_metrics()` 手工传 `segments` 得到的。

### 5.6 环境问题（非仓库代码）

- **解释器不一致**：项目依赖（numpy / soundfile / soxr / fastapi …）装在 pyenv **3.12.11**（`/Users/xpeng/.pyenv/versions/3.12.11/bin/python3`）。当前 shell 默认 `python3` 解析到 **miniconda 3.13.5**，未装 `soundfile`，直接跑会 `ModuleNotFoundError: No module named 'soundfile'`。本报告所有命令都用 `$PY` 显式指定解释器。
- **`cli.py` 不能当脚本跑**：`aec_demo/cli.py:10` 是 `from . import apm`（相对导入），`python aec_demo/cli.py` 会报 `attempted relative import with no known parent package`。
- **`-m` 必须在项目根目录执行**：在 `aecdemo/` 根目录下 `python3 -m aec_demo.cli` 正常；若 `cd aec_demo/` 进入包目录再执行，会 `ModuleNotFoundError: No module named 'aec_demo'`，因为 `-m` 只把当前目录加入 `sys.path`。

这三条都是使用方式问题，不是代码缺陷；除非想把 `cli.py` 改成可直接执行，否则不必改代码。

---

## 6. 复现步骤

### 6.1 环境

```bash
cd /Users/xpeng/Downloads/aecdemo          # 必须是项目根目录
export PY=/Users/xpeng/.pyenv/versions/3.12.11/bin/python3

$PY run.py --selftest                      # 确认 APM 动态库 OK
```

命令行入口三种写法（**都要在项目根目录**执行；§5.6）：

```bash
$PY -m aec_demo.cli --help
$PY -m aec_demo    --help
$PY run.py         --help
```

不要在 `aec_demo/` 包目录内执行 `-m`，也不要 `python aec_demo/cli.py`。

### 6.2 拆分立体声

```bash
$PY - <<'EOF'
import soundfile as sf
x, sr = sf.read('/Users/xpeng/facesync/sibon/logs/aec_live/rec_20261103_203827.wav',
                dtype='float32', always_2d=True)
sf.write('mic.wav', x[:, 0], sr, subtype='PCM_16')
sf.write('ref.wav', x[:, 1], sr, subtype='PCM_16')
EOF
```

### 6.3 跑 AEC + 分段指标

```bash
$PY - <<'EOF'
import numpy as np, soundfile as sf
from aec_demo.aec import AecCore
from aec_demo.config import AecSettings
from aec_demo.analysis import compute_metrics
from aec_demo.audioio import write_wav

sr = 16000
mic = sf.read('mic.wav', dtype='float32')[0]
ref = sf.read('ref.wav', dtype='float32')[0]

segments = {
    "echo_only_1": (15.0, 24.5),
    "echo_only_2": (27.0, 36.0),
    "double_talk": (7.5, 12.5),
    "near_only":   (2.95, 4.45),
}

for preset in ("aec_only", "voice_assistant"):
    st = AecSettings(preset=preset, rate=sr, delay_ms=203)
    with AecCore(st, input_rate=sr) as core:
        out = core.process_arrays(mic, ref, input_rate=sr)
    write_wav(f"aec_out_{preset}.wav", out, sr)
    m = compute_metrics(sr, ref, mic, out, None, segments, delay_ms=203)
    print(preset)
    for name, v in m["segments"].items():
        print(f"  {name:12s} mic={v['mic_dbfs']:7.1f} out={v['out_dbfs']:7.1f} ERLE={v['erle_db']:6.2f} dB")
EOF
```

### 6.4 WebUI 方式

```bash
$PY run.py          # http://127.0.0.1:8770
```

选「音频文件」→ mic/ref 分别上传 §6.2 拆出的两个文件 → **取消勾选「自动估计延迟」，手动填 203** → 开始测试。

---

## 7. 产物清单

`output/20261103_aec_live/`：

| 文件 | 说明 |
|---|---|
| `aec_in.wav` | AEC 输入合轨，L = mic / R = ref |
| `mic.wav` / `ref.wav` | 拆分后的两路原始信号 |
| `aec_out.wav` | `voice_assistant` 预设输出（主结果） |
| `aec_out_aec_only.wav` | `aec_only` 预设输出 |
| `aec_out_voice_assistant.wav` | 同 `aec_out.wav`，按预设名单独保存 |
| `metrics.json` | 分段指标（`voice_assistant` 为主 + 两种预设对比） |
| `metrics_compare.json` | 原始对比数据（含首版 15–36 s 合并段的旧口径，见下注） |
| `levels_per_second.csv` | 逐秒 mic/ref/两种输出的电平 |
| `plot.png` | 波形 + 逐 50 ms ERLE + 纯回声段频谱对比 |

> `metrics_compare.json` 里只保留分段口径（`echo_only_1` / `echo_only_2`），不再使用早期 (15, 36) 的合并口径 —— 该区间含 24.5–27 s 的近端人声，合并后 ERLE 会被拉到 2.6 dB，容易被误读为"AEC 无效"。
>
> `metrics.json` 的 `summary.erle_db` 为 `null`，因为汇总只认名为 `echo_only` 的段，而本报告用的是 `echo_only_1/2`；请直接看 `segments` 下的分项。若要填 `summary`，把两段合成一个 `echo_only` 键即可（但会损失"两段是否一致"的信息，本次两段分别是 15.2 / 18.2 dB）。

---

## 8. 结论与建议

**结论**：AEC 链路工作正常。`voice_assistant` 预设在本录音的纯回声段取得 15–18 dB 宽带 ERLE，人声段保留完好（150 Hz–4 kHz 损失 < 1 dB）。

**要提高 ERLE 测值，应改录音条件而非调算法**：

1. 提高回声/底噪比。目前仅 +0.1 ~ +1.2 dB，需提升到 15 dB 以上才能测出 25–35 dB 的典型值。手段：调高扬声器音量、降低 mic 前置增益（当前近端人声已削顶，说明增益有富余），或缩短 mic 到扬声器的距离。
2. 检查 ref 的通路。ref 比 mic 中实际回声高约 17 dB，若 ref 取的是播放前数字信号而实际经过功放/扬声器增益，建议取与回声同增益的点，或确保 ref 未被额外衰减。
3. 录音增益留 6–10 dB headroom，消除 223 个削顶样本。
4. 若目标是验证算法而非评估设备，用仓库的「离线仿真」模式（合成 mic/ref 且已知近端真值）更合适；文件模式受录音质量影响太大。

**代码侧**（见 §5）：建议优先修 `off` 预设（5.1）和立体声降混（5.4）——这两个会直接让人得到错误结论；`estimate_delay_ms`（5.2）和分段指标（5.5）次之。

---

## 附录 A 逐秒电平

完整数据见 `output/20261103_aec_live/levels_per_second.csv`，摘录关键区间（dBFS）：

| t (s) | mic | ref | `aec_only` | `voice_assistant` | 场景 |
|---:|---:|---:|---:|---:|---|
| 0 | −45.2 | — | −49.1 | −59.3 | 静音 |
| 1 | −45.4 | — | −48.6 | −58.3 | 静音 |
| 2 | −35.1 | — | −37.5 | −38.7 | 近端起 |
| 3 | −10.1 | — | −12.6 | −12.6 | 近端独讲（削顶） |
| 4 | −14.6 | — | −16.6 | −16.6 | 近端独讲 |
| 5 | −46.9 | −39.4 | −50.0 | −61.8 | 回声起 |
| 7 | −32.6 | −28.7 | −35.2 | −35.9 | 双讲 |
| 8 | −13.9 | −32.3 | −15.6 | −15.8 | 近端 |
| 15 | −44.6 | −51.6 | −46.0 | −53.8 | 纯回声 |
| 17 | −43.6 | −31.3 | −51.5 | −62.6 | 纯回声 |
| 20 | −42.1 | −28.6 | −51.3 | −60.1 | 纯回声 |
| 22 | −42.0 | −26.6 | −51.2 | −63.3 | 纯回声 |
| 23 | −42.2 | −29.3 | −51.0 | −60.1 | 纯回声 |
| 24 | −43.2 | −28.6 | −50.7 | −59.2 | 纯回声 |
| 25 | −17.0 | −30.2 | −19.9 | −20.0 | 近端（双讲） |
| 26 | −14.2 | −109.3 | −16.6 | −16.6 | 近端独讲 |
| 27 | −46.3 | — | −50.2 | −62.4 | 纯回声起 |
| 28 | −46.0 | −32.5 | −51.7 | −63.7 | 纯回声 |
| 30 | −43.6 | −29.9 | −52.1 | −62.6 | 纯回声 |
| 32 | −43.2 | −30.3 | −53.1 | −65.0 | 纯回声（最佳） |
| 35 | −43.7 | −30.8 | −51.6 | −63.9 | 纯回声 |
| 36 | −47.7 | — | −51.0 | −62.6 | 尾部静音 |

「—」表示 ref 全程数字静音（电平低于 −240 dBFS）。

---

## 附录 B 测试环境快照

```
$ python run.py --selftest
系统: Darwin / arm64
APM 动态库: libs/webrtc_apm/macos/arm64/libwebrtc_apm.dylib  → 通过
numpy 2.5.1 / soundfile 0.13.1
```

- 解释器：`/Users/xpeng/.pyenv/versions/3.12.11/bin/python3`（Python 3.12.11）
- `mobile_mode` 探测：未使用 mobile 预设，未触发降级逻辑
- 处理规模：3 723 帧 × 10 ms，RTF 0.011 ~ 0.012
- 自检时音频设备：3 个输入 / 3 个输出（仅查询未打开）
