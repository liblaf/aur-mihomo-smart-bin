<div align="center" markdown>

![Mihomo Smart for Arch Linux](https://socialify.git.ci/liblaf/aur-mihomo-smart-bin/image?description=1&forks=1&issues=1&language=1&name=1&owner=1&pattern=Transparent&pulls=1&stargazers=1&theme=Auto)

[![Validate and update Mihomo Smart](https://github.com/liblaf/aur-mihomo-smart-bin/actions/workflows/update.yml/badge.svg)](https://github.com/liblaf/aur-mihomo-smart-bin/actions/workflows/update.yml)
[![AUR version](https://img.shields.io/aur/version/mihomo-smart-bin)](https://aur.archlinux.org/packages/mihomo-smart-bin)

[AUR package](https://aur.archlinux.org/packages/mihomo-smart-bin) · [Upstream releases](https://github.com/vernesong/mihomo/releases) · [Workflow runs](https://github.com/liblaf/aur-mihomo-smart-bin/actions/workflows/update.yml)

</div>

---

Arch Linux packaging and automatic release maintenance for
[Mihomo Smart](https://github.com/vernesong/mihomo), the Mihomo fork with Smart
proxy groups. This repository maintains the existing `mihomo-smart-bin` AUR
package. Original packaging is credited to `neko_ayaya`; `liblaf` is a co-maintainer.

The package reuses the upstream **Linux amd64 v3** pacman archive. It requires an
x86-64-v3 CPU. It includes the upstream binary, default configuration, systemd
services, and license, plus the large LightGBM model. The wrapper keeps the
`mihomo-smart-bin` package identity and conflicts with the standard `mihomo`
package. It generates its own metadata with the relative backup path
`etc/mihomo/config.yaml` so pacman preserves local configuration edits.

## Install

Using an AUR helper:

```bash
paru -S mihomo-smart-bin
```

Or build as your normal user:

```bash
git clone https://github.com/liblaf/aur-mihomo-smart-bin.git
cd aur-mihomo-smart-bin
sudo pacman -S --needed base-devel
makepkg --syncdeps --install
```

Package installation does not switch a running proxy automatically. Review
`/etc/mihomo/config.yaml` before starting or restarting `mihomo.service`.

## Automatic maintenance

GitHub Actions checks the upstream rolling `Prerelease-Alpha` release and
`LightGBM-Model` release every six hours, at **02:37, 08:37, 14:37, and 20:37
Asia/Shanghai**. GitHub may delay scheduled runs. Unchanged releases skip package
builds, except for a compatibility check every 28 days.

For each update, the workflow:

1. Selects the exact amd64-v3 Smart `.pkg.tar.zst` asset and `Model-large.bin`.
2. Verifies GitHub's SHA256 digests, download sizes, archive contents, and ELF header;
   checks again that upstream metadata stayed stable during the download.
3. Reads the upstream Mihomo version from the package metadata, verifies the
   complete Git history and revision count, and regenerates `.SRCINFO` with `makepkg`.
4. Builds and installs the package in a disposable Arch Linux container, checks
   that local config edits survive reinstallation, validates a Smart configuration,
   and queries a running Smart group through its controller.
5. Publishes only validated packaging files to AUR in a separate job, then records
   the new metadata and AUR commit on GitHub with a verified signature.

Package versions follow the upstream Mihomo version and Git revision, for example
`1.19.34.alpha.smart.r4831.g4624551`. The base version comes from the verified
upstream package's `.PKGINFO`; `4831` is the number of commits reachable from the
release commit in its complete Git history. The revision count precedes the hash
so successive builds sort in commit order. The executable still reports its
upstream `alpha-smart-<commit>` version. Model and packaging updates increment
`pkgrel`; a new core version resets it to 1.

Unexpected URLs, missing digests, changed archive bytes for the same core commit,
and intervening AUR co-maintainer edits stop publication. New core commits must
descend from the previous commit, increase the revision count, and retain or
increase the upstream base version. The publisher checks the AUR commit and
complete packaging tree against
[aur-state.json](https://github.com/liblaf/aur-mihomo-smart-bin/blob/main/aur-state.json).
After reviewing an intentional co-maintainer update, reconcile the recipe and
tracked state before resuming automation. Ordinary pushes never overwrite AUR
history. If AUR publication succeeds but recording the GitHub commit fails, a
rerun of the failed publishing job can recover that exact update:

```bash
gh run rerun RUN_ID --failed --repo liblaf/aur-mihomo-smart-bin
```

Use the failed run's retained artifact for recovery. A fresh run may select a
newer upstream build and will stop on the unresolved AUR difference.

This automation updates the **AUR recipe**. Local machines receive it through
their usual AUR upgrade process; installing and restarting a proxy remains a
separate operation. Upstream deletes older rolling release assets, so old pinned
recipes may stop downloading once a newer build replaces them.

## Configure publishing

The `aur-publish` GitHub environment must allow only the `main` branch. Its
`AUR_SSH_PRIVATE_KEY` secret holds a dedicated Ed25519 key whose public half is
registered on the `liblaf` AUR account. The public half is tracked in
[config/aur-actions-key.pub](https://github.com/liblaf/aur-mihomo-smart-bin/blob/main/config/aur-actions-key.pub).

Register that public key in the account's AUR SSH key settings, then set the
matching private key as the environment secret:

```bash
gh secret set AUR_SSH_PRIVATE_KEY --repo liblaf/aur-mihomo-smart-bin \
  --env aur-publish < /path/to/dedicated/private-key
gh variable set AUR_PUBLISH_ENABLED --repo liblaf/aur-mihomo-smart-bin --body true
gh workflow run update.yml --repo liblaf/aur-mihomo-smart-bin \
  -f validate_current=true
```

The private key is never committed. GitHub publication uses the workflow's
`GITHUB_TOKEN` with `contents: write` and
[`suzuki-shunsuke/commit-action` v1.1.0](https://github.com/suzuki-shunsuke/commit-action/releases/tag/v1.1.0),
pinned to commit `5eaa5ecce3ffe93d09a85b946dc5136e3713b1b7`. The AUR key is the
only publishing secret needed. Validation jobs have no AUR credentials or GitHub
write token. The publishing job downloads only packaging metadata and never
executes the downloaded core or a candidate recipe. AUR's server host key is pinned in
[config/aur-known-hosts](https://github.com/liblaf/aur-mihomo-smart-bin/blob/main/config/aur-known-hosts).

After AUR accepts the update, the workflow creates a unique temporary branch at
the validated source commit. The action can commit only `PKGBUILD`, `.SRCINFO`,
`mihomo-smart-bin.install`, `LICENSE`, `aur-state.json`, and
`automation-validation.json`. The workflow verifies the resulting signature,
checks that `main` still matches the validated source commit, and advances `main`
with a non-forced API update. A concurrent update to `main` stops publication;
the temporary branch is removed on success or failure. The retained artifact
supports recovery after AUR has accepted the update.

The `main-integrity` ruleset requires verified signatures and forbids force
pushes, deletion, and branch creation. Main does not require a pull request for
publication.

Pushes and pull requests run validation without publication. Scheduled and manual
runs on this repository's `main` branch can publish. Successful monthly validation
records keep the repository active; GitHub can disable public-repository schedules
after 60 days without activity if those checks stop succeeding.

Set `AUR_PUBLISH_ENABLED=true` after registering the dedicated public key with
AUR. Set it to `false` to stop publication while checks and validation continue.

## Maintain locally

The updater and tests use Python's standard library, Git, and `bsdtar` from
`libarchive`. Check upstream without changing files, or prepare a verified update:

```bash
python3 scripts/update.py --check
python3 scripts/update.py --update
python3 -m unittest discover -s tests -v
shellcheck PKGBUILD scripts/validate.sh
git diff -- PKGBUILD .SRCINFO
```

Updating metadata requires Arch's `makepkg`. GitHub API requests can use `GH_TOKEN`
or `GITHUB_TOKEN`; an authenticated local `gh` CLI is also supported. Without
credentials, GitHub's lower anonymous rate limit applies.

`--check` reads release metadata without downloading package assets or Git history.
When it finds a new core commit, it reports `changed=true` with `pkgver=null` until
`--update` verifies the archive and derives the version. New core updates fetch
complete Git history with blob filtering, without checking out or running source.

For a packaging change, increment `pkgrel` and regenerate `.SRCINFO`:

```bash
makepkg --printsrcinfo > .SRCINFO
```

## License

New maintenance code is licensed under
[0BSD](https://github.com/liblaf/aur-mihomo-smart-bin/blob/main/LICENSE).
Mihomo Smart is licensed under
[GPL-3.0-or-later](https://github.com/vernesong/mihomo/blob/Alpha/LICENSE).
