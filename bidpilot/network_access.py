from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable

AUTO_TRUSTED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(value)
    for value in (
        "10.0.0.0/8",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
        "fe80::/10",
    )
)


def _tokens(value: str) -> list[str]:
    return [item for item in re.split(r"[,;\s]+", value.strip()) if item]


def parse_trusted_networks(
    value: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse a bounded private-network allowlist without trusting proxy headers."""

    tokens = _tokens(value)
    if not tokens:
        raise ValueError("可信网段不能为空；使用 auto 自动允许常见私有局域网")
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for token in tokens:
        if token.lower() == "auto":
            networks.extend(AUTO_TRUSTED_NETWORKS)
            continue
        try:
            network = ipaddress.ip_network(token, strict=False)
        except ValueError as exc:
            raise ValueError(f"可信网段格式无效：{token}") from exc
        if not any(
            network.version == allowed.version and network.subnet_of(allowed)
            for allowed in AUTO_TRUSTED_NETWORKS
        ):
            raise ValueError(f"可信网段必须位于私有、链路本地或组网地址范围内：{token}")
        networks.append(network)
    return tuple(dict.fromkeys(networks))


def client_in_trusted_networks(client_host: str, configured: str) -> bool:
    try:
        address = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in network for network in parse_trusted_networks(configured))


def networks_as_text(
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str:
    return ",".join(str(network) for network in networks)
