"""Naming, versioning and permission rules for Kubernetes image releases."""

from __future__ import annotations

from collections.abc import Iterable

from app.models.user import User, UserRole
from app.schemas.kubernetes import RELEASE_VERSION_PATTERN

DIRECTORY_AUTH_SOURCE = "active_directory"
DEFAULT_RELEASE_VERSION = "1.0.0"


def can_publish_images(user: User) -> bool:
    """Every AD user and every admin may publish for now.

    Kept in one place so it can later be limited to project members.
    """
    return user.role == UserRole.ADMIN or user.auth_source == DIRECTORY_AUTH_SOURCE


def image_repository(rfs_code: str, app_name: str) -> str:
    """Institutional image name: rfskodu/uygulama-tamadı."""
    return f"{rfs_code}/{app_name}"


def image_reference(registry_url: str, rfs_code: str, app_name: str, version: str) -> str:
    return f"{registry_url}/{image_repository(rfs_code, app_name)}:{version}"


def parse_release_version(value: str) -> tuple[int, int, int] | None:
    if not RELEASE_VERSION_PATTERN.fullmatch(value or ""):
        return None
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def highest_version(versions: Iterable[str]) -> str | None:
    parsed = [
        (key, value)
        for value in versions
        if (key := parse_release_version(value)) is not None
    ]
    return max(parsed)[1] if parsed else None


def suggest_next_version(versions: Iterable[str]) -> str:
    """Highest used version with patch + 1, or 1.0.0 for a new application."""
    latest = highest_version(versions)
    if latest is None:
        return DEFAULT_RELEASE_VERSION
    major, minor, patch = parse_release_version(latest)
    return f"{major}.{minor}.{patch + 1}"
