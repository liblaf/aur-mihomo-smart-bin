#!/usr/bin/env python3
"""Publish inert, validated packaging metadata to AUR and prepare GitHub files."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "liblaf/aur-mihomo-smart-bin"
FILES = ("PKGBUILD", ".SRCINFO", "mihomo-smart-bin.install", "LICENSE")
SCALARS = {"pkgver": r"[0-9]{8}\.[0-9]{6}\.g[0-9a-f]{7,40}",
           "pkgrel": r"[1-9][0-9]*", "_commit": r"[0-9a-f]{7,40}",
           "_binary_sha256": r"[0-9a-f]{64}", "_model_sha256": r"[0-9a-f]{64}"}
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class PublishError(RuntimeError):
    """An invariant required for publication failed."""


def git(directory: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(("git", "-c", "core.hooksPath=/dev/null", *args), cwd=directory, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise PublishError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def gh_api(args: tuple[str, ...]) -> dict:
    result = subprocess.run(("gh", "api", *args), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise PublishError("GitHub API failed: " + result.stderr.strip())
    if not result.stdout.strip():
        return {}
    try:
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or response.get("errors"):
            raise ValueError("API returned errors")
        return response
    except (ValueError, TypeError) as error:
        raise PublishError("invalid GitHub API response") from error


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text())
        if not isinstance(result, dict):
            raise ValueError("expected object")
        return result
    except (OSError, ValueError) as error:
        raise PublishError(f"invalid {path.name}: {error}") from error


def scalars(text: str) -> dict[str, str]:
    values = {}
    for key, pattern in SCALARS.items():
        matches = re.findall(rf"^{key}=(.*)$", text, re.MULTILINE)
        if len(matches) != 1:
            raise PublishError(f"expected one literal {key}")
        raw = matches[0]
        if len(raw) > 1 and raw[0] == raw[-1] and raw[0] in "'\"":
            raw = raw[1:-1]
        if not re.fullmatch(pattern, raw):
            raise PublishError(f"invalid literal {key}")
        values[key] = raw
    stamp, suffix = values["pkgver"].split(".g")
    try:
        datetime.strptime(stamp, "%Y%m%d.%H%M%S")
    except ValueError as error:
        raise PublishError("invalid release timestamp") from error
    if suffix != values["_commit"]:
        raise PublishError("version hash disagrees with binary commit")
    return values


def recipe_shape(text: str) -> str:
    for key in SCALARS:
        text = re.sub(rf"^{key}=.*$", key + "=<release>", text, flags=re.MULTILINE)
    return text


def srcinfo(before: str, old: dict, new: dict) -> str:
    """Regenerate release fields in trusted makepkg output without executing it."""
    lines = []
    for line in before.splitlines(keepends=True):
        if line.startswith("\tpkgver = "):
            line = f"\tpkgver = {new['pkgver']}\n"
        elif line.startswith("\tpkgrel = "):
            line = f"\tpkgrel = {new['pkgrel']}\n"
        elif line.startswith("\tsource = "):
            line = line.replace(old["_commit"], new["_commit"])
        elif line.startswith("\tsha256sums = "):
            for key in ("_binary_sha256", "_model_sha256"):
                if line.strip() == "sha256sums = " + old[key]:
                    line = f"\tsha256sums = {new[key]}\n"
                    break
        lines.append(line)
    return "".join(lines)


def validate(candidate: Path) -> dict:
    if not candidate.is_dir() or {p.name for p in candidate.iterdir()} != {*FILES, "manifest.json"}:
        raise PublishError("candidate must contain exactly four packaging files and manifest.json")
    if any(not p.is_file() or p.is_symlink() for p in candidate.iterdir()):
        raise PublishError("candidate entries must be regular files")
    manifest = read_json(candidate / "manifest.json")
    if set(manifest) != {"base_commit", "version", "pkgrel", "sha256"}:
        raise PublishError("unexpected manifest fields")
    if not isinstance(manifest["base_commit"], str) or not COMMIT.fullmatch(manifest["base_commit"]):
        raise PublishError("invalid base_commit")
    hashes = manifest["sha256"]
    if not isinstance(hashes, dict) or set(hashes) != set(FILES):
        raise PublishError("manifest must hash all packaging files")
    for name in FILES:
        if not isinstance(hashes[name], str) or not DIGEST.fullmatch(hashes[name]) or digest((candidate / name).read_bytes()) != hashes[name]:
            raise PublishError("candidate digest mismatch")
    if manifest["base_commit"] != git(ROOT, "rev-parse", "HEAD"):
        raise PublishError("candidate base is not checkout HEAD")
    before, after = (ROOT / "PKGBUILD").read_bytes().decode(), (candidate / "PKGBUILD").read_bytes().decode()
    old, new = scalars(before), scalars(after)
    if recipe_shape(before) != recipe_shape(after):
        raise PublishError("candidate changes executable recipe content")
    if new["pkgver"] != manifest["version"] or new["pkgrel"] != manifest["pkgrel"]:
        raise PublishError("manifest release disagrees with recipe")
    for name in ("LICENSE", "mihomo-smart-bin.install"):
        if (ROOT / name).read_bytes() != (candidate / name).read_bytes():
            raise PublishError("candidate changes auxiliary file")
    if srcinfo((ROOT / ".SRCINFO").read_bytes().decode(), old, new) != (candidate / ".SRCINFO").read_bytes().decode():
        raise PublishError("candidate .SRCINFO differs from regenerated metadata")
    if new["_commit"] == old["_commit"]:
        if new["pkgver"] != old["pkgver"] or new["_binary_sha256"] != old["_binary_sha256"]:
            raise PublishError("same-commit binary identity changed")
        changed_model = new["_model_sha256"] != old["_model_sha256"]
        expected = int(old["pkgrel"]) + int(changed_model)
        if int(new["pkgrel"]) != expected:
            raise PublishError("model update must increment pkgrel exactly once")
    elif new["pkgver"][:15] <= old["pkgver"][:15] or new["pkgrel"] != "1":
        raise PublishError("binary update must advance timestamp and reset pkgrel")
    return manifest


def tree_hashes(directory: Path, revision: str) -> dict[str, str]:
    names = git(directory, "ls-tree", "-r", "--name-only", revision).splitlines()
    result = {}
    for name in names:
        mode = git(directory, "ls-tree", revision, "--", name).split()[0]
        if mode != "100644":
            raise PublishError("AUR tree contains non-regular packaging entry")
        data = subprocess.run(("git", "show", f"{revision}:{name}"), cwd=directory,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
        result[name] = digest(data)
    return result


def check_aur(aur: Path, wanted: dict) -> tuple[str, bool]:
    state = read_json(ROOT / "aur-state.json")
    if set(state) != {"commit", "sha256"} or not isinstance(state["commit"], str) or not COMMIT.fullmatch(state["commit"]):
        raise PublishError("invalid tracked AUR state")
    tracked = state["sha256"]
    if not isinstance(tracked, dict) or set(tracked) not in (set(FILES), set(FILES) - {"LICENSE"}):
        raise PublishError("invalid tracked AUR file set")
    if any(not isinstance(v, str) or not DIGEST.fullmatch(v) for v in tracked.values()):
        raise PublishError("invalid tracked AUR digest")
    if git(aur, "status", "--porcelain", "--untracked-files=all"):
        raise PublishError("AUR checkout is dirty")
    head = git(aur, "rev-parse", "HEAD")
    remote = git(aur, "ls-remote", "origin", "HEAD").split()
    if not remote or remote[0] != head:
        raise PublishError("AUR remote moved")
    if tree_hashes(aur, state["commit"]) != tracked:
        raise PublishError("tracked AUR baseline differs from state")
    actual = tree_hashes(aur, head)
    entries = [p for p in aur.iterdir() if p.name != ".git"]
    if any(p.is_symlink() or not p.is_file() for p in entries) or {p.name: digest(p.read_bytes()) for p in entries} != actual:
        raise PublishError("AUR working files differ from committed tree")
    if head == state["commit"] and actual == tracked:
        return head, actual == wanted
    parents = git(aur, "rev-list", "--parents", "-n", "1", head).split()[1:]
    if parents == [state["commit"]] and actual == wanted:
        return head, True
    raise PublishError("AUR has untracked co-maintainer changes")


def publish(candidate: Path, aur: Path, branch: str = "main") -> None:
    manifest = validate(candidate)
    if branch != "main" or git(ROOT, "branch", "--show-current") != "main":
        raise PublishError("publication requires main")
    if git(ROOT, "status", "--porcelain", "--untracked-files=all"):
        raise PublishError("GitHub checkout is dirty")
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise PublishError("publication requires trusted GitHub repository")
    run = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if not run.isdecimal() or not attempt.isdecimal():
        raise PublishError("missing GitHub Actions run id or attempt")
    base = manifest["base_commit"]
    response = gh_api((f"repos/{REPOSITORY}/git/ref/heads/main",))
    if response.get("object", {}).get("sha") != base:
        raise PublishError("GitHub main moved")
    aur_head, already = check_aur(aur, manifest["sha256"])
    if not already:
        env = os.environ.copy()
        if not env.get("GIT_SSH_COMMAND"):
            raise PublishError("trusted AUR SSH command is required")
        env["GIT_LFS_SKIP_PUSH"] = "1"
        for name in FILES:
            shutil.copyfile(candidate / name, aur / name)
        git(aur, "add", "--", *FILES)
        git(aur, "-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "-c", "commit.gpgsign=false", "commit", "-m", f"Update to {manifest['version']}-{manifest['pkgrel']}")
        aur_head = git(aur, "rev-parse", "HEAD")
        git(aur, "-c", "push.gpgSign=false", "push", "origin", f"{aur_head}:refs/heads/master", env=env)
    state = {"commit": aur_head, "sha256": manifest["sha256"]}
    report = {"version": manifest["version"], "pkgrel": manifest["pkgrel"], "base_commit": base,
              "validated_at": datetime.now(timezone.utc).isoformat(),
              "run_url": f"https://github.com/{REPOSITORY}/actions/runs/{run}"}
    for name in FILES:
        shutil.copyfile(candidate / name, ROOT / name)
    for name, value in (("aur-state.json", state), ("automation-validation.json", report)):
        (ROOT / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--aur-checkout", required=True, type=Path)
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()
    try:
        publish(args.candidate, args.aur_checkout, args.branch)
    except (PublishError, OSError, UnicodeError, subprocess.SubprocessError) as error:
        print(f"publish: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
