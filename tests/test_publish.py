"""Offline publication tests: inert candidates, co-maintainers, and recovery."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("publish", Path(__file__).resolve().parents[1] / "scripts/publish.py")
publish = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publish)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root, self.aur, self.candidate = [self.directory / n for n in ("root", "aur", "candidate")]
        self.remote = self.directory / "remote.git"
        for path in (self.root, self.candidate):
            path.mkdir()
        self.run_git(self.directory, "init", "--bare", "--initial-branch=master", str(self.remote))
        self.run_git(self.directory, "clone", str(self.remote), str(self.aur))
        self.run_git(self.root, "init", "--initial-branch=main")
        for path in (self.root, self.aur):
            self.run_git(path, "config", "user.name", "Test")
            self.run_git(path, "config", "user.email", "test@example.com")
            self.run_git(path, "config", "commit.gpgsign", "false")
        recipe = ("pkgname=mihomo-smart-bin\npkgver=20261021.194528.g1234567\npkgrel=1\n"
                  "_commit=1234567\n_binary_sha256='" + "a" * 64 + "'\n_model_sha256='" + "b" * 64 + "'\n"
                  "package() { install -Dm755 mihomo \"${pkgdir}/usr/bin/mihomo\"; }\n")
        info = ("pkgbase = mihomo-smart-bin\n\tpkgver = 20261021.194528.g1234567\n\tpkgrel = 1\n"
                "\tsource = mihomo-1234567.gz::https://example.test/1234567.gz\n"
                "\tsha256sums = " + "a" * 64 + "\n\tsha256sums = " + "b" * 64 + "\npkgname = mihomo-smart-bin\n")
        for name, content in {"PKGBUILD": recipe, ".SRCINFO": info, "mihomo-smart-bin.install": "# install\n", "LICENSE": "license\n"}.items():
            (self.root / name).write_text(content)
            if name != "LICENSE":
                (self.aur / name).write_text(content)
        self.commit(self.aur)
        self.run_git(self.aur, "push", "origin", "master")
        state = {"commit": self.run_git(self.aur, "rev-parse", "HEAD"),
                 "sha256": {n: self.hash(self.aur / n) for n in publish.FILES if n != "LICENSE"}}
        (self.root / "aur-state.json").write_text(json.dumps(state))
        self.commit(self.root)
        self.base = self.run_git(self.root, "rev-parse", "HEAD")
        self.addCleanup(patch.stopall)
        patch.object(publish, "ROOT", self.root).start()
        patch.dict(os.environ, GITHUB_REPOSITORY=publish.REPOSITORY, GITHUB_RUN_ID="123", GIT_SSH_COMMAND="ssh -oBatchMode=yes").start()
        self.make_candidate()

    @staticmethod
    def run_git(path, *args):
        return subprocess.run(("git", "-c", "core.hooksPath=/dev/null", *args), cwd=path, check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

    def commit(self, path):
        self.run_git(path, "add", ".")
        self.run_git(path, "commit", "-m", "fixture")

    @staticmethod
    def hash(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def make_candidate(self, **changes):
        old = publish.scalars((self.root / "PKGBUILD").read_text())
        new = old | {"pkgver": "20261022.194528.g7654321", "_commit": "7654321", "_binary_sha256": "c" * 64} | changes
        recipe = (self.root / "PKGBUILD").read_text()
        for key, value in new.items():
            recipe = publish.re.sub(rf"^{key}=.*$", f"{key}='{value}'", recipe, flags=publish.re.MULTILINE)
        (self.candidate / "PKGBUILD").write_text(recipe)
        (self.candidate / ".SRCINFO").write_text(publish.srcinfo((self.root / ".SRCINFO").read_text(), old, new))
        for name in ("LICENSE", "mihomo-smart-bin.install"):
            (self.candidate / name).write_bytes((self.root / name).read_bytes())
        self.rehash(new)

    def rehash(self, values=None):
        values = values or publish.scalars((self.candidate / "PKGBUILD").read_text())
        (self.candidate / "manifest.json").write_text(json.dumps({
            "base_commit": self.base, "version": values["pkgver"], "pkgrel": values["pkgrel"],
            "sha256": {n: self.hash(self.candidate / n) for n in publish.FILES}}))

    def api(self, args, payload=None):
        if args[0] == "graphql":
            self.payload = payload
            return {"data": {"createCommitOnBranch": {"commit": {"oid": "f" * 40}}}}
        return {"object": {"sha": self.base}}

    def test_publishes_and_pins_github_expected_head(self):
        with patch.object(publish, "gh_api", side_effect=self.api):
            publish.publish(self.candidate, self.aur)
        self.assertEqual(self.run_git(self.remote, "rev-parse", "HEAD"), self.run_git(self.aur, "rev-parse", "HEAD"))
        self.assertEqual(publish.tree_hashes(self.aur, "HEAD"), json.loads((self.candidate / "manifest.json").read_text())["sha256"])
        inputs = self.payload["variables"]["input"]
        self.assertEqual(inputs["expectedHeadOid"], self.base)
        self.assertEqual(inputs["branch"], {"repositoryNameWithOwner": publish.REPOSITORY, "branchName": "main"})
        self.assertIn("aur-state.json", {f["path"] for f in inputs["fileChanges"]["additions"]})

    def test_recovers_after_aur_push_and_github_failure(self):
        def fail(args, payload=None):
            if args[0] == "graphql":
                raise publish.PublishError("temporary API failure")
            return self.api(args, payload)
        with patch.object(publish, "gh_api", side_effect=fail), self.assertRaisesRegex(publish.PublishError, "temporary"):
            publish.publish(self.candidate, self.aur)
        pushed = self.run_git(self.aur, "rev-parse", "HEAD")
        with patch.object(publish, "gh_api", side_effect=self.api):
            publish.publish(self.candidate, self.aur)
        self.assertEqual(self.run_git(self.aur, "rev-parse", "HEAD"), pushed)

    def test_rejects_recipe_execution_even_with_valid_manifest(self):
        recipe = self.candidate / "PKGBUILD"
        recipe.write_text(recipe.read_text() + "touch /tmp/should-never-run\n")
        self.rehash()
        with self.assertRaisesRegex(publish.PublishError, "executable recipe"):
            publish.validate(self.candidate)

    def test_rejects_shell_substitution_in_scalar(self):
        path = self.candidate / "PKGBUILD"
        path.write_text(path.read_text().replace("pkgrel='1'", "pkgrel=$(touch /tmp/should-never-run)"))
        manifest = json.loads((self.candidate / "manifest.json").read_text())
        manifest["sha256"]["PKGBUILD"] = self.hash(path)
        (self.candidate / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(publish.PublishError, "literal pkgrel"):
            publish.validate(self.candidate)

    def test_rejects_srcinfo_arbitrary_change(self):
        path = self.candidate / ".SRCINFO"
        path.write_text(path.read_text() + "\tdepends = evil\n")
        self.rehash()
        with self.assertRaisesRegex(publish.PublishError, "regenerated metadata"):
            publish.validate(self.candidate)

    def test_rejects_auxiliary_change_and_digest_mismatch(self):
        (self.candidate / "LICENSE").write_text("other license")
        with self.assertRaisesRegex(publish.PublishError, "digest mismatch"):
            publish.validate(self.candidate)
        self.rehash()
        with self.assertRaisesRegex(publish.PublishError, "auxiliary"):
            publish.validate(self.candidate)

    def test_rejects_extra_files_and_symlinks(self):
        (self.candidate / "extra").touch()
        with self.assertRaisesRegex(publish.PublishError, "exactly"):
            publish.validate(self.candidate)
        (self.candidate / "extra").unlink()
        (self.candidate / "LICENSE").unlink()
        (self.candidate / "LICENSE").symlink_to(self.root / "LICENSE")
        with self.assertRaisesRegex(publish.PublishError, "regular files"):
            publish.validate(self.candidate)

    def test_rejects_rollbacks_and_same_commit_binary_drift(self):
        self.make_candidate(pkgver="20261020.194528.g7654321")
        with self.assertRaisesRegex(publish.PublishError, "advance timestamp"):
            publish.validate(self.candidate)
        self.make_candidate(pkgver="20261021.194528.g1234567", _commit="1234567")
        with self.assertRaisesRegex(publish.PublishError, "binary identity"):
            publish.validate(self.candidate)

    def test_model_update_requires_exact_increment(self):
        changes = {"pkgver": "20261021.194528.g1234567", "_commit": "1234567", "_binary_sha256": "a" * 64, "_model_sha256": "d" * 64}
        self.make_candidate(**changes)
        with self.assertRaisesRegex(publish.PublishError, "increment"):
            publish.validate(self.candidate)
        self.make_candidate(**changes, pkgrel="2")
        self.assertEqual(publish.validate(self.candidate)["pkgrel"], "2")

    def test_rejects_comaintainer_commit_even_when_candidate_matches(self):
        (self.aur / "PKGBUILD").write_text("co-maintainer changes\n")
        self.commit(self.aur)
        self.run_git(self.aur, "push", "origin", "master")
        with patch.object(publish, "gh_api", side_effect=self.api), self.assertRaisesRegex(publish.PublishError, "co-maintainer"):
            publish.publish(self.candidate, self.aur)

    def test_rejects_hidden_worktree_edits_and_ignored_extra_files(self):
        self.run_git(self.aur, "update-index", "--assume-unchanged", "PKGBUILD")
        (self.aur / "PKGBUILD").write_text("hidden edit\n")
        with self.assertRaisesRegex(publish.PublishError, "working files"):
            publish.check_aur(self.aur, {})
        self.run_git(self.aur, "checkout-index", "--force", "PKGBUILD")
        (self.aur / ".git/info").mkdir(exist_ok=True)
        (self.aur / ".git/info/exclude").write_text("ignored\n")
        (self.aur / "ignored").touch()
        with self.assertRaisesRegex(publish.PublishError, "working files"):
            publish.check_aur(self.aur, {})

    def test_missing_ssh_command_prevents_local_aur_commit(self):
        previous = self.run_git(self.aur, "rev-parse", "HEAD")
        with patch.dict(os.environ, GIT_SSH_COMMAND=""), patch.object(publish, "gh_api", side_effect=self.api):
            with self.assertRaisesRegex(publish.PublishError, "SSH command"):
                publish.publish(self.candidate, self.aur)
        self.assertEqual(self.run_git(self.aur, "rev-parse", "HEAD"), previous)
        self.assertEqual(self.run_git(self.aur, "status", "--porcelain"), "")

    def test_rejects_remote_aur_movement(self):
        other = self.directory / "other"
        self.run_git(self.directory, "clone", str(self.remote), str(other))
        self.run_git(other, "config", "user.name", "Test")
        self.run_git(other, "config", "user.email", "test@example.com")
        self.run_git(other, "config", "commit.gpgsign", "false")
        (other / "PKGBUILD").write_text("new remote edit\n")
        self.commit(other)
        self.run_git(other, "push", "origin", "master")
        with self.assertRaisesRegex(publish.PublishError, "AUR remote moved"):
            publish.check_aur(self.aur, {})

    def test_remote_github_movement_prevents_aur_mutation(self):
        previous = self.run_git(self.aur, "rev-parse", "HEAD")
        with patch.object(publish, "gh_api", return_value={"object": {"sha": "0" * 40}}), self.assertRaisesRegex(publish.PublishError, "main moved"):
            publish.publish(self.candidate, self.aur)
        self.assertEqual(self.run_git(self.aur, "rev-parse", "HEAD"), previous)

    def test_rejects_stale_base_and_untracked_aur_files(self):
        manifest = json.loads((self.candidate / "manifest.json").read_text())
        manifest["base_commit"] = "0" * 40
        (self.candidate / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(publish.PublishError, "checkout HEAD"):
            publish.validate(self.candidate)
        self.rehash()
        (self.aur / "extra").touch()
        with self.assertRaisesRegex(publish.PublishError, "dirty"):
            publish.check_aur(self.aur, manifest["sha256"])


if __name__ == "__main__":
    unittest.main()
