from pathlib import Path

from app import __version__
from app.config import Settings


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_workspace_detail_metrics_and_tabs_have_responsive_assets():
    css = (PROJECT_ROOT / "app/static/css/kurumsal.css").read_text(encoding="utf-8")
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(encoding="utf-8")

    assert ".workspace-metrics-grid" in css
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in css
    assert ".workspace-tabs-nav" in css
    assert ".tab-btn.active" in css
    assert "data-detail-metrics-ws-id" in javascript
    assert "encodeURIComponent(detailWorkspaceId)" in javascript
    assert 'btn.style.color = "var(--heading)"' in javascript


def test_app_version_cannot_be_pinned_by_stale_environment(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "0.0.0")

    assert Settings(_env_file=None).APP_VERSION == __version__


def test_platform_updater_waits_for_new_healthy_service():
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(encoding="utf-8")

    assert "waitForPlatformReady" in javascript
    assert "/api/admin/system/update-info?ts=" in javascript
    assert 'cache: "no-store"' in javascript
    assert "response.ok" in javascript
    assert "data.version" in javascript
    assert "90000" in javascript
    assert "window.location.replace" in javascript


def test_admin_has_separate_worker_offline_bundle_controls():
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
    template = (PROJECT_ROOT / "app/templates/admin.html").read_text(encoding="utf-8")

    assert 'id="btn-update-worker-downloads"' in template
    assert 'id="worker-download-update-logs"' in template
    assert "/api/admin/downloads/worker/status" in javascript
    assert "/api/admin/downloads/worker/update" in javascript
    assert 'id="download-settings-form"' in template
    assert 'name="public_base_url"' in template
    assert "/api/admin/download-settings" in javascript
    assert "initDownloadSettings()" in javascript
    assert 'id="https-settings-form"' in template
    assert 'name="https_enabled"' in template
    assert 'name="http_fallback_enabled"' in template
    assert 'name="certificate"' in template
    assert 'name="private_key"' in template
    assert "/api/admin/download-settings/https" in javascript
    assert "initHttpsSettings()" in javascript


def test_public_download_page_exposes_worker_bootstrap_command():
    template = (PROJECT_ROOT / "app/templates/downloads.html").read_text(
        encoding="utf-8"
    )
    bootstrap = (PROJECT_ROOT / "app/templates/install_worker.sh").read_text(
        encoding="utf-8"
    )

    assert "worker_bootstrap_url" in template
    assert "curl -fsSL" in template
    assert "/opt/devcloud-worker" in bootstrap
    assert "sha256sum -c" in bootstrap
    assert "tar -xf" in bootstrap
    assert "tar -xzf" not in bootstrap
    assert "devcloud-offline-*.tar.sha256" in template
    assert "devcloud-worker-offline-*.tar.sha256" in template
    assert "read -r -s NODE_TOKEN" in bootstrap
    assert "DEVCLOUD_NODE_TOKEN" not in template


def test_institutional_brand_mark_replaces_text_placeholder():
    template = (PROJECT_ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    css = (PROJECT_ROOT / "app/static/css/kurumsal.css").read_text(encoding="utf-8")
    mark = (PROJECT_ROOT / "app/static/favicon.svg").read_text(encoding="utf-8")

    assert 'class="logo-mark"' in template
    assert 'aria-label="DevCloud ana sayfa"' in template
    assert 'logo-icon" aria-hidden="true">DC' not in template
    assert ".navbar-brand .logo-mark" in css
    assert "flex: 0 0 46px" in css
    assert "background: transparent" in css
    assert "DevCloud kurumsal amblemi" in mark
    assert '#d50032' in mark
    assert '#263244' in mark
    assert '#d6ca84' in mark
