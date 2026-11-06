# 为什么同一份 aec_in，aecdemo 比树莓派上的 sibon 好一些

分析对象：

| 文件 | 说明 |
|---|---|
| `rec_20261106_011024_aec_in.wav` | 16 kHz 双声道，ch1 = mic，ch2 = ref（同 `up_db2cd55092.wav`，md5 一致） |
| `rec_20261106_011024_aec_out.wav` | sibon 在 Pi 上的 AEC 输出，16 kHz 单声道 |
| sibon 代码 | `robot/aec/{engine,apm,config}.py`、`robot/config/aec.yaml` |

---

## 结论先行

**差的不是 AEC 配置，是延迟对齐方向反了，而且恰好撞上最坏的那个值。**

sibon 的做法是「先把参考音在时间轴上提前 D 毫秒，再告诉 APM 延迟为 0」：

```python
# robot/aec/engine.py:274
far = self._ref.get(end - dur - self._delay_ms / 1000.0, end - self._delay_ms / 1000.0)
...
# robot/aec/engine.py:173
self._apm.set_stream_delay_ms(0)
```

这段录音里，真实回声延迟 **42 ms**。而 Pi 上实际生效的预延迟落在 **42–46 ms**——
两者相等。这正好是 sibon 自己注释里点名的最坏情况（`engine.py:377-379`）：

> 预延迟必须**略小于**真实回声延迟……D 给大了（哪怕刚好相等）回声就出现在 AEC 的「未来」，永远对不上。

用 sibon 的引擎类离线重放这段录音，能精确复现 Pi 上的现象：

| 预延迟 D | 重放 ERLE | 说明 |
|---:|---:|---|
| 0 ms | 4.31 dB | |
| 20 ms | 4.34 dB | |
| 38 ms | 4.86 dB | D < 真实延迟，仍能工作 |
| **42 ms** | **3.52 dB** | ← Pi 实际值 3.47 dB |
| 44 ms | 3.16 dB | |
| 60 ms | 2.38 dB | |
| 100 ms | 1.31 dB | D 越大越差 |
| 200 ms | 4.69 dB | 反而回升（见下） |
| 410 ms | 4.70 dB | |

波形相关系数进一步钉死了这一点：Pi 输出与 `D=42` 重放的相关系数 **0.868**，
而与其他所有候选值（0/20/60/100/200/300/410）都在 0.26–0.85，断崖式分离。

**aecdemo 好一些，是因为它用「直通」语义**——参考音不提前平移，直接
`set_stream_delay_ms(60)`，让 APM 内部的前瞻缓冲自己去找 42 ms 的回声。
同一份文件上它是 4.7 dB，sibon 是 3.5 dB，差 1.2 dB，与你听到的「好一些」吻合。

---

## 一、为什么 sibon 的 D 会恰好等于 42

配置里写的是 200，但自动估计会改它（`engine.py:315-389`）。我在重放里插桩记录了估计器的
中间结果：

```
音频时刻   measured_ms   corr
   6.0s          41       0.700
   8.0s          41       0.426
  10.0s         121       0.432
  12.0s         484       0.756
```

估计器在 6.0s 就算出了 **41 ms**，而且相关度 0.70，是整段里最可信的一次。
但 `target = measured - delay_margin_ms = 41 - 40 = 1`，与当前值 200 相差 199 > 100，
于是采纳 `D = 1`。**从这一刻起，参考音被平移了 1 ms，AEC 立刻能对上 41 ms 的残差延迟，
开始正常工作**——这也解释了为什么 Pi 实测的等效预延迟是 42 而不是 200。

后面几次估计在 121 / 484 之间摆动，是双讲和近端人声把包络相关带偏了。
`engine.py:384` 的死区（`abs(target - self._delay_ms) <= 100` 就返回）挡住了这些坏值，
所以没有再乱改——这一点设计是对的。

> 顺带一个小问题：`delay_margin_ms=40` 的语义是「宁大勿小」，但在这套自减逻辑里
> 它变成了「让 D 尽量小」。当真实延迟本身就小于 40 ms 时，`target` 会被钳到 0，
> 相当于完全放弃预对齐，把全部工作丢回给 `set_stream_delay_ms(0)`。
> 本录音 42 ms 的情况刚好擦边。

---

## 二、两种延迟对齐语义，差多少

这不是「参数没调好」，是两套语义的本质区别：

