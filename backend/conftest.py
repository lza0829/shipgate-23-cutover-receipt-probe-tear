"""确保从任意 cwd 运行 pytest 都能 import 到 backend/app。"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
