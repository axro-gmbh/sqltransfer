from __future__ import annotations

import sys
from pathlib import Path

import flet as ft

sys.path.insert(0, str(Path(__file__).parent / "src"))

from sqltransfer_app.app import main


if __name__ == "__main__":
    ft.run(main)


