import sys
from pathlib import Path

# pytest puts tests/ on sys.path, not the project root, so the modules under
# test (devices/, config.py) would not be importable without this.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
