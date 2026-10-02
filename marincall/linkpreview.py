"""
Link previews: the title, description and picture of a page, like Discord's embeds.

Only the sender's computer opens the page (when the message is sent); everyone else gets the
finished card as a signed `embed` event, and the picture as an ordinary shared file. So nobody
else's address is shown to the site, and every peer sees the same card.
"""

import html
import ipaddress
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from .config import VERSION

UA = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) MarinCall/{VERSION} (link preview)"
TIMEOUT = 6
MAX_PAGE = 768 * 1024          # read at most this much HTML: the <head> is at the top
MAX_IMAGE = 4 * 1024 * 1024
PER_MESSAGE = 3
URL_RE = re.compile(r"https?://[^\s<>\"]+[^\s<>\".,:;!?)\]']")


def links(text):
    """The links of a message worth a card: not inside `code`, ```blocks``` or ||spoilers||."""
    text = re.sub(r"```.*?```|`[^`\n]*`|\|\|.+?\|\|", " ", text, flags=re.S)
    out = []
    for url in URL_RE.findall(text):
        if url not in out and _public(url):
            out.append(url)
    return out[:PER_MESSAGE]


def _public(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".lan", ".localhost")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global


class _Meta(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            raise StopIteration           # everything we need is in <head>

    def handle_data(self, data):
        if self._in_title and len(self.title) < 300:
            self.title += data


def _clean(value, limit):
    value = " ".join(html.unescape(value or "").split())
    return value[:limit - 1] + "…" if len(value) > limit else value


def _open(url, accept):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept,
                                               "Accept-Language": "ru,en;q=0.8"})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def fetch(url):
    """{url, title, desc, site, image_url, image_bytes} or None. Blocking — call off the UI thread."""
    with _open(url, "text/html,application/xhtml+xml,image/*;q=0.8,*/*;q=0.5") as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        final = r.geturl()
        if ctype.startswith("image/"):                 # a direct link to a picture
            data = r.read(MAX_IMAGE + 1)
            if len(data) > MAX_IMAGE:
                return None
            name = urllib.parse.unquote(urllib.parse.urlsplit(final).path.rsplit("/", 1)[-1])
            return {"url": url, "title": "", "desc": "", "site": _clean(name, 60),
                    "image_url": final, "image_bytes": data}
        if "html" not in ctype:
            return None
        raw = r.read(MAX_PAGE)
        charset = r.headers.get_content_charset()
    if not charset:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096], re.I)
        charset = m.group(1).decode() if m else "utf-8"
    try:
        page = raw.decode(charset, "replace")
    except LookupError:
        page = raw.decode("utf-8", "replace")
    out = parse(page, url, final)
    if not out:
        return None
    if out["image_url"].startswith(("http://", "https://")) and _public(out["image_url"]):
        try:
            with _open(out["image_url"], "image/*") as r:
                if (r.headers.get("Content-Type") or "").lower().startswith("image/"):
                    data = r.read(MAX_IMAGE + 1)
                    if len(data) <= MAX_IMAGE:
                        out["image_bytes"] = data
        except Exception:
            pass
    return out


def parse(page, url, final=None):
    """The card from a page's HTML (Open Graph first, then <title> and the description)."""
    final = final or url
    parser = _Meta()
    try:
        parser.feed(page)
    except StopIteration:
        pass
    m = parser.meta
    title = m.get("og:title") or m.get("twitter:title") or parser.title
    desc = m.get("og:description") or m.get("twitter:description") or m.get("description")
    site = m.get("og:site_name") or urllib.parse.urlsplit(final).hostname or ""
    image = m.get("og:image") or m.get("og:image:url") or m.get("twitter:image") or ""
    if site.startswith("www."):
        site = site[4:]
    out = {"url": url, "title": _clean(title, 200), "desc": _clean(desc, 300), "site": _clean(site, 60),
           "image_url": urllib.parse.urljoin(final, html.unescape(image)) if image else "",
           "image_bytes": None}
    return out if out["title"] else None


def thumbnail(data, path, box=(480, 360)):
    """The page's picture, scaled down and saved as JPEG/PNG; False if it is not a picture."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    img = QImage.fromData(data)
    if img.isNull() or img.width() < 32 or img.height() < 32:
        return False
    if img.width() > box[0] or img.height() > box[1]:
        img = img.scaled(box[0], box[1], Qt.KeepAspectRatio, Qt.SmoothTransformation)
    fmt = "PNG" if img.hasAlphaChannel() else "JPG"
    return img.save(str(path), fmt, 85)
