"""[KSM-TEST-404] The blocking ACME adapter and the authoritative-DNS check
behind `acme_issuer.async_issue` (KSM-BEHAVE-204, #200)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import dns.rdatatype
import pytest
from acme import challenges, messages

from custom_components.kiosk_satellite_manager import acme_issuer
from custom_components.kiosk_satellite_manager.le_certificate import CertificateUnavailable


def _authz(domain, status, challs):
    body = SimpleNamespace(status=status, identifier=SimpleNamespace(value=domain),
                           challenges=[SimpleNamespace(chall=c) for c in challs])
    return SimpleNamespace(body=body)


@pytest.fixture
def client():
    with patch.object(acme_issuer.acme_client, "ClientNetwork") as net, \
            patch.object(acme_issuer.acme_client, "ClientV2") as v2:
        yield SimpleNamespace(net=net, v2=v2, client=v2.return_value)


def test_account_url_rides_on_the_network_and_register_returns_the_uri(client):
    key = acme_issuer.new_key_pem()
    acme_issuer.AcmeProtocol(key, "https://acme.test/acct/1")
    account = client.net.call_args.kwargs["account"]
    assert account.uri == "https://acme.test/acct/1"
    assert client.net.call_args.kwargs["user_agent"] == "kiosk-satellite-manager"
    assert client.v2.get_directory.call_args.args[0] == acme_issuer.ACME_DIRECTORY_URL

    protocol = acme_issuer.AcmeProtocol(key, None)
    assert client.net.call_args.kwargs["account"] is None
    client.client.new_account.return_value = SimpleNamespace(uri="https://acme.test/acct/2")
    assert protocol.register("ops@example.com") == "https://acme.test/acct/2"
    reg = client.client.new_account.call_args.args[0]
    assert reg.terms_of_service_agreed is True and reg.emails == ("ops@example.com",)


def test_new_order_returns_one_dns_challenge_per_pending_authorization(client):
    protocol = acme_issuer.AcmeProtocol(acme_issuer.new_key_pem(), "u")
    dns01 = challenges.DNS01(token=b"t" * 16)
    order = SimpleNamespace(authorizations=[
        _authz("done.cfoxga.com", messages.STATUS_VALID, [dns01]),
        _authz("p.cfoxga.com", messages.STATUS_PENDING, [challenges.HTTP01(token=b"h" * 16), dns01]),
    ])
    client.client.new_order.return_value = order
    got, pending = protocol.new_order(b"csr")
    assert got is order
    assert [(name, chall.chall) for name, _value, chall in pending] == [
        ("_acme-challenge.p.cfoxga.com", dns01)]
    assert pending[0][1] == dns01.validation(protocol._key)

    protocol.answer(pending[0][2])
    assert client.client.answer_challenge.call_args.args[0] is pending[0][2]
    client.client.poll_and_finalize.return_value = SimpleNamespace(fullchain_pem="CHAIN")
    assert protocol.finalize(order, 5) == "CHAIN"
    assert client.client.poll_and_finalize.call_args.args[1].tzinfo is None

    client.client.new_order.return_value = SimpleNamespace(authorizations=[
        _authz("p.cfoxga.com", messages.STATUS_PENDING, [challenges.HTTP01(token=b"h" * 16)])])
    with pytest.raises(CertificateUnavailable, match="no DNS challenge"):
        protocol.new_order(b"csr")


def test_name_server_ips_skip_an_unresolvable_server():
    def resolve(name, _rtype, lifetime):
        if name == "bad.ns.test":
            raise OSError("nxdomain")
        return [SimpleNamespace(address="198.51.100.1"), SimpleNamespace(address="198.51.100.2")]
    with patch.object(acme_issuer.dns.resolver, "resolve", side_effect=resolve):
        assert acme_issuer._ns_ips(("bad.ns.test", "a.ns.test")) == ["198.51.100.1", "198.51.100.2"]


class _RRset(list):
    rdtype = dns.rdatatype.TXT


def _answer(*values):
    return SimpleNamespace(answer=[_RRset(SimpleNamespace(strings=[v.encode()]) for v in values)])


def test_txt_visible_needs_every_authoritative_server():
    """Negative: no servers, one server missing the value, or a query error is not visible."""
    replies = {"198.51.100.1": _answer("v", "old"), "198.51.100.2": _answer("old")}

    def udp(_query, ip, timeout):
        if ip == "198.51.100.3":
            raise OSError("timeout")
        return replies[ip]
    with patch.object(acme_issuer.dns.query, "udp", side_effect=udp):
        assert acme_issuer._txt_visible(["198.51.100.1"], "_acme-challenge.p", "v") is True
        assert acme_issuer._txt_visible(["198.51.100.1", "198.51.100.2"], "_acme-challenge.p", "v") is False
        assert acme_issuer._txt_visible(["198.51.100.3"], "_acme-challenge.p", "v") is False
        assert acme_issuer._txt_visible([], "_acme-challenge.p", "v") is False
