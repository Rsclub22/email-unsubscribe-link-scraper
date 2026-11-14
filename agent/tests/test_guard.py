"""URL guard: the agent's browser may only talk to public web addresses."""

import pytest

from guard import is_public_url


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


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.1.2.3", "172.16.5.4", "172.60.0.127", "192.168.8.111", "100.81.0.1", "169.254.169.254",
    "0.0.0.0", "::1", "fe80::1", "fd7a:115c:a1e0::53", "::ffff:192.168.8.111", "224.0.0.1"])
def test_private_and_special_addresses_fail(ip):
    host = f"[{ip}]" if ":" in ip else ip
    assert is_public_url(f"http://{host}/", resolver({ip: [ip]})) is False


def test_name_resolving_to_a_private_address_fails():
    assert is_public_url("https://intern.example/", resolver({"intern.example": ["192.168.8.111"]})) is False


def test_one_private_address_among_several_fails():
    assert is_public_url("https://mixed.example/", resolver({"mixed.example": ["93.184.216.34", "10.0.0.5"]})) is False


def test_unresolvable_name_fails():
    assert is_public_url("https://nowhere.example/", PUBLIC) is False
