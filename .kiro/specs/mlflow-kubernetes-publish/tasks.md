# MLflow → Kubernetes (Nexus image publishing) — Tasks

## Phase 0 — manual checks with the platform team (no code)

- [ ] 0.1 From IDMVAIFACT1, check that `btgvalmbxp1:8525` resolves and is
  reachable through the firewall. Install the CA in
  `/etc/containers/certs.d/<host:port>/ca.crt`, then run
  `skopeo login` as `aimldeployer`.
- [ ] 0.2 Build PayguardAnomalyUnified v3 with the AI Factory target, then
  `skopeo copy` the intermediate tag by hand to
  `btgvalmbxp1:8525/mbnllmd/mbnllmdml-swiftanomaly:<agreed version>`. Note
  whether Nexus accepts the OCI manifest or needs `--format v2s2`.
- [ ] 0.3 The platform team deploys it. Check these points:
  - Does the image start under the cluster's pod security? The MLflow image
    runs as root with nginx, so `DISABLE_NGINX=true` and non-root may be
    needed.
  - Are the `envFrom` database variables picked up?
  - Do the probes `/ping` and `/health` on port 8080 pass?
- [ ] 0.4 Get answers to the open questions in `requirements.md`.

## Phase 1 — publish to Nexus

- [ ] 1. Models `DeployRegistrySettings`, `K8sProject`, `K8sApplication`,
  `MlflowImageRelease` and `MlflowImageReleaseEvent`. Register them in
  `database.init_db` and add migration v32.
- [ ] 2. `app/deploy_registry.py` plus admin GET, PUT and test endpoints.
  Add the Entegrasyonlar card and the `app.js` handler.
- [ ] 3. Kubernetes Projeleri admin section: CRUD for projects and
  applications, with the lock rules after a release is published.
- [ ] 4. `skopeo` helpers: a multi-registry authfile,
  `registry_tag_digest` and `copy_registry_image` with a cert dir.
- [ ] 5. Split `_build_image` into `ensure_model_build(owner,
  import_image=...)` without changing AI Factory behaviour. Add the release
  reference guard to the three GC functions.
- [ ] 6. `image_release_service`: process, recovery and the background
  worker. Register it in the lifespan and `task_health`.
- [ ] 7. `MlflowClient.set_model_version_tag` and tagging after a publish.
- [ ] 8. `/api/mlflow/kubernetes/options` and `/api/mlflow/image-releases`
  routes, including `can_publish_images`.
- [ ] 9. Kubernetes panel in the deploy modal and the progress and result
  view. Make `DEPLOY_TARGETS.kubernetes` available from the options.
- [ ] 10. Kubernetes releases table on the Deployments page.
- [ ] 11. Tests, only for behaviour that can regress. Use a fake `skopeo`:
  - an existing tag is refused and nothing is overwritten;
  - a successful publish records the digest;
  - GC keeps a build while a release still uses it;
  - an interrupted publish is recovered by digest.
- [ ] 12. Docs (`ARCHITECTURE.md`, `CONTAINERS.md`, `DEVELOPMENT.md`,
  README) and a release bump to 3.22.0.

## Phase 2 — deploy workflow (after the platform team answers)

- [ ] 13. Trigger the platform workflow after a publish, using the agreed
  mechanism and payload.
- [ ] 14. Show workflow or deploy status if the platform exposes it.
