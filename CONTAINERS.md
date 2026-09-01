# DevCloud container deployment

## Decision

The controller and optional managed PostgreSQL database are packaged as OCI
images and run as rootful Podman Quadlet services. Nginx remains on the host so
the existing root-owned TLS handoff, firewall integration, and SELinux policy
remain intact.

Workers remain host-native for this phase. A worker owns workspace files and
controls host Podman. Putting that agent in a container would still require the
rootful /run/podman/podman.sock, the host workspace tree, disabled SELinux
separation for the socket, and host image management. That adds another image
without removing host coupling and grants the container full root-equivalent
Podman control. The existing systemd worker is the clearer boundary.

All-in-one is therefore a containerized controller plus the ordinary
host-native worker agent on the same VM. Distributed installations use the
same controller stack and add host-native workers normally.

## Runtime layout

    Host Nginx :80/:443 -> 127.0.0.1:8000 -> controller container
                                                    |
                                                    +-> PostgreSQL container
                                                        on private network

The database is never published to a host port. Controller data, download
artifacts, and ingress requests are bind-mounted from their existing managed
paths. The controller root filesystem is read-only and runs as UID/GID 10001.

Container images use Pull=never. Connected build/publish systems produce OCI
archives, and an air-gapped target verifies and loads those archives before
starting the Quadlet units. No registry is required at installation time.

## Build and export

Build the controller image on a connected Rocky/RHEL-compatible builder:

    bash deploy/container/build-controller-image.sh

The default PostgreSQL source is the upstream SCL PostgreSQL 16 image based on
CentOS Stream 10. Build sites that require the RHEL image or a Satellite mirror
can override DEVCLOUD_POSTGRES_SOURCE_IMAGE. Then export both images with
checksums:

    bash deploy/container/export-offline-images.sh

For example, to use the entitled RHEL image:

    podman login registry.redhat.io
    DEVCLOUD_POSTGRES_SOURCE_IMAGE=registry.redhat.io/rhel10/postgresql-16:latest \
      bash deploy/container/export-offline-images.sh

The resulting dist/container-images directory can be copied into the air gap
for manual use. The standard server air-gap bundle builder embeds the same two
archives under offline/controller-images, records their sizes and SHA-256
digests in the release manifest, and the interactive installer loads them
automatically.

## Workspace-image lifecycle

Workspace images are not part of the controller or worker base installation.
An administrator imports a maintained template image under **Admin > Workspace
Image'ları** from Quay, another reachable OCI registry, or an OCI/Docker archive.
All four maintained VS Code images bake in the Cline extension; its gateway,
API key, and model are injected from the controller-managed Workspace AI record
when a workspace container starts.
The controller converts registry and Docker inputs to a normalized Linux/amd64
OCI archive, records its size and SHA-256, and stores registry credentials only
in a temporary authentication file for the duration of the import.

Enrolled workers poll the authenticated image catalogue, download missing
archives from the controller, verify their size and SHA-256, and load them into
host Podman. Their heartbeat advertises the exact synchronized checksum and
download/load progress; the Admin image catalogue displays a per-worker status
bar and any synchronization error.
Scheduling selects a worker only after it reports the currently enabled version
of the image requested by the workspace template. No registry access is needed
from worker hosts.

For one-file transfer into an air gap, a connected builder can produce a
separate checksummed image pack:

    python3 deploy/package_workspace_images.py

Extract that pack and upload its individual `images/*.tar` archives through the
Admin page. This image pack can be updated on its own without rebuilding or
reinstalling controller and worker bundles.

## Lifecycle rules

- Database migrations run before every controller start and are idempotent.
- Application startup uses one Uvicorn process because worker tunnels are
  still process-local.
- Container hosts never build during an update. A trusted release builder
  creates one signed platform bundle containing the already-built controller
  and worker OCI archives. The Admin page or host installer queues that bundle
  for the root-owned updater, which reloads the Quadlet definitions.
- A Git repository contains only the small release-channel descriptor. It does
  not replace the OCI build or carry multi-gigabyte image archives in normal
  Git history.
- SQLite and external PostgreSQL remain supported; the PostgreSQL container is
  only installed for the bundled database choice.
- Backups target host bind mounts, so they survive image replacement.
