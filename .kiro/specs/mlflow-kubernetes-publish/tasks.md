# MLflow → Kubernetes (Nexus image publishing) — Tasks

## Phase 0 — manual checks with the platform team (no code)

- [ ] 0.1 From IDMVAIFACT1, check that `btgvalmbxp1:8525` resolves and is
  reachable through the firewall. After the 3.22 update, the Admin >
  Kubernetes **Test** button checks this, with TLS checking off if the
  certificate is not trusted.
- [ ] 0.2 Publish PayguardAnomalyUnified v3 from the Kubernetes target as
  the agreed version. If Nexus rejects the OCI manifest, select v2s2 and
  retry.
- [ ] 0.3 The platform team deploys it. Check these points:
  - Does the image start under the cluster's pod security? The MLflow image
    runs as root with nginx, so `DISABLE_NGINX=true` and non-root may be
    needed.
  - Are the `envFrom` database variables picked up?
  - Do the probes `/ping` and `/health` on port 8080 pass?
- [ ] 0.4 Get answers to the open questions in `requirements.md`.

## Phase 1 — publish to Nexus

- [x] 1. Models `DeployRegistrySettings`, `K8sProject`, `K8sApplication`,
  `MlflowImageRelease` and `MlflowImageReleaseEvent`. Register them in
  `database.init_db` and add migration v32.
- [x] 2. `app/deploy_registry.py` plus admin GET, PUT and test endpoints,
  and the registry card in the Kubernetes admin section.
- [x] 3. Kubernetes Projeleri admin section: CRUD for projects and
  applications, with the lock rules after a release is published.
- [x] 4. `skopeo` helpers: one authfile per registry,
  `registry_tag_digest` and `copy_registry_image` with the TLS and format
  options.
- [x] 5. Split `_build_image` into `ensure_model_build(owner,
  import_image=...)` without changing AI Factory behaviour. Add the release
  reference guard to the three GC functions.
- [x] 6. `image_release_service`: process, recovery and the background
  worker. Register it in the lifespan and `task_health`.
- [x] 7. `MlflowClient.set_model_version_tag` and tagging after a publish.
- [x] 8. `/api/mlflow/kubernetes/options` and `/api/mlflow/image-releases`
  routes, including `can_publish_images`.
- [x] 9. Kubernetes panel in the deploy modal and the progress and result
  view. Make `DEPLOY_TARGETS.kubernetes` available from the options.
- [x] 10. Kubernetes releases table on the Deployments page.
- [x] 11. Tests in `tests/test_image_releases.py`, only for behaviour that
  can regress. They use a fake registry:
  - an existing tag is refused and nothing is copied;
  - a ready build is reused and the digest is recorded;
  - GC keeps a build while a release still uses it;
  - an interrupted publish adopts its tag;
  - the API refuses non-AD users and a version that is already used.
- [x] 12. Docs (`ARCHITECTURE.md`, `CONTAINERS.md`, `DEVELOPMENT.md`,
  README).
- [ ] 12a. Release bump to 3.22.0, in a separate release PR as usual.

## Phase 2 — deploy workflow (after the platform team answers)

- [ ] 13. Trigger the platform workflow after a publish, using the agreed
  mechanism and payload.
- [ ] 14. Show workflow or deploy status if the platform exposes it.
