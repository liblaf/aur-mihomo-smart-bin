#!/usr/bin/env bash
# SPDX-License-Identifier: 0BSD
# Run only in a disposable Arch container without publishing credentials.
set -euo pipefail

pacman -Syu --needed --noconfirm base-devel git python namcap shellcheck > /tmp/setup.log 2>&1 || {
  tail -60 /tmp/setup.log
  exit 1
}
useradd --create-home builder
project=/home/builder/project
install -d "$project/scripts" "$project/tests"
cp /workspace/PKGBUILD /workspace/.SRCINFO /workspace/mihomo-smart-bin.install /workspace/LICENSE /workspace/aur-state.json "$project/"
cp /workspace/scripts/*.py "$project/scripts/"
cp /workspace/tests/*.py "$project/tests/"
chown -R builder:builder "$project"
as_builder() {
  runuser -u builder -- bash -c 'cd /home/builder/project && exec "$@"' bash "$@"
}
as_builder python3 -m unittest discover -s tests -v
if [[ ${SKIP_UPDATE:-0} != 1 ]]; then
  as_builder python3 scripts/update.py --update
fi
# The read-only GitHub token is used only for metadata; upstream code receives none.
unset GH_TOKEN GITHUB_TOKEN
cd "$project"
if [[ -d /source-cache ]]; then
  for file in /source-cache/*; do
    case "${file##*/}" in
      config.yaml|mihomo.service|Model.bin|mihomo-*.gz) cp "$file" . ;;
    esac
  done
  chown builder:builder config.yaml mihomo.service Model.bin mihomo-*.gz
fi
shellcheck PKGBUILD
as_builder makepkg --printsrcinfo > /tmp/generated.SRCINFO
cmp .SRCINFO /tmp/generated.SRCINFO
as_builder makepkg --cleanbuild --force --noconfirm > /tmp/build.log 2>&1 || {
  tail -80 /tmp/build.log
  exit 1
}
package=$(as_builder makepkg --packagelist)
namcap PKGBUILD > /tmp/namcap-pkgbuild.log 2>&1
namcap "$package" > /tmp/namcap-package.log 2>&1
cat /tmp/namcap-pkgbuild.log /tmp/namcap-package.log
if grep -E ' E: ' /tmp/namcap-pkgbuild.log /tmp/namcap-package.log; then exit 1; fi
pacman -U --noconfirm "$package" > /tmp/install.log 2>&1
pacman -Qk mihomo-smart-bin
as_builder mihomo -v > /tmp/version.txt
cat /tmp/version.txt
install -d -o builder -g builder /home/builder/smoke
cp /etc/mihomo/Model.bin /home/builder/smoke/Model.bin
cat > /home/builder/smoke/config.yaml <<'EOF'
mixed-port: 0
external-controller: 127.0.0.1:18090
allow-lan: false
mode: rule
log-level: warning
proxies: []
proxy-groups:
  - name: SMART
    type: smart
    proxies: [DIRECT]
    uselightgbm: true
    collectdata: false
rules:
  - MATCH,SMART
EOF
chown -R builder:builder /home/builder/smoke
as_builder mihomo -d /home/builder/smoke -f /home/builder/smoke/config.yaml -t
runuser -u builder -- python3 - <<'PY'
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

recipe = Path('/home/builder/project/PKGBUILD').read_text()
commit = re.search(r"^_commit=['\"]?([0-9a-f]+)", recipe, re.MULTILINE)[1]
log = Path('/home/builder/smoke/runtime.log')
with log.open('w') as output:
    process = subprocess.Popen(['mihomo', '-d', '/home/builder/smoke', '-f', '/home/builder/smoke/config.yaml'],
                               stdout=output, stderr=subprocess.STDOUT)
    try:
        for attempt in range(50):
            if process.poll() is not None:
                raise RuntimeError('Mihomo exited during startup: ' + log.read_text())
            try:
                with urllib.request.urlopen('http://127.0.0.1:18090/version', timeout=1) as response:
                    version = json.load(response)
                break
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        else:
            raise RuntimeError('Mihomo controller did not start: ' + log.read_text())
        assert version['version'] == 'alpha-smart-' + commit, version
        with urllib.request.urlopen('http://127.0.0.1:18090/proxies/SMART', timeout=2) as response:
            group = json.load(response)
        assert group['type'].lower() == 'smart', group
        print('Validated Smart group through the installed core controller.')
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
PY
install -m 644 PKGBUILD .SRCINFO mihomo-smart-bin.install LICENSE /candidate/
python3 - <<'PY'
import hashlib
import json
import os
import re
from pathlib import Path
root = Path('/candidate')
recipe = (root / 'PKGBUILD').read_text()
fields = {key: re.search(r'^' + key + r"=['\"]?([^'\"\n]+)", recipe, re.MULTILINE)[1]
          for key in ('pkgver', 'pkgrel')}
files = ('PKGBUILD', '.SRCINFO', 'mihomo-smart-bin.install', 'LICENSE')
manifest = {'base_commit': os.environ['BASE_COMMIT'], 'version': fields['pkgver'],
            'pkgrel': fields['pkgrel'], 'sha256': {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}}
(root / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
print('Validated package metadata:', fields)
PY
