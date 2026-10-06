# GenAI (LiteLLM) — Tasks

## Phase 1 — GenAI tab and personal key

- [ ] 1. Models and migration: `GenAiSettings` and `GenAiAccount`. Register them in `models/__init__.py` and `database.init_db`, and add migration v26 (`CURRENT_SCHEMA_VERSION = 26`).
- [ ] 2. Config fallback: `GENAI_LITELLM_URL` and `GENAI_LITELLM_ADMIN_KEY` in `config.py` and `.env.example`.
- [ ] 3. `app/integrations/litellm.py` client: config, validation and the calls listed in design.md, with cleaned-up errors.
- [ ] 4. Provisioning service: status, provision (safe to retry, adopts an existing LiteLLM user, cleans up if the commit fails), and rotate.
- [ ] 5. `/api/genai` routes (`GET`/`POST /account`, `POST /account/rotate`). Register them in `main.py`.
- [ ] 6. Admin `genai-settings` GET/PUT/test endpoints, the form in `admin.html` and the handlers in `app.js`.
- [ ] 7. `/genai` page: nav link, view, `genai.html`, `genai.js` (the four states, show the key once, copy button, usage).
- [ ] 8. Tests in `tests/test_genai.py` with a fake `httpx`: provisioning, adopting an existing user, retries, rotation, the admin key never in responses, non-admin gets 403 on admin endpoints, and the disabled state. Update `test_views.py` and `test_frontend_assets.py` if needed.
- [ ] 9. Docs: a GenAI section in README/INSTALL (how to create the `aifactory` proxy_admin user and key).

## Phase 2 — per-user workspace key

- [ ] 10. `ensure_workspace_key(user)`: creates the key if it is missing or can't be decrypted.
- [ ] 11. Pass `ai_gateway_token` at the three `create_workspace_container` call sites in `workspace_routes.py`, falling back to the shared token if anything fails.
- [ ] 12. Worker: add the field to the allowed list, and use it in `podman_service` for `ANTHROPIC_AUTH_TOKEN` and the Cline config.
- [ ] 13. Tests: env injection with and without the key, the fallback when LiteLLM is down, and the old-worker case.
