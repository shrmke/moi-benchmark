"""Compatibility entry point; implementation lives in toolathlon_astra.batch."""
import importlib
import runpy
import sys

if __name__ == "__main__":
    runpy.run_module("toolathlon_astra.batch", run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module("toolathlon_astra.batch")
