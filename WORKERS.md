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

The only required worker-to-controller firewall flow is outbound TCP 443.
Never expose Podman, workspace ports, or a worker management listener.
