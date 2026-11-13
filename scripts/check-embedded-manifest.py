#!/usr/bin/env python3
"""Release guard: a shipped binary must not carry a manifest signed by a throwaway key.

  check-embedded-manifest.py [--trusted-cert ASC]... [--throwaway-cert ASC]... BINARY|ZIP...

`MURMUR_EMBED_MANIFEST` (src-tauri/build.rs) compiles the manifest and its armored detached
signature into the binary with include_bytes!, so the signature appears verbatim. This script
finds every armored "PGP SIGNATURE" block in each binary (zip archives: every member), reads the
signer from the signature packet (issuer fingerprint subpacket, else the issuer key ID) and
fails when:

- the signer is a key of any --throwaway-cert (the CI throwaway key, when it is at hand), or
- the signer is not a key of a --trusted-cert (the committed release certificate), or
- no trusted certificate exists at all and the binary embeds a signature: a tag build without
  the release key must not embed a manifest.

A --trusted-cert path that does not exist is reported and treated as absent (the stricter case).
Pure Python, no gpg: runs on the Windows and Linux runners and in unit tests.
"""
import argparse
import base64
import hashlib
from pathlib import Path
import re
import sys
import zipfile

SIGNATURE_BLOCK = re.compile(
    rb"-----BEGIN PGP SIGNATURE-----\r?\n((?:[\x20-\x7e]*\r?\n){1,400}?)-----END PGP SIGNATURE-----")
KEY_BLOCK = re.compile(
    rb"-----BEGIN PGP PUBLIC KEY BLOCK-----\r?\n((?:[\x20-\x7e]*\r?\n)+?)-----END PGP PUBLIC KEY BLOCK-----")
SIGNATURE_TAG, PUBLIC_KEY_TAG, PUBLIC_SUBKEY_TAG = 2, 6, 14
ISSUER_KEY_ID, ISSUER_FINGERPRINT = 16, 33


class GuardError(Exception):
    pass


def dearmor(body):
    """Base64 payload of an armor body (headers, blank line and CRC24 line removed)."""
    lines = body.decode("ascii").replace("\r", "").split("\n")
    if "" in lines and any(":" in line for line in lines[: lines.index("")]):
        lines = lines[lines.index("") + 1:]
    data = "".join(line.strip() for line in lines if line.strip() and not line.startswith("="))
    try:
        return base64.b64decode(data, validate=True)
    except ValueError as error:
        raise GuardError(f"Invalid armor: {error}") from None


def packets(data):
    """(tag, body) for every OpenPGP packet (old and new header formats)."""
    position = 0
    while position < len(data):
        first = data[position]
        if not first & 0x80:
            raise GuardError("Not an OpenPGP packet stream")
        position += 1
        if first & 0x40:
            tag = first & 0x3F
            octet = data[position]
            if octet < 192:
                length, position = octet, position + 1
            elif octet < 224:
                length, position = ((octet - 192) << 8) + data[position + 1] + 192, position + 2
            elif octet == 255:
                length, position = int.from_bytes(data[position + 1:position + 5], "big"), position + 5
            else:
                raise GuardError("Partial-length packets are not expected here")
        else:
            tag, kind = (first >> 2) & 0x0F, first & 0x03
            if kind == 3:
                length = len(data) - position
            else:
                size = (1, 2, 4)[kind]
                length, position = int.from_bytes(data[position:position + size], "big"), position + size
        body = data[position:position + length]
        if len(body) != length:
            raise GuardError("Truncated OpenPGP packet")
        yield tag, body
        position += length


def subpackets(area):
    position = 0
    while position < len(area):
        octet = area[position]
        if octet < 192:
            length, position = octet, position + 1
        elif octet < 255:
            length, position = ((octet - 192) << 8) + area[position + 1] + 192, position + 2
        else:
            length, position = int.from_bytes(area[position + 1:position + 5], "big"), position + 5
        if length == 0 or position + length > len(area):
            raise GuardError("Malformed signature subpacket")
        yield area[position] & 0x7F, area[position + 1:position + length]
        position += length


def signature_issuers(body):
    """{"fingerprints": set, "key_ids": set} (upper-case hex) of one signature packet body."""
    version = body[0]
    fingerprints, key_ids = set(), set()
    if version == 3:
        key_ids.add(body[7:15].hex().upper())
    elif version in (4, 5, 6):
        size = 4 if version == 6 else 2
        offset = 4
        areas = []
        for _ in range(2):
            length = int.from_bytes(body[offset:offset + size], "big")
            areas.append(body[offset + size:offset + size + length])
            offset += size + length
        for area in areas:
            for kind, value in subpackets(area):
                if kind == ISSUER_FINGERPRINT and value:
                    fingerprints.add(value[1:].hex().upper())
                elif kind == ISSUER_KEY_ID and len(value) == 8:
                    key_ids.add(value.hex().upper())
    else:
        raise GuardError(f"Unsupported signature version {version}")
    return {"fingerprints": fingerprints, "key_ids": key_ids}


