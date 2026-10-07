# DEVELOPMENT

## Project snapshot

- DevCloud: self-hosted browser IDE platform (FastAPI + Podman), controller plus outbound-only CPU/GPU workers, MLflow model serving.
- Source: `https://github.com/aydinguven/devcloud` (GitHub by owner's choice; not on Forgejo). Default branch `main`, latest release `v3.12.0`.
- Release process: bump `app/__init__.py` `__version__` via PR, then push tag `vX.Y.Z` on the merged `main` commit. `.github/workflows/release-platform.yml` builds the bundles, creates the GitHub Release and advances the `stable` update channel.
- Deployment: own installer/offline bundle (`INSTALL.md`, `AIRGAP.md`, `WORKERS.md`); not a Tupperware project.
- Local setup: `python -m venv .venv`, `.venv\Scripts\python -m pip install -r requirements.txt`, run `python run.py` (http://127.0.0.1:8000).
- Tests: `.venv\Scripts\python -m pytest -q`.

## Active work

| Owner | Status | Branch | Task | Next step |
|---|---|---|---|---|
| Kiro | PR open | `ci/leaner-release` | Faster release pipeline: workspace images only when `containers/<image>` changed, direct uploads to a draft release, parallel controller/worker builds and packaging, docs-only changes skip CI | Merge, then cut the next release and confirm: no workspace jobs when nothing changed, total run under ~8 min, release published and `stable` advanced |
| Aydin | Pending | `main` | 3.12.0 rollout | Update IDMVAIFACT1 and workers, set `TCMB_Standard_User` + priority in Admin > GenAI, then create a workspace as a GenAI user and check the key in LiteLLM |

## Decisions

- 2026-10-07: Releases always build and ship controller and worker images; a workspace image is built only when `containers/<image>` changed since the newest *published* `vX.Y.Z` release, or when forced with the `rebuild_workspaces` dispatch input. Unchanged workspaces are no longer republished, so their newest GHCR tag/offline archive belongs to the release that last changed them. Build jobs upload straight to a draft release (no artifact round-trip); `publish` un-drafts it and advances `stable`. Workflow string-pinning tests were removed at Aydin's request for fewer unnecessary tests.

- 2026-09-29: The model-image scheduling wait follows real worker sync progress (stall limit 5 min, hard limit 30 min, 2 min once the image is on a worker but capacity is missing) instead of a fixed 2-minute window. Multi-GB images are pulled on a 30 s worker poll, then verified and `podman load`ed.

- 2026-10-06: The bundled PostgreSQL gets a fixed IP (`broadcast - 5`, e.g. `10.89.0.250`) on the `devcloud` network and the controller resolves `devcloud-postgresql` through `AddHost`, so database access never depends on aardvark-dns. IPv6-only or unreadable networks keep the DNS-only behaviour.
- 2026-10-06: Ingress changes firewalld permanently and at runtime; never `firewall-cmd --reload`, which discards netavark's runtime rules. The installer enables `netavark-firewalld-reload.service` when present.

- 2026-10-06: Quotas resolve per field as user override -> team group (`users.team`, AD `department`, normalized by `app.quotas.team_key`) -> default group (`user_group_quotas.group_key = ''`). A team quota applies to each member individually, not as a shared budget. Enforcement reads only `app/quotas.py`; the legacy `users.*_quota` columns are no longer read. Migration 25 turns every legacy value that differs from the configured default into an override, so effective quotas do not change on upgrade.

- 2026-10-06: Workspace AI uses the owner's own LiteLLM *workspace key* (separate from the personal key, encrypted, never shown) only when the owner opted in on the GenAI tab and GenAI and Workspace AI point at the same LiteLLM; otherwise the shared key. Keys bind to the first team in the admin priority list the user belongs to; new users join the default team.

## Known issues and risks

- `requirements.txt` uses open ranges. SQLAlchemy 2.1 no longer installs `greenlet`, which broke CI and the release build's pytest step (since removed; CI is the test gate) on 2026-09-29; fixed by requiring `sqlalchemy[asyncio]`. Other packages can still drift on a fresh install; consider exact pins or a constraints file.
- The deployment background worker processes jobs one at a time, so a long image-sync wait delays other queued deployments. This was already the case during image builds.
- `tests/test_mlflow_model_build.py::test_worker_unit_exposes_the_virtualenv_console_scripts_on_path` fails on Windows only (it asserts `os.sep` inside a Linux unit file).

## Validation

- 2026-10-07 (`ci/leaner-release`): suite 383 passed, 3 skipped, 1 known Windows-only failure. Both workflows parse; every `run:` step and `build-release-assets.sh` pass `bash -n`. A Git Bash probe confirmed the parallel helpers propagate a failure and stop the failing chain. A simulated scope step for `v3.12.0` compares against `v3.11.1` and selects no workspace images; `rebuild_workspaces` lists/`all` select the named images and an unknown name fails. Not yet validated: a real GitHub run (draft creation/upload/un-draft, parallel podman steps in Rocky 10).

- 2026-10-06: Full suite 368 passed, 3 skipped. Scratch checks: installer renders `IP=10.89.0.250` / `AddHost=devcloud-postgresql:10.89.0.250` for an existing `10.89.0.0/24` network, creates the network first when missing, and skips pinning for IPv6-only; `/readyz` returns 503 `gaierror` for an unresolvable DB host; an unhandled `/api` error returns JSON 500; a fake AD rejecting the service bind now yields 503 with a readable message instead of "wrong password".
- Still required for 3.9.5: an update on IDMVAIFACT1 confirming PostgreSQL comes up on the pinned IP and the controller is healthy.

- 2026-09-29: MLflow, node and lifecycle test files pass: 57 passed, 2 skipped, 1 Windows-only failure (listed above).
- 2026-09-29: A scratch smoke run confirmed the original code crashes on its second scheduling attempt (expired ORM row after rollback), while the fix retries, logs sync progress and returns a placement. The stall path raises a readable error.
- Still required: a real deployment of a new model version on a worker.

## Latest handoff

2026-10-07 release pipeline (`ci/leaner-release`): v3.12.0 took 14.3 min. Its critical path was platform 8.4 min + publish 5.8 min, and 3.9 min of publish was only downloading staged artifacts. It also rebuilt all six workspace images because the old scope treated any change to `deploy/ci/build-release-assets.sh` as a workspace change. The branch fixes both and parallelizes the Rocky build (estimated ~7-8 min with no workspace changes). After merge, watch the first run. If `gh release view/upload` on drafts misbehaves, the fallback is to restore the artifact staging for the platform assets only.

2026-10-06 IDMVAIFACT1 update 3.9.4 -> 3.10.0 failed with "Unit devcloud-postgresql.service not found" and rolled back cleanly. Root cause (from the journal): the queued update runs the *installed* release's installer (`devcloud-setup.sh` of 3.9.4), which rendered the 3.10.0 Quadlet templates without knowing the new `{{POSTGRES_STATIC_IP}}`/`{{DATABASE_HOSTS}}` placeholders. Quadlet rejected both files ("not a key-value pair") and skipped the units. Fix in 3.11.1: templates use only placeholders every installer knows (guarded by a test), the IP/AddHost keys are injected in code, rendering fails on any leftover placeholder, and from 3.11.1 on the update re-executes the target release's own installer. Updating 3.9.4 -> 3.11.1 still uses the 3.9.4 renderer, so the units get no IP/AddHost lines; the manual drop-ins keep the pinning until `devcloud-setup.sh repair` (3.11.1 code) or a later update writes them.

2026-10-06 production incident (IDMVAIFACT1, 3.9.4): logins returned intermittent HTTP 500 (`Unexpected token 'I'` on the login page), the dashboard 500'd and the worker tunnel flapped. Cause: `firewall-cmd --reload` (run by `apply_ingress.py` on every ingress apply, plus the `yum upgrade` of firewalld) removed netavark's runtime trusted-zone source `10.89.0.0/24`, so container DNS to `10.89.0.1:53` was dropped and `devcloud-postgresql` stopped resolving (`socket.gaierror`). The controller survived on one pooled connection, so `/readyz` stayed green. Restoring needed `podman network reload --all`, a restart, and manual drop-ins `/etc/containers/systemd/devcloud-postgresql.container.d/10-static-ip.conf` (`IP=10.89.0.250`) and `devcloud-controller.container.d/10-db-host.conf` (`AddHost=devcloud-postgresql:10.89.0.250`). 3.9.5 renders the same settings, so delete those two drop-ins after updating.

Earlier handoff:

`app/orchestrator/mlflow_deployment_service.py` `_reserve_when_image_synced`: the old retry loop reused `user` and `image` rows after `admission_transaction` rolled back the session. On AsyncSession that raises, so any deployment whose image was not already on a worker failed on the second attempt. A re-deploy worked because it reused the cached image that had synced by then. The fix reloads the rows each attempt, waits on worker progress and renews the deployment lease during the wait.

## Session log

- 2026-10-07: Profiled the v3.12.0 release run and slimmed the release pipeline (see Latest handoff).
- 2026-10-06: Diagnosed the production login 500s down to firewalld reloads breaking Podman DNS (see Latest handoff); restored service manually; prepared 3.9.5 with the permanent fix and login error hardening.
- 2026-09-29: Cloned the repo; diagnosed and fixed the MLflow deployment image-sync failure; merged as PR #26. Prepared 3.9.3 (PR #27); its tag build failed on the SQLAlchemy 2.1 greenlet issue, so fixed that and prepared 3.9.4.
