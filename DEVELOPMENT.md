# DEVELOPMENT

## Project snapshot

- DevCloud: self-hosted browser IDE platform (FastAPI + Podman), controller plus outbound-only CPU/GPU workers, MLflow model serving.
- Source: `https://github.com/aydinguven/devcloud` (GitHub by owner's choice; not on Forgejo). Default branch `main`, latest tag `v3.9.2` (3.9.3 in preparation).
- Release process: bump `app/__init__.py` `__version__` via PR, then push tag `vX.Y.Z` on the merged `main` commit. `.github/workflows/release-platform.yml` builds the bundles, creates the GitHub Release and advances the `stable` update channel.
- Deployment: own installer/offline bundle (`INSTALL.md`, `AIRGAP.md`, `WORKERS.md`); not a Tupperware project.
- Local setup: `python -m venv .venv`, `.venv\Scripts\python -m pip install -r requirements.txt`, run `python run.py` (http://127.0.0.1:8000).
- Tests: `.venv\Scripts\python -m pytest -q`.

## Active work

| Owner | Status | Branch | Task | Next step |
|---|---|---|---|---|
| Kiro | Image-sync fix merged to `main` (PR #26); release 3.9.3 PR open | `release/3.9.3` | Release 3.9.3 with the MLflow deployment image-sync fix | After the release PR merges, push tag `v3.9.3` on the merged `main` commit, watch `release-platform.yml`, then update the server and deploy a new model version on a worker |

## Decisions

- 2026-09-29: The model-image scheduling wait follows real worker sync progress (stall limit 5 min, hard limit 30 min, 2 min once the image is on a worker but capacity is missing) instead of a fixed 2-minute window. Multi-GB images are pulled on a 30 s worker poll, then verified and `podman load`ed.

## Known issues and risks

- `requirements.txt` uses open ranges. A fresh install resolves SQLAlchemy 2.1.x, which no longer installs `greenlet`, so the async engine fails to import. Pin versions or use `sqlalchemy[asyncio]`. Check what the offline wheel bundle ships.
- The deployment background worker processes jobs one at a time, so a long image-sync wait delays other queued deployments. This was already the case during image builds.
- `tests/test_mlflow_model_build.py::test_worker_unit_exposes_the_virtualenv_console_scripts_on_path` fails on Windows only (it asserts `os.sep` inside a Linux unit file).

## Validation

- 2026-09-29: MLflow, node and lifecycle test files pass: 57 passed, 2 skipped, 1 Windows-only failure (listed above).
- 2026-09-29: A scratch smoke run confirmed the original code crashes on its second scheduling attempt (expired ORM row after rollback), while the fix retries, logs sync progress and returns a placement. The stall path raises a readable error.
- Still required: a real deployment of a new model version on a worker.

## Latest handoff

`app/orchestrator/mlflow_deployment_service.py` `_reserve_when_image_synced`: the old retry loop reused `user` and `image` rows after `admission_transaction` rolled back the session. On AsyncSession that raises, so any deployment whose image was not already on a worker failed on the second attempt. A re-deploy worked because it reused the cached image that had synced by then. The fix reloads the rows each attempt, waits on worker progress and renews the deployment lease during the wait.

## Session log

- 2026-09-29: Cloned the repo; diagnosed and fixed the MLflow deployment image-sync failure; merged as PR #26. Prepared release 3.9.3.
