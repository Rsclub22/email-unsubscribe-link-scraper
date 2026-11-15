"""URL guard: the agent's browser may only talk to public web addresses."""

import importlib
from ipaddress import ip_network

import pytest

import guard
from guard import is_public_url, public_addresses


def resolver(table):
    def resolve(host):
        if host not in table:
            raise OSError("unknown host")
        return table[host]
    return resolve


PUBLIC = resolver({"shop.example": ["93.184.216.34"], "v6.example": ["2606:2800:220:1:248:1893:25c8:1946"]})


@pytest.mark.parametrize("url", ["https://shop.example/u?id=1", "http://shop.example", "https://v6.example/x"])
def test_public_addresses_pass(url):
    assert is_public_url(url, PUBLIC) is True


@pytest.mark.parametrize("url", [
    "ftp://shop.example/x", "javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "about:blank",
    "https://user:pw@shop.example/", "https:///nohost", "not a url", ""])
def test_other_schemes_credentials_and_garbage_fail(url):
    assert is_public_url(url, PUBLIC) is False


SITE_NETS = [ip_network("172.60.0.0/16")]


@pytest.mark.parametrize("ip,blocked", [
    ("127.0.0.1", ()), ("10.1.2.3", ()), ("172.16.5.4", ()), ("172.60.0.127", SITE_NETS), ("192.168.8.111", ()),
    ("100.81.0.1", ()), ("169.254.169.254", ()), ("0.0.0.0", ()), ("::1", ()), ("fe80::1", ()),
    ("fd7a:115c:a1e0::53", ()), ("::ffff:192.168.8.111", ()), ("224.0.0.1", ())])
def test_private_and_special_addresses_fail(ip, blocked):
    host = f"[{ip}]" if ":" in ip else ip
    assert is_public_url(f"http://{host}/", resolver({ip: [ip]}), blocked=blocked) is False


@pytest.mark.parametrize("ip", [
    "::127.0.0.1", "::192.168.8.111", "::10.0.0.1",                    # IPv4-compatible (::/96)
    "64:ff9b::7f00:1", "64:ff9b::c0a8:86f", "64:ff9b::a00:1",           # NAT64 (64:ff9b::/96) to private IPv4
    "2002:5db8:d822::1", "2002:c0a8:86f::1",                            # 6to4 (2002::/16), even of a public IPv4
    "2001:0:4136:e378:8000:63bf:3fff:fdd2", "2001::1"])                 # Teredo (2001::/32)
def test_ipv6_forms_with_embedded_or_tunnelled_ipv4_fail(ip):
    assert is_public_url(f"http://[{ip}]/", resolver({ip: [ip]})) is False


@pytest.mark.parametrize("ip", ["2606:2800:220:1:248:1893:25c8:1946", "2a00:1450:4001:82b::200e"])
def test_normal_public_ipv6_literal_passes(ip):
    assert is_public_url(f"http://[{ip}]/", resolver({ip: [ip]})) is True


def test_embedded_ipv4_is_judged_by_the_ipv4_rules_including_site_networks():
    nat64 = "64:ff9b::ac3c:7f"       # 172.60.0.127 behind NAT64
    assert is_public_url(f"http://[{nat64}]/", resolver({nat64: [nat64]})) is True
    assert is_public_url(f"http://[{nat64}]/", resolver({nat64: [nat64]}), blocked=SITE_NETS) is False


def test_public_addresses_returns_the_checked_addresses_or_nothing():
    two = resolver({"two.example": ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
                    "mixed.example": ["93.184.216.34", "10.0.0.5"]})
    assert public_addresses("https://two.example/x", two) == ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]
    assert public_addresses("https://mixed.example/x", two) == []
    assert public_addresses("https://user:pw@two.example/", two) == []
    assert public_addresses("ftp://two.example/", two) == []
    assert public_addresses("https://nowhere.example/", two) == []


def test_site_networks_block_only_when_given():
    r = resolver({"site.example": ["172.60.0.127"]})
    assert is_public_url("https://site.example/", r, blocked=()) is True
    assert is_public_url("https://site.example/", r, blocked=SITE_NETS) is False


@pytest.fixture
def reload_guard(monkeypatch):
    def load(value):
        if value is None:
            monkeypatch.delenv("BLOCKED_NETS", raising=False)
        else:
            monkeypatch.setenv("BLOCKED_NETS", value)
        return importlib.reload(guard)
    yield load
    monkeypatch.delenv("BLOCKED_NETS", raising=False)
    importlib.reload(guard)


def test_blocked_nets_default_comes_from_the_environment(reload_guard):
    g = reload_guard("172.60.0.0/16, 198.51.100.0/24")
    assert g.BLOCKED_NETS == (ip_network("172.60.0.0/16"), ip_network("198.51.100.0/24"))
    assert g.is_public_url("https://site.example/", resolver({"site.example": ["172.60.0.127"]})) is False
    assert g.is_public_url("https://site.example/", resolver({"site.example": ["93.184.216.34"]})) is True


@pytest.mark.parametrize("value", [None, "", " , "])
def test_blocked_nets_empty_or_unset_blocks_nothing_extra(reload_guard, value):
    g = reload_guard(value)
    assert g.BLOCKED_NETS == ()
    assert g.is_public_url("https://site.example/", resolver({"site.example": ["172.60.0.127"]})) is True


@pytest.mark.parametrize("value", ["172.60.0.0/33", "not-a-net", "172.60.0.1/16", "10.0.0.0/8,garbage"])
def test_invalid_blocked_nets_fail_the_start(reload_guard, value):
    with pytest.raises(ValueError, match="BLOCKED_NETS"):
        reload_guard(value)


def test_name_resolving_to_a_private_address_fails():
    assert is_public_url("https://intern.example/", resolver({"intern.example": ["192.168.8.111"]})) is False


def test_one_private_address_among_several_fails():
    assert is_public_url("https://mixed.example/", resolver({"mixed.example": ["93.184.216.34", "10.0.0.5"]})) is False


def test_unresolvable_name_fails():
    assert is_public_url("https://nowhere.example/", PUBLIC) is False
