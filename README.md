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

---

## Architecture Overview

```
User Browser
    │
    ▼ (HTTP / WebSockets)
FastAPI Web App & Reverse Proxy (:8000)
    ├── Auth Engine (JWT / Session Cookies)
    ├── SQLite / PostgreSQL Database
    └── Podman Orchestrator
            │
            ├── vscode-empty container    <── Mount: /var/lib/devcloud/workspaces/{uid}/{wsid}
            ├── vscode-python container   <── Mount: /var/lib/devcloud/workspaces/{uid}/{wsid}
            ├── vscode-react container    <── Mount: /var/lib/devcloud/workspaces/{uid}/{wsid}
            ├── jupyter-python container  <── Mount: /var/lib/devcloud/workspaces/{uid}/{wsid}
            └── vscode-java container     <── Mount: /var/lib/devcloud/workspaces/{uid}/{wsid}
```

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

For a secure public hostname, follow [the Cloudflare Tunnel deployment guide](CLOUDFLARE.md).
For Rocky/RHEL enforcing mode, follow [the SELinux deployment guide](SELINUX.md).

### 1. Automated Deployment Script
We provide an automated setup script that installs Podman, configures permissions, builds workspace container images, and sets up a systemd service:

```bash
# Make script executable and run
chmod +x deploy/deploy.sh
./deploy/deploy.sh
```

### 2. Manual Linux VM Setup

#### Step 1: Install Podman & Python
```bash
# On Ubuntu / Debian:
sudo apt-get update
sudo apt-get install -y podman python3 python3-pip python3-venv git curl

# On RHEL / CentOS / Rocky Linux / Fedora:
sudo dnf install -y podman python3 python3-pip git curl policycoreutils-python-utils selinux-policy-targeted
```

#### Step 2: Configure Workspace Storage Directory
```bash
DEVCLOUD_SERVICE_USER="$USER" bash deploy/configure_selinux.sh
```

#### Step 3: Build Container Images
```bash
chmod +x containers/build_images.sh
./containers/build_images.sh
```

#### Step 4: Configure Systemd Service
Copy and edit `deploy/devcloud.service` into `/etc/systemd/system/`:
```bash
sudo cp deploy/devcloud.service /etc/systemd/system/devcloud.service
# Replace {{USER}} and {{PROJECT_DIR}} in the file
sudo systemctl daemon-reload
sudo systemctl enable --now devcloud
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

DevCloud supports installation on isolated Linux x86_64 VMs. The target must
already have its operating-system prerequisites (Python 3.11+, pip/venv,
Podman, sudo/systemd, and SELinux tooling when enforcing).

### Step 1: Prepare the Offline Bundle (On Connected Machine)
Commit the source first, then run the fail-fast packager on an
internet-connected machine. Select the exact CPython major/minor installed on
the target VM:

```bash
git status
python3 deploy/package_offline.py --python-version 3.12
```

This generates an ignored archive and checksum under `dist/`. Upload those two
files to a Git release or artifact repository; do not commit the multi-gigabyte
archive to normal Git history.

### Step 2: Deploy on the Air-Gapped Linux VM
Transfer both generated files to the target Linux VM via approved media, verify
the outer checksum, extract it, and execute the offline installer:

```bash
sha256sum -c devcloud-offline-*.tar.gz.sha256
tar -xzf devcloud-offline-*.tar.gz
cd devcloud

python3 deploy/package_offline.py --verify . --check-runtime
bash deploy/deploy_offline.sh
```

A connected deployment can also publish bundles at `/download/`. Administrators
can trigger a verified background rebuild from the Admin page after explicitly
enabling the download settings documented in [AIRGAP.md](AIRGAP.md).

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
├── containers/                  # Containerfiles for workspace images
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
