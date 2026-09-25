"""
Release signatures.

Every file of a release (the installer and the source archive) goes up with a
`<name>.sig` next to it: the file's name and SHA-256, signed with the MarinCall
release key (Ed25519). The app installs an update only if that signature checks
out against the public key built into it — someone who gets hold of the GitHub
repository or swaps a download still cannot push an installer to everyone.

The name is part of what is signed, so an old (validly signed) installer cannot be
passed off as a newer version either.
"""

import hashlib
import json

MAGIC = b"MarinCall release v1\n"


def digest(data):
    """SHA-256 of bytes or of a file path, hex."""
    h = hashlib.sha256()
    if isinstance(data, (bytes, bytearray)):
        h.update(data)
    else:
        with open(data, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def _message(name, sha256):
    return MAGIC + name.encode() + b"\n" + sha256.encode()


def public_key(secret_hex):
    import iroh
    return iroh.SecretKey.from_bytes(bytes.fromhex(secret_hex)).public().to_bytes().hex()


def new_secret():
    import iroh
    return iroh.SecretKey.generate().to_bytes().hex()


def sign(secret_hex, name, sha256):
    """The text of `<name>.sig`."""
    import iroh
    key = iroh.SecretKey.from_bytes(bytes.fromhex(secret_hex))
    sig = key.sign(_message(name, sha256)).to_bytes().hex()
    return json.dumps({"file": name, "sha256": sha256, "sig": sig}, indent=1) + "\n"


def verify(public_hex, name, data, sig_text):
    """Raises ValueError unless `sig_text` is the release key's signature of this very file."""
    import iroh
    try:
        sig = json.loads(sig_text)
    except ValueError:
        raise ValueError("файл подписи повреждён") from None
    if not isinstance(sig, dict) or sig.get("file") != name:
        raise ValueError("подпись от другого файла")
    sha256 = digest(data)
    if sig.get("sha256") != sha256:
        raise ValueError("файл отличается от подписанного")
    try:
        iroh.EndpointId.from_bytes(bytes.fromhex(public_hex)).verify(
            _message(name, sha256), iroh.Signature.from_bytes(bytes.fromhex(str(sig.get("sig", "")))))
    except Exception:
        raise ValueError("подпись не от ключа MarinCall") from None
