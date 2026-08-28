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

Authenticate to the Red Hat registry and pull PostgreSQL 16, then export both
images with checksums:

    podman login registry.redhat.io
    podman pull registry.redhat.io/rhel10/postgresql-16:latest
    bash deploy/container/export-offline-images.sh

The resulting dist/container-images directory can be copied into the air gap
for manual use. The standard server air-gap bundle builder embeds the same two
archives under offline/controller-images, records their sizes and SHA-256
digests in the release manifest, and the interactive installer loads them
automatically.

## Lifecycle rules

- Database migrations run before every controller start and are idempotent.
- Application startup uses one Uvicorn process because worker tunnels are
  still process-local.
- In-application source updates are disabled for container installations.
  Run the host installer with a verified release bundle; it loads the new image
  archive and restarts the Quadlet service.
- SQLite and external PostgreSQL remain supported; the PostgreSQL container is
  only installed for the bundled database choice.
- Backups target host bind mounts, so they survive image replacement.
