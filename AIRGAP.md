# Air-Gapped Deployment

This runbook creates a self-contained Linux x86_64 bundle from a specific Git
commit. The bundle contains DevCloud source, Python wheels, all five workspace
container images, an artifact manifest, and SHA-256 checksums.

Generated bundles are intentionally excluded from normal Git history. Commit
and push the packaging code, then attach the generated archive and checksum to
a Git release (or store them in an approved artifact repository).

## 1. Prepare the target VM before isolation

The bundle cannot provide operating-system RPMs. While the VM can still access
its Rocky/RHEL/Fedora repository or installation media, install and verify:

- Linux x86_64
- CPython 3.11 or newer, including `pip` and `venv`
- Podman
- `sudo` and systemd
- `policycoreutils-python-utils` when SELinux is enforcing (`semanage`)

Record the exact target Python version:

```bash
python3 -c 'import sysconfig; print(sysconfig.get_python_version())'
uname -m
podman --version
```

The default bundle target is CPython 3.12 on Linux x86_64. Pass the target
version explicitly if it differs.

## 2. Commit and push the source

The packager refuses tracked, uncommitted changes and packages only files known
to Git. On the connected build machine:

```bash
git status
git add deploy/package_offline.py deploy/package_offline.sh deploy/deploy_offline.sh \
  AIRGAP.md README.md .gitignore tests/test_offline.py
git commit -m "harden air-gapped packaging"
git push origin main
```

Do not use `git add -f` on generated wheels, image archives, or `dist/`.

## 3. Build the bundle on a connected machine

The machine needs Git, Python/pip, Podman, internet access to package indexes
and container registries, and enough free disk space for all images twice
(staging plus the compressed archive).

```bash
git pull --ff-only origin main
python3 deploy/package_offline.py --python-version 3.12
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
4. rebuilds and exports all five Linux/amd64 Podman images;
5. writes `offline/MANIFEST.json` with artifact sizes and SHA-256 hashes;
6. verifies the stage and creates these ignored files:

```text
dist/devcloud-offline-<commit>.tar.gz
dist/devcloud-offline-<commit>.tar.gz.sha256
```

If the five local image tags are already known-good, add
`--skip-image-build`; missing tags still make packaging fail.

## 4. Publish the binary artifact

Push the source commit normally. In the Git server UI, create a release/tag
such as `airgap-2026-08-24` at that commit and attach both files from `dist/`.
Release assets keep multi-gigabyte images out of Git history while binding the
bundle to a source commit. Git LFS or an internal artifact repository are also
acceptable if your server has suitable quotas.

### Optional: publish and update bundles from the admin page

A connected DevCloud server can expose verified bundles at `/download/` and let
an administrator rebuild them from the Admin page. This feature is disabled by
default because it downloads wheels, rebuilds five Podman images, consumes
substantial disk space, and requires access to package and container registries.
It is not available on a truly disconnected server.

Create writable build and publication directories for the systemd service user:

```bash
SERVICE_USER="$(systemctl show -p User --value devcloud)"
test -n "${SERVICE_USER}" || SERVICE_USER=root
SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"

sudo install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 \
  /var/lib/devcloud/download-builds
sudo install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0755 \
  /srv/devcloud-downloads
```

Set these values in the project-root `.env`:

```dotenv
DOWNLOADS_ENABLED=True
DOWNLOAD_UPDATES_ENABLED=True
DOWNLOADS_ROOT=/srv/devcloud-downloads
DOWNLOAD_BUILD_ROOT=/var/lib/devcloud/download-builds
DOWNLOAD_TARGET_PYTHON_VERSION=3.12
```

Restart DevCloud, sign in as an administrator, and use **Admin > Offline
Downloads > Update downloads**:

```bash
bash deploy/restart.sh
```

The background job requires a clean tracked Git working tree. It builds into a
temporary directory, verifies both the internal artifact manifest and outer
SHA-256 checksum, copies the new version into the download directory, and only
then deletes older recognized bundle/checksum pairs. Status and the last 120
build log lines are shared across Uvicorn workers and shown on the Admin page.

The public listing is `https://dev.aydin.cloud/download/`. Keep the entire
hostname behind Cloudflare Access if downloads should be restricted.

## 5. Verify and install inside the air gap

Transfer both files using approved media. From the transfer directory:

```bash
sha256sum -c devcloud-offline-<commit>.tar.gz.sha256
tar -xzf devcloud-offline-<commit>.tar.gz
cd devcloud
python3 deploy/package_offline.py --verify . --check-runtime
bash deploy/deploy_offline.sh
```

The installer re-verifies every wheel and container archive before changing the
system. It then installs Python packages without an index, loads exactly five
container archives, applies the SELinux workspace policy, installs the systemd
unit, and starts DevCloud.

Validate the service:

```bash
sudo systemctl status devcloud --no-pager
sudo journalctl -u devcloud -n 100 --no-pager
curl -fsS http://127.0.0.1:8000/ >/dev/null
podman images | grep devcloud
```

Keep the archive and checksum together for rollback or rebuilding an identical
demo VM from the same source commit.
