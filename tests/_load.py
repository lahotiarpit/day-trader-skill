"""Load a script from a skill folder (the folder names have hyphens)."""

import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The tests pin their own market and data source: a standing TRADER_MARKET=IN or
# TRADER_DATA=kite in the shell must not change what they expect.
for _k in ("TRADER_MARKET", "TRADER_DATA"):
    os.environ.pop(_k, None)
FIX = os.path.join(ROOT, "tests", "fixtures")


def load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("trader_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
