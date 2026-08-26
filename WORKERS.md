# DevCloud CPU Worker Setup

DevCloud workers never accept inbound workspace or management connections. Each
worker opens one persistent WebSocket Secure connection to the master on TCP
443. HTTP, custom-port and WebSocket workspace traffic returns through that
connection.

## Master

1. Run the master with one Uvicorn process. Agent connections are currently
   stored in process memory; `deploy/devcloud.service` is configured accordingly.
2. Expose the normal DevCloud hostname on HTTPS/WSS 443 and retain the long
   proxy timeouts from `deploy/nginx.conf.example`.
3. Open **Yönetim → Worker Node'ları**, create `cpu-worker-01` and
   `cpu-worker-02`, and retain each node token. The UI displays it only once.
4. Once a worker connects, verify that its state is `online`, capacity is
   populated, and scheduling is enabled.

## Worker

Install the same DevCloud checkout, Python dependencies, Podman images and
workspace storage/SELinux configuration as the original single-host setup. The
worker does not run `app.main`; it runs `app.worker_agent`.

Copy `deploy/devcloud-worker.service` to systemd and replace `{{USER}}` and
`{{PROJECT_DIR}}`. Put the enrollment values in `/etc/devcloud/worker.env`
with mode `0600`:

```ini
DEVCLOUD_MASTER_URL=https://devcloud.example.com
DEVCLOUD_NODE_ID=<node-id>
DEVCLOUD_NODE_TOKEN=<node-token>
# Optional mTLS values:
# DEVCLOUD_AGENT_CA_FILE=/etc/devcloud/pki/ca.pem
# DEVCLOUD_AGENT_CERT_FILE=/etc/devcloud/pki/worker.pem
# DEVCLOUD_AGENT_KEY_FILE=/etc/devcloud/pki/worker-key.pem
```

Then start the agent:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now devcloud-worker
sudo journalctl -u devcloud-worker -f
```

### Air-gapped worker installation

The admin page publishes a separate `devcloud-worker-offline-*.tar.gz` archive.
It contains the worker agent source, the compatible Python wheel set, all five
workspace images, Rocky 10 or RHEL 10 bootstrap RPMs, a role-marked artifact manifest, and
`deploy/deploy_worker_offline.sh`.

After creating the node and retaining its one-time token on the master:

```bash
curl -fsSL https://master.example.com/download/install-worker.sh | sudo bash
```

The bootstrapper downloads the latest worker archive and checksum from that
master, verifies them, installs under `/opt/devcloud-worker`, and requests the
node ID and token through `/dev/tty` so the token is not placed in shell history
or the process list. For non-interactive provisioning, create
`/etc/devcloud/worker.env` first.

The bootstrap and worker connection base URL is managed from the Master's
**Admin > Çevrim Dışı İndirmeler** card. Its initial value is
`http://10.253.6.189`.

The manual transfer workflow remains available for fully isolated hosts:

```bash
sha256sum -c devcloud-worker-offline-*.tar.gz.sha256
tar -xzf devcloud-worker-offline-*.tar.gz
cd devcloud-worker

sudo install -d -m 0755 /etc/devcloud
sudo install -m 0600 deploy/worker.env.example /etc/devcloud/worker.env
sudo vi /etc/devcloud/worker.env
sudo bash deploy/deploy_worker_offline.sh
```

The installer refuses a master bundle, validates the Rocky/RHEL distribution,
installs missing Python, Podman, `crun`, and SELinux packages from verified local
RPMs, validates all bundled artifacts, loads the Podman images, installs the
outbound worker service, and starts it without opening an inbound port. Both
Rocky and RHEL bundles install `subscription-manager` for Foreman/Katello or
Red Hat registration, but do not register the host automatically.

The required firewall flow is worker to master TCP 443. No Podman socket,
workspace port or worker management port should be exposed.

## TLS

The long-lived, node-specific token authenticates the application-level
connection and can be rotated from the admin UI. For mTLS,
issue each worker a separate client certificate and configure the optional
`DEVCLOUD_AGENT_CA_FILE`, `DEVCLOUD_AGENT_CERT_FILE`, and
`DEVCLOUD_AGENT_KEY_FILE` variables. The TLS terminator in front of the master
must verify the client certificate for the agent connection. Keep the node token
enabled as a second credential.

## Scheduling and drain

The master places new workspaces on an online worker with enough free CPU and
RAM. Once any worker is registered, DevCloud will not silently fall back to the
master when all workers are offline. Select **Drain Et** before worker
maintenance; existing workspaces remain pinned to that worker and no new ones
are assigned.
