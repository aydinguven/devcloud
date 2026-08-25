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
