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

When an offline artifact manifest is present, the behavior is stricter: the
installer verifies and installs the bundled RPM closure with every repository
disabled before it starts the Python UI. It then verifies the complete artifact
manifest and installs wheels without an index. Both bundle roles include the
immutable rootful worker image; the server bundle also includes controller and
PostgreSQL images. Base bundles do not contain workspace images. It never
falls back to a connected repository for that installation.

## Controller

Choose SQLite for the smallest footprint, bundled PostgreSQL for a controller
expected to manage several workers, or an external PostgreSQL URL for an
operator-managed database.

The recommended controller runtime is an immutable OCI image supervised by
Podman Quadlet. The optional bundled PostgreSQL database is a second container
on a private network and is never published on a host port. Native Python
systemd installation remains available for existing deployments and
compatibility.
Connected installs pull versioned runtime images from
`quay.io/aaslangoren/devcloud` by default; air-gapped installs load the same
images from their verified bundle archives.

After installation, add each workspace image under **Admin > Workspace
Image'ları** from a Quay/internal-registry reference or an OCI/Docker tar
archive. The controller normalizes and verifies the archive, then enrolled
workers download it over the authenticated controller connection. Create worker
records under **Admin > Worker'lar** and retain each one-time enrollment token.
For a managed template, disabling its active image removes that template from
the user creation menu and blocks direct create API requests; enabling an image
restores it. Resource flavors can be enabled or disabled independently under
**Admin > Çalışma Alanları**. These controls affect new workspaces only, so an
existing workspace remains startable and restartable with its saved template
and flavor.

## All-in-one

All-in-one installs the selected controller runtime and the rootful worker
container. The installer creates one node ID/token pair, seeds
that node in the database, and connects the worker over loopback. This is a
convenience deployment, not a separate runtime architecture.
User-triggered workspace snapshots are supported only in this topology until
distributed registry export has its own explicit authorization workflow.

## Worker

New installations run the worker agent as a system Quadlet container. The
container uses host networking and the rootful host Podman API socket at
`/run/podman/podman.sock`; workspace containers and images therefore remain in
the host's root Podman store. The socket is root-equivalent, is mounted only
into the worker container, and does not require a privileged container. This
mode is intended for dedicated, trusted worker VMs. Existing native/rootless
worker installations retain their saved runtime during repair and update.

The exception is an automatically detected NVIDIA GPU worker. Its one-line
bootstrap validates the existing host driver, NVIDIA Container Toolkit, and
CDI devices before enrollment, then selects the native worker agent so those
host capabilities remain directly observable. DevCloud does not install or
modify any NVIDIA component. A failed GPU preflight exits before consuming the
single-use ticket. CPU workers retain the system Quadlet default.

After enrollment, set user GPU-slot quotas and choose the worker's physical GPU
sharing policy under **Admin > Worker Node'ları**. Zero means automatic: RTX
4090 uses two slots, RTX 5090 uses three, and other physical GPUs use one.
MIG instances always remain exclusive single slots.

When the worker can reach the controller, open **Admin > Worker Node'ları** and
select **Yeni Worker Kurulum Komutu Üret**. Run the generated command as root on
the worker within 10 minutes. The command is single-use. It asks only for the
worker name, creates the node and enrollment token through the controller, then
downloads and verifies the controller's current platform release before invoking
the unified installer. Set `DEVCLOUD_WORKER_NAME` to suppress the only prompt.

The reusable `/download/install-worker.sh` endpoint is intentionally disabled;
never place a permanent node token in a URL or shell command. If enrollment is
interrupted after the ticket is consumed, generate a fresh command in Admin.

A JSON answer file can be applied with:

    sudo bash deploy/devcloud-setup.sh --yes install worker \
      --answers /root/worker-answers.json

The token file and generated environment files must remain mode 0600.
Worker installation does not prompt for or preload workspace images. After the
agent connects, it reconciles the enabled controller image catalogue every 30
seconds, verifies archive size and SHA-256, loads missing images into rootful
host Podman through its socket, and reports exact synchronized checksums.
Scheduling waits until a worker reports the currently enabled checksum for the
requested template.

