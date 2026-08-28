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

## Controller

Choose SQLite for the smallest footprint, bundled PostgreSQL for a controller
expected to manage several workers, or an external PostgreSQL URL for an
operator-managed database.

The controller installs no Podman runtime. After installation, create worker
records in **Admin > Worker Nodes** and retain each one-time enrollment token.
If administrator-built custom images must run on more than one worker,
configure an external OCI registry prefix and authenticate each worker to it.

## All-in-one

All-in-one installs both devcloud-controller.service and
devcloud-worker.service. The installer creates one node ID/token pair, seeds
that node in the database, and connects the worker over loopback. This is a
convenience deployment, not a separate runtime architecture.
User-triggered workspace snapshots are supported only in this topology until
distributed registry export has its own explicit authorization workflow.

## Worker

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

## Updates

For an operator-reviewed Git source ZIP:

    sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh --yes update \
      --bundle /root/devcloud-source.zip --allow-unsigned

The allow-unsigned option is an explicit trust decision. Official releases
should contain release.json and release.json.asc and be verified by
/etc/devcloud/release-keyring.gpg.

Build an official source release with:

    python3 deploy/build_release.py --signing-key GPG_KEY_ID

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
