# Air-Gapped Deployment

This runbook creates self-contained Linux x86_64 master or CPU-worker bundles
from a specific Git commit. Each bundle contains the required DevCloud source,
Python wheels, all five workspace container images, distribution-matched
operating-system RPMs, a role-marked artifact manifest, and SHA-256 checksums.

Generated bundles are intentionally excluded from normal Git history. Commit
and push the packaging code, then attach the generated archive and checksum to
a Git release (or store them in an approved artifact repository).

## 1. Prepare the target VM before isolation

The self-bootstrapping bundle targets Rocky Linux 10.x or RHEL 10.x on x86_64.
It includes the complete DNF dependency closure for Python, pip, Podman, `crun`,
Nginx, SELinux tooling, and `subscription-manager`. Rocky and RHEL use
separate RPM closures, while Rocky nodes can still use the client for
Foreman/Katello.

The minimal target VM must already provide DNF, RPM, systemd, `sudo`, `tar`,
and `sha256sum`. Record its distribution before isolation:

```bash
source /etc/os-release
printf 'ID=%s VERSION_ID=%s ARCH=%s\n' "$ID" "$VERSION_ID" "$(uname -m)"
```

Build on the same distribution family as the target. Rocky 10.0, 10.1, and
10.2 share the `rocky-10-x86_64` profile; RHEL 10.x uses the separate
`rhel-10-x86_64` profile.

## 2. Commit and push the source

The packager refuses tracked, uncommitted changes and packages only files known
to Git. On the connected build machine:

```bash
git status
git add .
git commit -m "harden air-gapped packaging"
git push origin main
```

Do not use `git add -f` on generated wheels, image archives, or `dist/`.

## 3. Build the bundle on a connected machine

The machine needs Rocky Linux 10.x or RHEL 10.x, Git, Python/pip, Podman, DNF's
`download` command, internet access to Python/package/container repositories,
and enough free disk space for all images and RPMs twice. RHEL package builders
must have access to entitled BaseOS/AppStream repositories.

```bash
git pull --ff-only origin main
python3 deploy/package_offline.py --python-version 3.12
```

Build the standalone CPU-worker installer separately:

```bash
python3 deploy/package_offline.py \
  --bundle-role worker \
  --python-version 3.12
```

Repeat `--python-version` only if one archive must support several target
runtimes:

```bash
python3 deploy/package_offline.py \
  --python-version 3.11 \
  --python-version 3.12 \
  --python-version 3.13
```

The builder:

1. requires a clean tracked working tree;
2. copies only Git-tracked source into a temporary staging directory;
3. downloads Linux x86_64 binary wheels for the selected CPython version;
4. downloads the full Rocky/RHEL system RPM dependency closure, including
   Podman, `crun`, Nginx, and `subscription-manager`;
5. rebuilds and exports all five Linux/amd64 Podman images;
6. writes `offline/MANIFEST.json` with artifact sizes and SHA-256 hashes;
7. verifies the stage and creates these ignored files:

```text
dist/devcloud-offline-v<version>-<YYYYMMDD>-<commit>.tar
dist/devcloud-offline-v<version>-<YYYYMMDD>-<commit>.tar.sha256
dist/devcloud-worker-offline-v<version>-<YYYYMMDD>-<commit>.tar
dist/devcloud-worker-offline-v<version>-<YYYYMMDD>-<commit>.tar.sha256
```

The outer bundle is intentionally an uncompressed tar archive. Container image
layers and many RPM/wheel payloads are already compressed, so running gzip over
the whole bundle adds substantial CPU time for limited size reduction. Plain
tar makes both package creation and extraction faster. Previously published
.tar.gz bundles remain downloadable and the worker bootstrap can still extract
them during the transition.

If the five local image tags are already known-good, add
`--skip-image-build`; missing tags still make packaging fail.

## 4. Publish the binary artifact

Push the source commit normally. In the Git server UI, create a release/tag
such as `airgap-2026-08-25` at that commit and attach both files from `dist/`.
Release assets keep multi-gigabyte images out of Git history while binding the
bundle to a source commit. Git LFS or an internal artifact repository are also
acceptable if your server has suitable quotas.

### Optional: publish and update bundles from the admin page

A connected DevCloud server exposes verified bundles at `/download/` and lets
an administrator rebuild them from the management page. New installations
enable this feature and prepare its directories automatically. For an existing
installation, enable it once with:

