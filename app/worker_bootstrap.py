from __future__ import annotations

import hashlib
import ipaddress
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.download_config import normalize_public_base_url
from app.ingress_settings import (
    certificate_public_key_pin,
    ingress_manager,
    validate_agent_ca_bundle,
)
from app.models.download_settings import DownloadSettings
from app.models.worker_bootstrap_ticket import WorkerBootstrapTicket
from app.release_catalog import PublishedRelease, latest_release


WORKER_BOOTSTRAP_TTL_SECONDS = 10 * 60


@dataclass(frozen=True)
class WorkerBootstrapTransport:
    controller_url: str
    fallback_ipv4: str = ""
    certificate_pin: str = ""
    agent_ca_pem: bytes = b""

    @property
    def hostname(self) -> str:
        return urlsplit(self.controller_url).hostname or ""

    @property
    def port(self) -> int:
        parsed = urlsplit(self.controller_url)
        return parsed.port or (443 if parsed.scheme == "https" else 80)


def ticket_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_ticket_token() -> str:
    return secrets.token_urlsafe(32)


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def controller_base_url(request: Request, db: AsyncSession) -> str:
    record = await db.get(DownloadSettings, 1)
    if record and record.https_enabled and record.https_hostname:
        configured = f"https://{record.https_hostname}"
    else:
        configured = (
            record.public_base_url
            if record and record.public_base_url
            else settings.DOWNLOAD_PUBLIC_BASE_URL
        )
    value = configured.strip() or str(request.base_url).strip()
    try:
        normalized = normalize_public_base_url(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail="Geçerli public Controller URL ayarlanmamış.",
        ) from exc
    parsed = urlsplit(normalized)
    if parsed.scheme == "https":
        try:
            ipaddress.ip_address(parsed.hostname or "")
        except ValueError:
            pass
        else:
            raise HTTPException(
                status_code=500,
                detail="HTTPS worker bağlantısı IP yerine sertifikanın kapsadığı FQDN'i kullanmalıdır.",
            )
    return normalized


def require_https_controller_url(request: Request, controller_url: str) -> None:
    configured_scheme = urlsplit(controller_url).scheme
    request_scheme = str(request.scope.get("scheme") or "").lower()
    if configured_scheme != "https" or request_scheme != "https":
        raise HTTPException(
            status_code=409,
            detail=(
                "Uzak worker kurulumu için önce HTTPS'i etkinleştirin ve "
                "Controller FQDN'ini kaydedin."
            ),
        )


async def worker_bootstrap_transport(
    request: Request,
    db: AsyncSession,
) -> WorkerBootstrapTransport:
    """Return canonical FQDN identity plus optional pinned bootstrap routing."""
    controller_url = await controller_base_url(request, db)
    record = await db.get(DownloadSettings, 1)
    fallback_ipv4 = str(
        getattr(record, "worker_fallback_ipv4", "") or ""
    ).strip()
    if fallback_ipv4:
        try:
            address = ipaddress.ip_address(fallback_ipv4)
        except ValueError as exc:
            raise HTTPException(
                status_code=500,
                detail="Worker fallback IPv4 ayarı geçersiz.",
            ) from exc
        if address.version != 4 or address.is_unspecified or address.is_multicast:
            raise HTTPException(
                status_code=500,
                detail="Worker fallback IPv4 ayarı geçersiz.",
            )
        fallback_ipv4 = str(address)

    certificate_pin = ""
    agent_ca_pem = b""
    if urlsplit(controller_url).scheme == "https":
        try:
            certificate_pem = ingress_manager.certificate_path.read_bytes()
        except OSError:
            certificate_pem = b""
        if certificate_pem:
            certificate_pin = certificate_public_key_pin(certificate_pem)
        try:
            agent_ca_pem = ingress_manager.agent_ca_path.read_bytes()
        except OSError:
            agent_ca_pem = b""
        if agent_ca_pem:
            validate_agent_ca_bundle(agent_ca_pem)
    return WorkerBootstrapTransport(
        controller_url=controller_url,
        fallback_ipv4=fallback_ipv4,
        certificate_pin=certificate_pin,
        agent_ca_pem=agent_ca_pem,
    )


def bootstrap_install_command(
    install_url: str,
    transport: WorkerBootstrapTransport,
) -> str:
    """Build an FQDN-first command with a pin-authenticated recovery attempt."""
    import shlex

    primary = f"curl -fsSL {shlex.quote(install_url)}"
    recovery_commands: list[str] = []
    if transport.certificate_pin:
        # --insecure disables PKI validation only for this first fetch; the
        # authenticated Admin-provided SPKI pin remains mandatory.
        pinned_args = [
            "curl",
            "-fsSL",
            "--insecure",
            "--pinnedpubkey",
            transport.certificate_pin,
            install_url,
        ]
        recovery_commands.append(
            " ".join(shlex.quote(argument) for argument in pinned_args)
        )
    if transport.fallback_ipv4:
        fallback_args = ["curl", "-fsSL"]
        if transport.certificate_pin:
            fallback_args.extend(
                ["--insecure", "--pinnedpubkey", transport.certificate_pin]
            )
        fallback_args.extend(
            [
                "--resolve",
                f"{transport.hostname}:{transport.port}:{transport.fallback_ipv4}",
                install_url,
            ]
        )
        recovery_commands.append(
            " ".join(shlex.quote(argument) for argument in fallback_args)
        )
    if not recovery_commands:
        return f"{primary} | sudo bash"
    attempts = " || ".join([primary, *recovery_commands])
    pipeline = f"( {attempts} ) | sudo bash"
    return f"bash -o pipefail -c {shlex.quote(pipeline)}"


def current_platform_release() -> PublishedRelease:
    release = latest_release(Path(settings.DOWNLOADS_ROOT))
    if release is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Worker kurulumu için yayımlanmış platform release bulunamadı. "
                "Önce controller platform paketini yayımlayın."
            ),
        )
    return release


async def active_ticket(token: str, db: AsyncSession) -> WorkerBootstrapTicket:
    if not token or len(token) > 256 or any(character.isspace() for character in token):
        raise HTTPException(status_code=404, detail="Worker kurulum bileti bulunamadı.")
    record = (
        await db.execute(
            select(WorkerBootstrapTicket).where(
                WorkerBootstrapTicket.token_hash == ticket_hash(token)
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Worker kurulum bileti bulunamadı.")
    if record.used_at is not None:
        raise HTTPException(status_code=410, detail="Worker kurulum bileti kullanılmış.")
    if utc_datetime(record.expires_at) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Worker kurulum biletinin süresi dolmuş.")
    return record
