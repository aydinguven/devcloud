# MLflow → Kubernetes (Nexus image publishing) — Design

This builds on the AI Factory deploy pipeline in
`app/orchestrator/mlflow_deployment_service.py`:

1. A worker runs `mlflow models generate-dockerfile`, `podman build` and
   `podman push` to the Model Container Registry (the intermediate registry).
2. The controller copies the result with `skopeo` into the `WorkspaceImage`
   archive.

Publishing adds one controller-side step: a registry-to-registry
`skopeo copy` from the intermediate registry to the deploy registry. The
worker RPC (`image.mlflow.build`) is unchanged, so workers need no upgrade.

## Flow

```
modal (Kubernetes) ── POST /api/mlflow/image-releases ──► release QUEUED
release loop:
  validate   MLflow version (user creds), app binding, version unused,
             tag missing in deploy registry (skopeo inspect)
  BUILDING   reuse READY build, or worker build → intermediate registry
             (not imported into WorkspaceImage); tag still missing?
  PUBLISHING skopeo copy docker://<intermediate tag>
                         docker://<deploy>/<rfs>/<app>:<X.Y.Z>
             record digest; set MLflow model-version tags (warning on failure)
             any failure here: tag present → PUBLISHED, absent → FAILED
  PUBLISHED  build no longer pinned → orphan GC cleans the intermediate tag
             (a FAILED release pins it for 24 h, for the retry)
```

Why not import into `WorkspaceImage`? `/api/agent/images/catalog` sends every
enabled image to every CPU worker, so a publish-only build would make every
worker download a multi-GB image it never runs. Without an image record,
`garbage_collect_model_build` already rebuilds the tag from
`_image_tag(build, registry_url)`, so the intermediate tag is still cleaned
up. AI Factory's `_ready_build` requires `workspace_image_id`, so it rebuilds
instead of reusing a publish-only build. That cost is accepted.

## Data model (migration v32)

`init_db` creates the tables and the v32 step only records the version. Every
status column is a `String(32)`, not an `Enum`.

- `deploy_registry_settings` (singleton, `id=1`):
  - `enabled`
  - `registry_url` (`host:port`)
  - `username`
  - `encrypted_password`
  - `tls_verify` (default true)
  - `manifest_format` (`""` keeps the source manifest, `"v2s2"` converts it)
  - `updated_at`
- `k8s_projects`:
  - `id`
  - `rfs_code` (unique, `^[a-z0-9]{2,32}$`)
  - `name`
  - `namespace` (unique, DNS-1123 label, at most 63 characters)
  - `description`
  - `enabled`
  - timestamps
- `k8s_applications`:
  - `id`
  - `project_id` (FK, RESTRICT)
  - `app_name` (`^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`, at most 100 characters)
  - `mlflow_model_name`
  - `bitbucket_url`
  - `enabled`
  - timestamps
  - unique (`project_id`, `app_name`)
