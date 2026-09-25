"""Tests never touch the real profile: a throwaway %APPDATA% before anything is imported."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="marincall-tests-")
os.environ["MOYDISCORD_INSTANCE"] = "0"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
