"""Validated metadata and publication helpers for immutable platform updates."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.installer.platform import InstallerError


PLATFORM_BUNDLE_PATTERN = re.compile(
    r"^devcloud-platform-update-v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"
    r"(?:-(?P<revision>[0-9a-f]{7,40}))?\.tar\.gz$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PlatformImage:
    role: str
    image: str
    source: str
    digest: str
    archive: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class PlatformRelease:
    version: str
    source_commit: str
    controller: PlatformImage
    worker: PlatformImage


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_archive_path(raw: object, expected: str) -> str:
    value = str(raw or "").replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value != expected:
        raise InstallerError(f"Platform image archive path is invalid: {value}")
    return value


def _image(root: Path, value: object, role: str) -> PlatformImage:
    if not isinstance(value, dict):
        raise InstallerError(f"Platform manifest has no {role} image")
    expected = f"offline/{role}-images/devcloud-{role}.tar"
    archive_name = _safe_archive_path(value.get("archive"), expected)
    archive = root.joinpath(*PurePosixPath(archive_name).parts)
    checksum = str(value.get("sha256") or "")
    size = int(value.get("size") or 0)
    digest = str(value.get("digest") or "")
    image = str(value.get("image") or "").strip()
    source = str(value.get("source") or "").strip()
    if not image or not source:
        raise InstallerError(f"Platform {role} image references are missing")
    if not IMAGE_DIGEST_PATTERN.fullmatch(digest):
        raise InstallerError(f"Platform {role} image digest is invalid")
    if not SHA256_PATTERN.fullmatch(checksum):
        raise InstallerError(f"Platform {role} archive checksum is invalid")
    if not archive.is_file() or archive.is_symlink():
        raise InstallerError(f"Platform {role} image archive is missing")
    if archive.stat().st_size != size or sha256_file(archive) != checksum:
        raise InstallerError(f"Platform {role} image archive verification failed")
    return PlatformImage(role, image, source, digest, archive_name, checksum, size)


def load_platform_release(root: Path) -> PlatformRelease:
    path = root / "platform-release.json"
    if not path.is_file() or path.is_symlink():
        raise InstallerError("Platform release manifest is missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstallerError(f"Platform release manifest is invalid JSON: {exc}") from exc
    if not isinstance(value, dict) or value.get("format") != 1:
        raise InstallerError("Platform release manifest format is unsupported")
    version = str(value.get("version") or "")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise InstallerError("Platform release version is invalid")
    source_commit = str(value.get("source_commit") or "")
    if not re.fullmatch(r"[0-9a-f]{7,40}", source_commit):
        raise InstallerError("Platform release source commit is invalid")
    if value.get("workspace_images_included") is not False:
        raise InstallerError("Platform updates must not contain workspace images")
    images = value.get("images")
    if not isinstance(images, dict):
        raise InstallerError("Platform release image index is missing")
    return PlatformRelease(
        version=version,
        source_commit=source_commit,
        controller=_image(root, images.get("controller"), "controller"),
        worker=_image(root, images.get("worker"), "worker"),
    )


def publish_platform_bundle(bundle: Path, downloads_root: Path) -> Path:
    """Atomically publish one verified bundle for enrolled worker upgrades."""
    if not PLATFORM_BUNDLE_PATTERN.fullmatch(bundle.name):
        raise InstallerError("Platform update filename is invalid")
    release_root = downloads_root.resolve() / "releases"
    release_root.mkdir(parents=True, exist_ok=True)
    target = release_root / bundle.name
    temporary = release_root / f".{target.name}.{uuid.uuid4().hex}.partial"
    with bundle.open("rb") as source, temporary.open("xb") as destination:
        shutil.copyfileobj(source, destination, length=1024 * 1024)
    if sha256_file(temporary) != sha256_file(bundle):
        temporary.unlink(missing_ok=True)
        raise InstallerError("Published platform bundle checksum mismatch")
    temporary.replace(target)
    target.with_name(target.name + ".sha256").write_text(
        f"{sha256_file(target)}  {target.name}\n", encoding="ascii"
    )
    return target
