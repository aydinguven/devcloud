# DevCloud worker setup

Distributed worker behavior, enrollment, networking, scheduling, drain, and
failure semantics are documented in [ARCHITECTURE.md](ARCHITECTURE.md).
Installation and non-interactive answer files are documented in
[INSTALL.md](INSTALL.md).

The short path is:

1. Install the controller with the unified installer.
2. Under **Admin > Worker Node'ları**, generate a one-time worker installation
   command. It is valid for 10 minutes.
3. Run that exact command as root on the Rocky/RHEL 10 worker and enter only the
   worker name. The controller creates the node and token automatically.
4. Verify devcloud-worker.service is active and the node reports online.
5. Add workspace images under **Admin > Workspace Image'ları**. The worker
   downloads and verifies enabled versions automatically; no image archives are
   copied to the worker installation directory. The page shows download,
   verification, Podman load, ready, and failed status for every worker.

Platform updates are separate from workspace images. A clean controller install
publishes its verified platform release for initial worker setup. Later, after
the controller applies a signed platform bundle, it publishes that bundle to
enrolled workers; workers verify its SHA-256 and apply it through their
root-owned update queue.

The worker inventory displays each OTA phase and its detailed result. Selecting
**Güncelle** first checks the installed and published versions, then asks for
confirmation only when an update is available. A worker that already matches
the published release is reported as current without downloading or queuing the
bundle. Downgrades are blocked. If the root updater fails, the inventory shows
its final message and a bounded tail of the installer output instead of only a
generic error badge.

## Temporary unsigned OTA transition

Unsigned worker OTA is disabled by default. To temporarily enable it, set
`WORKER_OTA_ALLOW_UNSIGNED=true` in `/etc/devcloud/controller.env` and restart
`devcloud-controller`. Release downloads remain authenticated and SHA-256
verified, but the bundle signature is not checked. Restore the value to `false`
after signed releases and trusted keyrings are deployed.

Workers running v3.4.9 or older do not understand this controller approval and
still reject unsigned OTA. Upgrade each such worker once from its host:

```bash
sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh --yes update \
  --source-type git \
  --repository https://github.com/aydinguven/devcloud.git \
  --ref stable \
  --allow-unsigned
```

After that one-time transition, the worker inventory's **Güncelle** action can
apply later unsigned releases while the controller switch remains enabled.
Managed controller updates preserve this opt-in in
`/etc/devcloud/controller.env`.

The only required worker-to-controller firewall flow is outbound TCP 443.
Never expose Podman, workspace ports, or a worker management listener.

## Workspace AI connectivity and settings rollout

Jupyter AI and Cline settings are controller-managed. Every enrolled worker fetches the
shared LiteLLM root URL, restricted virtual key, default model, and exact model
catalogue on startup and every 30 seconds. New workers therefore need no local
Jupyter AI or Cline credential file.

Use **Admin > Entegrasyonlar > Workspace AI > Seçili Modeli Test Et** before
starting user workspaces. The controller sends a small real request through
every enabled worker, so the result validates the same worker-to-gateway network
path used by workspace containers. The gateway value must be a root URL such as
`http://llm-gateway:5003`, without `/v1`. Model IDs must exactly match the
LiteLLM aliases allowed for the shared key.

Maintained VS Code images contain `saoudrizwan.claude-dev`. At container
creation DevCloud writes both Cline's legacy state and current
`settings/providers.json`, selecting the OpenAI-compatible `<gateway>/v1`
profile with the Admin API key and model.

Settings are injected when a workspace container is created. To roll changed
settings into an existing workspace:

1. Stop the workspace in DevCloud.
2. On its assigned worker, run `podman rm -f <container-name>`.
3. Start the workspace again from DevCloud.

The workspace directory is bind-mounted and survives container recreation.
Do not use the dashboard Delete action as a recreation shortcut; Delete removes
the persistent workspace storage and releases its allocation.

## NVIDIA GPU workers and workspace allocation

Use the same one-line registration command for CPU and GPU workers. The script
detects NVIDIA display/3D PCI hardware automatically. Before it enrolls a GPU
worker, it performs read-only checks for:

- a working nvidia-smi driver interface with at least one physical GPU;
- NVIDIA Container Toolkit through nvidia-ctk;
- at least one nvidia.com/gpu CDI device.

DevCloud deliberately does not install, upgrade, or reconfigure the NVIDIA
driver, Container Toolkit, CDI, or MIG. If a check fails, the script prints the
failed prerequisite and exits before the one-time ticket is consumed. Correct
the host stack and run the same command again.

GPU bootstrap automatically selects the native worker agent so host NVIDIA and
CDI telemetry is available without an extra prompt. CPU bootstrap keeps the
containerized worker default. The Admin worker inventory reports the driver and
toolkit readiness, physical GPU model/count, live memory/utilization data, and
discovered MIG instances.

Users with a nonzero GPU-slot quota can select the `g1.shared` flavor. The
scheduler reserves one exact CDI device and a database-unique slot before
container creation. It never passes `nvidia.com/gpu=all`. Stopped and failed
workspaces retain their slot so restart returns to the same device; deleting
the workspace releases it.

Physical GPU sharing defaults are two workspaces for RTX 4090 and three for RTX
5090. Other physical GPUs default to one. Admins can set one, two, or three
slots per physical GPU in the worker inventory; zero means automatic. Each MIG
CDI slice is always one exclusive slot regardless of the override. On a shared
physical GPU, the flavor's VRAM value is scheduling guidance and does not
enforce a hard memory boundary. Use MIG on DGX/HGX systems when hard isolation
is required.
