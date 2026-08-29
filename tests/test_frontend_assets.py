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


def test_admin_generates_single_use_worker_bootstrap_commands():
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
    template = (PROJECT_ROOT / "app/templates/admin.html").read_text(encoding="utf-8")

    assert 'id="worker-bootstrap-ticket-form"' in template
    assert 'id="worker-bootstrap-ticket-box"' in template
    assert "/api/admin/worker-bootstrap-tickets" in javascript
    assert "renderWorkerBootstrapTicket" in javascript
    assert "10 dakika" in template
    assert "download/install-worker.sh' | sudo bash" not in template


def test_admin_panel_exposes_category_navigation():
    template = (PROJECT_ROOT / "app/templates/admin.html").read_text(
        encoding="utf-8"
    )
    navigation = (PROJECT_ROOT / "app/templates/partials/admin_nav.html").read_text(
        encoding="utf-8"
    )

    assert 'class="admin-shell"' in template
    assert "admin_section == 'overview'" in template
    assert 'href="/admin/users"' in navigation
    assert 'href="/admin/workspaces"' in navigation
    assert 'href="/admin/workers"' in navigation
    assert 'href="/admin/integrations"' in navigation
    assert 'href="/admin/system"' in navigation
    assert 'aria-current="page"' in navigation
    assert 'data-admin-filter=".admin-user-card"' in template
    assert 'data-admin-filter="#admin-workspace-table tbody tr"' in template

    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(
        encoding="utf-8"
    )
    assert "initAdminFilters()" in javascript


def test_dense_data_views_have_responsive_overflow_guards():
    base_css = (PROJECT_ROOT / "app/static/css/style.css").read_text(
        encoding="utf-8"
    )
    corporate_css = (PROJECT_ROOT / "app/static/css/kurumsal.css").read_text(
        encoding="utf-8"
    )
    admin = (PROJECT_ROOT / "app/templates/admin.html").read_text(encoding="utf-8")
    images = (
        PROJECT_ROOT / "app/templates/partials/admin_images.html"
    ).read_text(encoding="utf-8")
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(
        encoding="utf-8"
    )

    assert ".data-table-shell" in base_css
    assert "overflow-x: auto" in base_css
    assert ".responsive-card-table td::before" in corporate_css
    assert "content: attr(data-label)" in corporate_css
    assert ".worker-sync-item" in corporate_css
    assert 'class="data-table-shell' in admin
    assert 'class="table responsive-card-table" id="admin-workspace-table"' in admin
    assert 'data-label="Worker"' in admin
    assert "responsive-card-table-shell image-catalog-shell" in images
    assert 'data-label="Digest / SHA-256"' in javascript
    assert 'class="worker-sync-item"' in javascript


def test_worker_inventory_fits_admin_width_and_collapses_to_cards():
    css = (PROJECT_ROOT / "app/static/css/kurumsal.css").read_text(
        encoding="utf-8"
    )
    template = (PROJECT_ROOT / "app/templates/admin.html").read_text(
        encoding="utf-8"
    )

    assert "responsive-card-table-shell worker-table-shell" in template
    assert 'class="table responsive-card-table" id="admin-nodes-table"' in template
    for label in (
        "Worker",
        "Durum",
        "CPU",
        "RAM",
        "Disk / Container",
        "Etiketler",
        "İşlemler",
    ):
        assert f'data-label="{label}"' in template
    assert "#admin-nodes-table {" in css
    assert "table-layout: fixed" in css
    assert "#admin-nodes-table { min-width: 1050px; }" not in css
    assert "@media (max-width: 1180px)" in css
    assert ".worker-table-shell" in css
    assert "content: attr(data-label)" in css


