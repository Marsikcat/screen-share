"""The signed event log: what every chat feature is built on."""
import iroh
import pytest

from marincall.store import Store, validate


def keypair():
    key = iroh.SecretKey.generate()
    return key, key.public().to_bytes().hex()


def verify(author, data, sig):
    try:
        iroh.EndpointId.from_bytes(bytes.fromhex(author)).verify(data, iroh.Signature.from_bytes(sig))
        return True
    except Exception:
        return False


def make_store(path, key=None):
    key = key or iroh.SecretKey.generate()
    path.mkdir(parents=True, exist_ok=True)
    return Store(path, key.public().to_bytes().hex(), lambda data: key.sign(data).to_bytes(), verify), key


def test_ids_longer_than_a_key_survive(tmp_path):
    # 3.0–3.2 cut "<64-hex key>:<seq>" to 64 characters: replies, reactions, edits
    # and deletes all pointed at nothing
    st, _ = make_store(tmp_path)
    m = st.create("msg", ch="d:text:general", text="привет", reply=None, files=[])
    assert len(m["id"]) > 64
    r = st.create("msg", ch="d:text:general", text="ответ", reply=m["id"], files=[])
    assert r["reply"] == m["id"]
    st.create("react", target=m["id"], emoji="👍", on=True)
    assert st.reactions_of(m["id"]) == {"👍": [st.me]}
    st.create("edit", target=m["id"], text="привет!")
    assert st.text_of(m) == "привет!" and st.is_edited(m)
    st.create("del", target=r["id"])
    assert [x["id"] for x in st.visible_messages("d:text:general")] == [m["id"]]


def test_messages_in_a_created_channel(tmp_path):
    st, _ = make_store(tmp_path)
    ch = st.create("ch_new", kind="text", name="Игры и всё такое")
    assert st.channel(ch["id"])["name"] == "игры-и-всё-такое"
    st.create("msg", ch=ch["id"], text="сюда", reply=None, files=[])
    assert [m["text"] for m in st.visible_messages(ch["id"])] == ["сюда"]
    st.create("ch_ren", target=ch["id"], name="игры", topic="")
    assert st.channel(ch["id"])["name"] == "игры"
    st.create("ch_del", target=ch["id"])
    assert st.channel(ch["id"]) is None


def test_only_the_author_edits_and_deletes(tmp_path):
    a, _ = make_store(tmp_path / "a")
    b, _ = make_store(tmp_path / "b")
    m = a.create("msg", ch="d:text:general", text="моё", reply=None, files=[])
    b.add(m)
    for ev in (b.create("edit", target=m["id"], text="чужое"), b.create("del", target=m["id"])):
        a.add(ev)
    assert a.text_of(a.msg_by_id[m["id"]]) == "моё"
    assert a.visible_messages("d:text:general")


def test_forged_events_are_rejected(tmp_path):
    a, _ = make_store(tmp_path / "a")
    b, _ = make_store(tmp_path / "b")
    m = a.create("msg", ch="d:text:general", text="честно", reply=None, files=[])
    assert b.add({**m, "text": "подделка"}) is None            # changed after signing
    other, _ = keypair()
    assert b.add({**m, "sig": other.sign(b"x").to_bytes().hex()}) is None
    assert b.add(m) is not None
    assert b.add(m) is None                                      # already have it


def test_sync_brings_what_is_missing(tmp_path):
    a, _ = make_store(tmp_path / "a")
    b, _ = make_store(tmp_path / "b")
    for i in range(5):
        a.create("msg", ch="d:text:general", text=f"#{i}", reply=None, files=[])
    for ev in a.missing_for(b.vector()):
        b.add(ev)
    assert [m["text"] for m in b.visible_messages("d:text:general")] == [f"#{i}" for i in range(5)]
    assert a.missing_for(b.vector()) == []


