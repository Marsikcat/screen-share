"""Message formatting: safe HTML out of a Markdown subset."""
import pytest

from marincall.ui import richtext
from marincall.ui.theme import T


@pytest.fixture(autouse=True)
def theme():
    T.load({"theme": "dark", "accent": "#5865f2", "font_scale": 100})


def test_html_is_escaped():
    out = richtext.render("<script>alert(1)</script> & <b>")
    assert "<script>" not in out and "&lt;script&gt;" in out and "&amp;" in out


def test_formatting():
    out = richtext.render("**жирный** *курсив* ~~нет~~ __подч__ `код`")
    for tag in ("<b>жирный</b>", "<i>курсив</i>", "<s>нет</s>", "<u>подч</u>"):
        assert tag in out
    assert "Consolas" in out and "код" in out


def test_code_block_has_no_blank_line_around_it():
    out = richtext.render("смотри:\n```\nprint(1)\n```\nвсё")
    assert "<br><table" not in out and "</table><br>" not in out
    assert "print(1)" in out


def test_markdown_inside_code_is_left_alone():
    out = richtext.render("```\n**не жирный** <тег>\n```")
    assert "<b>" not in out and "&lt;тег&gt;" in out


def test_links():
    out = richtext.render("тут https://example.com/a?b=1&c=2.")
    assert 'href="https://example.com/a?b=1&amp;c=2"' in out
    assert out.rstrip().endswith(".")               # the full stop is not part of the link


def test_mentions():
    mine = richtext.render("@Алиса привет", "Алиса")
    other = richtext.render("@Борис привет", "Алиса")
    everyone = richtext.render("@все сбор", "Алиса")
    assert "@Алиса" in mine and "@Борис" in other
    assert T.c["header"] in mine and T.c["header"] in everyone and T.c["header"] not in other


def test_jumbo_emoji():
    assert richtext.is_jumbo("🔥🎮")
    assert not richtext.is_jumbo("🔥 ок")
    assert not richtext.is_jumbo("")


def test_spoilers_hide_until_clicked():
    out = richtext.render("ответ: ||Дарт Вейдер — отец||")
    assert 'href="spoiler:0"' in out
    shown = richtext.render("ответ: ||Дарт Вейдер — отец||", revealed={0})
    assert "spoiler:0" not in shown and "Дарт Вейдер" in shown
    assert richtext.plain_preview("кто? ||он||") == "кто? ▒▒▒"
    assert richtext.hide_spoilers("||a|| и ||b||") == "▒▒▒ и ▒▒▒"


def test_lists_and_headings():
    out = richtext.render("# План\n- первое\n* второе\n1. раз\n2) два")
    assert "font-weight:700" in out and "План" in out
    assert out.count("•") == 2 and "1." in out and "2)" in out
    assert "# План" not in richtext.plain_preview("# План")


def test_emoji_search():
    assert richtext.emoji_matches("🔥", "огонь")
    assert richtext.emoji_matches("😂", "сме")               # word start
    assert richtext.emoji_matches("🔥", "fire")              # Unicode name
    assert not richtext.emoji_matches("🔥", "кот")
    assert richtext.emoji_matches("🐱", "")
