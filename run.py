#!/usr/bin/env python3
"""AEC Demo 启动脚本：python run.py [--selftest|--simulate] [--port 8770]."""

import sys

from aec_demo.cli import main

if __name__ == "__main__":
    sys.exit(main())