def test_history_survives_a_restart(tmp_path):
    st, key = make_store(tmp_path)
    m = st.create("msg", ch="d:text:general", text="сохранится", reply=None, files=[])
    st.create("react", target=m["id"], emoji="🔥", on=True)
    again, _ = make_store(tmp_path, key)
    assert again.text_of(again.msg_by_id[m["id"]]) == "сохранится"
    assert again.reactions_of(m["id"]) == {"🔥": [again.me]}


def test_avatar_events(tmp_path):
    st, _ = make_store(tmp_path)
    assert st.avatar_of(st.me) == ""
    st.create("avatar", file="a" * 32)
    assert st.avatar_of(st.me) == "a" * 32
    st.create("avatar", file="")
    assert st.avatar_of(st.me) == ""


@pytest.mark.parametrize("ev", [
    {"k": "avatar", "file": "../../etc/passwd"},
    {"k": "msg", "ch": "", "text": "x"},
    {"k": "msg", "ch": "d:text:general", "text": ""},
    {"k": "react", "target": "x"},
    {"k": "nonsense"},
])
def test_malformed_events(ev):
    uid = "a" * 64
    assert validate({"id": f"{uid}:1", "a": uid, "s": 1, "ts": 1, **ev}, need_sig=False) is None


def test_pins(tmp_path):
    a, _ = make_store(tmp_path / "a")
    b, _ = make_store(tmp_path / "b")
    m1 = a.create("msg", ch="d:text:general", text="правила", reply=None, files=[])
    m2 = a.create("msg", ch="d:text:general", text="ссылка", reply=None, files=[])
    b.add(m1), b.add(m2)
    for ev in (b.create("pin", target=m1["id"], on=True), b.create("pin", target=m2["id"], on=True)):
        a.add(ev)                                   # anyone in the room may pin
    assert [m["text"] for m in a.pinned("d:text:general")] == ["ссылка", "правила"]   # newest pin first
    a.add(b.create("pin", target=m2["id"], on=False))
    assert [m["id"] for m in a.pinned("d:text:general")] == [m1["id"]]
    a.create("del", target=m1["id"])
    assert a.pinned("d:text:general") == [] and not a.is_pinned(m1["id"])


def test_search(tmp_path):
    st, _ = make_store(tmp_path)
    st.create("msg", ch="d:text:general", text="Встречаемся в субботу у Бориса", reply=None, files=[])
    gone = st.create("msg", ch="d:text:general", text="в субботу не смогу", reply=None, files=[])
    st.create("msg", ch="d:text:media", text="", reply=None,
              files=[{"id": "b" * 32, "name": "Суббота-фото.png", "size": 1, "type": "image/png"}])
    st.create("del", target=gone["id"])
    assert [m["text"] for m in st.search("СУББОТ бориса")] == ["Встречаемся в субботу у Бориса"]
    assert {m["ch"] for m in st.search("суббот")} == {"d:text:general", "d:text:media"}   # files too
    assert [m["ch"] for m in st.search("суббот", cid="d:text:media")] == ["d:text:media"]
    assert st.search("   ") == []


def test_link_cards_only_from_the_author(tmp_path):
    a, _ = make_store(tmp_path / "a")
    b, _ = make_store(tmp_path / "b")
    m = a.create("msg", ch="d:text:general", text="https://example.com", reply=None, files=[])
    b.add(m)
    card = dict(url="https://example.com", title="Example", desc="", site="example.com", image="")
    a.create("embed", target=m["id"], **card)
    b.add(a.events[f"{a.me}:2"])
    fake = b.create("embed", target=m["id"], **{**card, "url": "https://evil.example", "title": "!"})
    a.add(fake)                                     # someone else cannot put a card under my message
    assert a.embeds_of(m["id"]) == b.embeds_of(m["id"])[:1] == [card]
    a.create("embed", target=m["id"], **{**card, "title": "Example Domain"})   # newer one wins
    assert [e["title"] for e in a.embeds_of(m["id"])] == ["Example Domain"]


