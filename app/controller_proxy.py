"""Derive the narrow proxy allow-list for the containerized controller."""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
from pathlib import Path


ROUTE_UP = 0x1
ROUTE_GATEWAY = 0x2


def _valid_proxy_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise ValueError(f"Trusted proxy value is not an IP address: {value!r}") from exc
    if address.is_unspecified or address.is_multicast:
        raise ValueError(f"Trusted proxy IP is not usable: {address}")
    return str(address)


def default_ipv4_gateways(route_path: Path = Path("/proc/net/route")) -> list[str]:
    """Return active default-route gateways visible in this network namespace."""
    try:
        lines = route_path.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Cannot read container routes from {route_path}: {exc}") from exc

    gateways: list[str] = []
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            flags = int(fields[3], 16)
            packed = struct.pack("<I", int(fields[2], 16))
            gateway = socket.inet_ntoa(packed)
        except (OSError, ValueError, struct.error):
            continue
        if flags & (ROUTE_UP | ROUTE_GATEWAY) != (ROUTE_UP | ROUTE_GATEWAY):
            continue
        normalized = _valid_proxy_ip(gateway)
        if normalized not in gateways:
            gateways.append(normalized)
    return gateways


def trusted_proxy_ips() -> list[str]:
    """Build an exact-IP allow-list; never trust a wildcard or an entire subnet."""
    configured = os.environ.get("FORWARDED_ALLOW_IPS", "").strip()
    if configured:
        candidates = [part.strip() for part in configured.split(",") if part.strip()]
        if not candidates:
            raise RuntimeError("FORWARDED_ALLOW_IPS does not contain an IP address")
        proxies = [_valid_proxy_ip(candidate) for candidate in candidates]
    else:
        proxies = ["127.0.0.1", *default_ipv4_gateways()]

    unique: list[str] = []
    for proxy in proxies:
        if proxy not in unique:
            unique.append(proxy)
    if not any(not ipaddress.ip_address(proxy).is_loopback for proxy in unique):
        raise RuntimeError(
            "Container proxy gateway could not be determined; refusing broad forwarded-header trust"
        )
    return unique


def main() -> int:
    print(",".join(trusted_proxy_ips()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
