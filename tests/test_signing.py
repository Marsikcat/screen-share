"""Updates install only what the release key signed."""
import urllib.error

import pytest

from marincall import signing, updater


@pytest.fixture
def key():
    secret = signing.new_secret()
    return secret, signing.public_key(secret)


def test_round_trip(key):
    secret, public = key
    data = b"installer bytes"
    sig = signing.sign(secret, "MarinCall-Setup-9.9.9.exe", signing.digest(data))
    signing.verify(public, "MarinCall-Setup-9.9.9.exe", data, sig)       # no exception


@pytest.mark.parametrize("change", ["data", "name", "key", "garbage"])
def test_anything_off_is_refused(key, change):
    secret, public = key
    data, name = b"installer bytes", "MarinCall-Setup-9.9.9.exe"
    sig = signing.sign(secret, name, signing.digest(data))
    if change == "data":
        data += b"!"
    elif change == "name":
        name = "MarinCall-Setup-9.9.8.exe"
    elif change == "key":
        public = signing.public_key(signing.new_secret())
    else:
        sig = "{not json"
    with pytest.raises(ValueError):
        signing.verify(public, name, data, sig)


def _info(version="9.9.9"):
    name = updater.asset_name(version)
    return {"latest": version, "asset": name, "download": f"https://x/{name}",
            "signature": f"https://x/{name}.sig", "size": 0}


def test_updater_accepts_a_signed_release(key, monkeypatch):
    secret, public = key
    info = _info()
    data = b"new version"
    files = {info["download"]: data,
             info["signature"]: signing.sign(secret, info["asset"], signing.digest(data)).encode()}
    monkeypatch.setattr(updater, "RELEASE_KEY", public)
    monkeypatch.setattr(updater, "_get", lambda url, **kw: files[url])
    assert updater._verified(info) == data


def test_updater_refuses_unsigned_or_tampered(key, monkeypatch):
    secret, public = key
    info = _info()
    monkeypatch.setattr(updater, "RELEASE_KEY", public)

    def missing(url, **kw):
        if url.endswith(".sig"):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return b"new version"
    monkeypatch.setattr(updater, "_get", missing)
    with pytest.raises(RuntimeError, match="не подписан"):
        updater._verified(info)

    sig = signing.sign(secret, info["asset"], signing.digest(b"the real one")).encode()
    monkeypatch.setattr(updater, "_get", lambda url, **kw: sig if url.endswith(".sig") else b"evil")
    with pytest.raises(RuntimeError, match="подпись"):
        updater._verified(info)


def test_old_installer_cannot_pose_as_new(key, monkeypatch):
    secret, public = key
    info = _info("9.9.9")
    old = b"old but genuine"
    old_sig = signing.sign(secret, updater.asset_name("1.0.0"), signing.digest(old)).encode()
    monkeypatch.setattr(updater, "RELEASE_KEY", public)
    monkeypatch.setattr(updater, "_get", lambda url, **kw: old_sig if url.endswith(".sig") else old)
    with pytest.raises(RuntimeError):
        updater._verified(info)


def test_the_real_key_is_built_in():
    assert len(updater.RELEASE_KEY) == 64 and int(updater.RELEASE_KEY, 16)


def test_versions():
    assert updater.parse_version("v3.10.0") > updater.parse_version("3.9.9")
    assert updater.parse_version("3.2.1") > updater.parse_version("3.2")