### Workspace AI and the on-prem model gateway

The maintained jupyter-python workspace image includes Jupyter AI 3, notebook
magic commands, Claude Code, and the Claude ACP adapter. This preserves the
agent architecture used by the former JupyterHub deployment: Jupyter AI opens
Claude as the default persona, while Claude Code calls an Anthropic-compatible
on-prem gateway. Every maintained VS Code image also includes Cline and receives
an OpenAI-compatible profile generated from the same central gateway record.

Configure the gateway once under **Admin > Entegrasyonlar > Workspace AI ·
Jupyter + Cline**. The controller encrypts the shared API key at rest. Every enrolled
worker fetches the central setting on startup and every 30 seconds, so a newly
installed worker needs no local Jupyter AI or Cline credential provisioning.
The same page manages the shared model catalogue, display names, descriptions,
and the model initially selected for a new Claude session. The default catalogue
contains the former on-prem Qwen model plus the GLM, DeepSeek, Qwen Coder, and
Kimi OpenRouter routes.
Use HTTPS for the controller-to-worker connection, or keep an HTTP deployment
on an isolated trusted management network, because workers must receive the
decrypted shared token before creating Jupyter containers.

The following `/etc/devcloud/worker.env` values remain available only as a
rolling-upgrade fallback when the controller has no central Jupyter AI record:

~~~ini
JUPYTER_AI_GATEWAY_URL=http://llm-gateway.internal.example:5003
JUPYTER_AI_MODEL=qwen3.6-35b
JUPYTER_AI_GATEWAY_TOKEN=replace-with-shared-gateway-token
JUPYTER_AI_GATEWAY_MODEL_DISCOVERY=false
JUPYTER_AI_MODEL_CATALOG_JSON=[]
~~~

The gateway URL is the root URL, for example
`http://llm-gateway.internal.example:5003`. Do not append `/v1`: DevCloud and
Claude Code append `/v1/messages` and `/v1/models` as needed, while Cline uses
the same root at `/v1` through its OpenAI-compatible provider. A configured URL
ending in `/v1` therefore causes requests such as `/v1/v1/messages` and usually
returns HTTP 404. The root URL must be reachable from the workspace container
network. Do not use 127.0.0.1 unless the gateway runs inside the same workspace
container. Use routable internal DNS/IP, and install the internal CA in the
maintained workspace image when HTTPS uses a private certificate authority.

Use a restricted LiteLLM virtual key for workspaces, never the LiteLLM master
key. Jupyter AI launches Claude through the ACP adapter rather than calling the
gateway directly like an editor extension. The maintained image intentionally
lets that adapter use its bundled compatible Claude runtime; do not set
`CLAUDE_CODE_EXECUTABLE` unless the external CLI/adapter pair has been tested
together. The separately installed `claude` command remains available in the
workspace terminal.

When using that fallback, restart the worker after changing the file:

~~~bash
sudo systemctl restart devcloud-worker.service
~~~

Only newly created workspace containers receive changed environment values.
Existing containers must be recreated to pick up a changed gateway, token, or
model catalogue. Stop the workspace, run `podman rm -f <container-name>` on its
assigned worker, and start it again from DevCloud. The workspace directory is a
bind mount and is preserved. Do not use the dashboard Delete action for this
operation because Delete removes the persistent workspace storage.

The Admin default is only the initial model; it does not lock a workspace to a
single model. Users can switch among the centrally published routes from
Claude's model picker or with `/model`. Catalogue model IDs are sent to LiteLLM
unchanged, so they must exactly match configured aliases such as
`qwen3.6-35b` or `openrouter/provider/model`. Optional gateway discovery reads
LiteLLM's `/v1/models` endpoint. Claude Code only exposes dynamically discovered
gateway IDs beginning with `claude` or `anthropic`; use the explicit catalogue
for other LiteLLM and OpenRouter aliases.

