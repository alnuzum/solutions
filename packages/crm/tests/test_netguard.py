"""Which addresses count as public: tunnelled and translated IPv6 forms of IPv4 are refused
whole, since they can carry an internal IPv4 address past an ``is_global`` check."""

import pytest

from alnuzum_crm.netguard import BlockedAddress, check_public_url, is_public, split_https_url

REFUSED = [
    "64:ff9b::a00:1",  # NAT64 well-known prefix → 10.0.0.1
    "64:ff9b::7f00:1",  # NAT64 → 127.0.0.1
    "64:ff9b::808:808",  # NAT64 of a public address: the prefix is refused whole
    "64:ff9b:1::a00:1",  # NAT64 local-use prefix
    "2002:a00:1::1",  # 6to4 of 10.0.0.1
    "2002:808:808::1",  # 6to4 of a public address: refused whole
    "2001:0:4136:e378:8000:63bf:f5ff:fffe",  # Teredo
    "::a00:1",  # IPv4-compatible ::/96
    "::808:808",  # IPv4-compatible of a public address
    "::ffff:10.0.0.1",  # IPv4-mapped
    "fec0::1",  # site-local
    "::",  # unspecified
    "0.0.0.0",  # noqa: S104 - unspecified
    "240.0.0.1",  # reserved
    "255.255.255.255",
    "224.0.0.1",  # multicast
    "ff02::1",
    "2001:db8::1",  # documentation
    "fe80::1",
    "127.0.0.1",
    "10.0.0.1",
    "100.64.0.1",
]


@pytest.mark.parametrize("ip", REFUSED)
def test_non_public_addresses(ip: str) -> None:
    assert not is_public(ip)


@pytest.mark.parametrize("ip", ["93.184.215.14", "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses(ip: str) -> None:
    assert is_public(ip)


def test_split_https_url_normalises() -> None:
    assert split_https_url("https://Odoo.Example.com/odoo/") == (
        "odoo.example.com",
        443,
        "https://odoo.example.com/odoo",
    )
    assert split_https_url("https://[2606:4700:4700::1111]/")[2] == "https://[2606:4700:4700::1111]"
    with pytest.raises(BlockedAddress, match="port"):
        split_https_url("https://odoo.example.com:99999")
    with pytest.raises(BlockedAddress, match="no host"):
        split_https_url("https:///path")


async def test_literal_ip_and_unresolvable_host() -> None:
    with pytest.raises(BlockedAddress, match="non-public"):
        await check_public_url("https://[64:ff9b::a00:1]/")
    with pytest.raises(BlockedAddress, match="does not resolve"):
        await check_public_url("https://no-such-host.invalid/")