def test_platform_update_has_clear_methods_release_summary_and_live_log():
    css = (PROJECT_ROOT / "app/static/css/kurumsal.css").read_text(
        encoding="utf-8"
    )
    template = (PROJECT_ROOT / "app/templates/admin.html").read_text(
        encoding="utf-8"
    )
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(
        encoding="utf-8"
    )

    assert 'class="card platform-update-card"' in template
    assert 'class="platform-current-release"' in template
    assert "platform-update-option--primary" in template
    assert "Git Kanalından Güncelle" in template
    assert "Yerel platform bundle" in template
    assert 'class="platform-update-log"' in template
    assert 'aria-live="polite"' in template
    assert ".platform-update-options" in css
    assert ".platform-git-fields" in css
    assert ".platform-update-log" in css
    assert template.count('name="allow_unsigned"') == 2
    assert template.count('class="unsigned-update-control"') == 2
    assert 'value="https://github.com/aydinguven/devcloud.git"' in template
    assert 'name="allow_unsigned" type="checkbox" value="true" checked' in template
    assert ".unsigned-update-control:has(input:checked)" in css
    assert 'form.elements.namedItem("allow_unsigned")' in javascript
    assert "Release imzası doğrulanmayacak" in javascript
    assert '"badge-error"' in javascript
    assert '"Kuyruğa alınıyor..."' in javascript


def test_worker_inventory_shows_version_and_live_upgrade_state():
    css = (PROJECT_ROOT / "app/static/css/kurumsal.css").read_text(
        encoding="utf-8"
    )
    template = (PROJECT_ROOT / "app/templates/admin.html").read_text(
        encoding="utf-8"
    )
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(
        encoding="utf-8"
    )

    assert 'class="node-version badge badge-neutral"' in template
    assert "node-upgrade-state badge" in template
    assert "upgrade_status.get('target_version')" in template
    assert "renderWorkerUpgradeState" in javascript
    assert "d.agent_version" in javascript
    assert "d.upgrade_status" in javascript
    assert "Object.keys(d.upgrade_status).length" in javascript
    assert ".node-release-line" in css


def test_worker_bootstrap_uses_one_time_admin_command():
    template = (PROJECT_ROOT / "app/templates/downloads.html").read_text(
        encoding="utf-8"
    )
    bootstrap = (PROJECT_ROOT / "app/templates/install_worker.sh").read_text(
        encoding="utf-8"
    )

    assert "Admin &gt; Worker Node'ları" in template
    assert "worker_bootstrap_url" not in template
    assert "devcloud-setup.sh" in bootstrap
    assert "--yes install worker" in bootstrap
    assert "sha256sum -c" in bootstrap
    assert 'tar -xf "${BUNDLE_PATH}"' in bootstrap
    assert "devcloud-offline-*.tar.gz.sha256" in template
    assert "devcloud-worker-offline-*.tar.gz.sha256" in template
    assert "Worker name" in bootstrap
    assert "/api/bootstrap/workers/" not in bootstrap
    assert "DEVCLOUD_NODE_TOKEN" not in template


def test_institutional_brand_mark_replaces_text_placeholder():
    template = (PROJECT_ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    css = (PROJECT_ROOT / "app/static/css/kurumsal.css").read_text(encoding="utf-8")
    logo_svg = (PROJECT_ROOT / "app/static/img/tcmb_ai_factory_logo.svg").read_text(encoding="utf-8")
    favicon_svg = (PROJECT_ROOT / "app/static/favicon.svg").read_text(encoding="utf-8")

    assert 'TCMB' in template
    assert 'AI FACTORY' in template
    assert 'Yapay Zeka Geliştirme Platformu' in template
    assert 'Yapay Zekâ' not in template
    assert 'logo-icon" aria-hidden="true">DC' not in template
    assert ".brand-tcmb" in css
    assert ".brand-ai" in css
    assert 'TCMB' in logo_svg
    assert 'AI FACTORY' in logo_svg
    assert 'Yapay Zeka Geliştirme Platformu' in logo_svg
    assert 'Yapay Zekâ' not in logo_svg
    assert '<ellipse' in favicon_svg
    assert '#d50032' in favicon_svg
    assert '#263244' in favicon_svg
