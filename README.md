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

The package retains the upstream **Linux amd64 v3** binary used by the existing
recipe. It requires an x86-64-v3 CPU. It installs `/usr/bin/mihomo`, the system
service, a default configuration, and the large LightGBM model. It conflicts with
the standard `mihomo` package.

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

1. Selects the exact amd64-v3 Smart gzip asset and `Model-large.bin`.
2. Verifies GitHub's SHA256 digests, download sizes, gzip integrity, and ELF header;
   checks again that upstream metadata stayed stable during the download.
3. Updates the version/checksums and regenerates `.SRCINFO` with `makepkg`.
4. Builds and installs the package in a disposable Arch Linux container, validates
   a Smart configuration, and queries a running Smart group through its controller.
5. Publishes only validated packaging files to AUR in a separate job, then records
   the new metadata and AUR commit on GitHub.

The rolling release reuses its tag and release ID. Package versions therefore use
the binary asset's UTC upload timestamp and commit, for example
`20261021.194528.g4624551`. This replaces the old `1.19.19.alpha.*` scheme and sorts
as an upgrade. The executable still reports its upstream `alpha-smart-<commit>`
version. Model-only updates increment `pkgrel`.

Unexpected URLs, missing digests, binary timestamp rollback, changed bytes for the
same core commit, and intervening AUR co-maintainer edits stop publication. The
publisher checks the AUR commit and complete packaging tree against
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
gh workflow run update.yml --repo liblaf/aur-mihomo-smart-bin \
  -f validate_current=true
```

The private key is never committed. A second `GH_DEPLOY_KEY` environment secret
holds a write deploy key scoped to this GitHub repository; its public half is
tracked in
[config/github-actions-key.pub](https://github.com/liblaf/aur-mihomo-smart-bin/blob/main/config/github-actions-key.pub).
Validation jobs have no AUR credentials or
GitHub write token. The publishing job downloads only packaging metadata and
never executes the downloaded core or a candidate recipe. AUR's server host key
is pinned in
[config/aur-known-hosts](https://github.com/liblaf/aur-mihomo-smart-bin/blob/main/config/aur-known-hosts).
The workflow creates GitHub commits on a temporary branch using the workflow
token, verifies GitHub's signature, then advances `main` with the scoped deploy
key. The `main-integrity` ruleset still requires verified signatures and forbids
force pushes, deletion, and branch creation. The `main-review` ruleset preserves
human review requirements and grants that deploy key a review bypass for
validated automatic publication.

Pushes and pull requests run validation without publication. Scheduled and manual
runs on this repository's `main` branch can publish. Successful monthly validation
records keep the repository active; GitHub can disable public-repository schedules
after 60 days without activity if those checks stop succeeding.

## Maintain locally

The updater and tests use Python's standard library. Check upstream without
changing files, or prepare a verified update:

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

For a packaging change, increment `pkgrel` and regenerate `.SRCINFO`:

```bash
makepkg --printsrcinfo > .SRCINFO
```

## License

New maintenance code is licensed under
[0BSD](https://github.com/liblaf/aur-mihomo-smart-bin/blob/main/LICENSE).
Mihomo Smart is licensed under
[GPL-3.0-or-later](https://github.com/vernesong/mihomo/blob/Alpha/LICENSE).
