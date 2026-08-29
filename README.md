# DevCloud - Self-Hosted Cloud Development Environment Platform

DevCloud is a lightweight, high-performance cloud development platform built with **Python (FastAPI)** and **Podman**. It enables teams to create, launch, and manage browser-accessible development environments (VS Code / code-server and JupyterLab) on a Linux VM with persistent storage and resource quotas.

---

## Key Features

- **Podman Container Orchestration**: Rootless container lifecycle management with memory & CPU quotas.
- **Persistent Workspace Storage**: Host bind-mounts maintain project files across container restarts and updates.
- **Integrated Browser IDEs**:
  - **VS Code (Empty Project)**: Clean environment with git and essential tools.
  - **VS Code (Python 3.14 / 3.12)**: Preloaded with Python, `pip`, `uv`, and the official Python/Jupyter VS Code extensions.
  - **JupyterLab (Python)**: Data science environment with NumPy, Pandas, Matplotlib, and interactive notebooks.
  - **VS Code (Java 21 LTS)**: Preloaded with OpenJDK 21, Maven, Gradle, and Red Hat Java Language Support.
- **Resource Flavors**:
  - `t1.nano`: 0.5 CPU, 512 MB RAM (Light scripts and utilities)
  - `t1.micro`: 1 CPU, 1 GB RAM (Default profile for newly registered users)
  - `t1.small`: 1 CPU, 2 GB RAM (Python, React/Node.js, and standard VS Code development)
  - `t1.medium`: 2 CPU, 4 GB RAM (Java, Jupyter, and medium builds)
  - `t1.large`: 4 CPU, 8 GB RAM (Heavy builds, data analysis, and multi-service projects)
  - `t1.xlarge`: 8 CPU, 16 GB RAM (Compute-intensive builds and large notebooks)

  The former `t1.mini` profile remains internally available for existing workspaces but is no longer offered for new deployments.
- **Modular Authentication**:
  - Internal Database Auth (Argon2 / Bcrypt + JWT + HTTP-only cookies).
  - Runtime-configured LDAPS / Active Directory authentication with encrypted bind credentials.
  - Allowed-user and administrator group mapping, including nested AD groups.
- **Built-in Reverse Proxy & WebSocket Tunneling**: Access all running workspaces without opening separate firewall ports for every container.
- **Modern Responsive Web UI**: Dashboard with dark theme, real-time container log viewer, and administrative oversight.
- **Resource Usage Dashboard**: Host CPU/RAM/disk utilization, per-user allocations, and remaining quota on the workspace dashboard.
- **Self-Service Registration**: New users can sign up from the login screen with a default allowance of 1 CPU core, 1 GB RAM, and 10 GB disk; admins can adjust individual quotas.
- **Per-User Quotas**: Admin-managed CPU, RAM, and persistent-disk limits with workspace deployment enforcement.
- **Outbound-Only CPU Workers**: Schedule workspaces across worker nodes without opening inbound worker or container ports.
- **MLflow Registry View**: Configure a server-side, read-only MLflow API connection and browse registered models, versions, aliases, and tags.

---

## Architecture Overview

```
User Browser ── HTTPS/WSS :443 ──> DevCloud Controller
                                      ├── UI / API / LDAP
                                      ├── Scheduler / Database
                                      ├── Workspace Port Proxy
                                      └── MLflow API Connector
                                                ▲
                                                │ outbound WSS :443
                           ┌────────────────────┴────────────────────┐
                           │                                         │
                    CPU Worker 01                            CPU Worker 02
                    ├── Worker Agent                         ├── Worker Agent
                    ├── Podman                               ├── Podman
                    └── Local workspace storage              └── Local workspace storage
```

