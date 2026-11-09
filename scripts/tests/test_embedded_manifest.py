"""Release guard: no throwaway-signed manifest may be shipped (scripts/check-embedded-manifest.py)."""
import base64
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

SCRIPTS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_embedded_manifest", SCRIPTS / "check-embedded-manifest.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def packet(tag, body):
    """New-format packet with a one- or two-octet length."""
    if len(body) < 192:
        return bytes([0xC0 | tag, len(body)]) + body
    length = len(body) - 192
    return bytes([0xC0 | tag, (length >> 8) + 192, length & 0xFF]) + body


def key_body(seed):
    # v4, creation time, EdDSA (22), opaque key material: enough for a fingerprint.
    return bytes([4]) + (1700000000).to_bytes(4, "big") + bytes([22]) + seed * 8


def signature_body(fingerprint=None, key_id=None, version=4):
    hashed = b""
    if fingerprint:
        value = bytes([4]) + bytes.fromhex(fingerprint)
        hashed += bytes([len(value) + 1, guard.ISSUER_FINGERPRINT]) + value
    unhashed = b""
    if key_id:
        unhashed += bytes([9, guard.ISSUER_KEY_ID]) + bytes.fromhex(key_id)
    return (bytes([version, 0, 22, 10]) + len(hashed).to_bytes(2, "big") + hashed
            + len(unhashed).to_bytes(2, "big") + unhashed + b"\x00\x00" + b"\x00\x08\xff")


def armor(kind, data):
    text = base64.b64encode(data).decode()
    lines = [text[i:i + 64] for i in range(0, len(text), 64)]
    return (f"-----BEGIN PGP {kind}-----\n\n" + "\n".join(lines) + "\n=AAAA\n"
            + f"-----END PGP {kind}-----\n").encode()


def certificate(*bodies):
    data = packet(guard.PUBLIC_KEY_TAG, bodies[0]) + b"".join(packet(guard.PUBLIC_SUBKEY_TAG, b) for b in bodies[1:])
    return armor("PUBLIC KEY BLOCK", data)


def binary_with(signature):
    return b"\x00MZ\x90" + os.urandom(64) + b'{"manifest":1}' + armor("SIGNATURE", packet(2, signature)) + b"\x00" * 16


