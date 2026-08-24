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
  - `t1.micro`: 1.0 CPU, 1 GB RAM (Standard single-threaded development)
  - `t1.mini`: 2.0 CPU, 2 GB RAM (Java builds and multi-threaded data tasks)
- **Modular Authentication**:
  - Internal Database Auth (Argon2 / Bcrypt + JWT + HTTP-only cookies).
  - Pluggable `AuthProvider` interface ready for Active Directory / LDAP integration.
- **Built-in Reverse Proxy & WebSocket Tunneling**: Access all running workspaces without opening separate firewall ports for every container.
- **Modern Responsive Web UI**: Dashboard with dark theme, real-time container log viewer, and administrative oversight.
- **Resource Usage Dashboard**: Host CPU/RAM/disk utilization, per-user allocations, and remaining quota on the workspace dashboard.
- **Self-Service Registration**: New users can sign up from the login screen with a default allowance of 1 CPU core and 1 GB RAM; admins can adjust individual quotas.
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

---

## Production Deployment on Linux VM

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
sudo dnf install -y podman python3 python3-pip git curl
```

#### Step 2: Configure Workspace Storage Directory
```bash
sudo mkdir -p /var/lib/devcloud/workspaces
sudo chown -R $USER:$USER /var/lib/devcloud/workspaces
sudo chmod 755 /var/lib/devcloud/workspaces
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

DevCloud provides native support for 100% offline installation on isolated Linux VMs without internet access.

### Step 1: Prepare the Offline Bundle (On Connected Machine)
Run the offline packaging script to download all Python wheels and export all Podman container images to tar archives:

```bash
# On Linux / macOS:
chmod +x deploy/package_offline.sh
./deploy/package_offline.sh

# Or cross-platform via Python:
python deploy/package_offline.py
```
This generates:
- `offline/wheels/`: All downloaded `.whl` dependency packages.
- `offline/images/`: Exported `.tar` container images for all 5 environments (`vscode-empty`, `vscode-python`, `vscode-react`, `jupyter-python`, `vscode-java`).
- `devcloud-offline-bundle.tar.gz`: Self-contained deployable archive.

### Step 2: Deploy on the Air-Gapped Linux VM
Transfer `devcloud-offline-bundle.tar.gz` to the target Linux VM via USB / SCP, extract it, and execute the offline installer:

```bash
# Extract bundle
tar -xzf devcloud-offline-bundle.tar.gz
cd devcloud

# Run offline installer (loads images into Podman & installs wheels without internet)
chmod +x deploy/deploy_offline.sh
./deploy/deploy_offline.sh
```

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
│   ├── auth/                    # Modular Auth (Internal DB + Active Directory stub)
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
| `GET` | `/api/workspaces/flavors` | List resource flavors (`t1.nano`, `t1.micro`, `t1.mini`) | No |
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
