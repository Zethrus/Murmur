import hashlib
import importlib.util
from pathlib import Path
import tempfile
import tomllib
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "stage-test-model.py"
spec = importlib.util.spec_from_file_location("stage_test_model", SCRIPT)
stage_test_model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage_test_model)

GOOD = b"model bytes"


def provenance(name="model.bin", data=GOOD, url="https://huggingface.co/x/resolve/abc/model.bin"):
    return {"components": {"speech-tiny": {
        "kind": "speech-model", "model": "tiny",
        "files": [{"name": name, "url": url, "size_bytes": len(data),
                   "sha256": hashlib.sha256(data).hexdigest()}]}}}


def fetcher(payload, calls):
    def fetch(url, target):
        calls.append(url)
        partial = target.with_name(target.name + ".partial")
        partial.write_bytes(payload)
        return partial
    return fetch


class StageTestModel(unittest.TestCase):
    def test_downloads_then_reuses_verified_files(self):
        with tempfile.TemporaryDirectory() as dest:
            calls = []
            folder, fetched = stage_test_model.stage("tiny", dest, provenance(), fetcher(GOOD, calls))
            self.assertEqual((folder / "model.bin").read_bytes(), GOOD)
            self.assertEqual(fetched, ["model.bin"])
            _, again = stage_test_model.stage("tiny", dest, provenance(), fetcher(GOOD, calls))
            self.assertEqual((again, len(calls)), ([], 1))

    def test_wrong_download_is_refused_and_removed(self):
        with tempfile.TemporaryDirectory() as dest:
            with self.assertRaisesRegex(ValueError, "does not match"):
                stage_test_model.stage("tiny", dest, provenance(), fetcher(b"tampered!!!", []))
            self.assertEqual(list((Path(dest) / "tiny").iterdir()), [])

    def test_a_corrupt_existing_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as dest:
            (Path(dest) / "tiny").mkdir()
            (Path(dest) / "tiny" / "model.bin").write_bytes(b"corrupt bytes")
            _, fetched = stage_test_model.stage("tiny", dest, provenance(), fetcher(GOOD, []))
            self.assertEqual(fetched, ["model.bin"])
            self.assertEqual((Path(dest) / "tiny" / "model.bin").read_bytes(), GOOD)

    def test_only_safe_names_and_hugging_face_urls(self):
        for bad in (provenance(name="../x"), provenance(url="https://example.com/model.bin")):
            with tempfile.TemporaryDirectory() as dest, self.assertRaises(ValueError):
                stage_test_model.stage("tiny", dest, bad, fetcher(GOOD, []))
        with tempfile.TemporaryDirectory() as dest, self.assertRaisesRegex(ValueError, "No pinned"):
            stage_test_model.stage("missing", dest, provenance(), fetcher(GOOD, []))

    def test_release_gpu_model_is_pinned(self):
        pinned = tomllib.loads(stage_test_model.PROVENANCE.read_text())
        files = stage_test_model.pinned_files(pinned, "distil-small.en")
        self.assertIn("model.bin", {entry["name"] for entry in files})


if __name__ == "__main__":
    unittest.main()
