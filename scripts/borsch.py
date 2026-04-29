#!/usr/bin/env python3

import importlib
import sys

from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.modules.pop("nextgis", None)
main = importlib.import_module("nextgis.bridge").main


if __name__ == "__main__":
    raise SystemExit(main())