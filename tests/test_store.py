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