def key_identity(body):
    """(fingerprint, key ID) of a public key or subkey packet body (v4, v5, v6)."""
    version = body[0]
    if version == 4:
        fingerprint = hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()
        return fingerprint, fingerprint[-16:]
    if version in (5, 6):
        prefix = b"\x9a" if version == 5 else b"\x9b"
        fingerprint = hashlib.sha256(prefix + len(body).to_bytes(4, "big") + body).hexdigest().upper()
        return fingerprint, fingerprint[:16]
    raise GuardError(f"Unsupported key version {version}")


def certificate_keys(text):
    """Every (fingerprint, key ID) of the primary keys and subkeys in an armored certificate."""
    keys = set()
    blocks = KEY_BLOCK.findall(text)
    if not blocks:
        raise GuardError("No armored public key block")
    for block in blocks:
        for tag, body in packets(dearmor(block)):
            if tag in (PUBLIC_KEY_TAG, PUBLIC_SUBKEY_TAG):
                keys.add(key_identity(body))
    if not keys:
        raise GuardError("Certificate holds no public keys")
    return keys


def embedded_signatures(data):
    """Issuers of every armored signature found in `data` (binary contents)."""
    found = []
    for block in SIGNATURE_BLOCK.findall(data):
        for tag, body in packets(dearmor(block)):
            if tag == SIGNATURE_TAG:
                found.append(signature_issuers(body))
    return found


def signed_by(issuers, keys):
    fingerprints = {fingerprint for fingerprint, _ in keys}
    key_ids = {key_id for _, key_id in keys}
    if issuers["fingerprints"]:
        return bool(issuers["fingerprints"] & fingerprints)
    return bool(issuers["key_ids"] & key_ids)


def describe(issuers):
    return ", ".join(sorted(issuers["fingerprints"] or issuers["key_ids"])) or "unknown signer"


def contents(path):
    """(label, bytes) of a binary, or of every member of a zip archive."""
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if not member.is_dir():
                    yield f"{path.name}:{member.filename}", archive.read(member)
    else:
        yield path.name, path.read_bytes()


def check(binaries, trusted=(), throwaway=()):
    """Raise GuardError on a throwaway or unknown embedded signer; returns report lines."""
    trusted_keys = set().union(*trusted) if trusted else set()
    throwaway_keys = set().union(*throwaway) if throwaway else set()
    report, failures = [], []
    for binary in binaries:
        for label, data in contents(binary):
            signatures = embedded_signatures(data)
            if not signatures:
                report.append(f"{label}: no embedded manifest signature")
                continue
            for issuers in signatures:
                signer = describe(issuers)
                if not issuers["fingerprints"] and not issuers["key_ids"]:
                    failures.append(f"{label}: embedded signature names no signer")
                elif throwaway_keys and signed_by(issuers, throwaway_keys):
                    failures.append(f"{label}: embedded manifest is signed by the throwaway key {signer}")
                elif not trusted_keys:
                    failures.append(f"{label}: embedded manifest signed by {signer}, but no release certificate "
                                    "exists; a release build must not embed a manifest")
                elif not signed_by(issuers, trusted_keys):
                    failures.append(f"{label}: embedded manifest signed by {signer}, not by the release certificate")
                else:
                    report.append(f"{label}: embedded manifest signed by the release key {signer}")
    if failures:
        raise GuardError("\n".join(failures))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("binaries", nargs="+", type=Path)
    parser.add_argument("--trusted-cert", action="append", default=[], type=Path)
    parser.add_argument("--throwaway-cert", action="append", default=[], type=Path)
    args = parser.parse_args(argv)
    try:
        trusted = []
        for path in args.trusted_cert:
            if path.is_file():
                trusted.append(certificate_keys(path.read_bytes()))
            else:
                print(f"Trusted certificate {path} is absent: no embedded manifest is acceptable.")
        throwaway = [certificate_keys(path.read_bytes()) for path in args.throwaway_cert]
        for line in check(args.binaries, trusted, throwaway):
            print(line)
    except (GuardError, OSError, zipfile.BadZipFile) as error:
        print(f"Embedded manifest guard failed:\n{error}", file=sys.stderr)
        return 1
    print("Embedded manifest guard passed: no throwaway-signed manifest is shipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