def test_soundboard_sounds(tmp_path):
    a, _ = make_store(tmp_path / "a")
    b, _ = make_store(tmp_path / "b")
    s1 = a.create("sound", file="c" * 32, name="Мяу", emoji="🐱", target="")
    b.add(s1)
    assert [x["name"] for x in b.sound_list()] == ["Мяу"] and b.sound_files() == {"c" * 32}
    a.add(b.create("sound", file="", name="", emoji="", target=s1["id"]))   # anyone may remove one
    assert a.sound_list() == []
    assert validate({"id": f"{a.me}:9", "a": a.me, "s": 9, "ts": 1, "k": "sound", "file": "../x",
                     "name": "x"}, need_sig=False) is None


def test_kinds_from_newer_versions_are_kept_and_passed_on(tmp_path):
    # 3.5 and older dropped them: the author's log had a hole, and everything after it was
    # sent again on every connect
    from marincall.store import canonical
    a, key = make_store(tmp_path / "a")
    b, _ = make_store(tmp_path / "b")
    ev = {"id": f"{a.me}:1", "a": a.me, "s": 1, "k": "poll", "ts": 5, "question": "Во сколько?",
          "options": ["19:00", "20:00"]}
    ev["sig"] = key.sign(canonical(ev)).to_bytes().hex()
    m = a.create("msg", ch="d:text:general", text="после", reply=None, files=[])
    assert m["s"] == 1                                   # a never saw the poll itself
    c, _ = make_store(tmp_path / "c")
    assert c.add(ev) == ev                                # kept as it is, signature and all
    assert c.add({**ev, "question": "подделка"}) is None
    assert c.vector() == {a.me: 1} and c.missing_for({}) == [ev]
    again, _ = make_store(tmp_path / "c")                 # survives a restart
    assert again.events[ev["id"]] == ev
    huge = {**ev, "id": f"{a.me}:2", "s": 2, "blob": "x" * 9000}
    huge["sig"] = key.sign(canonical(huge)).to_bytes().hex()
    assert c.add(huge) is None
    assert b.vector() == {}


def test_clocks_in_the_future_do_not_break_the_chat(tmp_path):
    import time as _time
    from marincall.store import canonical
    a, key = make_store(tmp_path / "a")
    b, _ = make_store(tmp_path / "b")
    seq = 1
    for ts in (int(_time.time() * 1000) + 10 * 365 * 86400 * 1000, -5, 10 ** 18):
        ev = {"id": f"{a.me}:{seq}", "a": a.me, "s": seq, "k": "msg", "ts": ts, "ch": "d:text:general",
              "text": f"#{seq}", "reply": None, "files": []}
        ev["sig"] = key.sign(canonical(ev)).to_bytes().hex()
        assert b.add(ev)
        seq += 1
    now = int(_time.time() * 1000)
    for m in b.visible_messages("d:text:general"):
        assert 86400 * 1000 < m["ts"] <= now + 61_000            # shown as "now", not years ahead
    assert b.events[f"{a.me}:1"]["ts"] > now + 86400 * 1000      # the signed event is untouched


def test_settings_survive_a_damaged_file(monkeypatch):
    from marincall import config
    s = config.Settings()
    uid, key = s.uid, s["secret_key"]
    s.save()
    assert config.BACKUP_FILE.exists()
    config.SETTINGS_FILE.write_text("{ broken", encoding="utf-8")      # a crash mid-write, a bad disk
    again = config.Settings()
    assert again.uid == uid and again["secret_key"] == key                # restored from the backup
    assert list(config.DATA.glob("settings.damaged-*.json"))            # and the damaged one kept
    def locked(*a, **k):
        raise PermissionError("locked")
    monkeypatch.setattr(config.Path, "read_text", locked)
    monkeypatch.setattr(config.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):                                 # never a new identity instead
        config.Settings()
