"""Direct messages carried through the room: sealing, receipts, limits."""
import base64

import iroh
import pytest

from marincall import mailbox


def ident():
    key = iroh.SecretKey.generate()
    return key, key.to_bytes().hex(), key.public().to_bytes().hex()


def sign_with(key):
    return lambda data: key.sign(data).to_bytes()


def verify(author, data, sig):
    try:
        iroh.EndpointId.from_bytes(bytes.fromhex(author)).verify(data, iroh.Signature.from_bytes(sig))
        return True
    except Exception:
        return False


def test_only_the_recipient_can_open_it():
    _, vika_seed, vika = ident()
    _, boris_seed, _ = ident()
    box = mailbox.Keys.seal(vika, b"secret plans")
    assert mailbox.Keys(vika_seed).open(box) == b"secret plans"
    assert mailbox.Keys(boris_seed).open(box) is None            # a relay cannot read it
    assert mailbox.Keys(vika_seed).open(box[:-1] + bytes([box[-1] ^ 1])) is None


def test_sealed_files():
    key, sealed = mailbox.seal_file(b"picture bytes" * 100)
    assert b"picture" not in sealed
    assert mailbox.open_file(key, sealed) == b"picture bytes" * 100
    assert mailbox.open_file(bytes(32), sealed) is None
    assert mailbox.blob_id(sealed) != mailbox.blob_id(mailbox.seal_file(b"picture bytes" * 100)[1])


def test_envelopes_are_checked():
    _, _, vika = ident()
    env = mailbox.make_envelope(vika, mailbox.Keys.seal(vika, b"hi"), ["a" * 32])
    assert mailbox.validate(env) == env
    assert mailbox.validate({**env, "id": "b" * 32}) is None                 # id is the box's hash
    assert mailbox.validate({**env, "to": "nobody"}) is None
    assert mailbox.validate({**env, "blobs": ["../../x"]}) is None
    assert mailbox.validate({**env, "ts": env["ts"] - mailbox.TTL_MS - 1}) is None
    big = base64.b64encode(bytes(mailbox.MAX_BOX + 10)).decode()
    assert mailbox.validate({**env, "box": big}) is None


def test_receipts_drop_mail_and_cannot_be_forged(tmp_path):
    vika_key, _, vika = ident()
    boris_key, _, boris = ident()
    box = mailbox.Mailbox(tmp_path / "mail.json")
    env = mailbox.make_envelope(vika, mailbox.Keys.seal(vika, b"hi"))
    assert box.add(env) and not box.add(env)
    # a relay signing a "receipt" for mail addressed to someone else: it must not count
    fake = mailbox.check_receipt(mailbox.make_receipt(boris, [env["id"]], sign_with(boris_key)), verify)
    assert fake is not None
    box.add_receipt(fake)
    assert env["id"] in box.env
    # a receipt in the recipient's name with someone else's signature fails the check
    forged = {**mailbox.make_receipt(boris, [env["id"]], sign_with(boris_key)), "by": vika}
    assert mailbox.check_receipt(forged, verify) is None
    real = mailbox.check_receipt(mailbox.make_receipt(vika, [env["id"]], sign_with(vika_key)), verify)
    box.add_receipt(real)
    assert env["id"] not in box.env and not box.add(env)           # collected: never kept again
    assert box.has_receipt(real) and box.receipt_of(vika, env["id"]) == real


def test_own_mail_is_tracked_and_persisted(tmp_path):
    vika_key, _, vika = ident()
    box = mailbox.Mailbox(tmp_path / "mail.json")
    env = mailbox.make_envelope(vika, mailbox.Keys.seal(vika, b"hi"), ["c" * 32])
    box.add(env, own_seqs=[5, 6])
    box.blobs.add("c" * 32)
    box.save()
    again = mailbox.Mailbox(tmp_path / "mail.json")
    assert again.env == {env["id"]: env} and again.own[env["id"]]["seqs"] == [5, 6]
    assert again.blob_ids() == {"c" * 32} and again.blobs == {"c" * 32}
    rec = mailbox.check_receipt(mailbox.make_receipt(vika, [env["id"]], sign_with(vika_key)), verify)
    delivered, new = again.add_receipt(rec)
    assert new and delivered == [{"to": vika, "seqs": [5, 6]}] and again.blob_ids() == set()


def test_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(mailbox, "MAX_PER_RECIPIENT", 3)
    _, _, vika = ident()
    box = mailbox.Mailbox(tmp_path / "mail.json")
    envs = [mailbox.make_envelope(vika, mailbox.Keys.seal(vika, bytes([i]))) for i in range(5)]
    assert [box.add(e) for e in envs] == [True, True, True, False, False]
    monkeypatch.setattr(mailbox, "MAX_TOTAL", sum(len(e["box"]) for e in envs[:2]))
    _, _, dima = ident()
    other = mailbox.make_envelope(dima, mailbox.Keys.seal(dima, b"x"))
    assert box.add(other)                        # the oldest of others' mail makes room
    assert other["id"] in box.env and len(box.env) <= 2


def test_expiry(tmp_path):
    _, _, vika = ident()
    box = mailbox.Mailbox(tmp_path / "mail.json")
    env = mailbox.make_envelope(vika, mailbox.Keys.seal(vika, b"old"))
    box.add(env)
    box.env[env["id"]]["ts"] -= mailbox.TTL_MS + 1
    box.expire()
    assert box.env == {}


@pytest.mark.parametrize("rec", [None, {}, {"by": "x", "ts": 1, "ids": [], "sig": ""},
                                 {"by": "a" * 64, "ts": 1, "ids": ["a" * 32], "sig": "0" * 128}])
def test_bad_receipts(rec):
    assert mailbox.check_receipt(rec, verify) is None
