"""Offline behavior tests for upstream updates and failure recovery."""

import copy
import gzip
import hashlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("release_updater", Path(__file__).parents[1] / "scripts/update.py")
updater = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = updater
SPEC.loader.exec_module(updater)

BINARY = gzip.compress(b"\x7fELF" + b"offline fixture" * 10, mtime=0)
MODEL = b"offline model fixture"
OLD_BINARY_HASH = "a" * 64
OLD_MODEL_HASH = "b" * 64


def record(name, body, tag, asset_id):
    return {"id": asset_id, "name": name, "size": len(body),
            "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
            "updated_at": "2026-10-22T19:45:28Z",
            "browser_download_url": f"https://github.com/vernesong/mihomo/releases/download/{tag}/{name}"}


def releases():
    return {
        updater.BINARY_TAG: {"tag_name": updater.BINARY_TAG, "draft": False, "assets": [
            record(updater.BINARY_PREFIX + "7654321.gz", BINARY, updater.BINARY_TAG, 1)]},
        updater.MODEL_TAG: {"tag_name": updater.MODEL_TAG, "draft": False, "assets": [
            record("Model-large.bin", MODEL, updater.MODEL_TAG, 2)]},
    }


class FakeClient:
    def __init__(self):
        self.releases = releases()
        self.downloads = []
        self.after_download = None

    def release(self, tag):
        return copy.deepcopy(self.releases[tag])

    def download(self, asset, path):
        self.downloads.append(asset.name)
        path.write_bytes(MODEL if asset.name == "Model-large.bin" else BINARY)
        if self.after_download:
            self.after_download(self)


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.recipe = self.root / "PKGBUILD"
        self.info = self.root / ".SRCINFO"
        self.recipe.write_text(
            "pkgname=mihomo-smart-bin\npkgver=20261021.194528.g4624551\npkgrel=3\n"
            f"_commit=4624551\n_binary_sha256='{OLD_BINARY_HASH}'\n_model_sha256='{OLD_MODEL_HASH}'\n"
            "# Preserve service checksum and package implementation verbatim.\n"
            "sha256sums=('fixed-service-checksum')\npackage() { :; }\n")
        self.info.write_text("original srcinfo\n")
        self.original = self.recipe.read_bytes(), self.info.read_bytes()
        self.client = FakeClient()

    def assert_unchanged(self):
        self.assertEqual((self.recipe.read_bytes(), self.info.read_bytes()), self.original)

    def generated_srcinfo(self, *args, **kwargs):
        fields = updater.read_fields(self.recipe.read_text())
        asset = self.client.releases[updater.BINARY_TAG]["assets"][0]
        content = ("pkgbase = mihomo-smart-bin\n"
                   f"\tpkgver = {fields['pkgver']}\n\tpkgrel = {fields['pkgrel']}\n"
                   f"\tsource = {asset['browser_download_url']}\n"
                   f"\tsha256sums = {fields['_binary_sha256']}\n"
                   f"\tsha256sums = {fields['_model_sha256']}\n")
        return subprocess.CompletedProcess(args, 0, content.encode(), b"")

    def test_check_reports_calendar_version_without_download_or_execution(self):
        # Upstream ships the same architecture and commit in several package formats.
        for suffix in ("deb", "rpm", "pkg.tar.zst"):
            other = copy.deepcopy(self.client.releases[updater.BINARY_TAG]["assets"][0])
            other["name"] = updater.BINARY_PREFIX + "7654321." + suffix
            self.client.releases[updater.BINARY_TAG]["assets"].append(other)
        with patch.object(updater.subprocess, "run", side_effect=AssertionError("unexpected execution")):
            result = updater.update(self.root, self.client)
        self.assertEqual(result["changed"], "true")
        self.assertEqual(result["pkgver"], "20261022.194528.g7654321")
        self.assertEqual(result["pkgrel"], "1")
        self.assertEqual(self.client.downloads, [])
        self.assert_unchanged()

    def test_update_verifies_then_generates_metadata_and_is_idempotent(self):
        with patch.object(updater.subprocess, "run", side_effect=self.generated_srcinfo) as command:
            result = updater.update(self.root, self.client, write=True)
            self.assertEqual(result["changed"], "true")
            command.assert_called_once()
            self.assertEqual(command.call_args.args[0], ["makepkg", "--printsrcinfo"])
            self.assertEqual(updater.update(self.root, self.client, write=True)["changed"], "false")
            command.assert_called_once()
        self.assertEqual(len(self.client.downloads), 2)
        self.assertIn("sha256sums=('fixed-service-checksum')", self.recipe.read_text())
        self.assertIn("pkgver = 20261022.194528.g7654321", self.info.read_text())

    def test_model_only_change_bumps_release_preserving_binary_version(self):
        binary = self.client.releases[updater.BINARY_TAG]["assets"][0]
        binary["name"] = updater.BINARY_PREFIX + "4624551.gz"
        binary["browser_download_url"] = (
            f"https://github.com/vernesong/mihomo/releases/download/{updater.BINARY_TAG}/{binary['name']}")
        binary["digest"] = "sha256:" + OLD_BINARY_HASH
        binary["updated_at"] = "2026-10-21T19:45:28Z"
        result = updater.update(self.root, self.client)
        self.assertEqual(result["pkgver"], "20261021.194528.g4624551")
        self.assertEqual(result["pkgrel"], "4")
        self.assertEqual(result["binary_sha256"], OLD_BINARY_HASH)
        self.assertEqual(result["model_sha256"], hashlib.sha256(MODEL).hexdigest())
        self.assert_unchanged()

    def test_mutated_same_commit_requires_review(self):
        asset = self.client.releases[updater.BINARY_TAG]["assets"][0]
        asset["name"] = updater.BINARY_PREFIX + "4624551.gz"
        asset["browser_download_url"] = (
            f"https://github.com/vernesong/mihomo/releases/download/{updater.BINARY_TAG}/{asset['name']}")
        with self.assertRaisesRegex(updater.UpdateError, "same commit"):
            updater.update(self.root, self.client, write=True)
        self.assertEqual(self.client.downloads, [])
        self.assert_unchanged()

    def test_rollback_and_equal_timestamp_different_commit_are_rejected(self):
        asset = self.client.releases[updater.BINARY_TAG]["assets"][0]
        for date in ("2026-10-20T19:45:28Z", "2026-10-21T19:45:28Z"):
            with self.subTest(date=date):
                asset["updated_at"] = date
                with self.assertRaises(updater.UpdateError):
                    updater.update(self.root, self.client, write=True)
                self.assert_unchanged()

    def test_release_race_leaves_both_files_untouched(self):
        def changed(client):
            client.releases[updater.MODEL_TAG]["assets"][0]["id"] += 1
        self.client.after_download = changed
        with patch.object(updater.subprocess, "run") as command:
            with self.assertRaisesRegex(updater.UpdateError, "changed during download"):
                updater.update(self.root, self.client, write=True)
            command.assert_not_called()
        self.assert_unchanged()

    def test_makepkg_failure_restores_recipe_and_srcinfo(self):
        with patch.object(updater.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "makepkg")):
            with self.assertRaises(subprocess.CalledProcessError):
                updater.update(self.root, self.client, write=True)
        self.assert_unchanged()

    def test_invalid_generated_metadata_restores_both_files(self):
        result = subprocess.CompletedProcess([], 0, b"pkgbase = other\n", b"")
        with patch.object(updater.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(updater.UpdateError, "pkgbase"):
                updater.update(self.root, self.client, write=True)
        self.assert_unchanged()

    def test_failed_generation_preserves_absent_srcinfo(self):
        self.info.unlink()
        with patch.object(updater.subprocess, "run", side_effect=OSError("missing makepkg")):
            with self.assertRaises(OSError):
                updater.update(self.root, self.client, write=True)
        self.assertEqual(self.recipe.read_bytes(), self.original[0])
        self.assertFalse(self.info.exists())

    def test_bad_gzip_never_rewrites_recipe(self):
        def truncated(asset, path):
            path.write_bytes(BINARY[:-4])
        self.client.download = truncated
        with self.assertRaisesRegex(updater.UpdateError, "gzip"):
            updater.update(self.root, self.client, write=True)
        self.assert_unchanged()

    def test_local_edit_during_download_is_preserved(self):
        self.client.after_download = lambda client: self.recipe.write_text("concurrent user edit\n")
        with self.assertRaisesRegex(updater.UpdateError, "Local recipe changed"):
            updater.update(self.root, self.client, write=True)
        self.assertEqual(self.recipe.read_text(), "concurrent user edit\n")
        self.assertEqual(self.info.read_bytes(), self.original[1])

    def test_malformed_metadata_is_rejected_without_writes(self):
        cases = {
            "missing binary": lambda data: data[updater.BINARY_TAG].update(assets=[]),
            "ambiguous binary": lambda data: data[updater.BINARY_TAG]["assets"].append(
                copy.deepcopy(data[updater.BINARY_TAG]["assets"][0])),
            "missing model": lambda data: data[updater.MODEL_TAG].update(assets=[]),
            "missing digest": lambda data: data[updater.BINARY_TAG]["assets"][0].pop("digest"),
            "invalid hash": lambda data: data[updater.BINARY_TAG]["assets"][0].update(digest="sha256:bad"),
            "foreign URL": lambda data: data[updater.BINARY_TAG]["assets"][0].update(
                browser_download_url="https://evil.example/malware.gz"),
            "URL suffix": lambda data: data[updater.BINARY_TAG]["assets"][0].update(
                browser_download_url=data[updater.BINARY_TAG]["assets"][0]["browser_download_url"] + "?extra"),
            "oversize": lambda data: data[updater.MODEL_TAG]["assets"][0].update(size=updater.MAX_ASSET_SIZE + 1),
            "wrong timestamp": lambda data: data[updater.BINARY_TAG]["assets"][0].update(updated_at="yesterday"),
            "wrong tag": lambda data: data[updater.BINARY_TAG].update(tag_name="other"),
            "draft": lambda data: data[updater.BINARY_TAG].update(draft=True),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                self.client.releases = releases()
                mutate(self.client.releases)
                with self.assertRaises(updater.UpdateError):
                    updater.update(self.root, self.client, write=True)
                self.assertEqual(self.client.downloads, [])
                self.assert_unchanged()


class DownloadTests(unittest.TestCase):
    def test_download_checks_exact_size_and_compressed_digest(self):
        asset = updater.Asset.parse(record(updater.BINARY_PREFIX + "7654321.gz", BINARY,
                                           updater.BINARY_TAG, 1), updater.BINARY_TAG)
        for name, body in (("valid", BINARY), ("truncated", BINARY[:-1]),
                           ("oversized", BINARY + b"x"), ("corrupted", b"x" + BINARY[1:])):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                response = io.BytesIO(body)
                response.geturl = lambda: "https://release-assets.githubusercontent.com/example"
                path = Path(directory) / "binary.gz"
                with patch.object(updater.urllib.request, "urlopen", return_value=response) as request:
                    if name == "valid":
                        updater.Client().download(asset, path)
                        self.assertEqual(path.read_bytes(), BINARY)
                    else:
                        with self.assertRaises(updater.UpdateError):
                            updater.Client().download(asset, path)
                    self.assertNotIn("Authorization", request.call_args.args[0].headers)


if __name__ == "__main__":
    unittest.main()
