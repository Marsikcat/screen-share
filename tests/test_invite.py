import pytest

from marincall.net import make_invite, parse_invite

PEER = "ab" * 32
RELAY = "https://euw1-1.relay.n0.iroh.link./"


def test_open_room_round_trip():
    code = make_invite(PEER, RELAY, room="общая")
    assert code.startswith("marin-") and len(code) < 90
    inv = parse_invite(code)
    assert inv["peer"]["id"] == PEER and inv["peer"]["relay"] == RELAY
    assert inv["room"] == "общая" and inv["secret"] is None


def test_closed_room_round_trip():
    secret = bytes(range(16))
    inv = parse_invite(make_invite(PEER, RELAY, secret=secret))
    assert inv["secret"] == secret.hex() and inv["room"] is None


def test_pasted_with_line_breaks_and_old_prefix():
    code = make_invite(PEER, RELAY, room="комната")
    assert parse_invite(code[:20] + "\n  " + code[20:])["room"] == "комната"
    assert parse_invite("moyd-" + code[len("marin-"):])["room"] == "комната"


@pytest.mark.parametrize("bad", ["", "hello", "marin-", "marin-0OIl"])
def test_garbage(bad):
    with pytest.raises(ValueError):
        parse_invite(bad)


def test_a_typo_is_caught():
    code = make_invite(PEER, RELAY, room="общая")
    i = len(code) // 2
    typo = code[:i] + ("2" if code[i] != "2" else "3") + code[i + 1:]
    with pytest.raises(ValueError):
        parse_invite(typo)
