#!/usr/bin/env python3
"""Update the AUR recipe from verified upstream release assets."""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass


REPOSITORY = "vernesong/mihomo"
BINARY_TAG = "Prerelease-Alpha"
MODEL_TAG = "LightGBM-Model"
BINARY_PREFIX = "mihomo-linux-amd64-v3-alpha-smart-"
BINARY_PATTERN = re.compile(re.escape(BINARY_PREFIX) + r"([0-9a-f]{7,40})\.pkg\.tar\.zst\Z")
VERSION_PATTERN = re.compile(r"((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))"
                             r"\.alpha\.smart\.r([1-9][0-9]*)\.g([0-9a-f]{7,40})\Z")
MAX_ASSET_SIZE = 1024 * 1024 * 1024
MAX_METADATA_SIZE = 4 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
FIELDS = ("pkgver", "pkgrel", "_commit", "_binary_sha256", "_model_sha256")


class UpdateError(Exception):
    """An upstream or recipe invariant requires manual review."""


def require(condition, message):
    if not condition:
        raise UpdateError(message)


def timestamp(value):
    require(isinstance(value, str), "Asset timestamp must be a string")
    require(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value),
            "Asset timestamp must use UTC seconds")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise UpdateError("Invalid asset timestamp") from exc


@dataclass(frozen=True)
class Asset:
    id: int
    name: str
    url: str
    size: int
    sha256: str
    updated_at: str

    @classmethod
    def parse(cls, data, tag):
        require(isinstance(data, dict), "Invalid asset record")
        asset_id, name, size = data.get("id"), data.get("name"), data.get("size")
        require(type(asset_id) is int and asset_id > 0, "Invalid asset ID")
        require(isinstance(name, str), "Invalid asset name")
        require(type(size) is int and 0 < size <= MAX_ASSET_SIZE, "Invalid asset size")
        digest = data.get("digest")
        require(isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest),
                "A GitHub SHA256 asset digest is required")
        url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
        require(data.get("browser_download_url") == url, "Unexpected asset download URL")
        timestamp(data.get("updated_at"))
        return cls(asset_id, name, url, size, digest[7:], data["updated_at"])


@dataclass(frozen=True)
class ReleaseAssets:
    binary: Asset
    model: Asset

    @property
    def commit(self):
        return BINARY_PATTERN.fullmatch(self.binary.name)[1]



class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # API requests carry credentials; never forward them to redirects.
        raise UpdateError("Unexpected GitHub API redirect")