| | aecdemo | sibon |
|---|---|---|
| 参考音平移 | 不平移 | 前移 D |
| `set_stream_delay_ms(...)` | 填 D | 恒为 **0** |
| 谁负责对齐 | APM 内部自适应 | 应用层 |

用同一份合成信号（真实回声延迟 42 ms）跑 sibon 引擎，扫 D：

| 预延迟 D | ERLE |
|---:|---:|
| 0 ms | **36.90 dB** |
| 20 ms | 36.82 dB |
| 42 ms | 9.73 dB |
| 60 ms | 20.18 dB |
| 100 ms | 8.11 dB |
| 200 ms | 8.53 dB |
| 410 ms | 7.03 dB |

`D=0` 反而最好。原因是 `set_stream_delay_ms(0)` 让 APM 从零开始搜索，
它内部有足够的前瞻缓冲吃掉 42 ms；一旦应用层把参考音提前了 D，APM 就只能在
`[0, 真实延迟 - D]` 这个越来越窄的区间里找，D 越大越容易被挤到负区间——
即「回声在 AEC 的未来」。

**这不代表 sibon 的设计是错的。** 它的动机写在 `engine.py:14-19`：实时链路里
mic 是 100 ms 帧、参考音是 20 ms 块，采样率也不同，必须按时间戳对齐。
用显式平移能容忍数百毫秒的系统缓冲延迟，这是 aecdemo 的离线模型不具备的能力。

问题在于：**当真实延迟本来就小（几十毫秒）时，这套机制是负收益。**
它把一个本来免费的 36 dB 变成了 10 dB。

---

## 三、这份录音能测出的上限是多少

抛开对齐问题，这段信号本身的条件也决定了天花板。逐 400 ms 窗计算 mic 与 ref 的相关：

| 窗中心 | mic | ref | corr@42ms | 最佳 lag |
|---:|---:|---:|---:|---:|
| 5.20 s | −35.5 | −33.3 | 0.241 | 40.8 ms |
| 5.60 s | −40.1 | −35.6 | 0.390 | 41.4 ms |
| 6.00 s | −49.5 | −52.3 | 0.004 | 111.8 ms |
| 6.40 s | −32.8 | −30.6 | 0.399 | 41.4 ms |
| 6.80 s | −36.2 | −33.7 | 0.028 | 41.3 ms |
| 7.20 s | −35.8 | −34.8 | 0.045 | 40.7 ms |
| 8.40 s | −39.4 | −31.5 | 0.057 | 40.7 ms |
| 8.80 s | −38.4 | −34.1 | 0.413 | 41.4 ms |
| 9.20 s | −34.3 | −32.5 | 0.249 | 42.1 ms |

延迟非常稳定（40.7–42.1 ms，抖动 < 1.5 ms），但**相关强度只在 0.03–0.41 之间跳**。
一部分是播放内容本身的起伏（ref 电平在 −29 到 −52 dBFS 间变化），
另一部分是双讲/近端人声在稀释相关。

用更贴近 AEC 工作方式的指标——每 0.5 s 独立拟合一个 60 ms 的 FIR：

| 窗中心 | mic | 拟合后残差 | 可消除 |
|---:|---:|---:|---:|
| 5.45 s | −35.4 | −36.3 | 0.9 dB |
| 6.45 s | −33.8 | −33.9 | 0.1 dB |
| 6.95 s | −36.9 | −37.3 | 0.4 dB |
| 7.45 s | −36.7 | −37.4 | 0.7 dB |
| 8.45 s | −40.5 | −41.0 | 0.5 dB |
| 8.95 s | −39.2 | −39.6 | 0.4 dB |

**局部线性可消除量中位数只有 0.5 dB。** 这说明 mic 里除了回声，还有相当多
与 ref 不相关的成分（近端人声 + 环境噪声），AEC 无论怎么调都碰不到它们。

用背景电平做独立校验：

```
mic 在 ref 静音段（背景） = −47.2 dBFS
mic 在 ref 活跃段         = −36.4 dBFS
高于背景的能量            = 91.7%   → 理论上限 10.8 dB
```

**所以这段录音的物理上限大约 10–11 dB（理想线性回声路径）。**
aecdemo 做到 4.7 dB、sibon 做到 3.5 dB，都在合理范围内——
想再往上提，要改的是录音条件（回声更干净、少人声混入），不是参数。

---

## 四、还有两个次要差异

