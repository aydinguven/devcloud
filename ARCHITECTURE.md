# DevCloud deployment architecture

## Supported topology

Distributed deployment is the canonical architecture. It has two roles:

- **Controller**: UI/API, authentication, scheduler, database access, ingress,
  worker control tunnels, release publication, and HTTP/WebSocket proxying.
  It never starts workspace containers or reads workspace files.
- **Worker**: outbound agent, Podman, images, and persistent workspace storage.
  Workers need no inbound management or workspace ports.

An **all-in-one** installation is the same controller and worker services on
one VM. The local worker is enrolled in the database with an ordinary node ID
and token. There is no special local runtime or nullable placement: every
workspace has a worker assignment.

Enterprise controller HA is intentionally outside the current target. The
controller runs one Uvicorn process because live agent tunnels are held in
process. A future HA design must externalize connection ownership and event
routing before adding controller replicas.

The preferred controller packaging is an OCI image supervised by Podman
Quadlet. The optional bundled PostgreSQL database is a separate container on a
private network; Nginx remains host-managed. Workers remain host-native because
they own workspace storage and require root-equivalent Podman control. See
CONTAINERS.md for the packaging boundary and offline image workflow.

## Request and control paths

    Browser -- HTTPS/WSS --> Controller -- worker tunnel --> Worker --> Workspace
                                  |
                                  +--> SQLite or PostgreSQL

Workers initiate the long-lived WSS connection. The controller authenticates
the node token and carries commands, file operations, backups, HTTP streams,
custom ports, and WebSockets over that connection. A worker heartbeat includes
capacity, utilization, capabilities, and its complete managed-container
inventory. The controller records reconciliation drift without deleting
unrecognized worker data.

## Placement and failure behavior

The scheduler considers only enabled, schedulable, connected workers with
sufficient CPU and memory. Reserved resources and live utilization contribute
to the score. Host ports are unique per worker, not globally.

If no worker is available, workspace creation fails with HTTP 503; it never
falls back to the controller. Existing workspaces remain pinned to their
worker. Deleting a worker is blocked while any workspace remains assigned,
including stopped workspaces. Drain a worker before maintenance. Automated
cross-worker data migration is not in the current target; do not reassign node
IDs without an operator-controlled backup/restore process. Remove assigned
workspaces before deleting a worker.

Losing a controller temporarily removes management and proxy access, but
Podman containers continue according to their restart policy. Losing a worker
removes access to the workspaces and local storage on that worker.

## Database choices

- **SQLite**: smallest installation and suitable for all-in-one or a modest
  single-controller deployment. Store it on durable local storage and back it
  up.
- **Bundled PostgreSQL**: installed and managed on the controller VM. This is
  the recommended default when several workers are expected.
- **External PostgreSQL**: operator-managed and supplied as a
  postgresql+asyncpg URL.

The application uses explicit, versioned migrations. Managed installations run
migrations before service startup and disable schema mutation in Uvicorn.
Legacy workspaces without a worker can be migrated automatically only when
there is exactly one unambiguous target worker.

## Releases and reconciliation

Managed releases are immutable version-and-revision directories under
/var/lib/devcloud/releases, selected by /opt/devcloud/current. Updates stage
the new release atomically, verify its
manifest/signature, run migrations, refresh systemd units, and restart the
selected services.

Authenticated administrators may upload a signed release or explicitly approve
an unsigned Git source ZIP. The web process only writes a queue;
devcloud-update.path invokes a root-owned one-shot service to apply it. Workers
obtain published releases through node-authenticated endpoints, validate
SHA-256, and use the same root-owned queue.

## Images and storage

Base images are loaded or built on each worker and may be preloaded in an
air-gap bundle. Administrator-built custom images require an external registry
prefix so all workers can pull them. Workspace snapshots remain all-in-one
only: publishing a snapshot could export user data and therefore requires a
future, separately authorized registry workflow. Workspace data remains on the
assigned worker unless external storage is mounted at the configured workspace
root.

## Network flows

- Browser to controller: TCP 443, and optionally 80 for redirect/fallback.
- Worker to controller: TCP 443 outbound.
- Controller to PostgreSQL, LDAP, MLflow, and registry as configured.
- Worker to image registry and package repositories as configured.

Do not expose Podman sockets, worker workspace ports, or worker management
ports.