class Client:
    def __init__(self):
        self.token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    def release(self, tag):
        require(tag in (BINARY_TAG, MODEL_TAG), "Unexpected release tag")
        endpoint = f"/repos/{REPOSITORY}/releases/tags/{tag}"
        if not self.token and shutil.which("gh") and subprocess.run(
            ["gh", "auth", "status"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=30,
        ).returncode == 0:
            result = subprocess.run(
                ["gh", "api", "--hostname", "github.com", endpoint,
                 "-H", "Accept: application/vnd.github+json"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=60,
            )
            require(result.returncode == 0, "GitHub API request failed")
            raw = result.stdout
        else:
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "aur-mihomo-smart-bin-updater",
                       "X-GitHub-Api-Version": "2022-11-28"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            request = urllib.request.Request("https://api.github.com" + endpoint, headers=headers)
            with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        require(len(raw) <= 4 * 1024 * 1024, "GitHub metadata is too large")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise UpdateError("Invalid GitHub metadata JSON") from exc

    def download(self, asset, path):
        # No API credentials are sent to the public release URL or its CDN.
        request = urllib.request.Request(asset.url, headers={"User-Agent": "aur-mihomo-smart-bin-updater"})
        digest, size = hashlib.sha256(), 0
        with urllib.request.urlopen(request, timeout=120) as response, path.open("wb") as target:
            require(response.geturl().startswith((
                "https://github.com/", "https://release-assets.githubusercontent.com/",
                "https://objects.githubusercontent.com/",
            )), "Unexpected release download redirect")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                require(size <= asset.size, f"Download exceeded declared size: {asset.name}")
                digest.update(chunk)
                target.write(chunk)
        require(size == asset.size, f"Incomplete download: {asset.name}")
        require(digest.hexdigest() == asset.sha256, f"SHA256 mismatch: {asset.name}")


def select_assets(client):
    selected = []
    for tag in (BINARY_TAG, MODEL_TAG):
        release = client.release(tag)
        require(isinstance(release, dict) and release.get("tag_name") == tag,
                "Unexpected release metadata")
        require(release.get("draft") is False, "Draft releases are not accepted")
        assets = release.get("assets")
        require(isinstance(assets, list) and all(isinstance(a, dict) for a in assets),
                "Invalid release asset list")
        if tag == BINARY_TAG:
            matches = [a for a in assets if isinstance(a.get("name"), str)
                       and a["name"].startswith(BINARY_PREFIX) and a["name"].endswith(".pkg.tar.zst")]
            require(len(matches) == 1, "Expected exactly one amd64 v3 smart binary")
            require(BINARY_PATTERN.fullmatch(matches[0]["name"]), "Invalid binary asset name")
        else:
            matches = [a for a in assets if a.get("name") == "Model-large.bin"]
            require(len(matches) == 1, "Expected exactly one Model-large.bin")
        selected.append(Asset.parse(matches[0], tag))
    return ReleaseAssets(*selected)


def read_fields(text):
    result = {}
    for field in FIELDS:
        matches = re.findall(r"^" + re.escape(field) + r"=(.*)$", text, re.MULTILINE)
        require(len(matches) == 1, f"Expected one scalar assignment for {field}")
        value = matches[0]
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[field] = value
    version = VERSION_PATTERN.fullmatch(result["pkgver"])
    require(version is not None, "Current pkgver must use upstream base and commit count")
    require(re.fullmatch(r"[1-9][0-9]*", result["pkgrel"]), "Invalid pkgrel")
    require(re.fullmatch(r"[0-9a-f]{7,40}", result["_commit"]), "Invalid current commit")
    require(version[3] == result["_commit"], "pkgver and _commit disagree")
    for field in ("_binary_sha256", "_model_sha256"):
        require(re.fullmatch(r"[0-9a-f]{64}", result[field]), f"Invalid {field}")
    return result


def plan(current, assets, base=None, count=None):
    result = current.copy()
    same_commit = current["_commit"] == assets.commit
    if same_commit:
        require(current["_binary_sha256"] == assets.binary.sha256,
                "Binary digest changed for the same commit; manual review required")
        if current["_model_sha256"] != assets.model.sha256:
            result["_model_sha256"] = assets.model.sha256
            result["pkgrel"] = str(int(current["pkgrel"]) + 1)
    else:
        version = None
        if base is not None:
            old = VERSION_PATTERN.fullmatch(current["pkgver"])
            require(type(count) is int and count > int(old[2]),
                    "Upstream revision count does not advance")
            require(tuple(map(int, base.split("."))) >= tuple(map(int, old[1].split("."))),
                    "Upstream base version moved backwards")
            version = f"{base}.alpha.smart.r{count}.g{assets.commit}"
        result.update(pkgver=version, pkgrel="1", _commit=assets.commit,
                      _binary_sha256=assets.binary.sha256, _model_sha256=assets.model.sha256)
    return result


def render_recipe(original, fields):
    for field, value in fields.items():
        original = re.sub(r"^" + re.escape(field) + r"=.*$",
                          f"{field}='{value}'", original, flags=re.MULTILINE)
    return original


def atomic_write(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_srcinfo(content, fields, assets):
    require("pkgbase = mihomo-smart-bin\n" in content, "Invalid generated .SRCINFO pkgbase")
    for field in ("pkgver", "pkgrel"):
        require(f"\t{field} = {fields[field]}\n" in content,
                f"Generated .SRCINFO has incorrect {field}")
    require(assets.binary.name in content, "Generated .SRCINFO has incorrect binary URL")
    for field in ("_binary_sha256", "_model_sha256"):
        require(re.search(r"^\tsha256sums(?:_x86_64)? = " + fields[field] + r"$",
                          content, re.MULTILINE), "Generated .SRCINFO has incorrect checksum")


def validate_archive(path, commit):
    """Inspect an upstream pacman package without extracting or executing it.

    libarchive converts zstd to a tar stream; Python checks structured member
    metadata and bounded payloads rather than parsing human-readable listings.
    """
    required = {".PKGINFO", "usr/bin/mihomo", "etc/mihomo/config.yaml",
                "usr/lib/systemd/system/mihomo.service"}
    seen, total, metadata = set(), 0, None
    process = subprocess.Popen(
        ["bsdtar", "-c", "-P", "--format=pax", "-f", "-", "@" + str(path.resolve())],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                name = member.name
                while name.startswith("./"):
                    name = name[2:]
                parts = name.rstrip("/").split("/")
                require(name and not name.startswith("/") and
                        all(part not in ("", ".", "..") for part in parts) and
                        "\\" not in name and not any(ord(c) < 32 for c in name),
                        "Unsafe archive member path")
                name = "/".join(parts)
                require(name not in seen, "Duplicate archive member")
                seen.add(name)
                require(len(seen) <= MAX_ARCHIVE_MEMBERS, "Too many archive members")
                require(member.isdir() or member.isreg(), "Unsafe archive member type")
                if member.isdir():
                    require(name not in required, "Required archive payload is not a file")
                    continue
                limit = MAX_ASSET_SIZE if name == "usr/bin/mihomo" else MAX_METADATA_SIZE
                require(0 <= member.size <= limit, "Expanded archive member is too large")
                total += member.size
                require(total <= MAX_ASSET_SIZE + 16 * MAX_METADATA_SIZE,
                        "Expanded archive is too large")
                stream = archive.extractfile(member)
                prefix = stream.read(min(member.size, 4))
                if name == "usr/bin/mihomo":
                    require(prefix == b"\x7fELF", "Binary is not an ELF executable")
                payload = bytearray(prefix) if name == ".PKGINFO" else None
                remaining = member.size - len(prefix)
                while remaining:
                    chunk = stream.read(min(remaining, 1024 * 1024))
                    require(chunk, "Truncated archive member")
                    remaining -= len(chunk)
                    if payload is not None:
                        payload.extend(chunk)
                if payload is not None:
                    metadata = payload.decode("utf-8")
        # Consume tar padding and let libarchive report decompression failures.
        require(len(process.stdout.read(MAX_METADATA_SIZE + 1)) <= MAX_METADATA_SIZE,
                "Unexpected trailing archive data")
        require(process.wait(timeout=60) == 0, "Invalid or truncated package archive")
    except (tarfile.TarError, UnicodeError, EOFError) as exc:
        raise UpdateError("Invalid or truncated package archive") from exc
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    require(required <= seen, "Missing required package payload")
    values = {}
    for line in metadata.splitlines():
        if " = " in line and not line.startswith("#"):
            key, value = line.split(" = ", 1)
            values.setdefault(key, []).append(value)
    require(values.get("pkgname") == ["mihomo"] and values.get("arch") == ["x86_64"],
            "Unexpected upstream package identity")
    versions = values.get("pkgver", [])
    require(len(versions) == 1, "Expected one upstream package version")
    version = re.fullmatch(r"((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))"
                           r"\.alpha\.smart-" + re.escape(commit) + r"-smart-1", versions[0])
    require(version is not None, "Unexpected upstream package version or release commit")
    return version[1]


def git_command(repo, *arguments):
    env = os.environ.copy()
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull,
                             "--git-dir=" + str(repo), *arguments],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False, timeout=300, env=env)
    require(result.returncode == 0, "Unable to verify upstream Git history")
    return result.stdout.decode("ascii").strip()


def inspect_history(repo, commit, previous):
    """Verify a complete upstream graph and return its reachable commit count."""
    require(git_command(repo, "rev-parse", "--is-shallow-repository") == "false",
            "Shallow upstream Git history is not accepted")
    head = git_command(repo, "rev-parse", "--verify", "refs/tags/upstream^{commit}")
    require(head.startswith(commit), "Upstream tag does not match release asset commit")
    # Resolve the asset prefix itself as well, rejecting ambiguous short hashes.
    require(git_command(repo, "rev-parse", "--verify", commit + "^{commit}") == head,
            "Ambiguous upstream asset commit")
    ancestor = git_command(repo, "rev-parse", "--verify", previous + "^{commit}")
    git_command(repo, "merge-base", "--is-ancestor", ancestor, head)
    count = git_command(repo, "rev-list", "--count", head)
    require(re.fullmatch(r"[1-9][0-9]*", count), "Invalid upstream commit count")
    return int(count)


def history_revision(commit, previous):
    with tempfile.TemporaryDirectory(prefix="mihomo-history-") as directory:
        repo = Path(directory) / "upstream.git"
        git_command(repo, "init", "--bare", str(repo))
        git_command(repo, "fetch", "--filter=blob:none", "--no-tags",
                    "https://github.com/vernesong/mihomo.git",
                    "refs/tags/Prerelease-Alpha:refs/tags/upstream")
        return inspect_history(repo, commit, previous)


def update(root, client, write=False):
    recipe, srcinfo = root / "PKGBUILD", root / ".SRCINFO"
    original = recipe.read_bytes()
    old_info = srcinfo.read_bytes() if srcinfo.exists() else None
    current = read_fields(original.decode("utf-8"))
    assets = select_assets(client)
    fields = plan(current, assets)
    changed = fields != current
    if write and changed:
        with tempfile.TemporaryDirectory(prefix="mihomo-update-") as directory:
            for asset in (assets.binary, assets.model):
                path = Path(directory) / asset.name
                client.download(asset, path)
                if asset == assets.binary:
                    base = validate_archive(path, assets.commit)
            if current["_commit"] != assets.commit:
                fields = plan(current, assets, base, history_revision(assets.commit, current["_commit"]))
            require(select_assets(client) == assets,
                    "Upstream release changed during download; retry after it stabilizes")
        require(recipe.read_bytes() == original and
                (srcinfo.read_bytes() if srcinfo.exists() else None) == old_info,
                "Local recipe changed during update")
        try:
            atomic_write(recipe, render_recipe(original.decode("utf-8"), fields).encode())
            generated = subprocess.run(["makepkg", "--printsrcinfo"], cwd=root,
                                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       timeout=60).stdout
            validate_srcinfo(generated.decode("utf-8"), fields, assets)
            atomic_write(srcinfo, generated)
        except BaseException:
            atomic_write(recipe, original)
            if old_info is None:
                srcinfo.unlink(missing_ok=True)
            else:
                atomic_write(srcinfo, old_info)
            raise
    return {"changed": str(changed).lower(), "pkgver": fields["pkgver"],
            "pkgrel": fields["pkgrel"], "commit": fields["_commit"],
            "binary_sha256": fields["_binary_sha256"], "model_sha256": fields["_model_sha256"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Inspect release metadata without downloading or editing")
    mode.add_argument("--update", action="store_true", help="Verify assets and update PKGBUILD and .SRCINFO")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = update(args.root, Client(), write=args.update)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as stream:
                stream.write("".join(f"{key}={value}\n" for key, value in result.items() if value is not None))
        print(json.dumps(result, sort_keys=True))
    except (UpdateError, OSError, ValueError, subprocess.SubprocessError, urllib.error.URLError) as exc:
        # Avoid raw HTTP or subprocess output, which can contain authentication data.
        message = str(exc) if isinstance(exc, UpdateError) else type(exc).__name__
        print(f"Update failed: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
