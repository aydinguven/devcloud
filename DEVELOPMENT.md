# DEVELOPMENT

## Project snapshot

- DevCloud: self-hosted browser IDE platform (FastAPI + Podman), controller plus outbound-only CPU/GPU workers, MLflow model serving.
- Source: `https://github.com/aydinguven/devcloud` (GitHub by owner's choice; not on Forgejo). Default branch `main`, latest release `v3.9.2`. Tag `v3.9.3` exists but its release run was cancelled (CI broken by SQLAlchemy 2.1, see known issues); 3.9.4 supersedes it.
- Release process: bump `app/__init__.py` `__version__` via PR, then push tag `vX.Y.Z` on the merged `main` commit. `.github/workflows/release-platform.yml` builds the bundles, creates the GitHub Release and advances the `stable` update channel.
- Deployment: own installer/offline bundle (`INSTALL.md`, `AIRGAP.md`, `WORKERS.md`); not a Tupperware project.
- Local setup: `python -m venv .venv`, `.venv\Scripts\python -m pip install -r requirements.txt`, run `python run.py` (http://127.0.0.1:8000).
- Tests: `.venv\Scripts\python -m pytest -q`.

## Active work

| Owner | Status | Branch | Task | Next step |
|---|---|---|---|---|
| Kiro | 3.9.5 merged (PR #29) | — | Controller DB access independent of Podman DNS | Tag `v3.9.5`/update IDMVAIFACT1, then delete the manual Quadlet drop-ins (see Latest handoff) |
| Kiro | PR open | `feat/grouped-user-quotas` | 3.10.0: admin users grouped by AD `department`, per-member team quota, per-field user override, editable default group (schema v25) | Merge, tag `v3.10.0`, update and review the Users panel |

## Decisions

- 2026-09-29: The model-image scheduling wait follows real worker sync progress (stall limit 5 min, hard limit 30 min, 2 min once the image is on a worker but capacity is missing) instead of a fixed 2-minute window. Multi-GB images are pulled on a 30 s worker poll, then verified and `podman load`ed.

- 2026-10-06: The bundled PostgreSQL gets a fixed IP (`broadcast - 5`, e.g. `10.89.0.250`) on the `devcloud` network and the controller resolves `devcloud-postgresql` through `AddHost`, so database access never depends on aardvark-dns. IPv6-only or unreadable networks keep the DNS-only behaviour.
- 2026-10-06: Ingress changes firewalld permanently and at runtime; never `firewall-cmd --reload`, which discards netavark's runtime rules. The installer enables `netavark-firewalld-reload.service` when present.

- 2026-10-06: Quotas resolve per field as user override -> team group (`users.team`, AD `department`, normalized by `app.quotas.team_key`) -> default group (`user_group_quotas.group_key = ''`). A team quota applies to each member individually, not as a shared budget. Enforcement reads only `app/quotas.py`; the legacy `users.*_quota` columns are no longer read. Migration 25 turns every legacy value that differs from the configured default into an override, so effective quotas do not change on upgrade.

## Known issues and risks

- `requirements.txt` uses open ranges. SQLAlchemy 2.1 no longer installs `greenlet`, which broke CI and the release build's pytest step on 2026-09-29; fixed by requiring `sqlalchemy[asyncio]`. Other packages can still drift on a fresh install; consider exact pins or a constraints file.
- The deployment background worker processes jobs one at a time, so a long image-sync wait delays other queued deployments. This was already the case during image builds.
- `tests/test_mlflow_model_build.py::test_worker_unit_exposes_the_virtualenv_console_scripts_on_path` fails on Windows only (it asserts `os.sep` inside a Linux unit file).

## Validation

- 2026-10-06: Full suite 368 passed, 3 skipped. Scratch checks: installer renders `IP=10.89.0.250` / `AddHost=devcloud-postgresql:10.89.0.250` for an existing `10.89.0.0/24` network, creates the network first when missing, and skips pinning for IPv6-only; `/readyz` returns 503 `gaierror` for an unresolvable DB host; an unhandled `/api` error returns JSON 500; a fake AD rejecting the service bind now yields 503 with a readable message instead of "wrong password".
- Still required for 3.9.5: an update on IDMVAIFACT1 confirming PostgreSQL comes up on the pinned IP and the controller is healthy.

- 2026-09-29: MLflow, node and lifecycle test files pass: 57 passed, 2 skipped, 1 Windows-only failure (listed above).
- 2026-09-29: A scratch smoke run confirmed the original code crashes on its second scheduling attempt (expired ORM row after rollback), while the fix retries, logs sync progress and returns a placement. The stall path raises a readable error.
- Still required: a real deployment of a new model version on a worker.

## Latest handoff

2026-10-06 production incident (IDMVAIFACT1, 3.9.4): logins returned intermittent HTTP 500 (`Unexpected token 'I'` on the login page), the dashboard 500'd and the worker tunnel flapped. Cause: `firewall-cmd --reload` (run by `apply_ingress.py` on every ingress apply, plus the `yum upgrade` of firewalld) removed netavark's runtime trusted-zone source `10.89.0.0/24`, so container DNS to `10.89.0.1:53` was dropped and `devcloud-postgresql` stopped resolving (`socket.gaierror`). The controller survived on one pooled connection, so `/readyz` stayed green. Restoring needed `podman network reload --all`, a restart, and manual drop-ins `/etc/containers/systemd/devcloud-postgresql.container.d/10-static-ip.conf` (`IP=10.89.0.250`) and `devcloud-controller.container.d/10-db-host.conf` (`AddHost=devcloud-postgresql:10.89.0.250`). 3.9.5 renders the same settings, so delete those two drop-ins after updating.

Earlier handoff:

`app/orchestrator/mlflow_deployment_service.py` `_reserve_when_image_synced`: the old retry loop reused `user` and `image` rows after `admission_transaction` rolled back the session. On AsyncSession that raises, so any deployment whose image was not already on a worker failed on the second attempt. A re-deploy worked because it reused the cached image that had synced by then. The fix reloads the rows each attempt, waits on worker progress and renews the deployment lease during the wait.

## Session log

- 2026-10-06: Diagnosed the production login 500s down to firewalld reloads breaking Podman DNS (see Latest handoff); restored service manually; prepared 3.9.5 with the permanent fix and login error hardening.
- 2026-09-29: Cloned the repo; diagnosed and fixed the MLflow deployment image-sync failure; merged as PR #26. Prepared 3.9.3 (PR #27); its tag build failed on the SQLAlchemy 2.1 greenlet issue, so fixed that and prepared 3.9.4.
