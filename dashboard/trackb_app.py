"""Track B operations & security dashboard launcher.

    streamlit run dashboard/trackb_app.py        (from the repository root)

The implementation lives in ``backend/aorts/dashboard/app.py``; it is executed on every Streamlit
rerun via ``runpy`` (a plain import would only run once).
"""

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

runpy.run_module("backend.aorts.dashboard.app", run_name="__main__")