Worker hosts expose no workspace ports. The controller keeps the public proxy URL,
authenticates each request, resolves the assigned node, and carries HTTP and
WebSocket streams over the worker-initiated tunnel. Every workspace is assigned
to a real worker. All-in-one installs that same worker beside the controller;
there is no controller-local runtime fallback. See [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Quick Start (Development)

### 1. Prerequisites
- Python 3.11+
- Virtualenv (`python -m venv .venv`)

### 2. Setup Virtual Environment & Install Dependencies
```bash
# Create and activate virtual environment
python -m venv .venv

# On Linux / macOS:
source .venv/bin/activate

# On Windows (PowerShell):
.\.venv\Scripts\Activate.ps1

# Install requirements
pip install -r requirements.txt
```

### 3. Run Development Server
```bash
python run.py
```
Visit `http://127.0.0.1:8000` in your browser.

- **Default Admin User**: `admin`
- **Default Admin Password**: `admin123`

### LDAPS / Active Directory

Sign in with the local administrator and open **Yönetim Paneli → Kurumsal
Dizin**. The form defaults to `ldaps.tcmb.gov.tr:686` and supports testing the
bind and user search before enabling directory login.

- Set a strong, persistent `SECRET_KEY` before saving the bind password. The
  application derives a Fernet key from it and never returns the saved password
  through the API or admin page.
- Keep TLS certificate validation enabled. For an internal CA, install it in the
  operating-system trust store or enter its server-side PEM path.
- `İzinli kullanıcı grubu DN` limits login to one AD group. Leaving it empty
  permits every successfully authenticated directory user.
- `DevCloud yönetici grubu DN` maps members to the DevCloud administrator role.
  Other directory users receive the standard user role.
- Nested group lookup uses Active Directory matching-rule-in-chain and can be
  disabled for non-AD LDAP servers.
- When directory login is enabled, public self-registration is disabled. The
  existing local administrator remains available as an emergency fallback.

---

## Production Deployment on Linux VM

New managed installations use the root-run interactive lifecycle installer:

    sudo bash deploy/devcloud-setup.sh

It supports controller, all-in-one, and worker roles plus update, repair,
status, backup, restore, and uninstall. See [INSTALL.md](INSTALL.md). The
older role-specific scripts below remain only as migration references for
installations created before the unified installer.

For a secure public hostname, follow [the Cloudflare Tunnel deployment guide](CLOUDFLARE.md).
For Rocky/RHEL enforcing mode, follow [the SELinux deployment guide](SELINUX.md).
For the controller plus CPU-worker topology, follow [the worker deployment guide](WORKERS.md).

### 1. Clean install from Git

Use this legacy flow on a pre-unified connected controller. Run it as the Linux account that
should own the DevCloud service; the script requests sudo only for operating
system changes:

```bash
git clone https://git.aydin.cloud/aydin/devcloud.git
cd devcloud
bash deploy/deploy.sh
```

The clean installer installs Python, Podman, Nginx, and SELinux prerequisites;
creates persistent workspace/download/ingress directories; installs Python
dependencies; installs the DevCloud service; and configures Nginx as the port
80 entrypoint. Workspace images have a separate lifecycle: import them later
from an OCI registry or archive under **Admin > Workspace Image'ları**. HTTPS
remains disabled until a valid certificate and key are uploaded from Admin.

Verify the clean installation:

```bash
systemctl status devcloud nginx devcloud-ingress.path --no-pager
curl -I http://127.0.0.1:8000/login
curl -I http://aifactory.tcmb.gov.tr/login
```

### 2. Update a managed installation

Managed production VMs do not run `git pull` and do not build images. A trusted
builder builds the controller and worker OCI images once and packages both in a
signed platform release. Git stores only the small checksum-pinned release
channel descriptor.

Apply a configured Git channel or a transferred bundle from the host:

```bash
sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh --yes update \
  --source-type git --repository https://git.example/devcloud.git --ref stable

sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh --yes update \
  --bundle /root/devcloud-platform-update-v3.4.4-COMMIT.tar.gz
```

The same two choices are available under **Admin > System > Platform
Güncelleme**. Signature verification remains the default. An administrator can
explicitly select **İmzasız güncellemeye izin ver** for a trusted internal Git
channel or reviewed local bundle; the UI presents a second warning before the
request is queued. A root-owned systemd service creates a pre-update backup,
loads immutable OCI archives, applies migrations, and restarts Quadlet
services. The controller then publishes the same release to enrolled workers
for authenticated OTA. Workspace images remain independently managed in the
image catalogue.

Verify the result:

```bash
systemctl status devcloud-controller devcloud-update.path --no-pager
podman ps
curl -fsS http://127.0.0.1:8000/healthz
```

See [RELEASE.md](RELEASE.md) for GitHub Actions release automation and runner
setup, [INSTALL.md](INSTALL.md) for manual release-builder commands, and
[AIRGAP.md](AIRGAP.md) for disconnected updates.

### 3. Manual Linux VM Setup

#### Step 1: Install Podman & Python
```bash
# On Ubuntu / Debian:
sudo apt-get update
sudo apt-get install -y podman python3 python3-pip python3-venv git curl nginx
sudo apt-get install -y pigz  # Optional: accelerates offline bundle compression

# On RHEL / CentOS / Rocky Linux / Fedora:
sudo dnf install -y podman python3 python3-pip git curl nginx policycoreutils-python-utils selinux-policy-targeted
sudo dnf install -y pigz  # Optional; repository availability varies
```

#### Step 2: Configure Workspace Storage Directory
```bash
DEVCLOUD_SERVICE_USER="$USER" bash deploy/configure_selinux.sh
```

#### Step 3: Add Workspace Images
```bash
# On a connected build host, only when maintaining local image definitions:
chmod +x containers/build_images.sh
./containers/build_images.sh
```

Import the resulting OCI/Docker archives, or a Quay/internal-registry image
reference, under **Admin > Workspace Image'ları**. Enrolled workers fetch the
enabled images from the controller; the base installation does not build them.

#### Step 4: Configure Systemd Service
Copy and edit `deploy/devcloud.service` into `/etc/systemd/system/`:
```bash
sudo cp deploy/devcloud.service /etc/systemd/system/devcloud.service
# Replace {{USER}} and {{PROJECT_DIR}} in the file
sudo systemctl daemon-reload
sudo systemctl enable --now devcloud
```

#### Step 5: Configure the Nginx Ingress

Install the root-owned ingress helper and watcher. This also configures the
initial HTTP port 80 entrypoint; HTTPS can then be enabled from Admin:

```bash
sudo bash deploy/install_ingress.sh "$USER"
```

#### Workspace-Safe Service Reload

Use the project helper after application updates instead of `systemctl restart`:

```bash
bash deploy/restart.sh
```

The helper sends `SIGHUP` to Uvicorn's manager so its workers load the new code
one at a time without stopping the systemd unit or its Podman workspace
supervisors. It bounds the reload/start wait, verifies
`http://127.0.0.1:8000/login`, and prints service logs if the application does
not become healthy. The installed unit also supports `sudo systemctl reload
devcloud` after `deploy/devcloud.service` has been copied into place.

---

## Air-Gapped / Offline Installation

DevCloud supports installation on Rocky Linux 10.x and RHEL 10.x x86_64 VMs
that are isolated from the internet but can reach an internal Satellite or
Foreman/Katello service. New bundles include a small distribution-matched local
DNF repository only for bootstrapping `subscription-manager`. Python, Podman,
`crun`, Nginx, SELinux tooling, and database packages are installed from the
repositories enabled by the administrator's Satellite/Foreman registration.
The base VM still needs DNF, systemd, `sudo`, `tar`, and `sha256sum`.

### Step 1: Prepare the Offline Bundle (On Connected Machine)
Commit the source first, then run the fail-fast packager on an
internet-connected Rocky 10 or RHEL 10 machine matching the target distribution.
Its enabled DNF repositories must be reachable; a RHEL builder must have access
to the entitled RHEL repositories. Install `createrepo_c` on the builder, then
select the target CPython major/minor:

```bash
sudo dnf install -y createrepo_c
git status
python3 deploy/package_offline.py --python-version 3.12
```

This generates an ignored archive and checksum under `dist/`. Upload those two
files to a Git release or artifact repository; do not commit the multi-gigabyte
archive to normal Git history.

Workspace images are deliberately excluded from both controller and worker
base bundles. The worker runtime image is included in both roles; server
bundles also include controller and PostgreSQL runtime images. To transport
the maintained workspace image set separately, run:

```bash
python3 deploy/package_workspace_images.py
```

Transfer and verify that archive independently, extract it, and upload each
archive from its `images/` directory under **Admin > Workspace Image'ları**.
You may instead import the corresponding Quay or internal-registry references.

New bundles use `.tar.gz` archives to keep transfer and storage size practical.
The builder uses multi-core `pigz` when available and falls back to Python's
standard gzip writer otherwise. Existing uncompressed `.tar` downloads remain
supported during migration.

### Step 2: Deploy on the Air-Gapped Linux VM
Transfer both generated files to the target Linux VM via approved media, verify
the outer checksum, extract it, and execute the offline installer:

```bash
sha256sum -c devcloud-offline-*.tar.gz.sha256
tar -xzf devcloud-offline-*.tar.gz
cd devcloud

sudo bash deploy/devcloud-setup.sh
```

On the first run, the installer verifies the bootstrap RPM repository, disables
all other repositories for that one transaction, and installs only
`subscription-manager` from the bundle's `file://` repository. It then stops
so the administrator can register the VM using the normal organization and
activation-key workflow:

```bash
subscription-manager register --org='YOUR_ORG' --activationkey='YOUR_KEY'
dnf repolist
sudo bash deploy/devcloud-setup.sh
```

The second run installs all remaining operating-system prerequisites from the
registered Satellite/Foreman repositories, verifies the full offline manifest,
and continues into the interactive installer.

A connected deployment can also publish separate controller and CPU-worker bundles
at `/download/`. Administrators can trigger either verified background rebuild
from the Admin page after enabling the download settings documented in
[AIRGAP.md](AIRGAP.md).

After controller installation, open **Admin > Worker Node'ları** and generate a
10-minute, single-use installation command. Run the generated command as root on
the new worker. The script asks only for the worker name; the controller creates
the node and enrollment token, then serves the checksummed platform release.

The initial bootstrap/controller address is `http://10.253.6.189` and can be
changed without restarting DevCloud from the Offline Downloads card in Admin.

### In-app HTTPS and certificate upload

A clean connected or offline controller installation configures Nginx on port 80.
In **Admin > Çevrim Dışı İndirmeler > HTTPS & Sertifika Yönetimi**, set the
hostname, upload the PEM certificate chain and its unencrypted PEM private key,
then enable HTTPS. DevCloud verifies the certificate validity period, server
authentication usage, SAN coverage, and certificate/key match before applying
the Nginx configuration. The private key is stored only in the restricted
ingress directory and is never returned by the API.

For the planned deployment, the certificate SAN must contain
devcloud.example.com, DNS must resolve that name to the controller, and clients
and workers must trust TCMB-CA. Keep **Port 80 HTTP fallback** enabled while
that trust is being rolled out. The fallback deliberately does not enable HSTS.
Disabling fallback changes port 80 to a permanent redirect to HTTPS.

The generated Nginx configuration preserves the /proxy/... path, WebSocket
upgrade headers, and long-lived workspace sessions. Git-based updates install
the ingress watcher automatically. The preserve-existing command in the update
section is only needed if the host was upgraded using an older updater.

Verify both the application and the workspace proxy after applying a
certificate:

    curl -I http://aifactory.tcmb.gov.tr/login
    curl --cacert /path/to/tcmb-ca.pem -I https://aifactory.tcmb.gov.tr/login
    sudo nginx -t
    sudo systemctl status nginx devcloud-ingress.path --no-pager

See [AIRGAP.md](AIRGAP.md) for prerequisites, multi-version bundles, Git release
publishing, SELinux notes, and validation commands.

---

## Directory Structure

```
intelligent-nobel/
├── app/
│   ├── main.py                  # FastAPI entrypoint, lifespan & router mounts
│   ├── config.py                # Pydantic Settings & environment variables
│   ├── database.py              # Async SQLAlchemy engine & session factory
│   ├── models/                  # User and Workspace SQLAlchemy models
│   ├── schemas/                 # Pydantic request/response schemas
│   ├── auth/                    # Internal + runtime-configured LDAP/AD authentication
│   ├── orchestrator/            # Podman CLI manager, Flavors, and Templates
│   ├── proxy/                   # Streaming Reverse Proxy & WebSocket router
│   ├── routes/                  # REST APIs (Auth, Workspaces, Admin) & HTML pages
│   ├── static/                  # CSS and JavaScript frontend logic
│   └── templates/               # Jinja2 HTML templates
├── containers/                  # Controller, worker, and workspace Containerfiles
│   ├── build_images.sh          # Podman batch builder
│   ├── vscode-empty/
│   ├── vscode-python/
│   ├── vscode-react/
│   ├── jupyter-python/
│   └── vscode-java/
├── deploy/                      # Linux VM deployment scripts & systemd unit
├── tests/                       # Pytest test suite (Auth, Workspaces, Podman, Proxy)
├── requirements.txt
├── run.py                       # Local launcher script
└── README.md
```

---

## API Endpoints

| Method | Endpoint | Description | Auth Required |
|---|---|---|---|
| `POST` | `/api/auth/register` | Register a new user | No |
| `POST` | `/api/auth/login` | Login and receive JWT / session cookie | No |
| `POST` | `/api/auth/logout` | Clear session cookie | Yes |
| `GET` | `/api/auth/me` | Get current user profile | Yes |
| `GET` | `/api/workspaces/templates` | List all available templates | No |
| `GET` | `/api/workspaces/flavors` | List selectable resource flavors (`t1.nano` through `t1.xlarge`) | No |
| `GET` | `/api/workspaces` | List current user's workspaces | Yes |
| `POST` | `/api/workspaces` | Create & deploy new workspace container | Yes |
| `GET` | `/api/workspaces/usage` | Host usage and current user's quota summary | Yes |
| `GET` | `/api/workspaces/{id}` | Workspace detail | Yes |
| `POST` | `/api/workspaces/{id}/start` | Start container | Yes |
| `POST` | `/api/workspaces/{id}/stop` | Stop container | Yes |
| `DELETE` | `/api/workspaces/{id}` | Delete workspace container | Yes |
| `GET` | `/api/workspaces/{id}/logs` | Fetch container logs | Yes |
| `GET` | `/proxy/{id}/{path}` | Reverse proxy to container IDE | Yes (Owner/Admin) |
| `GET` | `/api/admin/users` | List all users | Yes (Admin) |
| `PUT` | `/api/admin/users/{id}/quota` | Update a user's CPU/RAM/disk quota | Yes (Admin) |
| `GET` | `/api/admin/workspaces` | List all workspaces | Yes (Admin) |
| `GET` | `/api/admin/stats` | System statistics | Yes (Admin) |

---

## Running the Automated Test Suite

Run the full pytest suite with:
```bash
pytest -v
```
