"""Link previews: which links get a card, and what is read from the page."""
from marincall import linkpreview as lp


def test_which_links():
    text = ("смотри https://github.com/Marsikcat/screen-share, и `https://code.example` "
            "||https://spoiler.example|| http://192.168.1.5/x http://localhost:3000 https://github.com/Marsikcat/screen-share")
    assert lp.links(text) == ["https://github.com/Marsikcat/screen-share"]
    assert lp.links("```\nhttps://in.block\n```") == []


def test_open_graph():
    page = """<html><head><meta charset="utf-8"><title>Запасной</title>
    <meta property="og:title" content="Видео &amp; музыка">
    <meta property="og:description" content="Описание">
    <meta property="og:site_name" content="ВидеоХостинг">
    <meta property="og:image" content="/img/cover.jpg"></head><body>…</body></html>"""
    card = lp.parse(page, "https://video.example/watch?v=1")
    assert card["title"] == "Видео & музыка" and card["desc"] == "Описание"
    assert card["site"] == "ВидеоХостинг" and card["image_url"] == "https://video.example/img/cover.jpg"


def test_plain_title():
    card = lp.parse("<title> Просто   страница </title><meta name=description content='Текст'>",
                    "https://www.site.example/a")
    assert card["title"] == "Просто страница" and card["desc"] == "Текст" and card["site"] == "site.example"
    assert lp.parse("<html><body>нет заголовка</body></html>", "https://x.example") is None
