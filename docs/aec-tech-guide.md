# AEC 技术手册：从原理到二次开发与移植

> 面向读者：**第一次接触回声消除**的工程师、需要把这个 demo 接到自己产品里的开发者、
> 以及要把 `libwebrtc_apm` 换到新平台（树莓派 / 嵌入式板卡 / 国产 SoC）的移植者。
>
> 读完你应该能做到三件事：
> 1. 说清楚「回声是怎么被消掉的」，以及为什么延迟填错就完全无效；
> 2. 看懂本项目每一个文件在干什么、每 10 ms 发生了什么；
> 3. 在不动核心算法的前提下，加预设、加测试模式、加指标，或把动态库换掉。

---

## 目录

- [0. 五分钟上手](#0-五分钟上手)
- [1. 技术框架](#1-技术框架)
- [2. 原理篇：回声到底是怎么消掉的](#2-原理篇回声到底是怎么消掉的)
- [3. 流程图](#3-流程图)
- [4. 关键代码导读](#4-关键代码导读)
- [5. 移植指南](#5-移植指南)
- [6. 二次开发指南](#6-二次开发指南)
- [7. 新手实操演练](#7-新手实操演练)
- [8. 故障排查速查表](#8-故障排查速查表)
- [9. 术语表](#9-术语表)

---

## 0. 五分钟上手

### 0.1 这个项目解决什么问题

一个带喇叭和麦克风的设备，**喇叭放出来的声音会被自己的麦克风收进去**，对方就会听到自己的回声。
这叫**声学回声（acoustic echo）**。AEC（Acoustic Echo Cancellation，回声消除）就是把这个
「自己放出去、又被自己收回来」的部分从麦克风信号里减掉。

本项目是一个**测试台**：它把 WebRTC 的音频处理模块（APM）包成 Python 接口，
让你能在三种输入上验证 AEC 效果——程序合成的信号、你手上的录音、真实设备实时采集。

### 0.2 三条命令

```bash
cd aecdemo                                   # 必须是项目根目录，原因见 §5.2
pip install -r requirements.txt

python run.py --selftest    # ① 检查动态库能不能加载
python run.py --simulate    # ② 无界面跑一次仿真，产物在 output/
python run.py               # ③ 打开 WebUI，http://127.0.0.1:8770
```

第一条命令的输出里，`APM 动态库自检: 通过` 是后续一切的前提。**不过这一条，先别往下读。**

### 0.3 你需要建立的最小认知

| 概念 | 一句话 | 为什么重要 |
|---|---|---|
| **near / mic** | 麦克风采到的信号（= 近端人声 + 回声 + 噪声） | 这是要被处理的信号 |
| **far / ref** | 扬声器正在播放的信号 | **没有它，AEC 根本无法工作** |
| **延迟 delay** | ref 从「送出」到「被 mic 收到」经过的时间 | 填错 = 对不齐 = 消不掉 = 白跑 |
| **10 ms 帧** | APM 只接受固定 160/480 个样本一块 | 喂错长度直接报错 |
| **ERLE** | 回声被压下去多少 dB | 唯一能证明「AEC 起作用了」的指标 |

---

## 1. 技术框架

### 1.1 分层架构

```
┌──────────────────────────────────────────────────────────────────────┐
│  入口层                                                                │
│    run.py  →  aec_demo/cli.py      命令行：--selftest / --simulate / 服务 │
│    webui/  →  aec_demo/server.py   FastAPI + 静态页，浏览器操作           │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  提交任务（不阻塞请求）
┌───────────────────────────────▼──────────────────────────────────────┐
│  编排层   aec_demo/runner.py                                          │
│    JobManager  单线程队列，一次只跑一个任务（避免抢麦克风）               │
│    run_simulation / run_files / run_device / run_compare              │
│    统一：进度上报 → 日志 → 产物落盘 → metrics.json / plot.png           │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  输入两路 float32 信号
┌───────────────────────────────▼──────────────────────────────────────┐
│  信号层   aec_demo/audioio.py   读写、重采样、多声道拆分（不降混）        │
│          aec_demo/simulate.py   合成 far/mic（含房间响应、非线性、噪声）  │
│          aec_demo/analysis.py   ERLE / 分频段 / 近端保留 / 绘图          │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  mic, far → (采样率, 延迟, 预设)
┌───────────────────────────────▼──────────────────────────────────────┐
│  引擎层   aec_demo/aec.py                                             │
│    AecCore          分帧、near/far 配对、重采样、逐帧统计                │
│    estimate_delay_ms 包络互相关估延迟                                  │
│    apply_preset     预设展开成具体开关                                  │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  int16 缓冲区 + 不透明句柄
┌───────────────────────────────▼──────────────────────────────────────┐
│  绑定层   aec_demo/apm.py                                             │
│    ctypes 结构体 / 函数签名 / 平台路径解析 / 可用性探测                   │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  C ABI
┌───────────────────────────────▼──────────────────────────────────────┐
│  原生层   libs/webrtc_apm/<系统>/<架构>/libwebrtc_apm.{so,dylib,dll}   │
│    WebRTC APM：高通 → 回声消除 → 噪声抑制 → 增益控制                     │
│    （预编译，本项目不含其源码）                                          │
└──────────────────────────────────────────────────────────────────────┘
```

**为什么这样分层。** 关键在**引擎层和绑定层之间那条线**：线以上全部是纯 Python + numpy，
可以离线跑、可以单测、可以在 CI 上跑；线以下只有一个动态库和一份 ABI 约定。
这意味着**换平台时你几乎不用动上面五层**——只要新平台能提供同样符号名的动态库。

### 1.2 模块职责表

| 文件 | 行数 | 职责 | 改动频率 |
|---|---:|---|---|
| [aec_demo/apm.py](aec_demo/apm.py) | 451 | ctypes 绑定：结构体布局、函数签名、平台路径、自检、mobile 探测 | 极低（除非升级动态库） |
| [aec_demo/aec.py](aec_demo/aec.py) | 277 | 处理内核：分帧循环、near/far 配对、重采样、延迟估计、预设展开 | 低 |
| [aec_demo/config.py](aec_demo/config.py) | 142 | 全部常量与参数 dataclass（采样率、预设、仿真参数、运行选项） | **高**（调参都在这） |
| [aec_demo/runner.py](aec_demo/runner.py) | 691 | 四种模式的任务编排、进度、产物落盘 | 中 |
| [aec_demo/analysis.py](aec_demo/analysis.py) | 297 | ERLE / 分频段 / 近端保留 / 曲线 / 绘图 | 中 |
| [aec_demo/simulate.py](aec_demo/simulate.py) | 287 | 合成 far/mic、房间脉冲响应、类语音信号 | 低 |
| [aec_demo/audioio.py](aec_demo/audioio.py) | 190 | 读（三级兜底）、逐声道重采样、多声道拆分、写 WAV | 低 |
| [aec_demo/server.py](aec_demo/server.py) | 532 | HTTP 接口、设备枚举与去重、上传、静态页 | 中 |
| [aec_demo/cli.py](aec_demo/cli.py) | 121 | argparse 入口（服务 / 自检 / 仿真 / 文件） | 低 |

### 1.3 一次处理的数据流

不管是哪种输入，最终都收敛成同一个形状：**两条等长的单声道 float32 数组 + 一个采样率**。

```
                 ┌── 离线仿真 ──→ simulate() ──────┐
                 │     合成 far/mic/echo/near/噪声   │
 输入来源 ────────┼── 音频文件(两个) ─→ read_audio() ┤
                 │     两路 WAV 分别重采样到 rate    │
                 ├── 音频文件(一个) ─→ 按声道拆分 ──┤
                 │     read_audio_multi()           │
                 │     多声道 → mic = ch[i], ref=ch[j]│
                 │                                  ▼
                 └── 设备实测 ──→ sounddevice 回调 ──┐   mic: float32[N]
                       边播 ref 边录 mic             └─→ far: float32[N]
                                                        │
                                    ┌───────────────────▼──────────────┐
                                    │  AecCore.process_arrays(mic, far) │
                                    │   ① 重采样到 APM 采样率            │
                                    │   ② 截到 10ms 整数倍              │
                                    │   ③ 逐帧：feed far → process mic  │
                                    └───────────────────┬──────────────┘
                                                        │
                                              out: float32[N]
                                                        │
                        ┌───────────────────────────────┼──────────────────────┐
                        ▼                               ▼                      ▼
              写 aec_out.wav / mic.wav        compute_metrics()         make_plot()
                 / ref.wav / aec_in.wav        ERLE、分段、近端保留       波形 + ERLE 曲线 + 频谱
```

### 1.4 目录结构

```
aecdemo/
├── run.py                    启动壳（9 行，转调 cli.main）
├── start.sh / start.bat      自动挑解释器 + 装依赖 + 启动
├── requirements.txt
├── pytest.ini
├── aec_demo/                 ← 全部 Python 逻辑
│   ├── apm.py                绑定层
│   ├── aec.py                引擎层
│   ├── config.py             参数中心
│   ├── runner.py             编排层
│   ├── analysis.py           指标与绘图
│   ├── simulate.py           信号仿真
│   ├── audioio.py            音频 IO
│   ├── server.py             HTTP
│   └── cli.py                命令行
├── libs/webrtc_apm/          ← 原生库，按 系统/架构 分目录
│   ├── macos/{x64,arm64}/libwebrtc_apm.dylib
│   ├── linux/{x64,arm64}/libwebrtc_apm.so
│   └── windows/{x64,x86}/libwebrtc_apm.dll
├── webui/                    纯前端静态页（app.js / index.html / style.css）
├── tests/test_offline.py     离线单测（不需要音频设备）
├── docs/                     文档
└── output/<时间戳_任务ID>/    每次运行的产物
```

---

## 2. 原理篇：回声到底是怎么消掉的

这一节是全文最需要读懂的部分。**如果只想跑起来，可以跳到 §3；如果要移植或调参，必须读完 §2.3 和 §2.4。**

### 2.1 回声从哪来

```
   远端说话人 A                          本地设备 B                           远端说话人 A
       │                                     │                                    │
       │  ① 语音 → 网络 → 到达 B              │                                    │
       │─────────────────────────────────────▶                                    │
       │                                     │                                    │
       │                              ② 扬声器播放                              │
       │                                     │ ~~~~声波~~~~▶ 空气传播 ~~~~▶       │
       │                                     │                         麦克风 ◀──┤
       │                                     │                            │       │
       │                              ④ 网络 ◀── ③ mic 收进自己的播放声音 ─┘       │
       │◀─────────────────────────────────────                                    │
       │                                                                          │
       │  A 听到自己刚才说的话 = 回声                                              │
```

**回声 = 扬声器的输出，经过房间（延迟 + 反射 + 衰减 + 失真）后，被自己的麦克风收进去。**

关键观察：如果 B 知道「我刚才送去扬声器的是什么」（②），并且知道「它经过房间后会变成什么样」（③ 的变换），
那就能**预测**出麦克风里的回声成分，然后从麦克风信号里减掉。

- 「我刚送出去的是什么」= **far 信号**（也叫 ref / 参考信号 / render stream）
- 「经过房间后变成什么」= **回声路径**（echo path），用**自适应滤波器**在线学习
- 「从 mic 里减掉」= 得到 **out**（上行信号，送去网络）

**这就是 AEC 的全部核心思想。** 后面所有复杂度都在解决两件事：
① 回声路径会变（人走动、音量变、房间不同）；② 本地人在说话时（双讲）不能乱减。

### 2.2 为什么没有 ref 就做不了

市面上确实有「免参考」的回声抑制（residual echo suppression），但那是**统计式**的：
它只能猜「麦克风里这段能量像不像回声」，猜不准就会误伤人声。

有 ref 的 AEC 是**确定性**的：它拿 ref 和 mic 做相关，能算出「mic 里有多少成分和 ref 同源」。
这解释了实测报告里的一个对照实验（`docs/aec-live-test-report.md` §4.1）：

| 送进 AEC 的 ref | 纯回声段 ERLE |
|---|---|
| 真实 ref | **6.02 / 7.23 dB**（两段） |
| 随机噪声（与 mic 无关） | 1.83 / 1.96 dB |
| 真实 ref **倒序** | 1.80 / 1.93 dB |

换成无关信号后，剩下的那 ~2 dB 是「与 ref 无关的固定处理」（高通削低频底噪 + 残余 NS），
**真正来自 AEC 的贡献是那 4–5 dB 差值。** 倒序 ref 也一样失效——说明 AEC 依赖的是
**时序上严格对应的** far 信号，不是「长得像」的 far。

> ⚠️ 一个容易犯的统计错误：早期把 15–36 s 当成一个纯回声窗口算，结论会反过来。
> 因为 24.5–27 s 有一段近端人声，能量主导了整体 ERLE。**算 ERLE 只能用在纯回声段。**

### 2.3 自适应滤波器：AEC 的发动机

把回声路径建模成一个 FIR 滤波器 `h`：

```
预测回声  ŷ(n) = Σ h(k) · x(n - k)        x = ref 信号，h = 待学习的滤波器系数
残差      e(n) = mic(n) - ŷ(n)            e 就是「消掉回声后的输出」
更新      h ← h + μ · e(n) · x(n-d)       最简 NLMS 形式，μ 是步长
```

用框图看：

```
                          ┌──────────────────────────────┐
      ref x(n) ──────────▶│  延迟对齐 (d 个样本)          │
                          └───────────┬──────────────────┘
                                      │
                ┌─────────────────────┼──────────────────────┐
                │                     │                      │
                │            ┌────────▼─────────┐            │
                │            │ 自适应滤波器 h    │  预测回声   │
                │            │  ŷ = h * x        │───────┐    │
                │            └────────▲─────────┘       │    │
                │                     │ 误差反馈        ▼    │
                │                     │          ┌───────────┴──┐
                │                     └──────────┤  e = mic - ŷ │──── e ──▶ 输出
                │                                └──────▲───────┘
   mic ─────────┼───────────────────────────────────────┘
                │
                └────────────── 双讲检测器（决定要不要更新 h）
```

**一句话解释 `e = mic - ŷ`：** 滤波器学会「声音从喇叭到麦克风会发生什么」，然后按 ref
把这段声音**重放出来**，从 mic 里减掉。剩下的 `e` 就是人声 + 没消干净的回声 + 噪声。

**为什么它必须在线自适应，不能事先算好？** 回声路径 `h` 取决于：
喇叭和麦克风的距离/朝向、房间混响、音量档位、设备外壳共振……
换一台机器就完全不同，而且**用户挪动一下笔记本就会变**。所以只能边跑边学。

**WebRTC 的实现比这复杂得多**：分块频域（partitioned block frequency domain）、
多延迟块（允许多个候选延迟并行）、非线性处理（NLP，压制线性滤波后的残余）、
双讲检测、舒适噪声……但骨架就是上面这个。

### 2.4 near/far 双流与延迟对齐 —— 最容易出错的地方

APM 要求你**交替喂两路信号**：

```
时间轴 ──────────────────────────────────────────────────▶
far  (扬声器)  [帧0][帧1][帧2][帧3][帧4]...
mic  (麦克风)  [帧0][帧1][帧2][帧3][帧4]...
                ↑ 帧 i 的 far 和帧 i 的 mic 是「同一时刻」采到的
```

但物理上，**far 帧 i 播出的声音，要过 delay 毫秒才会出现在 mic 里**。AEC 需要知道这个 delay
才能把 `h` 对齐到正确的位置。API 就是干这个的：

```python
apm.set_stream_delay_ms(203)    # 「我这套设备，回声延迟 203 ms」
```

**填错会怎样？实测结论和教科书说法不一样。** 下面是本仓库仿真模式（`aec_only`，
默认房间响应/非线性/二次路径）扫出来的 ERLE 真值表，横轴是「你填的 delay」，
纵轴是「仿真的真实 delay」：

| 真实 delay ↓ / 填的值 → | 0 | 25 | 50 | 75 | 100 | 150 | 200 | 300 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **0 ms** | 8.3 | 7.6 | 6.0 | 6.2 | 5.7 | 7.1 | 7.2 | 5.3 |
| **50 ms** | **30.4** | **30.5** | **30.6** | **30.4** | 16.2 | 16.2 | 16.2 | 16.2 |
| **100 ms** | **30.0** | **30.0** | **30.0** | **29.9** | **29.7** | 19.5 | 19.5 | 19.5 |
| **200 ms** | **29.3** | **29.4** | **29.5** | **29.1** | **29.2** | **28.8** | **28.8** | 20.1 |

（单位 dB，纯回声段 ERLE）

读这张表能得出三条**很反直觉、但很重要**的结论：

**① 填小了没关系，填大了才掉性能。**
真实 100 ms 时，你填 0、25、50、75、100 都是 29.7–30.0 dB，**几乎无差别**。
真实 200 ms 时填 0 也有 29.3 dB。原因是 APM 内部有一个**前瞻缓冲**：
它会拿 far 去和**未来**一段 mic 做相关，只要真实回声还在它的搜索范围内，填小了就自己找回来了。
但**填大了**（把参考点推到回声之后）就找不回来了。

**② 过估有一个明确的容限，大约 +35 ms。**
以真实 200 ms 为例，逐 5 ms 细扫：

```
Δ = -20 → ERLE 28.8 dB        ← 欠估：完全无影响
Δ =   0 → ERLE 28.7 dB
Δ = +30 → ERLE 28.6 dB        ← 过估 30ms：还是好的
Δ = +35 → ERLE 28.3 dB        ← 临界
Δ = +40 → ERLE 22.5 dB        ← 暴跌 6 dB
Δ = +60 → ERLE 21.7 dB
```

**所以工程实践是：宁可填小，不要填大。** 不确定时，取你测到的最小可能延迟。

**③ 真实延迟为 0 反而最难。**
真实 delay = 0（直达声无延迟）时，怎么填都只有 5–8 dB——即使把房间响应、
二次路径、非线性全部关掉、只剩一根理想直达声，也只有 7.3 dB。这是一个**实测现象**：
AEC 需要 far 相对 mic 有若干毫秒的**前瞻**才能建立相关，零延迟正好落在它的退化区。
现实设备总有声卡缓冲、功放和空气传播带来的几到几十毫秒延迟，所以真实场景不会撞上——
但**自己造测试信号时要注意**，`SimulateSettings.echo_delay_ms` 不要设成 0，
否则你会得到一个"算法坏了"的假阴性。

实测报告（`docs/aec-live-test-report.md` §1.3）里那份录音，真实延迟 **203.4 ms**，
全 37 秒内首尾估计差 0.13 ms——**同一台设备、同一段录音，延迟是稳定的**，
所以可以测一次、写死。

> 注意该报告 §4.2 的结论：在**回声已经埋进底噪**的录音上，延迟在 0–500 ms 内怎么填，
> ERLE 都只在 7.14–7.35 dB 之间波动。这和上面表格并不矛盾——那份录音的 ERLE 上限
> 本来就只有 3 dB 左右，延迟的影响被底噪掩蔽了。**延迟重要不重要，取决于回声/底噪比。**

**本项目怎么算它需要喂多少 far**：

`AecCore.process_arrays()` 的处理是——把整段 far 和整段 mic 都按 10 ms 切片，
**第 i 帧喂 far[i]，再处理 mic[i]**（[aec.py:165-172](aec_demo/aec.py#L165-L172)）。

这意味着：**调用方必须已经让 far 和 mic 在同一时间轴上对齐**。也就是说，
如果录音里 ref 比 mic 早 203 ms，你要么先手工平移 ref，要么用 `delay_ms` 参数告诉 APM。
本项目选择后者：`AecCore` 把 `delay_ms` 透传给 `set_stream_delay_ms()`，
由 APM 内部去对齐。

**如果两个都做会怎样？** 关键在于理解：**`delay_ms` 描述的是平移之后、真实的 far→mic 延迟**。
你手工把 far 提前 200 ms，真实延迟就从 200 ms 变成了 400 ms——这时 `delay_ms` 也应该跟着改成 400。
若仍填 200，就是「欠估 200 ms」，按 §2.4 的规律**无害**（实测 29.3 dB，与正确配置的 28.7 dB 持平）。
反过来你把 far 延后 200 ms（真实延迟变成 0），`delay_ms` 还填 200，就是「过估」，
落进零延迟退化区——实测 8.1 dB。

**结论**：重复补偿不一定致命，但会让 `delay_ms` 与实际不符，而后果完全由
§2.4 的方向性规律决定。**选一种做法就好**：要么平移 far（`delay_ms=0`），
要么不动 far（`delay_ms=真实延迟`）。这也是实测报告那份录音的做法——没有平移，
直接把 203 ms 填进 `delay_ms`。

### 2.5 双讲（double-talk）：AEC 最难的场景

**双讲** = 本地人说话的同时，远端声音也在放。此时 mic = 回声 + 本地人声 + 噪声。

如果 AEC 还继续老老实实做 `e = mic - ŷ`，它会把**本地人声也当成回声的一部分**，
于是开始错误地更新 `h`、错误地减，结果**本地人声被削掉**（"吃字"）。

所以 AEC 内部有**双讲检测器（DTD）**：

```
      双讲检测
         │
    ┌────▼─────┐
    │ 检测到双讲│──是──▶ 冻结 h 更新（或大幅减小步长）→ 只做保守的残余抑制
    └────┬─────┘
         │否
         ▼
    正常更新 h，激进消除
```

**代价是必然的**：双讲时 AEC 只能"少消"。本项目实测（`aec-live-test-report.md` §3.1）：

| 场景 | ERLE | 解读 |
|---|---:|---|
| 纯回声 15.0–24.5 s | 15.20 dB | AEC 的真实能力 |
| 纯回声 27.0–36.0 s | 18.20 dB | 同上，且更干净 |
| 双讲 7.5–12.5 s | **2.19 dB** | **正常**，人声本就不该被消 |
| 近端独讲 2.95–4.45 s | 2.38 dB | 这个"ERLE"其实是人声被削弱了 2.38 dB |

**看到双讲段 ERLE 低，不要以为是 bug。** 只有纯回声段的 ERLE 才有意义。

### 2.6 为什么必须是 10 ms 帧

WebRTC APM 的内部流水线（分帧、FFT、滤波器组、状态机）全部按 10 ms 组织。
这带来一个**硬约束**：

| 采样率 | 每帧样本数 |
|---:|---:|
| 8 000 Hz | 80 |
| 16 000 Hz | **160** |
| 32 000 Hz | 320 |
| 48 000 Hz | 480 |

喂整数倍以外的长度，APM 直接返回错误。本项目用 [config.py:19](aec_demo/config.py#L19) 统一换算：

```python
def frame_samples(rate: int) -> int:
    return int(rate * FRAME_MS / 1000)
```

**10 ms 也是延迟与算力的平衡点**：帧太短 → 调用开销占比高；帧太长 → 算法延迟增加、
双讲检测变迟钝。48 kHz 下每帧 480 样本，20 ms 就是 960——但 WebRTC 内部仍会拆回两块。

### 2.7 WebRTC APM 的完整流水线

本项目用的动态库暴露的是**整个 APM**，不只是 AEC。采集流（capture / near）经过的顺序是：

```
  mic (int16)
     │
     ▼
┌──────────────┐   渲染流（reverse / far）只走这里
│  前级调整     │   ┌───────────────────────┐
│ pre_amp      │   │  process_reverse_stream│
│ level_adjust │   │   仅做重采样/带宽处理    │
└──────┬───────┘   │   并存入「参考缓冲」     │
       │           └───────────┬───────────┘
       ▼                       │ far 数据
┌──────────────┐               ▼
│  高通滤波     │        ┌──────────────┐
│  high_pass   │───────▶│  回声消除 AEC │
└──────┬───────┘        │  echo        │
       │                └──────┬───────┘
       │                       │
       ▼                       ▼
┌──────────────┐        ┌──────────────┐
│  噪声抑制 NS  │        │  残余回声抑制 │
│  noise_supp  │        │  (NLP，内部) │
└──────┬───────┘        └──────┬───────┘
       │                       │
       └───────────┬───────────┘
                   ▼
           ┌──────────────┐
           │  瞬态抑制      │  可选：键盘敲击、鼠标点击
           └──────┬───────┘
                  ▼
           ┌──────────────┐
           │  增益控制 AGC │  AGC1（模拟/数字）/ AGC2
           └──────┬───────┘
                  ▼
              out (int16)
```

对应到 `apm.Config`（176 字节，9 个子结构体，按顺序内存排布）：

| 偏移 | 子结构 | 本项目用到的字段 | 作用 |
|---:|---|---|---|
| 0 | `pipeline_config` | `maximum_internal_processing_rate` | 内部处理率（本项目 16k 输入时设 16k，48k 时设 48k） |
| 12 | `pre_amp` | — | 固定增益前置放大 |
| 20 | `level_adjustment` | — | 采集电平调整 / 模拟 mic 增益仿真 |
| 40 | `high_pass` | `enabled`, `apply_in_full_band` | 高通（去直流与低频轰鸣） |
| 42 | `echo` | `enabled`, `mobile_mode`, `enforce_high_pass_filtering`, `export_linear_aec_output` | **AEC 主开关** |
| 48 | `noise_suppress` | `enabled`, `noise_level` | NS，0/1/2/3 四档 |
| 60 | `transient_suppress` | `enabled` | 瞬态抑制 |
| 64 | `gain_control1` | `enabled`, `controller_mode`, `target_level_dbfs` … | AGC1 |
| 144 | `gain_control2` | `enabled`, `adaptive_controller` … | AGC2（更现代的数字 AGC） |

> 📌 `echo.enforce_high_pass_filtering` 默认 `True`，**即使你把 `high_pass.enabled` 关掉，
> AEC 内部仍会强制加一个高通**。所以 `high_pass.enabled` 基本是个冗余开关——仿真实测中
> 开关它不改变任何指标。要真正关掉低频处理，改的是 `echo.enforce_high_pass_filtering`。
>
> 真实录音上实测（`aec-live-test-report.md` §3.3）：关掉 `enforce_highpass` 后近端损失从
> 2.4 dB 降到 0，代价是纯回声段 ERLE 从 7.24 降到 4.69 dB——高通同时在压低频底噪，对 ERLE
> 有贡献。这笔账需要在**你自己的信号**上算，不能照搬。

### 2.8 ERLE 的物理上限 —— 为什么是 15–18 dB 而不是 25–35 dB

这是初学者最容易被误导的地方。文档里常写「AEC 收敛后 ERLE 25–35 dB」，
但你自己在真实录音上测出来只有几 dB，就以为坏了。

**ERLE 的上限由录音的信噪比决定，不由算法决定。** 推导很简单：

```
mic  = 回声 + 底噪                 （纯回声段，没有本地人声）
ERC  = 回声 / 底噪                 回声/底噪比（线性）
如果 AEC 完美工作，out = 底噪：
ERLE_max = 10·log10( P(mic) / P(底噪) )
         = 10·log10( P(回声) + P(底噪) ) / P(底噪) )
         = 10·log10( 1 + ERC )
```

所以：

| 回声/底噪比 | 完美 AEC 的最好 ERLE |
|---:|---:|
| 0 dB | **3.0 dB** |
| +6 dB | 7.0 dB |
| +10 dB | 10.4 dB |
| +20 dB | 20.4 dB |
| +30 dB | 30.0 dB |

实测录音（`aec-live-test-report.md` §1.4）：

```
纯回声段 mic 总电平        −43.5 dBFS
其中回声分量               ≈ −46 dBFS
麦克风底噪                  −46.5 dBFS
                          ─────────────
回声/底噪比                 +0.1 ~ +1.2 dB
                            ↓
理论 ERLE 上限              ≈ 3 dB（宽带）
                            5.6 dB（150–7 kHz 分频段）
```

对照实测值：

| 配置 | 纯回声段实测 ERLE | 结论 |
|---|---:|---|
| `aec_only` | 6.02 / 7.23 dB | **已超过理论上限**——多出的部分来自与 ref 无关的高通削低频底噪 |
| 单独 NS（level 1） | 在纯底噪段 13.2 dB | NS 在大幅压底噪 |
| `voice_assistant`（AEC+NS+HP） | 15.20 / 18.20 dB | AEC 消一部分 → NS 把残余压到底噪以下 |

**记住这条**：先测「回声中到底有多少是回声」，再判断 AEC 好坏。
如果回声已经接近底噪，任何算法都测不出高 ERLE，这时候该改的是**录音条件**（调大音量、
降低 mic 增益、缩短距离），不是算法参数。

---

## 3. 流程图

### 3.1 程序从哪里进来

```
                          python run.py [args]
                                  │
                    ┌─────────────┴─────────────┐
                    │  aec_demo/cli.py  main()  │
                    └─────────────┬─────────────┘
                                  │
        ┌───────────────┬─────────┴──────────┬────────────────┐
        ▼               ▼                    ▼                ▼
    --selftest      --simulate          无参数              --help
        │               │                    │
        ▼               ▼                    ▼
  apm.check_library()  run_simulation()   uvicorn 启动
  platform_info()      （见 §3.3）         aec_demo.server:app
  打印设备统计                              │
        │                                   ▼
        └─ return 0/1                  浏览器访问 8770
                                         点击「开始测试」
                                           POST /api/{simulate,files,device,compare}
                                               │
                                               ▼
                                         JobManager.submit()
                                               │
                                         （见 §3.4）
```

### 3.2 核心：每 10 ms 发生什么

这是**整个项目的心脏**，只有 8 行代码，但顺序绝对不能错。

```
   process_arrays(mic, far)
            │
            ▼
   ┌────────────────────────────┐
   │ ① 采样率不同?  ──是──▶ 整段重采样（soxr HQ）
   │    mic/far → self.rate     │
   └────────────┬───────────────┘
                ▼
   ┌────────────────────────────┐
   │ ② 取两路较短长度 n          │
   │    截到 10ms 整数倍 usable  │
   └────────────┬───────────────┘
                ▼
   ┌────────────────────────────┐
   │ ③ float32 → int16 转换      │
   │    乘以 32768 并裁剪        │
   └────────────┬───────────────┘
                ▼
   ╔════════════▼════════════════════════════════════════════╗
   ║  for off in range(0, usable, frame):                     ║
   ║                                                          ║
   ║    ④ _feed_far(far16[off : off+frame])                   ║
   ║         memmove → self._rin                              ║
   ║         process_reverse_stream(_rin, cfg, cfg, _rout)    ║
   ║         【必须在下面之前】                                ║
   ║                     │                                    ║
   ║                     ▼                                    ║
   ║    ⑤ memmove(mic16[off:off+frame] → self._in)            ║
   ║                     │                                    ║
   ║                     ▼                                    ║
   ║    ⑥ process_stream(_in, cfg, cfg, _out)                 ║
   ║         ret != 0 → 抛异常，整个任务失败                   ║
   ║                     │                                    ║
   ║                     ▼                                    ║
   ║    ⑦ int16 → float32，写入 out[off:off+frame]            ║
   ║                                                          ║
   ╚══════════════════════════════════════════════════════════╝
                │
                ▼
        返回 out（长度 = usable）
```

**为什么 ④ 必须在 ⑥ 之前？**
`process_reverse_stream` 的作用是把这一帧 far **存进参考缓冲**，让紧接着的
`process_stream` 能拿它做相关。顺序反了，AEC 看到的永远是「上一帧的 far」，
相当于恒定少喂 10 ms 参考——不是累积，是固定 10 ms 的欠估。

**实测这个顺序差别有多大**（同一份仿真，`aec_only`，延迟 50 ms）：

| 调用顺序 | ERLE |
|---|---:|
| 先 `process_reverse_stream`，再 `process_stream` | **30.56 dB** |
| 先 `process_stream`，再 `process_reverse_stream` | 30.47 dB |

几乎无差别——因为按 §2.4 的实测，**APM 对「欠估」有约几十毫秒的容限**，
固定的 10 ms 欠喂正好落在容限内。所以：

- **这不是崩溃点，不要指望靠它调出性能**；
- 但仍然**必须按契约来**：① 真实实时链路上 far 未必"迟到"得这么巧；
  ② 欠估容限是有限度的，10 ms + 其他误差可能叠加超限；
  ③ 其他构建/版本的 WebRTC 未必同样宽容。
  **按文档顺序写，不要依赖实现细节。**

**为什么用 `memmove` 而不是列表/切片？**
`ctypes` 缓冲区是 C 内存。`memmove` 直接把 numpy 的连续内存拷过去，**零 Python 对象创建**。
每帧只做一次 320 字节的内存拷贝，这是 RTF 能到 0.01 量级的原因之一。

### 3.3 四种测试模式

```
┌─────────────────────────────────────────────────────────────────────┐
│ ① 离线仿真  run_simulation()                                        │
│                                                                     │
│   simulate(settings)                                                │
│      ├─ make_speech_like()   类语音（f0 120Hz + 共振峰 + 摩擦噪声）    │
│      ├─ make_room_ir()       房间 IR（直达 + 早期反射 + 指数尾）       │
│      ├─ 卷积 → echo          np.convolve(far, ir)                   │
│      ├─ + 第二路径           延迟 +37ms，模拟反射                    │
│      ├─ tanh 非线性          模拟功放/喇叭失真                       │
│      └─ + 近端人声 + 噪声     → mic                                 │
│            │                                                        │
│            ▼   5 段时间轴（时长可配，默认 6 s）                       │
│   ┌────────┬──────────┬──────────┬──────────┬────────┐             │
│   │baseline│ echo_only│double_talk│ near_only│  tail  │             │
│   │ 300ms  │  34%     │   38%     │   28%    │ 300ms  │             │
│   │ 只底噪  │ 只有回声  │ 回声+人声  │ 只有人声  │ 全静音  │             │
│   └────────┴──────────┴──────────┴──────────┴────────┘             │
│            │                                                        │
│            ▼                                                        │
│   已知近端真值 near_speech → 可算「近端保留率」← 仿真模式的独特价值    │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ ② 音频文件  run_files() / run_files_stereo()                        │
│                                                                     │
│   两个文件：                                                        │
│   read_audio(mic_path, rate) ──┐                                    │
│   read_audio(ref_path, rate) ──┤  各自独立重采样到 aec.rate          │
│                                │                                    │
│   一个文件（多声道）：                                              │
│   read_audio_multi(path, rate) → [N, C]                             │
│   mic = data[:, mic_channel]  ref = data[:, ref_channel]            │
│   C < 2 或两路同声道 → ValueError                                   │
│                                ▼                                    │
│   汇合到 _run_files_core()：同一段 AEC + 指标代码                    │
│   长度差 > 50ms 时打印提示，按较短者裁剪                              │
│                                │                                    │
│   可选：estimate_delay_ms()    └─▶ 无真值，只能算整体抑制量           │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ ③ 设备实测  run_device()   ← 必须显式允许播放，否则拒绝              │
│                                                                     │
│   ref_play = ref_audio_for_device()  + 0.3s 前静音 + 0.4s 尾静音     │
│                     │                                               │
│                     ▼                                               │
│   sounddevice.Stream(callback)  ┌─ 回调（每 10ms 一次）              │
│                                 │   indata  → 追加到 rec_mic         │
│                                 │   ref_play[pos:pos+frames] → outdata│
│                                 │   pos += frames                    │
│                                 └─▶ 边播边录，两条流天然同时间轴      │
│                     │                                               │
│   播放完成/超时/卡死检测 ◀──────┘                                    │
│                     ▼                                               │
│   mic = concat(rec_mic)   ref = concat(played)                      │
│   （ref 存的是「实际送进声卡的数据」，不是「以为要播的」）              │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ ④ 参数对比  run_compare()                                          │
│                                                                     │
│   for preset in presets:                                            │
│     for delay in delays:                                            │
│         新建 AecCore → 同一份仿真输入 → 记录 ERLE/近端/RTF           │
│                     │                                               │
│                     ▼                                               │
│              compare.json（笛卡尔积表格）                            │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.4 WebUI 的任务队列

APM 实例和音频设备都是**独占资源**，所以服务端用单线程串行执行。

```
   浏览器                      FastAPI                    JobManager
     │                            │                          │
     │ POST /api/files            │                          │
     ├───────────────────────────▶│                          │
     │                            │ JOBS.submit(kind, fn)    │
     │                            ├─────────────────────────▶│
     │                            │                          │ 入队
     │◀─────── {job_id} ──────────┤                          │
     │                            │                          ▼
     │ GET /api/jobs/{id}         │                    ┌──────────┐
     ├───────────────────────────▶│                    │ 单线程    │
     │◀── {status, progress, ...}─┤                    │ while 队列│
     │        （前端 400 ms～1.2 s 轮询）      │                    │  非空:    │
     │                            │                    │  取一个   │
     │                            │                    │  跑 fn()  │
     │                            │                    │  异常→error│
     │                            │                    └──────────┘
     │                            │                          │
     │ GET /api/results           │                          │
     ├───────────────────────────▶│◀─────────────────────────┤
     │◀── 历史产物列表 ────────────┤                          │
     │                            │                          │
     │ GET /files/{dir}/{name}.wav│                          │
     ├───────────────────────────▶│  FileResponse            │
     │◀────────── 音频文件 ────────┤                          │
```

取消：`POST /api/jobs/{id}/cancel` 只是把 `job.cancel = True`，**不会打断正在跑的处理**
（离线处理是同步循环），下一阶段检查标志位后停下。这是有意的——强行中断 ctypes 调用不安全。

### 3.5 ctypes 调用时序（绑定层）

```
 Python 侧                              C 侧 (libwebrtc_apm)
 ─────────                              ────────────────────
 ctypes.CDLL(路径)
    └─ dlsym 解析符号
 _init_function_signatures()
    └─ 设置 argtypes / restype  ←── 关键！不设的话 int 会被当 32 位截断，
                                   指针会被当 int 处理，必崩
 WebRTCAudioProcessing()
    └─ WebRTC_APM_Create()  ─────────▶  void* (不透明句柄)
 create_stream_config(rate, ch)
    └─ CreateStreamConfig() ─────────▶  void* (流配置句柄)
 apply_config(config)
    └─ ApplyConfig(handle, &cfg) ────▶  int (0=成功)
                                          └─ C 侧 memcpy 整个 176 字节结构体
 set_stream_delay_ms(ms)
    └─ SetStreamDelayMs(handle, ms) ─▶  void
 process_reverse_stream(...)
    └─ ProcessReverseStream(...) ────▶  int
 process_stream(...)
    └─ ProcessStream(...) ───────────▶  int
 close()
    ├─ DestroyStreamConfig(cfg) ─────▶  void
    └─ Destroy(handle) ──────────────▶  void
```

**内存布局是唯一的契约。** `ctypes.Structure` 的字段顺序、类型、对齐必须和 C 的
`struct` 完全一致。本项目 `Config` 实测 **176 字节、4 字节对齐**，9 个子结构体，
偏移分别是 0/12/20/40/42/48/60/64/144。任何一个字段改了（比如加一个 `bool`），
后面的字段全部错位，**AEC 会悄悄用错配置而不报错**——这是移植时最危险的地方。

可以用一条命令自己验：

```bash
python -c "import ctypes; from aec_demo import apm; \
print(ctypes.sizeof(apm.Config)); \
print([(f, apm.Config.__dict__[f].offset, apm.Config.__dict__[f].size) for f,_ in apm.Config._fields_])"
```

---

## 4. 关键代码导读

按「读代码的顺序」排，不是按文件名字母序。

### 4.1 先读 `config.py`（142 行）—— 参数都在这里

```python
SUPPORTED_RATES = (8000, 16000, 32000, 48000)   # APM 只接受这四个
DEFAULT_RATE = 16000
FRAME_MS = 10
```

三个 dataclass 分工明确：

| 类 | 管什么 | 谁在用 |
|---|---|---|
| `AecSettings` | 一次 AEC 处理的全部开关（预设、延迟、NS 等级、AGC…） | `AecCore`、CLI、server |
| `SimulateSettings` | 仿真信号的物理参数（延迟、增益、RT60、非线性、噪声） | `simulate()` |
| `RunOptions` | 产物保存策略等运行期选项 | `runner` |

`PRESETS` 是**语义层**：它把「语音助手常用」这种意图翻译成具体开关。
见 §6.1 怎么加一个。

### 4.2 再读 `apm.py`（451 行）—— 只关心三件事

**① 库在哪**（[apm.py:195-209](aec_demo/apm.py#L195-L209)）

```python
def library_path() -> Path:
    system = platform.system().lower()
    arch = _normalize_arch(platform.machine())
    if system == "linux":   name, sub = "libwebrtc_apm.so",    "linux"
    elif system == "darwin": name, sub = "libwebrtc_apm.dylib", "macos"
    elif system == "windows": name, sub = "libwebrtc_apm.dll",  "windows"
    return LIBS_DIR / "webrtc_apm" / sub / arch / name
```

`_normalize_arch` 把 `x86_64/amd64 → x64`、`aarch64/arm64 → arm64`、`i386/i686/x86 → x86`。
**移植时改这里就够了**（见 §5）。

**② 结构体长什么样**（[apm.py:21-182](aec_demo/apm.py#L21-L182)）
4 个 enum + 16 个 `Structure`。`Config` 在最底部，是「结构体的结构体」。

**③ 符号怎么调**（[apm.py:221-237](aec_demo/apm.py#L221-L237)）
7 个 C 函数，每个都要设 `argtypes` 和 `restype`。

### 4.3 核心 `aec.py`（277 行）

```
AecCore.__init__                     aec.py:101
  ├─ 复制 settings（不改调用方的对象）
  ├─ apply_preset()                  展开预设
  ├─ mobile_mode_supported() 探测     不支持就降级并记录
  ├─ WebRTCAudioProcessing()        创建实例
  ├─ apply_config(build_apm_config) 应用 176 字节配置
  ├─ create_stream_config(rate, 1)  ⚠️ 永远单声道
  ├─ set_stream_delay_ms(delay_ms)  延迟
  └─ 预分配 4 个 ctypes 缓冲区        每帧复用，不重复分配

process_arrays(mic, far)             aec.py:148   离线整段
process_chunk(mic, far)              aec.py:183   实时分块（带缓冲，凑够 10ms 才处理）
_feed_far(frame_i16)                 aec.py:176   喂一帧 far
close() / __enter__ / __exit__       aec.py:218   资源释放
estimate_delay_ms(mic, far, rate)    aec.py:241   延迟估计
```

**`process_arrays` 与 `process_chunk` 的区别：**

| | `process_arrays` | `process_chunk` |
|---|---|---|
| 输入 | 整段等长数组 | 任意长度块（可能不足 10ms） |
| 内部缓冲 | 无 | `_far_buf`/`_near_buf` 累积 |
| 重采样 | 整段 soxr 高质量 | 流式 `soxr.ResampleStream`（保持相位连续） |
| 用途 | 离线仿真 / 文件 / 设备实测后处理 | 真实实时链路 |

> 📌 阅读注意：[aec.py:6](aec_demo/aec.py#L6) 的模块 docstring 写的是「失败即旁路，不中断链路」，
> 但实际代码在 `process_stream` 返回非 0 时**抛 `RuntimeError`**（[aec.py:170](aec_demo/aec.py#L170)）。
> 行为是「整个任务失败并在 `job.error` 里报错」，不是静默旁路。以代码为准。

### 4.4 `analysis.py`（297 行）—— 三个指标公式

```python
ERLE(段)        = 10·log10( P(mic) / P(out) )              # 回声压制量
近端保留率(段)   = 10·log10( P(out) / P(near_truth) )       # 只有仿真能算
回声耦合损耗     = 10·log10( P(ref) / P(mic) )              # 物理量，与算法无关
```

`compute_metrics()` 的 `summary` 只认名字叫 `echo_only` / `double_talk` / `near_only`
的段，其余段只出现在 `segments` 字典里。这个约定见 §6.3。

### 4.5 `runner.py`（638 行）—— 四个 run_* 骨架一致

```
run_dir(job)                    建 output/<时间戳>_<id>/
job.stage = "..."               告诉前端现在在干嘛
job.progress = 0.xx             进度条
job.log("...")                  写日志（保留最近 400 条）
构建信号 → AecCore → process → compute_metrics → _save_outputs → _add_plot_and_metrics
job.result = {...}              返回给前端
```

读懂一个 `run_*`，其余三个都一样。

### 4.6 `simulate.py`（287 行）—— 为什么不用纯正弦

```python
make_speech_like()                f0=120Hz + 2.3Hz 颤音 + 最多 39 次谐波
                                  共振峰加权（500/1500/2500/3500 Hz）
                                  + 摩擦噪声 + 4Hz 音节包络
make_room_ir()                    直达声 + 6 个早期反射(0.5–20ms) + 指数衰减尾
echo = convolve(far, ir) * gain   线性回声路径
echo[d2:] += far * gain2          +37ms 的第二条反射路径
echo = tanh(echo*(1+2n))/(1+n)    非线性失真（喇叭功放削顶）

⚠️ make_sweep() 的注释很重要：连续长扫频会让 WebRTC AEC 持续重收敛，
   ERLE 看起来很差。这是信号特性，不是实现问题。
```

**为什么这些细节重要？** 因为一个只含单频正弦的测试信号，会让 AEC 看起来「完美」
（滤波器只需学一个延迟和增益），但真实语音有宽带频谱和时变包络，才是有效验证。

---

## 5. 移植指南

### 5.1 现有平台矩阵

```
libs/webrtc_apm/
├── macos/
│   ├── x64/    libwebrtc_apm.dylib     Intel Mac
│   └── arm64/  libwebrtc_apm.dylib     Apple Silicon (M1/M2/M3/M4)
├── linux/
│   ├── x64/    libwebrtc_apm.so        x86_64 服务器/PC
│   └── arm64/  libwebrtc_apm.so        ARM64 Linux（树莓派 4/5 64位、Jetson）
└── windows/
    ├── x64/    libwebrtc_apm.dll + .lib
    └── x86/    libwebrtc_apm.dll + .lib
```

**没有的**：32 位 ARM（armhf / armv7），MIPS，RISC-V。

### 5.2 三条使用约束（不是代码 bug，是用法）

| 现象 | 原因 | 正确做法 |
|---|---|---|
| `ImportError: attempted relative import with no known parent package` | `cli.py` 用 `from . import apm`，直接当脚本跑没有父包 | 用 `python -m aec_demo.cli` 或 `python run.py` |
| `ModuleNotFoundError: No module named 'aec_demo'` | `-m` 只把**当前目录**加进 `sys.path`；你在包目录里了 | **先 `cd` 到项目根目录**再执行 |
| `ModuleNotFoundError: No module named 'soundfile'` | 换了 Python 解释器（比如 shell 里 pyenv 变成了 conda） | `which python3` 确认，或用 `$PY` 显式指定 |

### 5.3 移植到新平台/新架构：三种情况

#### 情况 A：目标平台已有目录，只是没对应的库

比如你要跑在 **RISC-V Linux**：

**① 改路径解析**（[apm.py:195-209](aec_demo/apm.py#L195-L209)）

```python
def _normalize_arch(arch: str) -> str:
    arch = arch.lower()
    if arch in ("x86_64", "amd64"):        return "x64"
    if arch in ("aarch64", "arm64"):       return "arm64"
    if arch in ("i386", "i686", "x86"):    return "x86"
    if arch in ("riscv64", "rv64"):        return "riscv64"   # ← 加这一行
    return arch
```

不需要改 `library_path()`——它已经拼成 `linux/riscv64/libwebrtc_apm.so`。

**② 放进库**

```bash
mkdir -p libs/webrtc_apm/linux/riscv64
cp /path/to/your/build/libwebrtc_apm.so libs/webrtc_apm/linux/riscv64/
```

**③ 验证**

```bash
python run.py --selftest
```

输出里应该看到：
```
lib_path: .../libs/webrtc_apm/linux/riscv64/libwebrtc_apm.so
lib_exists: True
APM 动态库自检: 通过 - ok
```

#### 情况 B：要自己编译 `libwebrtc_apm`

动态库必须导出这 **7 个符号**，签名如下（`aec_demo/apm.py:211-237` 是它们的 Python 侧镜像）：

```c
/* 生命周期 */
void* WebRTC_APM_Create(void);
void  WebRTC_APM_Destroy(void* apm);

/* 流配置：采样率必须是 8000/16000/32000/48000，声道 1 或 2 */
void* WebRTC_APM_CreateStreamConfig(int sample_rate, int num_channels);
void  WebRTC_APM_DestroyStreamConfig(void* config);

/* 配置：cfg 指向一个 176 字节、与 Config 结构体布局一致的 C struct */
int   WebRTC_APM_ApplyConfig(void* apm, const void* cfg);

/* 处理：每帧必须是 10ms 的样本数 */
int   WebRTC_APM_ProcessReverseStream(void* apm, const short* src,
                                      void* src_cfg, void* dst_cfg, short* dst);
int   WebRTC_APM_ProcessStream(void* apm, const short* src,
                               void* src_cfg, void* dst_cfg, short* dst);

/* 延迟 */
void  WebRTC_APM_SetStreamDelayMs(void* apm, int delay_ms);
```

**结构体必须逐字节对齐。** C 侧定义（与 `apm.py` 一一对应）：

```c
typedef struct { int maximum_internal_processing_rate;
                 bool multi_channel_render;      /* 注意 bool 大小 */
                 bool multi_channel_capture;
                 int capture_downmix_method; } Pipeline;      /* 12 字节 */

typedef struct { bool enabled; float fixed_gain_factor; } PreAmplifier;  /* 8 */

/* … 其余见 apm.py … */

typedef struct {
  Pipeline          pipeline_config;     /* offset   0 */
  PreAmplifier      pre_amp;             /* offset  12 */
  CaptureLevelAdjustment level_adjustment; /* offset 20 */
  HighPassFilter    high_pass;           /* offset  40 */
  EchoCanceller     echo;                /* offset  42 */
  NoiseSuppression  noise_suppress;      /* offset  48 */
  TransientSuppression transient_suppress; /* offset 60 */
  GainController1   gain_control1;       /* offset  64 */
  GainController2   gain_control2;       /* offset 144 */
} Config;                                /* 总 176 字节，4 字节对齐 */
```

**移植排查顺序**（遇到问题按这个顺序查）：

```
① 符号在不在？
   nm -D libwebrtc_apm.so | grep WebRTC_APM_      (Linux)
   nm -gU libwebrtc_apm.dylib | grep WebRTC_APM   (macOS)
   dumpbin /exports libwebrtc_apm.dll             (Windows)

② 结构体大小对不对？
   python -c "import ctypes; from aec_demo import apm; print(ctypes.sizeof(apm.Config))"
   必须是 176。不是的话逐个字段比 offset。

③ 自检通过吗？
   python run.py --selftest

④ 一帧真实数据能过吗？
   用 --simulate 跑一次，看 metrics.json 里的 aec_stats.frames 和 rtf。
```

#### 情况 C：没有 WebRTC 移植条件，换别的引擎

如果目标平台实在跑不了（比如 MCU 没有足够 RAM），可以把整个引擎层换掉。
`runner.py` 只依赖 `AecCore` 的这两个方法：

```python
with AecCore(settings, input_rate=rate) as core:
    out = core.process_arrays(mic, ref, input_rate=rate)
    stats = core.stats      # .frames / .proc_time_s / .realtime_factor
```

只要你的替代实现提供同名接口（上下文管理器 + `process_arrays` + `stats`），
上层的仿真、指标、绘图、WebUI 全部可以复用。

候选方案：SpeexDSP（`speex_echo_cancellation`，轻量，ARM Cortex-M 可跑）、
`webrtc-audio-processing`（Linux 发行版包）、`libWebRTC` 的 `AudioProcessing` 直接编进 App。

### 5.4 移植检查清单

- [ ] 动态库文件名与路径符合 `libs/webrtc_apm/<系统小写>/<架构规范化>/`
- [ ] 系统关键字是 `linux` / `macos` / `windows`（**macOS 的 `platform.system()` 返回 `Darwin`，代码里已转成 `darwin`→`macos`**）
- [ ] 架构关键字是 `x64` / `arm64` / `x86`（不是 `x86_64` / `aarch64`）
- [ ] 7 个符号全部导出，名字完全一致（区分大小写）
- [ ] `sizeof(Config) == 176`，9 个字段 offset 正确
- [ ] 采样率只支持 8k/16k/32k/48k
- [ ] 帧长 = 采样率 ÷ 100（10 ms）
- [ ] `--selftest` 通过
- [ ] `--simulate` 能跑完且 `aec_stats.frames > 0`
- [ ] 若 ARM32/嵌入式：确认 `bool` 是 1 字节（某些编译器非标开启下不是）

---

## 6. 二次开发指南

### 6.1 加一个预设

**场景**：你要给一个车载场景加「强降噪 + 大增益」预设。

**一步到位**——只改 [config.py](aec_demo/config.py) 的 `PRESETS`：

```python
PRESETS = {
    # ... 现有预设 ...

    "car": {
        "name": "车载（强降噪+AGC）",
        "echo": True,
        "preprocess": True,
        "ns_level": 3,          # VERY_HIGH
        "agc1": True,
        "agc1_mode": 1,         # ADAPTIVE_DIGITAL
        "agc1_target_dbfs": 3,
        "agc1_compression_db": 12,
    },
}
```

**全链路自动生效**，不用改任何其他文件：

- `cli.py` 的 `--preset` 用 `choices=sorted(PRESETS)`，自动出现新选项
- `server.py` 的 `/api/health` 返回 `PRESETS`，前端下拉框自动出现
- `apply_preset()` 里的 `p.get(...)` 会自动读取
- `run_compare()` 接受任意预设名

**但注意 `apply_preset()` 的一个坑**（[aec.py:71-73](aec_demo/aec.py#L71-L73)）：

```python
if "preprocess" in p:
    s.highpass = p["preprocess"]
    s.ns_enabled = p["preprocess"]
```

**只有写了 `preprocess` 键，才会覆盖 `highpass` / `ns_enabled`。** 不写就会从
`AecSettings` 的默认值继承（`highpass=True, ns_enabled=True`）。这就是现有 `off`
预设的问题——它没写 `preprocess`，所以选「全部关闭」时实际还在做高通和降噪。
**加新预设时，如果不想做前处理，务必显式写 `"preprocess": False`。**

### 6.2 加一个测试模式

**场景**：你要加「上传一个双声道文件，自动拆 L/R 当 mic/ref」的模式。

**① config.py**：加请求参数（一般复用现有的 `AecSettings` 就够）

**② server.py**：加 Pydantic 请求体 + 路由

```python
class SplitRequest(BaseModel):
    file_id: str
    aec: Dict[str, Any] = {}

@app.post("/api/split")
def start_split(req: SplitRequest) -> Dict[str, Any]:
    patch = _merge(AecSettings().to_dict(), req.aec)
    aec = AecSettings(**patch)
    path = _upload_path(req.file_id)
    job = JOBS.submit("split", f"拆分 {Path(path).name}",
                      lambda j: run_split(j, str(path), aec, RunOptions()))
    return {"job_id": job.id}
```

**③ runner.py**：写 `run_split()`，抄 `run_files()` 的骨架，只把「读两路」换成「读一路再拆」

```python
def run_split(job: Job, path: str, aec: AecSettings, options: RunOptions) -> None:
    out_dir = run_dir(job)
    job.stage = "读取双声道文件"
    data, sr = sf.read(path, dtype="float32", always_2d=True)   # 注意要拿原始二维数据
    mic = to_mono(data[:, [0]])     # 只取 ch0，不要平均！见 §6.5
    ref = to_mono(data[:, [1]])
    # ↓ 以下与 run_files() 完全一致
    ...
```

**④ webui/app.js**：加一个按钮，`fetch('/api/split', {method:'POST', ...})`，其余复用现有的轮询逻辑。

### 6.3 加一个指标

**场景**：你想看「回声消除后，还剩多少能量在 500–2000 Hz 语音主频段」。

**① analysis.py**：在 `compute_metrics()` 的 `per_segment` 循环里加字段

```python
# 在 item = {...} 之后
from numpy.fft import rfft, rfftfreq
def band_power(x, rate, lo, hi):
    if len(x) == 0: return 0.0
    spec = np.abs(rfft(x.astype(np.float64))) ** 2
    f = rfftfreq(len(x), 1/rate)
    return float(np.sum(spec[(f >= lo) & (f < hi)]))

item["speechband_erle_db"] = round(10 * np.log10(
    max(band_power(m, rate, 500, 2000), EPS) /
    max(band_power(o, rate, 500, 2000), EPS)), 2)
```

**② 段名约定很重要。** `summary` 只认三个固定键（[analysis.py:132-139](aec_demo/analysis.py#L132-L139)）：

```python
echo_seg = per_segment.get("echo_only") or per_segment.get("all")
dt_seg   = per_segment.get("double_talk")
near_seg = per_segment.get("near_only")
```

- 你要算「AEC 能力」，段名**必须**叫 `echo_only`
- 你的段叫 `echo_only_1` / `echo_only_2`（像实测报告那样分了两个纯回声段），
  `summary.erle_db` 会是 `None`——这不是 bug，是约定没对上。看 `segments` 字典即可

**③ 给 `band_erle()` 加频段**：改 [analysis.py:71](aec_demo/analysis.py#L71) 的 `centers` 列表即可。

### 6.4 换掉延迟估计

现有 `estimate_delay_ms()`（[aec.py:241](aec_demo/aec.py#L241)）用的是 **1 ms 精度包络互相关**，
只在 `[0, max_ms]` 内搜索，且用 `peak < 3·std(corr)` 判「无回声」。实测报告 §4.2 记录过它在
一份真实录音上返回 0 ms（真实 203.4 ms）的情况。

**换成 GCC-PHAT**（对混响和谱形变化更稳健，且能搜更大的延迟范围）：

```python
def estimate_delay_gcc_phat(mic, far, rate, max_ms=1000):
    """广义互相关-相位变换。返回 |far→mic| 延迟（ms）。"""
    mic = np.asarray(mic, np.float64); far = np.asarray(far, np.float64)
    n = min(len(mic), len(far))
    mic, far = mic[:n], far[:n]

    # 取 mic 能量最大处的 6s 窗口，避开长静音拖累
    win = min(n, rate * 6)
    start = int(np.argmax(np.convolve(mic ** 2, np.ones(rate), 'same')))
    a = max(0, min(start - win // 2, n - win))
    mic, far = mic[a:a + win], far[a:a + win]

    N = 1 << int(np.ceil(np.log2(2 * win)))
    R = np.fft.rfft(mic, N) * np.conj(np.fft.rfft(far, N))
    R /= (np.abs(R) + 1e-10)                  # ← PHAT 加权：只保留相位
    cc = np.fft.irfft(R, N)
    L = max_ms * rate // 1000                  # 搜索窗口（样本）
    cc = np.concatenate([cc[-L:], cc[:L]])     # 把负延迟搬到前面
    lag = int(np.argmax(cc)) - L
    return abs(lag) / rate * 1000
```

在本仓库仿真上验证过（`duration_s=8`，注入延迟 0/50/100/200/300 ms）：
估计值 **37/50/100/200/300 ms**——50 ms 以上全部精确命中；
0 ms 报 37 ms 是"零延迟本就不可测"的表现（见 §2.4 ③）。

**接进去的最小改动**：在 `aec.py` 里替换函数体，保持签名
`(mic, far, rate, max_ms=500, prefer_positive=True) -> float` 不变，`runner.py` 一行都不用动。
然后加一条单测（`tests/test_offline.py::test_delay_estimation_finds_known_delay` 已有骨架，
用仿真的已知延迟做断言）。

### 6.5 单文件双声道输入（已实现）

**原始问题**：`read_audio()` 对多声道直接求平均，把「ch0=mic, ch1=ref」的录音平均成
一路，AEC 拿到的 `mic` 和 `ref` 是同一个混音信号，结果无意义且**不报错**。

**现在的做法**：在 `audioio.py` 里加了一组保留声道的读写原语，`read_audio()` 退化成
它们之上的一行便捷封装。

| 函数 | 作用 |
|---|---|
| `read_audio_multi(path, target_rate)` | 返回 `[N, C]` 浮点数组（单声道文件仍返回 `[N]`），**不做降混** |
| `resample_multi(data, sr_in, sr_out)` | 逐声道重采样，保持 `[N, C]` 形状 |
| `split_channels(path, target_rate)` | 返回 `(mic, ref)` 两路，声道数 < 2 时抛 `ValueError` |
| `read_audio(path, target_rate)` | = `to_mono(read_audio_multi(...))`，仅用于确实要单路的场景 |

三种解码兜底（`soundfile` → 标准库 `wave` → `ffmpeg`）全部改成了保留多声道版本：

- `_read_wave_stdlib_multi()` 用 `wave` 的 `readframes` 后按交错顺序 reshape 成 `[N, C]`
- `_read_ffmpeg_multi()` 先用 `ffprobe -show_entries stream=channels` 探测声道数，
  再 `ffmpeg -ac <n>` 输出，而不是写死 `-ac 1`

`runner.py` 里新增 `run_files_stereo()`，与 `run_files()` 共享后半段 `_run_files_core()`：

```python
data = read_audio_multi(path, rate)
channels = 1 if data.ndim == 1 else data.shape[1]
if channels < 2:
    raise ValueError("该文件只有 1 个声道，无法同时提供 mic 与 ref…")
mic = np.ascontiguousarray(data[:, mic_channel], dtype=np.float32)
ref = np.ascontiguousarray(data[:, ref_channel], dtype=np.float32)
```

接口层 `/api/files` 两种请求体二选一：`{file, mic_channel, ref_channel}` 或
`{mic_file, ref_file}`。WebUI 用单选按钮切换，选完文件后显示探测到的声道数。

**验证**：`tests/test_offline.py` 里
`test_stereo_file_mode_matches_two_file_mode` 断言单文件与双文件走法的 ERLE 差 < 0.05 dB；
`test_stereo_file_mode_honours_channel_selection` 直接读回落盘的 `mic.wav` / `ref.wav`，
断言它们分别等于原始的第 2 / 第 1 声道——**用数据内容验证，不用 ERLE 差值验证**。
后者一开始写成「声道对调后 ERLE 必须差 3 dB」，实测只差 1.44 dB（对调后 AEC 仍能
找到部分相关解），阈值是无依据猜的，所以换成了上面的做法。

### 6.6 加分段指标到文件模式

**现状**（[runner.py:360](aec_demo/runner.py#L360)）：

```python
segments = {"all": (0.0, n / rate)}
```

只有一整段。像实测报告那份素材（前 15 s 有人声、后 21 s 纯回声），整体 ERLE 只有
2.37 dB，纯回声段却是 18.20 dB——**差 7 倍，很容易被误读为「AEC 无效」**。

**改法**：文件模式加一个可选的段列表参数，前端给几个输入框（起止秒），或自动按能量切分。

```python
def run_files(..., segments: Optional[Dict[str, Tuple[float, float]]] = None):
    ...
    if segments is None:
        segments = {"all": (0.0, n / rate)}
```

自动切分的思路（实测报告用的就是这种）：按 20 ms 能量判定「ref 有没有在放」和
「mic 有没有近端人声」，把时间轴分成 `echo_only` / `double_talk` / `near_only` / `silence` 四类。

### 6.7 二次开发的通用注意事项

| 注意点 | 说明 |
|---|---|
| **不要修改传入的 `AecSettings`** | `AecCore.__init__` 会复制一份（[aec.py:103](aec_demo/aec.py#L103)），你在外面改原对象不会影响已创建的实例 |
| **一个任务一个 `AecCore`** | APM 有内部状态（滤波器系数、延迟缓冲）。复用实例会让不同片段互相污染 |
| **`AecCore` 不是线程安全的** | 它持有一个 ctypes 句柄和共享缓冲区。设备回调和主线程**不能同时**调用 |
| **`output/` 目录会无限增长** | `runner.run_dir()` 每次新建目录，不清理。生产环境要加保留策略 |
| **`job.logs` 最多 400 条** | 超出从头删 100 条（[runner.py:47-48](aec_demo/runner.py#L47-L48)），长任务别指望完整日志 |

---

## 7. 新手实操演练

**目标**：不接任何硬件，30 分钟内建立完整直觉。

### 第 1 步：确认环境（2 分钟）

```bash
cd aecdemo
export PY=$(which python3)          # 或显式指定装了依赖的那个
$PY run.py --selftest
```

要看到 `APM 动态库自检: 通过`。记下 `lib_path` 那一行——后面移植排查全靠它。

### 第 2 步：跑一次基线仿真（3 分钟）

```bash
$PY run.py --simulate --preset off
```

打开 `output/<最新>/metrics.json`，看 `segments.echo_only.erle_db`。

> **预期**：这个值是 **0.45 dB**，接近 0 但**不是 0**。因为 `off` 预设的已知缺陷（§6.1），
> 它实际仍在做高通 + 降噪，只是不做 AEC。想看到真正的「无处理」基线，只能拿 `mic.wav`
> 本身当输出——把 echo/hp/ns **全部关掉**时 APM 会输出全 0，不是直通。

### 第 3 步：跑一次完整 AEC（3 分钟）

```bash
$PY run.py --simulate --preset voice_assistant
```

对照前后两次的 `metrics.json`（以下为默认 6 s 仿真的实测值）：

```json
{
  "summary": {
    "erle_db": 42.45,           ← 纯回声段，这是 AEC+NS 的真实能力
    "double_talk_erle_db": 10.78, ← 双讲段，天然很低，正常
    "near_retention_db": -2.45,   ← 近端保留，这里被削了 2.45 dB（高通造成）
    "echo_return_loss_db": 4.39,
    "out_clipped": false
  },
  "aec_stats": { "frames": 600, "realtime_factor": 0.014 }
}
```

三个预设的对照（同一份仿真，`--delay 50`）：

| 预设 | `erle_db` | `double_talk_erle_db` | `near_retention_db` | RTF |
|---|---:|---:|---:|---:|
| `off` | 0.45 | 0.77 | −2.45 | 0.001 |
| `aec_only` | **30.56** | 9.86 | **−0.14** | 0.013 |
| `voice_assistant` | **42.45** | 10.78 | −2.45 | 0.013 |

**三个数字各自的含义：**

- `off` 的 0.45 dB 说明「不做 AEC 时，回声几乎完全没被消」——这就是基线。
- `aec_only` 比 `off` 高出 **30 dB**，这是 **AEC 本身的净贡献**。
- `voice_assistant` 再高出 12 dB，这 12 dB 来自 **NS**（把 AEC 后的残余压得更低）。
- 但 `voice_assistant` 的 `near_retention_db` 从 −0.14 掉到 −2.45：**NS 把近端人声也一起削了**。

两组配置拆开单跑可以确认这件事（同一份仿真，直接调 APM 的 `Config`）：

| echo | high_pass | NS | ERLE | 近端保留 |
|---:|---:|---:|---:|---:|
| ✗ | ✓ | ✓ | 0.45 | −2.45 | ← `off` 实际生效的配置 |
| ✓ | ✗ | ✗ | 30.56 | −0.14 |
| ✓ | ✓ | ✗ | 30.56 | −0.14 | ← 加高通**不改变**任何指标 |
| ✓ | ✗ | ✓ | **42.45** | **−2.45** | ← 那 12 dB 和那 −2.3 dB 都来自 NS |
| ✓ | ✓ | ✓ | **42.45** | **−2.45** | ← `voice_assistant` |

**注意两件事**：
① 仿真里 `high_pass` 开关**完全无影响**——因为 `echo.enforce_high_pass_filtering` 默认为 `True`，
AEC 内部已经在做强高通，外面再开一遍是重复的。
② 近端那 −2.45 dB 全部来自 NS。实测报告 §3.3 在**真实录音**上测到的 −2.38 dB 归因于
强制高通，与这里的仿真相差——真实录音的低频内容远比合成的类语音丰富，
150 Hz 以下的能量占比不同，损失的来源也就不同。**结论要按你自己的信号测，不要照搬。**

**读书顺序**：先看 `erle_db`（AEC 有没有效），再看 `near_retention_db`（有没有误伤），
最后看 `realtime_factor`（< 1 才满足实时）。

### 第 4 步：亲手验证延迟的方向性（5 分钟）

这是**最值得亲手做的一次实验**，能让你永久记住延迟的容限在哪一侧：

```bash
$PY run.py --simulate --preset aec_only --echo-delay 100 --delay 50    # 欠估 50ms
$PY run.py --simulate --preset aec_only --echo-delay 100 --delay 100   # 准确
$PY run.py --simulate --preset aec_only --echo-delay 100 --delay 140   # 过估 40ms
$PY run.py --simulate --preset aec_only --echo-delay 100 --delay 200   # 过估 100ms
```

对比四次的 `erle_db`，你会看到（实测值）：

| 实验 | delay 设定 | ERLE |
|---|---:|---:|
| 欠估 50 ms | 50 | **30.0 dB** |
| 准确 | 100 | **29.7 dB** |
| 过估 40 ms | 140 | 22.55 dB |
| 过估 100 ms | 200 | 19.5 dB |

**结论：填小无害，填大有害。** 这与"填错就不对齐、填对才有效"的朴素理解完全不同——
APM 内部有前瞻缓冲，欠估可以由它自己吸收。记住这条，你在真机上标定延迟时就知道
**该往小的一侧猜**。

### 第 5 步：跑一次参数对比（3 分钟）

```bash
$PY run.py     # 打开 WebUI → 参数对比
```

得到一张 5 预设 × N 延迟的表。注意观察：
- 不同预设的 `erle_db` 差异
- 不同延迟下同一预设的剧烈变化
- `rtf` 是否都在 1 以下

### 第 6 步：用你自己的录音（10 分钟）

1. 准备录音，**同一时间轴**：
   - 一个双声道文件（ch1=mic，ch2=ref）——最简单，`output/<...>/aec_in.wav` 就是这个格式
   - 或者两条等长单声道 WAV：`mic.wav`（含回声）和 `ref.wav`（当时播的信号）
2. WebUI → 音频文件 → 选「一个双声道文件」或「两个文件」，指定 mic/ref 声道
3. 先勾「自动估计延迟」跑一次，看 `metrics.json` 里的 `delay_ms`
4. 再手动填一个值跑一次，对比 `erle_db`
5. 确认声道没选反：听 `mic.wav` 应该听到回声+你自己说话，`ref.wav` 应该只有远端播放的内容

**如果 ERLE 很低，按这个顺序排查**（这是本章最有价值的部分）：

```
① 先算回声/底噪比
   纯回声段的 mic 电平  vs  纯静音段的 mic 电平
   （把两段分别算 RMS，相减）
        │
        ├─ 差 < 6 dB  ──▶ 停下。ERLE 上限就是 6 dB，换算法也没用。
        │                 改录音条件：调大音量 / 降低 mic 增益 / 缩短距离
        │
        └─ 差 ≥ 15 dB ─▶ 继续排查
                              │
                              ▼
② 延迟对吗？
   metrics.json 里的 delay_ms，与你实际测的延迟对比
                              │
                              ▼
③ ref 和 mic 是真对齐的吗？
   两路是不是同一时刻开始的？长度差多少？日志里有提示吗？
                              │
                              ▼
④ ref 信号内容对吗？
   是「送进扬声器之前」的数字信号，还是经过功放之后？
   增益一致吗？（实测报告里 ref 比真实回声高 17 dB）
                              │
                              ▼
⑤ 回声是不是强非线性？
   内置小喇叭开大音量 → 线性滤波器学不了 → ERLE 天生低
   换外放音箱 / 保持距离
```

---

## 8. 故障排查速查表

| 症状 | 最可能的原因 | 定位方法 | 处理 |
|---|---|---|---|
| `FileNotFoundError: 未找到 WebRTC APM 动态库` | 平台/架构目录名不对 | 打印 `apm.platform_info()` | 按 §5.3 检查 `lib_path` |
| `OSError: ... wrong ELF class` / `incompatible architecture` | 库的架构与 Python 不一致（如 arm64 Python 加载 x64 库） | `file libwebrtc_apm.so` + `python -c "import platform;print(platform.machine())"` | 换对应架构的库，或用同架构的 Python |
| `process_stream 返回 -11` | `mobile_mode=True`，该构建不支持 | 看任务日志里的降级提示 | 已自动降级；或别用 `mobile` 预设 |
| ERLE ≈ 0 | 延迟填错 / ref 时间轴不对 / ref 是静音 | 先跑 §7 第 4 步实验 | 填对延迟；检查 ref |
| ERLE 只有 2–3 dB，但回声明显 | 回声/底噪比不足 | 算纯回声段与静音段的电平差 | 改录音条件，不是算法 |
| ERLE 很高但人声也被削了 | NS 过强 / 双讲检测失效 | 看 `near_retention_db` 分频段值 | 降 `ns_level`；确认 `enforce_highpass` |
| 输出全是 0 | echo/hp/ns **全部**关闭 | 检查配置快照 | 至少开一项（APM 不提供直通） |
| 单文件模式报「只有 1 个声道」 | 文件确实是单声道 | 看文件头的声道数 | 换成双声道文件，或用「两个文件」模式 |
| 单文件模式两路选成了同一个声道 | 前端下拉被改成一样 | 检查请求体的 `mic_channel` / `ref_channel` | 服务端会返回 400，选不同声道 |
| 立体声文件测出来完全没效果 | mic/ref 选反了，或某一路是静音 | 检查落盘的 `mic.wav` / `ref.wav` 是否与预期声道一致 | 交换声道选择（§6.5） |
| `attempted relative import` | 直接跑 `python aec_demo/cli.py` | — | 用 `python -m aec_demo.cli` 或 `run.py` |
| `No module named aec_demo` | 在包目录内执行 `-m` | `pwd` | 回到项目根目录 |
| `No module named soundfile` | 解释器换了 | `which python3` | 用装了依赖的那个解释器 |
| WebUI 打不开 | 端口占用 | 终端输出 | `--port 8771` |
| 设备模式报「需要显式允许播放」 | 安全保护，防止突然出声 | — | 勾选确认框 |
| 设备模式录音为空 | 麦克风权限 / 设备选错 | `--selftest` 看设备数 | macOS 系统设置授权；换设备 |

---

## 9. 术语表

| 缩写 | 全称 | 中文 | 一句话 |
|---|---|---|---|
| **AEC** | Acoustic Echo Cancellation | 声学回声消除 | 用 far 预测并从 mic 里减掉回声 |
| **APM** | Audio Processing Module | 音频处理模块 | WebRTC 里一整套采集端处理链 |
| **near-end** | — | 近端 | 本地麦克风这一侧 |
| **far-end** | — | 远端 | 网络对端 / 扬声器这一侧 |
| **ref / far** | reference signal | 参考信号 | 送去扬声器的那路信号，AEC 的输入之一 |
| **ERLE** | Echo Return Loss Enhancement | 回声抑制增强量 | `10log10(P(mic)/P(out))`，越大越好 |
| **ERL** | Echo Return Loss | 回声耦合损耗 | `10log10(P(ref)/P(mic))`，物理量，与算法无关 |
| **NLP** | Non-Linear Processing | 非线性处理 | 线性 AEC 之后压残余回声的一级 |
| **NS** | Noise Suppression | 噪声抑制 | 压稳态背景噪声 |
| **AGC** | Automatic Gain Control | 自动增益控制 | 把音量拉到目标电平 |
| **DTD** | Double-Talk Detector | 双讲检测 | 判断本地是不是也在说话，决定要不要更新滤波器 |
| **FIR** | Finite Impulse Response | 有限冲激响应 | 回声路径的常用线性模型 |
| **NLMS** | Normalized LMS | 归一化最小均方 | 一种自适应滤波更新规则 |
| **IR** | Impulse Response | 冲激响应 | 房间对一声"啪"的响应，回声路径的完整描述 |
| **RT60** | Reverberation Time 60 dB | 混响时间 | 声音衰减 60 dB 所需时间，房间越"空"越长 |
| **RTF** | Real-Time Factor | 实时因子 | 处理耗时 ÷ 音频时长，< 1 才能实时 |
| **10 ms 帧** | — | 帧粒度 | APM 唯一接受的输入块大小 |

---

## 附录：相关文档

| 文档 | 内容 |
|---|---|
| [README.md](README.md) | 项目使用说明、四种模式、已知限制 |
| [docs/aec-live-test-report.md](docs/aec-live-test-report.md) | 真实录音实测报告：分段 ERLE、延迟测量、回声/底噪比分析、发现的仓库缺陷 |
| [libs/webrtc_apm/README.md](libs/webrtc_apm/README.md) | 上游 ctypes 封装的原始说明与示例 |
| [tests/test_offline.py](tests/test_offline.py) | 18 条离线单测，不需要音频设备，改代码后先跑它 |

```bash
$PY -m pytest tests/ -q        # 全部通过才继续
```
