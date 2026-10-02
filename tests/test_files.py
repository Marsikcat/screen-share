"""Attachment names come from other people: they must never become a path of their choosing."""
import pytest

from marincall.ui.message import RISKY, safe_name


@pytest.mark.parametrize("name, expected", [
    (r"..\..\..\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\x.bat", "x.bat"),
    ("C:/Windows/System32/evil.exe", "evil.exe"),
    ("фото с катки.jpg", "фото с катки.jpg"),
    ("CON.txt", "file_CON.txt"),
    ("nul", "file_nul"),
    ("...", "file_"),
    ('a:b?c*"d".png', "a_b_c__d_.png"),
    ("x\x00y\x1f.txt", "x_y_.txt"),
    (" .hidden. ", "hidden"),
])
def test_safe_names(name, expected):
    assert safe_name(name) == expected


def test_programs_ask_first():
    assert {".exe", ".bat", ".ps1", ".lnk", ".scr", ".msi"} <= RISKY
    assert ".jpg" not in RISKY and ".pdf" not in RISKY
