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
   `cpu-worker-02`, and retain each one-time enrollment token.
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
DEVCLOUD_NODE_TOKEN=<one-time-enrollment-token>
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

The required firewall flow is worker to master TCP 443. No Podman socket,
workspace port or worker management port should be exposed.

## TLS

The enrollment token authenticates the application-level connection. For mTLS,
issue each worker a separate client certificate and configure the optional
`DEVCLOUD_AGENT_CA_FILE`, `DEVCLOUD_AGENT_CERT_FILE`, and
`DEVCLOUD_AGENT_KEY_FILE` variables. The TLS terminator in front of the master
must verify the client certificate for the agent connection. Keep the enrollment
token enabled as a second, node-specific credential.

## Scheduling and drain

The master places new workspaces on an online worker with enough free CPU and
RAM. Once any worker is registered, DevCloud will not silently fall back to the
master when all workers are offline. Select **Drain Et** before worker
maintenance; existing workspaces remain pinned to that worker and no new ones
are assigned.
