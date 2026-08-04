from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from urllib.parse import urlparse

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
PROXY_TRUST_BOUNDARIES = AUTO_TRUSTED_NETWORKS + (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
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


def parse_trusted_proxy_networks(
    value: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse explicit proxy CIDRs; loopback is allowed, public and wildcard ranges are not."""

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for token in _tokens(value):
        try:
            network = ipaddress.ip_network(token, strict=False)
        except ValueError as exc:
            raise ValueError(f"受信代理网段格式无效：{token}") from exc
        if not any(
            network.version == allowed.version and network.subnet_of(allowed)
            for allowed in PROXY_TRUST_BOUNDARIES
        ):
            raise ValueError(f"受信代理必须是明确的私有或回环网段：{token}")
        networks.append(network)
    return tuple(dict.fromkeys(networks))


def parse_https_origins(value: str) -> tuple[str, ...]:
    """Return normalized, exact HTTPS browser origins."""

    origins: list[str] = []
    for token in _tokens(value):
        parsed = urlparse(token)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"企业浏览器来源必须是无路径的完整 HTTPS Origin：{token}")
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
        normalized = f"https://{host}{port}"
        origins.append(normalized)
    return tuple(dict.fromkeys(origins))


def resolve_forwarded_client(
    peer_host: str,
    forwarded_for: str,
    trusted_proxies: str,
) -> tuple[str, bool]:
    """Resolve X-Forwarded-For only when every hop to the selected client is trusted.

    Returns ``(client_ip, used_forwarded_header)``. Malformed or untrusted headers are
    ignored, so callers never grant access based on a user-supplied forwarding header.
    """

    try:
        peer = ipaddress.ip_address(peer_host)
    except ValueError:
        return peer_host, False
    if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
        peer = peer.ipv4_mapped
    proxy_networks = parse_trusted_proxy_networks(trusted_proxies)
    if not proxy_networks or not any(peer in network for network in proxy_networks):
        return str(peer), False

    chain: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for item in forwarded_for.split(","):
        try:
            address = ipaddress.ip_address(item.strip())
        except ValueError:
            return str(peer), False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        chain.append(address)
    if not chain:
        return str(peer), False

    for address in reversed(chain):
        if any(address in network for network in proxy_networks):
            continue
        return str(address), True
    return str(chain[0]), True


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