```bash
sudo bash deploy/enable_downloads.sh
```

The helper prepares writable build/publication directories, updates the
project-root `.env`, restores standard SELinux file contexts when available,
and reloads DevCloud. Then use the separate **Master Paketini Güncelle** and
**Worker Paketini Güncelle** controls under **Yönetim > Çevrim Dışı
İndirmeler**.

The update operation downloads wheels and the Rocky/RHEL RPM dependency closure,
rebuilds five Podman images, consumes substantial disk space, and requires
access to package and container registries.
On a truly disconnected server, keep published downloads available but set
`DOWNLOAD_UPDATES_ENABLED=False` to prevent rebuild attempts.

Each background job requires a clean tracked Git working tree. It builds into a
temporary directory, verifies both the internal artifact manifest and outer
SHA-256 checksum, copies the new version into the download directory, and only
then deletes older recognized bundle/checksum pairs of the same role. The latest
master and worker packages are retained together. Status and the last 120 build
log lines are shared across Uvicorn workers and shown separately on the Admin
page.

The public listing is `https://dev.aydin.cloud/download/`. Keep the entire
hostname behind Cloudflare Access if downloads should be restricted.

When workers can reach the master, the listing also exposes a one-command
bootstrapper:

```bash
curl -fsSL https://dev.aydin.cloud/download/install-worker.sh | sudo bash
```

The initial Master URL is `http://10.253.6.189`. Change it at any time under
**Admin > Çevrim Dışı İndirmeler > Worker kurulumunda kullanılacak Master URL**.
`DOWNLOAD_PUBLIC_BASE_URL` in `.env` remains the first-run fallback, including
when DevCloud runs behind a reverse proxy. The bootstrapper still uses the separately verified worker archive;
it does not duplicate installation logic. It prompts for node ID and token via
the terminal and never places the token in the command line.

## 5. Verify and install inside the air gap

This section is the clean-install flow. Do not extract a new bundle over a live
project directory: runtime database and local configuration files are
intentionally excluded from generated archives.

Transfer both files using approved media. From the transfer directory:

```bash
sha256sum -c devcloud-offline-*.tar.sha256
tar -xf devcloud-offline-*.tar
cd devcloud
sudo bash deploy/deploy_offline.sh
```

The installer first checks the outer archive workflow, detects Rocky versus
RHEL, verifies `offline/system-rpms/SHA256SUMS`, and installs the local RPM
transaction with all network repositories disabled. It then re-verifies every
manifest artifact, installs Python packages without an index, loads exactly
five container archives, applies the SELinux workspace policy, installs the
DevCloud and ingress systemd units, configures Nginx on port 80, and starts
DevCloud. On Rocky and RHEL, `subscription-manager` is
installed but registration is intentionally left to the administrator because
Foreman/Katello or Red Hat registration needs organization credentials and an
activation key.

For an existing master that can reach the Git remote, use the Git update flow
in README.md instead of rerunning the clean offline installer. A fully
disconnected in-place application upgrade requires an explicit backup and
migration procedure for the existing project-root data and is not performed by
deploy_offline.sh.

## 6. Enable HTTPS from Admin

The master remains usable over HTTP when no certificate exists. To enable TLS,
open **Admin > Çevrim Dışı İndirmeler > HTTPS & Sertifika Yönetimi**:

1. set the hostname to aifactory.tcmb.gov.tr;
2. upload the CA-issued certificate chain in PEM format;
3. upload the matching, unencrypted PEM private key;
4. leave HTTP fallback enabled until DNS and TCMB-CA trust are confirmed;
5. select **Kaydet ve Nginx'e Uygula**.

The panel rejects expired/not-yet-valid certificates, SAN mismatches, non-server
certificates, mismatched private keys, and oversized uploads. Nginx is tested
before reload and the previous active files are restored on failure. The
root-owned devcloud-ingress.path unit handles only fixed files under
/var/lib/devcloud/ingress; the application user is not granted passwordless
sudo.

The application URL used by worker bootstrap changes to
https://aifactory.tcmb.gov.tr when HTTPS is enabled. Before using the one-line
worker installer over HTTPS, ensure the worker trusts TCMB-CA and can resolve
the hostname. No HSTS header is emitted while fallback is supported.
