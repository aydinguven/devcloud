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

When the worker can reach the controller, publish a worker bundle and run:

    curl -fsSL https://controller.example/download/install-worker.sh | sudo bash

The bootstrap downloads the bundle and checksum from the controller, verifies
the checksum, asks for the node ID/token through /dev/tty, and invokes the same
unified installer. Installation succeeds only after the controller accepts the
credentials and reports the worker tunnel connected. Non-interactive provisioning may set
DEVCLOUD_CONTROLLER_URL, DEVCLOUD_NODE_ID, and DEVCLOUD_NODE_TOKEN.

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

For an operator-reviewed Git source ZIP:

    sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh --yes update \
      --bundle /root/devcloud-source.zip --allow-unsigned

The allow-unsigned option is an explicit trust decision. Official releases
should contain release.json and release.json.asc and be verified by
/etc/devcloud/release-keyring.gpg.

Build an official source release with:

    python3 deploy/build_release.py --signing-key GPG_KEY_ID

Container deployments update through the host installer, not through Git
inside the controller container. A verified server air-gap bundle contains
controller, worker, and PostgreSQL OCI archives. A worker bundle contains the
worker archive. Update and reinstall therefore require no registry connection.

Copy published worker releases to /srv/devcloud-downloads/releases. Worker
downloads require its node ID/token; release source is not publicly exposed.

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