class PacketTests(unittest.TestCase):
    def test_v4_fingerprint_matches_rfc4880_formula(self):
        body = key_body(b"\x01\x02\x03\x04")
        import hashlib
        expected = hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()
        self.assertEqual(guard.key_identity(body), (expected, expected[-16:]))

    def test_issuer_fingerprint_and_key_id_are_read(self):
        fingerprint = "AB" * 20
        issuers = guard.signature_issuers(signature_body(fingerprint, "CD" * 8))
        self.assertEqual(issuers, {"fingerprints": {fingerprint}, "key_ids": {"CD" * 8}})

    def test_old_format_and_long_lengths_parse(self):
        body = b"\x04" + b"x" * 300
        old = bytes([0x80 | (6 << 2) | 1]) + len(body).to_bytes(2, "big") + body
        self.assertEqual(list(guard.packets(old)), [(6, body)])
        self.assertEqual(list(guard.packets(packet(6, body))), [(6, body)])

    def test_non_armor_strings_are_ignored(self):
        data = b"-----BEGIN PGP SIGNATURE-----\x00\x01binary\x00-----END PGP SIGNATURE-----"
        self.assertEqual(guard.embedded_signatures(data), [])


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.release_primary, self.release_sub = key_body(b"\x10\x11\x12\x13"), key_body(b"\x20\x21\x22\x23")
        self.throwaway_primary, self.throwaway_sub = key_body(b"\x30\x31\x32\x33"), key_body(b"\x40\x41\x42\x43")
        self.release = guard.certificate_keys(certificate(self.release_primary, self.release_sub))
        self.throwaway = guard.certificate_keys(certificate(self.throwaway_primary, self.throwaway_sub))
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)

    def write(self, name, data):
        path = self.work / name
        path.write_bytes(data)
        return path

    def signed(self, body, by_fingerprint=True):
        fingerprint, key_id = guard.key_identity(body)
        return signature_body(fingerprint if by_fingerprint else None, None if by_fingerprint else key_id)

    def test_binary_without_manifest_passes_even_without_a_release_certificate(self):
        app = self.write("murmur.exe", b"MZ" + os.urandom(256))
        self.assertEqual(guard.check([app]), ["murmur.exe: no embedded manifest signature"])

    def test_throwaway_signed_manifest_fails(self):
        app = self.write("murmur.exe", binary_with(self.signed(self.throwaway_sub)))
        with self.assertRaisesRegex(guard.GuardError, "throwaway key"):
            guard.check([app], [self.release], [self.throwaway])

    def test_unknown_signer_fails_against_the_release_certificate(self):
        app = self.write("murmur.exe", binary_with(self.signed(self.throwaway_sub)))
        with self.assertRaisesRegex(guard.GuardError, "not by the release certificate"):
            guard.check([app], [self.release])

    def test_any_embedded_manifest_fails_without_a_release_certificate(self):
        app = self.write("murmur.exe", binary_with(self.signed(self.release_sub)))
        with self.assertRaisesRegex(guard.GuardError, "no release certificate"):
            guard.check([app])

    def test_release_subkey_signature_passes(self):
        app = self.write("murmur", binary_with(self.signed(self.release_sub)))
        self.assertIn("signed by the release key", guard.check([app], [self.release])[0])

    def test_key_id_only_signature_is_matched(self):
        app = self.write("murmur", binary_with(self.signed(self.release_sub, by_fingerprint=False)))
        guard.check([app], [self.release])
        with self.assertRaises(guard.GuardError):
            guard.check([app], [self.release], [guard.certificate_keys(certificate(self.release_sub))])

    def test_zip_members_are_scanned(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("README.txt", "portable")
            archive.writestr("murmur.exe", binary_with(self.signed(self.throwaway_primary)))
        portable = self.write("murmur-windows-x86_64.zip", buffer.getvalue())
        with self.assertRaisesRegex(guard.GuardError, r"murmur-windows-x86_64\.zip:murmur\.exe"):
            guard.check([portable], [self.release], [self.throwaway])

    def test_cli_treats_a_missing_release_certificate_as_absent(self):
        app = self.write("murmur.exe", binary_with(self.signed(self.release_sub)))
        self.assertEqual(guard.main([str(app), "--trusted-cert", str(self.work / "missing.asc")]), 1)
        clean = self.write("clean.exe", b"MZ")
        self.assertEqual(guard.main([str(clean), "--trusted-cert", str(self.work / "missing.asc")]), 0)


@unittest.skipUnless(shutil.which("gpg") and os.name != "nt", "needs GnuPG (Linux CI)")
class RealSignatureTests(unittest.TestCase):
    """The same throwaway key layout CI uses (scripts/components/dev-key.sh)."""

    def test_dev_key_signature_is_recognised(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            home = work / "key"
            env = dict(os.environ, GNUPGHOME=str(home))
            try:
                subprocess.run(["bash", str(SCRIPTS / "components/dev-key.sh"), str(home)], check=True,
                               capture_output=True, env=env)
                manifest = work / "components-manifest.json"
                manifest.write_text('{"schema":1}')
                subprocess.run(["bash", str(SCRIPTS / "components/sign-manifest.sh"), str(manifest), str(work / "cert.asc")],
                               check=True, capture_output=True, env=env)
                app = work / "murmur"
                app.write_bytes(b"\x7fELF" + os.urandom(128) + manifest.read_bytes() + (work / "components-manifest.json.sig").read_bytes())
                throwaway = guard.certificate_keys((work / "cert.asc").read_bytes())
                with self.assertRaisesRegex(guard.GuardError, "throwaway key"):
                    guard.check([app], [], [throwaway])
                self.assertIn("release key", guard.check([app], [throwaway])[0])
            finally:
                subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], capture_output=True)


if __name__ == "__main__":
    unittest.main()
