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
        patch.dict(os.environ, GITHUB_REPOSITORY=publish.REPOSITORY, GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2", GITHUB_GIT_SSH_COMMAND="ssh -i /github-key -oBatchMode=yes", GIT_SSH_COMMAND="ssh -oBatchMode=yes").start()
        self.api_calls = []
        self.github_git_calls = []
        self.aur_push_env = None
        self.github_git_error = None
        self.real_git = publish.git
        patch.object(publish, "git", side_effect=self.routed_git).start()
        self.make_candidate()

    def routed_git(self, directory, *args, env=None):
        if directory == self.aur and "push" in args:
            self.aur_push_env = env
        if f"git@github.com:{publish.REPOSITORY}.git" in args:
            self.github_git_calls.append((args, env))
            if self.github_git_error and "push" in args:
                raise publish.PublishError(self.github_git_error)
            return ""
        return self.real_git(directory, *args, env=env)

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
        self.api_calls.append((args, payload))
        if "/git/commits/" in args[0]:
            return {"verification": {"verified": True}}
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
        self.assertEqual(inputs["branch"], {"repositoryNameWithOwner": publish.REPOSITORY, "branchName": "aur-automation/123-2"})
        self.assertIn("aur-state.json", {f["path"] for f in inputs["fileChanges"]["additions"]})

    def test_signed_commit_uses_scoped_key_and_cleans_temporary_branch(self):
        with patch.object(publish, "gh_api", side_effect=self.api):
            publish.publish(self.candidate, self.aur)
        creation = next(payload for args, payload in self.api_calls if args[:2] == ("--method", "POST"))
        self.assertEqual(creation, {"ref": "refs/heads/aur-automation/123-2", "sha": self.base})
        self.assertEqual(self.api_calls[-1][0], ("--method", "DELETE", f"repos/{publish.REPOSITORY}/git/refs/heads/aur-automation/123-2"))
        self.assertEqual(len(self.github_git_calls), 2)
        for args, env in self.github_git_calls:
            self.assertEqual(env["GIT_SSH_COMMAND"], os.environ["GITHUB_GIT_SSH_COMMAND"])
            self.assertEqual(env["GIT_LFS_SKIP_PUSH"], "1")
        self.assertEqual(self.aur_push_env["GIT_SSH_COMMAND"], "ssh -oBatchMode=yes")
        self.assertEqual(self.github_git_calls[-1][0][-1], "f" * 40 + ":refs/heads/main")
        self.assertEqual(self.run_git(self.root, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.run_git(self.root, "status", "--porcelain"), "")

    def test_unverified_commit_cannot_reach_main_and_branch_is_cleaned(self):
        def unsigned(args, payload=None):
            response = self.api(args, payload)
            if "/git/commits/" in args[0]:
                return {"verification": {"verified": False}}
            return response
        with patch.object(publish, "gh_api", side_effect=unsigned):
            with self.assertRaisesRegex(publish.PublishError, "not verified"):
                publish.publish(self.candidate, self.aur)
        self.assertEqual(self.github_git_calls, [])
        self.assertEqual(self.api_calls[-1][0][:2], ("--method", "DELETE"))

    def test_github_push_failure_still_cleans_temporary_branch(self):
        self.github_git_error = "GitHub fast-forward rejected"
        with patch.object(publish, "gh_api", side_effect=self.api):
            with self.assertRaisesRegex(publish.PublishError, "fast-forward rejected"):
                publish.publish(self.candidate, self.aur)
        self.assertEqual(len(self.github_git_calls), 2)
        self.assertEqual(self.api_calls[-1][0][:2], ("--method", "DELETE"))

    def test_missing_github_credentials_or_attempt_prevents_aur_mutation(self):
        before = self.run_git(self.aur, "rev-parse", "HEAD")
        for field in ("GITHUB_RUN_ATTEMPT", "GITHUB_GIT_SSH_COMMAND"):
            with self.subTest(field=field), patch.dict(os.environ, {field: ""}):
                with self.assertRaises(publish.PublishError):
                    publish.publish(self.candidate, self.aur)
        self.assertEqual(self.run_git(self.aur, "rev-parse", "HEAD"), before)

    def test_github_delete_accepts_empty_204_response(self):
        result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(publish.subprocess, "run", return_value=result):
            self.assertEqual(publish.gh_api(("--method", "DELETE", "repos/example/git/refs/heads/temp")), {})

    def test_recovers_after_aur_push_and_github_failure(self):
        def fail(args, payload=None):
            if args[0] == "graphql":
                raise publish.PublishError("temporary API failure")
            return self.api(args, payload)
        with patch.object(publish, "gh_api", side_effect=fail), self.assertRaisesRegex(publish.PublishError, "temporary"):
            publish.publish(self.candidate, self.aur)
        self.assertEqual(self.api_calls[-1][0][:2], ("--method", "DELETE"))
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