- `mlflow_image_releases`:
  - `id`, `user_id`
  - `application_id` (FK, RESTRICT)
  - `build_id` (FK `mlflow_model_builds`, SET NULL)
  - `model_name`, `model_version`, `run_id`
  - `version` (`X.Y.Z`)
  - `image_ref` (full deploy-registry reference)
  - `source_ref` (intermediate tag)
  - `digest`
  - `mlflow_tagged`
  - `status` (`queued` | `building` | `publishing` | `published` | `failed`)
  - `status_message`, `error_message`
  - `lease_owner`, `lease_expires_at`
  - `created_at`, `updated_at`, `completed_at`

  Version uniqueness ("no non-failed release with the same application and
  version") is checked in `admission_transaction`, like deployment names.
  There is no database constraint, so a failed row can be retried in place.
- `mlflow_image_release_events`: same shape as `mlflow_deployment_events`.

## Components

- **`app/deploy_registry.py`**:
  - config dataclass, validation, `effective_deploy_registry_config(db)` and
    the settings serializer;
  - `test_deploy_registry`, which runs `GET /v2/`. On a 401 Bearer challenge
    it requests a token from the realm with basic auth. With TLS checking
    off it uses `verify=False` and falls back to `http://`.
  - Host validation is `normalize_deploy_registry_host`: `host:port`, no
    path.
- **`app/kubernetes_publish.py`**: image naming, X.Y.Z parsing and the next
  suggested version, and `can_publish_images`.
- **`app/workspace_image_service.py`**:
  - Generalize `_registry_auth_environment` to write one authfile with several
    `auths` entries (intermediate and deploy registry).
  - `registry_tag_digest(ref, creds, tls_verify) -> str | None` runs
    `skopeo inspect` (`--tls-verify=false` when TLS checking is off) and
    reads `Digest`. It returns `None` on the "manifest unknown", "name
    unknown" or "not found" markers already used by `delete_registry_image`.
  - `copy_registry_image(...)` runs `skopeo copy --remove-signatures` with
    `--src-authfile` and `--dest-authfile`, one temporary 0600 file per
    registry. It adds `--dest-tls-verify=false` when TLS checking is off and
    `--format v2s2` when that is selected.
  - The digest is read back with `registry_tag_digest` after the copy, so the
    code does not rely on `--digestfile`.
- **Build refactor in `mlflow_deployment_service.py`**:
  - Split `_build_image` into `ensure_model_build(db, owner, client, *,
    import_image: bool)`.
  - `owner` is a small adapter that provides the event append, the lease
    assertion, the lease heartbeat and the `build_id` setter. There is one
    adapter for deployments and one for releases.
  - AI Factory keeps `import_image=True` and its current behaviour.
  - Releases use `import_image=False`. They reuse any READY build of the same
    user, model, version and recipe, with or without an image.
- **GC guards**: `enroll_model_build_cleanup`, `enroll_orphaned_model_builds`
  and `garbage_collect_model_build` also count as references:
  - releases in `queued`, `building` or `publishing`;
  - releases that failed less than 24 hours ago.
- **`app/orchestrator/image_release_service.py`**:
  - `process_image_release` performs the claim and then each stage from the
    flow above.
  - The tag is checked twice: before the build, and again just before the
    release enters `publishing`. So `publishing` means "the tag was free and
    the copy began".
  - `_settle_started_publish` handles any failure in `publishing` and the
    recovery after a restart:
    - If the tag exists, the release becomes `published` with a warning, and
      the MLflow tags are set.
    - If it does not, the release becomes `failed`, or `queued` during
      recovery.
    - If the registry cannot be reached, it is checked again 30 s later.
    - The digest is not compared with the source, because a v2s2 conversion
      changes it.
  - `recover_interrupted_image_releases` claims a stale lease for the full
    lease time and re-checks it before writing. A release in `building` goes
    back to `queued`.
  - Never-overwrite is check-then-copy, and `skopeo copy` would replace a tag
    pushed in between. Nexus "Disable redeploy" on the hosted repository is the
    actual guarantee.
  - `image_release_background_worker` is its own task, started in the
    `main.py` lifespan and registered in `task_health` as "Kubernetes imaj
    yayın kuyruğu". It is separate from the deploy loop, because that loop is
    sequential and a long build would block it.
- **MLflow client**: add `set_model_version_tag(name, version, key, value)`,
  which calls `POST /api/2.0/mlflow/model-versions/set-tag`. It runs with the
  user's credentials. A 403 or other error is logged as a warning event.
- **Permission**: `can_publish_images(user) = user.role == ADMIN or
  user.auth_source == "active_directory"`, defined in one place.

## API

Admin endpoints (`app/routes/admin/`, `get_current_admin_user`):
- `GET` and `PUT /api/admin/deploy-registry-settings`, and `POST
  /api/admin/deploy-registry-settings/test`. These follow the
  model-container-registry endpoints.
- `GET` and `POST /api/admin/k8s-projects`, and `PUT` and `DELETE
  /api/admin/k8s-projects/{id}`.
- `POST /api/admin/k8s-projects/{id}/applications`, and `PUT` and `DELETE
  /api/admin/k8s-applications/{id}`.
- Deleting returns 409 when releases exist. Changing a code, name or
  namespace returns 409 after a release has been published.

User endpoints (`/api/mlflow`):
- `GET /kubernetes/options?model_name=` returns:

  ```
  {enabled, registry, applications: [{id, project, rfs_code, namespace,
   repository, last_version, suggested_version}]}
  ```

  It lists only enabled applications bound to that model.
- `POST /image-releases` takes `{application_id, model_name, model_version,
  version}` and returns 202. If the user may not publish, it returns 403.
  If the registry is not configured, it returns 503.
- `GET /image-releases` returns the user's own releases, or all releases for
  admins.
- `GET /image-releases/{id}` and `GET /image-releases/{id}/events`.
- `POST /image-releases/{id}/retry` works only when the release has failed.

## UI

- **Deploy modal**, Kubernetes panel:
  - Remove the namespace, replica and flavor inputs. Hide the deployment name
    for this target.
  - Add an **Uygulama** select. Show the namespace, the image repository and
    the last version read-only.
  - Add a **Versiyon (X.Y.Z)** input, prefilled with the suggested version,
    and a live preview of the full reference.
  - Pipeline steps: İmaj → Nexus → Workflow (platform ekibi) → Kubernetes.
  - Submit button: **Nexus'a Yayınla**.
  - If no application is bound to the model, explain that an admin must
    define one in Kubernetes Projeleri.
  - `DEPLOY_TARGETS.kubernetes.available` comes from `options.enabled`.
- **Progress view**: reuses the event terminal. The result card shows the
  image reference and digest with copy buttons, and this note: "Platform
  ekibi bu imajı X.Y.Z-RC.BN olarak etiketleyip dağıtır."
- **Deployments page** (`mlflow_deployments.html`): a "Kubernetes yayınları"
  table with a retry action for failed releases.
- **Admin**: a new `kubernetes` entry in `ADMIN_SECTIONS`
  (`partials/admin_kubernetes.html`, `static/js/admin_kubernetes.js`). It
  holds the registry card and the CRUD for projects and applications.

## Docs

- `ARCHITECTURE.md` network flows:
  - The controller talks to the deploy registry.
  - The only write to MLflow is the model-version tags set after a publish.
- `CONTAINERS.md`: a section on Kubernetes publishing.
- `DEVELOPMENT.md`: a decision entry.
- `README`: a short user guide.

## Security notes
- Deploy registry credentials stay on the controller. They live only in a
  0600 temporary authfile for a single `skopeo` call, and are never passed on
  the command line.
- Turning TLS checking off is the admin's choice for a trusted internal
  network. It applies only to the deploy registry.
- Publishing never deletes or overwrites anything in the deploy registry.
- Admins bind each application to an MLflow model, so an AD user cannot push
  an unrelated model under another application's name.
