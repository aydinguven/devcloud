# DevCloud installer and lifecycle guide

The supported production target is Rocky Linux 10 or RHEL 10 on x86_64. Run
the installer as root; operating-system packages come from the VM's enabled
DNF repositories, including Satellite or Foreman-managed repositories.

## Start the interactive installer

From a verified checkout or extracted release:

    sudo bash deploy/devcloud-setup.sh

The menu provides controller, all-in-one, and worker installation plus update,
repair, status, backup, restore, and uninstall.

The shell bootstrap validates the OS, architecture, root privileges, DNF, and
systemd. If subscription-manager is absent, it installs it from configured
repositories. If those repositories cannot provide it, a matching
offline/system-rpms/distribution-10-x86_64 closure is used when present. The
installer does not register a subscription or activation key.

When an offline artifact manifest is present, the behavior is stricter: the
installer verifies and installs the bundled RPM closure with every repository
disabled before it starts the Python UI. It then verifies the complete artifact
manifest and installs wheels without an index. Both bundle roles include the
immutable rootful worker image; the server bundle also includes controller and
PostgreSQL images. Base bundles do not contain workspace images. It never
falls back to a connected repository for that installation.

## Controller

Choose SQLite for the smallest footprint, bundled PostgreSQL for a controller
expected to manage several workers, or an external PostgreSQL URL for an
operator-managed database.

The recommended controller runtime is an immutable OCI image supervised by
Podman Quadlet. The optional bundled PostgreSQL database is a second container
on a private network and is never published on a host port. Native Python
systemd installation remains available for existing deployments and
compatibility.
Connected installs pull versioned runtime images from
`quay.io/aaslangoren/devcloud` by default; air-gapped installs load the same
images from their verified bundle archives.

After installation, add each workspace image under **Admin > Workspace
Image'ları** from a Quay/internal-registry reference or an OCI/Docker tar
archive. The controller normalizes and verifies the archive, then enrolled
workers download it over the authenticated controller connection. Create worker
records under **Admin > Worker'lar** and retain each one-time enrollment token.

## All-in-one

All-in-one installs the selected controller runtime and the rootful worker
container. The installer creates one node ID/token pair, seeds
that node in the database, and connects the worker over loopback. This is a
convenience deployment, not a separate runtime architecture.
User-triggered workspace snapshots are supported only in this topology until
distributed registry export has its own explicit authorization workflow.

## Worker

New installations run the worker agent as a system Quadlet container. The
container uses host networking and the rootful host Podman API socket at
`/run/podman/podman.sock`; workspace containers and images therefore remain in
the host's root Podman store. The socket is root-equivalent, is mounted only
into the worker container, and does not require a privileged container. This
mode is intended for dedicated, trusted worker VMs. Existing native/rootless
worker installations retain their saved runtime during repair and update.

When the worker can reach the controller, open **Admin > Worker Node'ları** and
select **Yeni Worker Kurulum Komutu Üret**. Run the generated command as root on
the worker within 10 minutes. The command is single-use. It asks only for the
worker name, creates the node and enrollment token through the controller, then
downloads and verifies the controller's current platform release before invoking
the unified installer. Set `DEVCLOUD_WORKER_NAME` to suppress the only prompt.

The reusable `/download/install-worker.sh` endpoint is intentionally disabled;
never place a permanent node token in a URL or shell command. If enrollment is
interrupted after the ticket is consumed, generate a fresh command in Admin.

A JSON answer file can be applied with:

    sudo bash deploy/devcloud-setup.sh --yes install worker \
      --answers /root/worker-answers.json

The token file and generated environment files must remain mode 0600.
Worker installation does not prompt for or preload workspace images. After the
agent connects, it reconciles the enabled controller image catalogue every 30
seconds, verifies archive size and SHA-256, loads missing images into rootful
host Podman through its socket, and reports exact synchronized checksums.
Scheduling waits until a worker reports the currently enabled checksum for the
requested template.

## Updates

Production updates are build-once/deploy-many. Git is the release channel, not
the runtime artifact: every application release builds the controller and
worker OCI images once on a trusted builder, exports them into one signed
platform bundle, and deploys that same bundle without building on the VMs.
Workspace images are never included.

On the release builder:

    bash deploy/container/build-controller-image.sh
    bash deploy/container/build-worker-image.sh
    python3 deploy/build_platform_update.py \
      --signing-key GPG_KEY_ID \
      --channel-output /tmp/release-channel/devcloud-update-channel.json \
      --channel-url https://artifacts.example/devcloud/devcloud-platform-update-v3.4.1-COMMIT.tar.gz

Commit only `devcloud-update-channel.json` to the selected `stable` branch (or
another configured branch/tag). Store the multi-gigabyte bundle in a Git
release, an internal artifact server, or beside the channel file for a local
repository. The descriptor pins its filename, size, and SHA-256.

The controller installer asks for the Git repository and branch/tag and stores
them in `/etc/devcloud/controller.env`. They can also be supplied directly:

    sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh --yes update \
      --source-type git \
      --repository https://git.example/devcloud.git \
      --ref stable

The same Git source or an uploaded signed bundle can be selected under
**Admin > System > Platform Güncelleme**. The controller process only writes a
request; `devcloud-update.path` invokes the root-owned updater. A verified
pre-update database/configuration backup is created automatically. On success,
the controller publishes the platform bundle to enrolled workers, which obtain
it through authenticated release endpoints and use their own root-owned queue.

For an air-gapped or local update:

    sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh --yes update \
      --bundle /root/devcloud-platform-update-v3.4.1-COMMIT.tar.gz

Official releases contain `release.json` and `release.json.asc` and are
verified by `/etc/devcloud/release-keyring.gpg`. Unsigned updates are disabled
on installed controllers. The CLI break-glass option is intended only for
explicit development use when the host configuration enables it.

For an operator-reviewed development source ZIP:

    sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh --yes update \
      --bundle /root/devcloud-source.zip --allow-unsigned

## Back up, restore, and uninstall

    sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh backup \
      --output /backup/devcloud-config-db.tar.gz

    sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh backup \
      --include-workspaces --output /backup/devcloud-full.tar.gz

    sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh restore \
      --bundle /backup/devcloud-config-db.tar.gz

    sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh uninstall

Backups contain checksummed members and are written mode 0600 because they
include secrets. PostgreSQL backups use custom-format pg_dump; SQLite uses its
online backup API. Restore stops services, verifies every member, restores
state, reapplies migrations, and starts services.

Uninstall preserves configuration, releases, database, and workspaces by
default. The purge option permanently removes managed data after a typed
confirmation.

## Managed paths

- /opt/devcloud/current: active release link
- /var/lib/devcloud/releases: immutable releases
- /etc/devcloud/controller.env: controller secrets
- /etc/devcloud/worker.env: worker enrollment secrets
- /var/lib/devcloud/installer/install-state.json: non-secret installer state
- /var/lib/devcloud/update-queue: service-writable, root-consumed update handoff
- /var/lib/devcloud/workspaces: default worker storage
- /srv/devcloud-downloads: controller artifacts
