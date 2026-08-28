# Air-Gapped Deployment

The unified lifecycle entry point for newly produced bundles is
sudo bash deploy/devcloud-setup.sh; it prompts for controller, all-in-one, or
worker role. The older deploy_offline.sh commands later in this document
describe pre-unified bundles and remain for migration compatibility. For an
in-place disconnected update, transfer a signed release or reviewed Git ZIP
and use devcloud-setup update with the bundle path; do not extract over the
active release.

This runbook creates Linux x86_64 server or CPU-worker bundles
from a specific Git commit. The server bundle supports both Controller and
All-in-one installation, including SQLite, bundled PostgreSQL, and external
PostgreSQL choices. Each bundle contains the required DevCloud source, Python
wheels, all five workspace container images, a distribution-matched
`subscription-manager` bootstrap repository, an artifact manifest, and SHA-256
checksums. All other operating-system packages come from an internal
Satellite/Foreman service after registration.

Generated bundles are intentionally excluded from normal Git history. Commit
and push the packaging code, then attach the generated archive and checksum to
a Git release (or store them in an approved artifact repository).

## 1. Prepare the target VM before isolation

The bootstrap bundle targets Rocky Linux 10.x or RHEL 10.x on x86_64 and
contains only the DNF dependency closure required to install
`subscription-manager`. Rocky and RHEL use separate bootstrap closures.
The VM must be able to reach the organization's Satellite or Foreman/Katello
service after this bootstrap step.

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
`download` command, `createrepo_c`, internet access to Python/package/container repositories,
and enough free disk space for all images and RPMs twice. RHEL package builders
must have access to entitled BaseOS/AppStream repositories. Install `pigz` on
the builder to compress with multiple CPU cores; packaging still works with a
slower single-threaded gzip fallback when `pigz` is unavailable.

```bash
sudo dnf install -y createrepo_c
```

```bash
git pull --ff-only
python3 deploy/package_offline.py --bundle-role server --python-version 3.12
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
4. downloads the Rocky/RHEL `subscription-manager` bootstrap dependency
   closure;
5. creates local DNF repository metadata and checksums it with the RPM payload;
6. rebuilds and exports all five Linux/amd64 Podman images;
7. writes `offline/MANIFEST.json` with artifact sizes and SHA-256 hashes;
8. verifies the stage and creates these ignored files:

```text
dist/devcloud-offline-v<version>-<YYYYMMDD>-<commit>.tar.gz
dist/devcloud-offline-v<version>-<YYYYMMDD>-<commit>.tar.gz.sha256
dist/devcloud-worker-offline-v<version>-<YYYYMMDD>-<commit>.tar.gz
dist/devcloud-worker-offline-v<version>-<YYYYMMDD>-<commit>.tar.gz.sha256
```

The outer bundle is gzip-compressed because the exported Podman layers benefit
substantially from it. The builder automatically uses multi-core `pigz` when it
is installed, reducing packaging time without changing target-host
compatibility; otherwise it falls back to the standard gzip writer. Previously
published plain `.tar` bundles remain downloadable and the worker bootstrap can
still extract them during the transition.

For an already extracted bundle that needs only installer/RPM-repository fixes,
`deploy/package_offline_patch.py` can build a role-specific delta ZIP from the
original archive. The generated `apply-patch.sh` verifies its payload, preserves
the old RPM directory as a backup, merges repository records into the target's
own manifest, and leaves its wheels and container images in place. This permits
compatible earlier bundle commits while still enforcing bundle format and role.

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
and reloads DevCloud. Then use the separate **Controller Paketini Güncelle** and
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
controller and worker packages are retained together. Status and the last 120 build
log lines are shared across Uvicorn workers and shown separately on the Admin
page.

The public listing is `https://dev.aydin.cloud/download/`. Keep the entire
hostname behind Cloudflare Access if downloads should be restricted.

When workers can reach the controller, the listing also exposes a one-command
bootstrapper:

```bash
curl -fsSL https://dev.aydin.cloud/download/install-worker.sh | sudo bash
```

The initial Controller URL is `http://10.253.6.189`. Change it at any time under
**Admin > Çevrim Dışı İndirmeler > Worker kurulumunda kullanılacak Controller URL**.
`DOWNLOAD_PUBLIC_BASE_URL` in `.env` remains the first-run fallback, including
when DevCloud runs behind a reverse proxy. The bootstrapper still uses the separately verified worker archive;
it does not duplicate installation logic. It prompts for node ID and token via
the terminal and never places the token in the command line.

## 5. Verify and install inside the air gap

This section is the clean-install flow. Do not extract a new bundle over a live
project directory: runtime database and local configuration files are
intentionally excluded from generated archives.

Transfer both files using approved media. Keep the target's public repository
access disabled for this test. From the transfer directory:

```bash
sha256sum -c devcloud-offline-*.tar.gz.sha256
tar -xzf devcloud-offline-*.tar.gz
cd devcloud
sudo bash deploy/devcloud-setup.sh
```

The first unified-installer run detects the manifest, verifies
`offline/system-rpms/SHA256SUMS`, disables every configured DNF repository for
the bootstrap transaction, enables only the bundle's `file://` repository,
and installs `subscription-manager`. It then exits so registration credentials
never need to be stored in the bundle.

Register the host using the organization's normal activation-key workflow,
verify its repositories, and rerun setup:

```bash
subscription-manager register --org='YOUR_ORG' --activationkey='YOUR_KEY'
dnf repolist
sudo bash deploy/devcloud-setup.sh
```

The second run installs all other operating-system prerequisites from the
registered Satellite/Foreman repositories. It then verifies every artifact in
the manifest before showing the role and database questions. Python
dependencies are installed from bundled wheels with `--no-index`; worker roles
load the five verified container archives instead of building or pulling
images. External PostgreSQL still requires access to the operator-provided
database endpoint.

For an existing controller that can reach the Git remote, use the Git update flow
in README.md instead of rerunning the clean offline installer. A fully
disconnected in-place application upgrade requires an explicit backup and
migration procedure for the existing project-root data and is not performed by
deploy_offline.sh.

## 6. Enable HTTPS from Admin

The controller remains usable over HTTP when no certificate exists. To enable TLS,
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