### 1. AGC 都关，但 NS 档位不同

| | sibon | aecdemo |
|---|---|---|
| preset | `voice_assistant` → ns_level **1** | 默认 `voice_assistant`；WebUI 默认也是 **1** |
| AGC | 关 | 关 |

这一项两边其实是一致的，不是差异来源。

### 2. 采样率 16k vs 48k

sibon 固定 16 kHz（mic 原生），aecdemo 在这份文件上也是 16 kHz（文件采样率）。
**这项没有影响**——两边都跑 16k。

---

## 五、建议怎么改

### 方案 A（最小改动，推荐）：延迟小时退回直通语义

在 `engine.py` 里加一个阈值：真实延迟小于某个门限（比如 120 ms）时，
不做应用层平移，把 D 直接交给 APM。

```python
# engine.py:274 附近
if self._delay_ms >= self.PRE_SHIFT_MIN_MS:      # 比如 120
    far = self._ref.get(end - dur - self._delay_ms / 1000.0, end - self._delay_ms / 1000.0)
    apm_delay = 0
else:
    far = self._ref.get(end - dur, end)          # 不平移
    apm_delay = self._delay_ms                    # 交给 APM

self._apm.set_stream_delay_ms(apm_delay)
```

代价是 `set_stream_delay_ms` 要多调用一次（`engine.py:173` 改成条件设置即可）。
收益：本录音从 3.5 dB 回到 4.7 dB；在小延迟场景上避免最坏值。

### 方案 B：让 APM 两种模式都试，选好的那个

`engine.py` 已经有 `_rebuild()`（预延迟变化超过 100 ms 时重建 APM）。
可以在重建时轮流尝试「平移 / 直通」两种语义，用 ERLE 挑更好的。
代价是收敛期更长，实现复杂度高。

### 方案 C（顺带）：修自动估计的 margin 语义

`delay_margin_ms` 当前是「让 D 更小」。如果意图确实是「宁小勿大」，
注释和变量名应该改；如果意图是「留安全余量」，那应该把 margin 加到
`set_stream_delay_ms` 那一侧而不是从 D 里扣。

### 参数层面能立刻做的

`robot/config/aec.yaml` 里 `ref_delay_ms: 200` 与注释里说的 450 ms 不符。
但依据上面的分析，**单纯把 200 改大或改小都救不了**——真正的问题是预平移语义本身。
先按方案 A 改造，再用 `tools/analyze_aec_rec.py` 复测。

---

## 复现方法

```bash
# 用 sibon 自己的引擎重放这段录音，扫预延迟
python - <<'EOF'
import sys, numpy as np, soundfile as sf
sys.path.insert(0, '/Users/xpeng/facesync/sibon')
from robot.aec.config import AecConfig
from robot.aec.engine import WebRtcAec

p = '/Users/xpeng/Downloads/rec_20261106_000801/'
inp, sr = sf.read(p + 'rec_20261106_011024_aec_in.wav', dtype='float32', always_2d=True)
mic, ref = inp[:, 0], inp[:, 1]
db = lambda x: 20 * np.log10(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2)) + 1e-12)

for D in (0, 20, 42, 60, 100, 200, 410):
    cfg = AecConfig(mode="on", preset="voice_assistant", input_rate=16000,
                    ref_rate=16000, ref_delay_ms=D, auto_delay=False)
    eng = WebRtcAec(cfg)
    outs = []
    for i in range(0, len(mic) - 1600, 1600):
        t = 1e5 + i / sr                       # 必须给大数值，0.0 会被当成 None
        eng.add_ref((ref[i:i+1600] * 32768).astype(np.int16), t)
        y = eng.process((mic[i:i+1600] * 32768).astype(np.int16).tobytes(), t)
        outs.append(np.frombuffer(y, dtype=np.int16).astype(np.float32) / 32768)
    o = np.concatenate(outs)[:len(mic)]
    print(f"D={D:4d}ms  ERLE={db(mic)-db(o):.2f} dB")
EOF
```

注意：`add_ref` 的时间戳必须给大数值（如 `1e5`）。`_RefTimeline.push`
（`engine.py:107`）里写的是 `t if t else time.monotonic()`，
时间戳恰好为 `0.0` 时会被当成 `None`，导致参考时间轴起点错乱。
实时场景下 `time.monotonic()` 永远是大数，碰不到；但离线回放/自检会踩到。
