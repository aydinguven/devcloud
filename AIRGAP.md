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
- Podman and an OCI runtime (`crun` or `runc`)
- `sudo` and systemd
- `policycoreutils-python-utils` when SELinux is enforcing (`semanage`)

On Rocky Linux / RHEL / Fedora / CentOS:

```bash
sudo dnf install -y podman crun python3 python3-pip policycoreutils-python-utils
```

Record the exact target Python version and verify OCI runtime:

```bash
python3 -c 'import sysconfig; print(sysconfig.get_python_version())'
uname -m
podman --version
crun --version || runc --version
```

The default bundle target is CPython 3.12 on Linux x86_64. Pass the target
version explicitly if it differs.

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
dist/devcloud-offline-v<version>-<YYYYMMDD>-<commit>.tar.gz
dist/devcloud-offline-v<version>-<YYYYMMDD>-<commit>.tar.gz.sha256
```

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
and reloads DevCloud. Then use **Yönetim > Çevrim Dışı İndirmeler > İndirmeleri
Güncelle**.

The update operation downloads wheels, rebuilds five Podman images, consumes
substantial disk space, and requires access to package and container registries.
On a truly disconnected server, keep published downloads available but set
`DOWNLOAD_UPDATES_ENABLED=False` to prevent rebuild attempts.

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
sha256sum -c devcloud-offline-*.tar.gz.sha256
tar -xzf devcloud-offline-*.tar.gz
cd devcloud
python3 deploy/package_offline.py --verify . --check-runtime
sudo bash deploy/deploy_offline.sh
```

The installer re-verifies every wheel and container archive before changing the
system. It then installs Python packages without an index, loads exactly five
container archives, applies the SELinux workspace policy, installs the systemd
unit, and starts DevCloud.
