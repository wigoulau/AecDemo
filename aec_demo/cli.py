"""启动入口：WebUI 服务 / 命令行自检 / 无界面跑一次仿真."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from . import apm
from .config import OUTPUT_DIR, PRESETS, AecSettings, RunOptions, SimulateSettings


def cmd_selftest() -> int:
    info = apm.platform_info()
    print("平台信息:")
    for k, v in info.items():
        print(f"  {k}: {v}")
    ok, msg = apm.check_library()
    print(f"APM 动态库自检: {'通过' if ok else '失败'} - {msg}")
    try:
        import numpy, soundfile  # noqa: F401

        print(f"numpy {numpy.__version__} / soundfile {soundfile.__version__}")
    except Exception as exc:
        print(f"依赖检查失败: {exc}")
    try:
        import sounddevice as sd

        ins = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
        outs = [d for d in sd.query_devices() if d["max_output_channels"] > 0]
        print(f"音频设备: {len(ins)} 个输入 / {len(outs)} 个输出（仅统计，未打开）")
    except Exception as exc:
        print(f"音频设备查询失败: {exc}")
    return 0 if ok else 1


def cmd_simulate(args: argparse.Namespace) -> int:
    from .runner import run_simulation
    from types import SimpleNamespace
    from .runner import Job

    job = Job(id="cli", kind="simulate", title="CLI 仿真")
    sim = SimulateSettings(duration_s=args.duration, echo_delay_ms=args.echo_delay)
    aec = AecSettings(preset=args.preset, delay_ms=args.delay, delay_auto=args.auto_delay)
    run_simulation(job, sim, aec, RunOptions())
    print("\n".join(job.logs))
    print(f"\n产物目录: {OUTPUT_DIR / job.result.get('dir', '')}")
    for a in job.result.get("artifacts", []):
        print(f"  - {a['label']}: {a['name']}")
    return 0 if job.status == "done" else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    ok, msg = apm.check_library()
    print(f"[AEC] APM 自检: {'OK' if ok else 'FAIL'} ({msg})")
    print(f"[AEC] 打开 http://{args.host}:{args.port}")
    if args.open:
        webbrowser.open(f"http://127.0.0.1:{args.port}")
    uvicorn.run("aec_demo.server:app", host=args.host, port=args.port,
                reload=False, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AEC Demo：WebRTC APM 回声消除测试台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--selftest", action="store_true", help="仅跑环境/库自检")
    parser.add_argument("--simulate", action="store_true", help="无界面跑一次离线仿真")
    parser.add_argument("--duration", type=float, default=6.0)
    parser.add_argument("--echo-delay", type=int, default=50)
    parser.add_argument("--delay", type=int, default=60, help="AEC 设定的流延迟 ms")
    parser.add_argument("--auto-delay", action="store_true", help="自动估计延迟")
    parser.add_argument("--preset", default="voice_assistant", choices=sorted(PRESETS))
    args = parser.parse_args(argv)

    if args.selftest:
        return cmd_selftest()
    if args.simulate:
        return cmd_simulate(args)
    return cmd_serve(args)


if __name__ == "__main__":
    sys.exit(main())
