"""Offline behavior tests for upstream updates and failure recovery."""

import copy
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("release_updater", Path(__file__).parents[1] / "scripts/update.py")
updater = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = updater
SPEC.loader.exec_module(updater)

def package_fixture(overrides=None, extra=()):
    files = {
        ".PKGINFO": b"pkgname = mihomo\narch = x86_64\npkgver = 1.19.34.alpha.smart-7654321-smart-1\n",
        "usr/bin/mihomo": b"\x7fELF" + b"offline fixture" * 10,
        "etc/mihomo/config.yaml": b"mode: rule\n",
        "usr/lib/systemd/system/mihomo.service": b"[Service]\nExecStart=/usr/bin/mihomo\n",
    }
    files.update(overrides or {})
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.tar"
        with tarfile.open(source, "w") as archive:
            for name, body in files.items():
                if body is None:
                    continue
                member = tarfile.TarInfo(name)
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
            for member in extra:
                archive.addfile(member)
        target = Path(directory) / "package.pkg.tar.zst"
        subprocess.run(["bsdtar", "-P", "--zstd", "-cf", str(target), "@" + str(source)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return target.read_bytes()


BINARY = package_fixture()
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
            record(updater.BINARY_PREFIX + "7654321.pkg.tar.zst", BINARY, updater.BINARY_TAG, 1)]},
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
            "pkgname=mihomo-smart-bin\npkgver=1.19.34.alpha.smart.r100.g4624551\npkgrel=3\n"
            f"_commit=4624551\n_binary_sha256='{OLD_BINARY_HASH}'\n_model_sha256='{OLD_MODEL_HASH}'\n"
            "# Preserve service checksum and package implementation verbatim.\n"
            "sha256sums=('fixed-service-checksum')\npackage() { :; }\n")
        self.info.write_text("original srcinfo\n")
        self.original = self.recipe.read_bytes(), self.info.read_bytes()
        self.client = FakeClient()
        history = patch.object(updater, "history_revision", return_value=101)
        self.history = history.start()
        self.addCleanup(history.stop)

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

    def test_check_reports_pending_version_without_download_or_execution(self):
        # Upstream ships the same architecture and commit in several package formats.
        for suffix in ("deb", "rpm", "gz"):
            other = copy.deepcopy(self.client.releases[updater.BINARY_TAG]["assets"][0])
            other["name"] = updater.BINARY_PREFIX + "7654321." + suffix
            self.client.releases[updater.BINARY_TAG]["assets"].append(other)
        with patch.object(updater.subprocess, "run", side_effect=AssertionError("unexpected execution")):
            result = updater.update(self.root, self.client)
        self.assertEqual(result["changed"], "true")
        self.assertIsNone(result["pkgver"])
        self.assertEqual(result["pkgrel"], "1")
        self.assertEqual(self.client.downloads, [])
        self.history.assert_not_called()
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
        self.assertIn("pkgver = 1.19.34.alpha.smart.r101.g7654321", self.info.read_text())

    def test_model_only_change_bumps_release_preserving_binary_version(self):
        binary = self.client.releases[updater.BINARY_TAG]["assets"][0]
        binary["name"] = updater.BINARY_PREFIX + "4624551.pkg.tar.zst"
        binary["browser_download_url"] = (
            f"https://github.com/vernesong/mihomo/releases/download/{updater.BINARY_TAG}/{binary['name']}")
        binary["digest"] = "sha256:" + OLD_BINARY_HASH
        binary["updated_at"] = "2026-10-21T19:45:13Z"
        result = updater.update(self.root, self.client)
        self.assertEqual(result["pkgver"], "1.19.34.alpha.smart.r100.g4624551")
        self.assertEqual(result["pkgrel"], "4")
        self.assertEqual(result["binary_sha256"], OLD_BINARY_HASH)
        self.assertEqual(result["model_sha256"], hashlib.sha256(MODEL).hexdigest())
        self.assert_unchanged()

    def test_same_commit_and_digest_preserve_version_with_earlier_timestamp(self):
        assets = updater.select_assets(self.client)
        binary = updater.Asset(assets.binary.id, updater.BINARY_PREFIX + "4624551.pkg.tar.zst",
                               assets.binary.url, assets.binary.size, OLD_BINARY_HASH,
                               "2026-10-21T19:45:13Z")
        model = updater.Asset(assets.model.id, assets.model.name, assets.model.url,
                              assets.model.size, OLD_MODEL_HASH, assets.model.updated_at)
        current = updater.read_fields(self.recipe.read_text())
        self.assertEqual(updater.plan(current, updater.ReleaseAssets(binary, model)), current)
        self.history.assert_not_called()

    def test_mutated_same_commit_requires_review(self):
        asset = self.client.releases[updater.BINARY_TAG]["assets"][0]
        asset["name"] = updater.BINARY_PREFIX + "4624551.pkg.tar.zst"
        asset["browser_download_url"] = (
            f"https://github.com/vernesong/mihomo/releases/download/{updater.BINARY_TAG}/{asset['name']}")
        with self.assertRaisesRegex(updater.UpdateError, "same commit"):
            updater.update(self.root, self.client, write=True)
        self.assertEqual(self.client.downloads, [])
        self.assert_unchanged()

    def test_base_and_revision_rollback_are_rejected(self):
        current = updater.read_fields(self.recipe.read_text())
        assets = updater.select_assets(self.client)
        for base, count in (("1.19.34", 100), ("1.19.34", 99), ("1.19.33", 101)):
            with self.subTest(base=base, count=count), self.assertRaises(updater.UpdateError):
                updater.plan(current, assets, base, count)
        result = updater.plan(current, assets, "1.20.0", 101)
        self.assertEqual(result["pkgver"], "1.20.0.alpha.smart.r101.g7654321")

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

    def test_truncated_archive_never_rewrites_recipe(self):
        def truncated(asset, path):
            path.write_bytes(BINARY[:len(BINARY) // 2])
        self.client.download = truncated
        with self.assertRaisesRegex(updater.UpdateError, "archive"):
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


class ArchiveTests(unittest.TestCase):
    def validate(self, body):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "package.pkg.tar.zst"
            path.write_bytes(body)
            return updater.validate_archive(path, "7654321")

    def test_real_zstd_package_returns_upstream_base(self):
        self.assertEqual(self.validate(BINARY), "1.19.34")

    def test_required_payload_and_identity_are_checked(self):
        cases = (
            {"usr/bin/mihomo": b"not ELF"},
            {"etc/mihomo/config.yaml": None},
            {"usr/lib/systemd/system/mihomo.service": None},
            {".PKGINFO": None},
            {".PKGINFO": b"pkgname = other\narch = x86_64\npkgver = 7654321\n"},
            {".PKGINFO": b"pkgname = mihomo\narch = aarch64\npkgver = 7654321\n"},
            {".PKGINFO": b"pkgname = mihomo\narch = x86_64\npkgver = 0000000\n"},
            {".PKGINFO": b"pkgname = mihomo\narch = x86_64\npkgver = 1.19.34.alpha.smart-7654321-smart-2\n"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(updater.UpdateError):
                self.validate(package_fixture(overrides))

    def test_unsafe_paths_and_types_are_rejected(self):
        for name in ("../escape", "/absolute", "usr/../escape", "bad\\path"):
            with self.subTest(name=name), self.assertRaisesRegex(updater.UpdateError, "path"):
                self.validate(package_fixture({name: b"unsafe"}))
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            member = tarfile.TarInfo("unsafe-link")
            member.type, member.linkname = kind, "usr/bin/mihomo"
            with self.subTest(kind=kind), self.assertRaisesRegex(updater.UpdateError, "type"):
                self.validate(package_fixture(extra=[member]))

    def test_duplicate_members_and_expansion_limits_are_rejected(self):
        member = tarfile.TarInfo("usr/bin/mihomo")
        with self.assertRaisesRegex(updater.UpdateError, "Duplicate"):
            self.validate(package_fixture(extra=[member]))
        with patch.object(updater, "MAX_METADATA_SIZE", 16):
            with self.assertRaisesRegex(updater.UpdateError, "too large"):
                self.validate(BINARY)
        with patch.object(updater, "MAX_ASSET_SIZE", 8):
            with self.assertRaisesRegex(updater.UpdateError, "too large"):
                self.validate(BINARY)

    def test_invalid_or_truncated_archive_is_rejected(self):
        for body in (b"not an archive", BINARY[:len(BINARY) // 2], BINARY[:-4]):
            with self.subTest(size=len(body)), self.assertRaises(updater.UpdateError):
                self.validate(body)


class GitHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name) / "history.git"
        self.env = os.environ.copy()
        self.env.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                        GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        self.git("init", "--bare", str(self.repo))
        self.tree = self.git("mktree", input=b"")
        self.first = self.commit("root")
        # Choose a descendant whose lexical hash descends, exercising count ordering.
        for index in range(1000):
            self.second = self.commit("child " + str(index), self.first)
            if self.second < self.first:
                break
        else:
            self.fail("Could not create a descending hash fixture")
        self.git("update-ref", "refs/tags/upstream", self.second)

    def git(self, *args, input=None):
        return subprocess.run(["git", "--git-dir=" + str(self.repo), *args], input=input,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=self.env, check=True).stdout.decode().strip()

    def commit(self, message, parent=None):
        args = ["commit-tree", self.tree, "-m", message]
        if parent:
            args.extend(["-p", parent])
        return self.git(*args)

    def test_complete_history_count_orders_descending_hashes(self):
        count = updater.inspect_history(self.repo, self.second[:7], self.first[:7])
        self.assertEqual(count, 2)
        self.assertLess(self.second, self.first)
        current = {"pkgver": "1.19.34.alpha.smart.r1.g" + self.first[:7], "pkgrel": "1",
                   "_commit": self.first[:7], "_binary_sha256": OLD_BINARY_HASH,
                   "_model_sha256": OLD_MODEL_HASH}
        assets = updater.select_assets(FakeClient())
        binary = updater.Asset(1, updater.BINARY_PREFIX + self.second[:7] + ".pkg.tar.zst",
                               assets.binary.url, assets.binary.size, assets.binary.sha256,
                               "2020-01-01T00:00:00Z")
        result = updater.plan(current, updater.ReleaseAssets(binary, assets.model), "1.19.34", count)
        self.assertEqual(result["pkgver"], "1.19.34.alpha.smart.r2.g" + self.second[:7])

    def test_branch_reset_and_wrong_asset_commit_are_rejected(self):
        other = self.commit("unrelated root")
        with self.assertRaises(updater.UpdateError):
            updater.inspect_history(self.repo, self.second[:7], other[:7])
        with self.assertRaisesRegex(updater.UpdateError, "does not match"):
            updater.inspect_history(self.repo, self.first[:7], self.first[:7])

    def test_shallow_graph_is_rejected(self):
        (self.repo / "shallow").write_text(self.second + "\n")
        with self.assertRaisesRegex(updater.UpdateError, "Shallow"):
            updater.inspect_history(self.repo, self.second[:7], self.first[:7])


class DownloadTests(unittest.TestCase):
    def test_download_checks_exact_size_and_compressed_digest(self):
        asset = updater.Asset.parse(record(updater.BINARY_PREFIX + "7654321.pkg.tar.zst", BINARY,
                                           updater.BINARY_TAG, 1), updater.BINARY_TAG)
        for name, body in (("valid", BINARY), ("truncated", BINARY[:-1]),
                           ("oversized", BINARY + b"x"), ("corrupted", b"x" + BINARY[1:])):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                response = io.BytesIO(body)
                response.geturl = lambda: "https://release-assets.githubusercontent.com/example"
                path = Path(directory) / "binary.pkg.tar.zst"
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
