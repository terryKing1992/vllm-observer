"""Optional startup hook: add this directory to PYTHONPATH to make it visible.

Python discovers this file through its standard site initialization. No vLLM
source files or environment-wide sitecustomize files need to be modified.
"""

import os
import sys

if os.getenv("OBSERVER_ENABLED", "0") == "1":
    try:
        from vllm_observer.bootstrap import install

        install()
    except Exception as exc:
        # Missing observer dependencies must not prevent the model from starting.
        try:
            sys.stderr.write(f"vllm-observer startup hook skipped: {exc}\n")
        except Exception:
            pass