After saving, select a catalogue entry and run **Seçili Modeli Test Et**. The
controller asks every enabled worker to send a real Anthropic-compatible
request with at most four output tokens using the saved gateway and token. A
successful result confirms worker-to-gateway routing, authentication, and model
alias resolution before a workspace is created. An HTTP 404 usually indicates
an extra `/v1`; a model-not-found response indicates that the catalogue ID does
not match a LiteLLM alias available to the shared key.

The shared gateway API key is forwarded as ANTHROPIC_AUTH_TOKEN to every Jupyter
workspace and written into Cline's managed provider state in every VS Code
workspace, so users can open either assistant without entering credentials.
This also means each workspace user can inspect and reuse the key. Use a
gateway credential intended for this shared audience, restrict it at the
gateway, and rotate it regularly.

In JupyterLab, open the chat panel and select Claude. Notebook magic commands
are also installed:

~~~python
%load_ext jupyter_ai_magic_commands
~~~

The current maintained workspace image is
`quay.io/aaslangoren/devcloud:jupyter-python-3.5.2`. DevCloud 3.5.5 continues to
use that image because no Jupyter image build context changed in the 3.5.3,
3.5.4, or 3.5.5 platform releases. Import it under **Admin > Workspace Image'ları** as the
source for the `jupyter-python` template. Enrolled workers then receive the
controller-managed archive automatically. A future platform release publishes
a new Jupyter tag only when the image definition or bundled dependencies change.
The four maintained VS Code images must likewise be rebuilt or republished after
this change so their baked-in Cline extension is present on enrolled workers.

## Updates

Production updates are build-once/deploy-many. Git is the release channel, not
the runtime artifact: every application release builds the controller and
worker OCI images once on a trusted builder, exports them into one signed
platform bundle, and deploys that same bundle without building on the VMs.
Workspace images are not embedded in platform bundles. The release publishes
the maintained Jupyter image separately for controller-managed import only when
its image build context changed; ordinary controller or worker updates reuse the
currently enabled workspace image.

The automated GitHub Actions release process, Rocky 10 runner requirements,
Quay secrets, signing configuration, release assets, and the machine-managed
`stable` channel are documented in [RELEASE.md](RELEASE.md).

On the release builder:

    bash deploy/container/build-controller-image.sh
    bash deploy/container/build-worker-image.sh
    python3 deploy/build_platform_update.py \
      --signing-key GPG_KEY_ID \
      --channel-output /tmp/release-channel/devcloud-update-channel.json \
      --channel-url https://artifacts.example/devcloud/devcloud-platform-update-VERSION-COMMIT.tar.gz

Commit only `devcloud-update-channel.json` to the selected `stable` branch (or
another configured branch/tag). Store the multi-gigabyte bundle in a Git
release, an internal artifact server, or beside the channel file for a local
repository. The descriptor pins its filename, size, and SHA-256.

The controller installer asks for the Git repository and branch/tag and stores
them in `/etc/devcloud/controller.env`. They can also be supplied directly:

    sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh --yes update \
      --source-type git \
      --repository https://git.example/devcloud.git \
      --ref stable

The same Git source or an uploaded bundle can be selected under **Admin >
System > Platform Güncelleme**. The controller Git form defaults to
`https://github.com/aydinguven/devcloud.git`, branch `stable`, with
**İmzasız güncellemeye izin ver** selected for the current internal release
workflow. Clear the checkbox to require a signature. The controller process
only writes a request; `devcloud-update.path` invokes the root-owned updater.
A pre-update database/configuration backup is created automatically. Worker
inventory reports the running agent version and OTA state through heartbeat
telemetry.

For an air-gapped or local update:

    sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh --yes update \
      --bundle /root/devcloud-platform-update-VERSION-COMMIT.tar.gz

Official releases contain `release.json` and `release.json.asc` and are
verified by `/etc/devcloud/release-keyring.gpg`. Unsigned updates require an
explicit UI checkbox and warning confirmation, or the CLI `--allow-unsigned`
break-glass option. Only use either bypass for a reviewed source you trust.

For an operator-reviewed development source ZIP:

    sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh --yes update \
      --bundle /root/devcloud-source.zip --allow-unsigned

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
