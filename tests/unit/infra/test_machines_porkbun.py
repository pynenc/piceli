"""Porkbun DNS records for servers at other providers: rendering, credentials, ownership."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from piceli.infra import (
    DnsRecord,
    Firewall,
    Hetzner,
    Infrastructure,
    OpenStack,
    Porkbun,
    PrimaryIp,
    Rule,
    Server,
)
from piceli.infra.machines import credentials
from piceli.infra.machines.model import InfraError
from piceli.infra.machines.plan import (
    INVENTORY,
    Workspace,
    _check,
    changes_of,
    estimate,
)
from piceli.infra.machines.porkbun import PORKBUN_PIN
from piceli.infra.machines.render import render
from piceli.infra.machines.state import write_record
from piceli.infra.machines.tofu import TofuResult, _secret_name

from .machines_support import PASSPHRASE, needs_tofu

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests owner@example"
API_KEY = "pk1_" + "a" * 60
SECRET_KEY = "sk1_" + "b" * 60
DNS = Porkbun(credentials="porkbun")
HCLOUD = Hetzner(credentials="hcloud", location="fsn1")
OS = OpenStack(
    credentials="cloud",
    auth_url="https://identity.example.net/v3",
    region="region-1",
    public_network="public",
    prices={"small": 4.7},
    flavors=["small"],
)
PORT = "openstack_networking_port_v2.ip-host-1-ip"
V4 = "${[for ip in " + PORT + '.all_fixed_ips : ip if !strcontains(ip, ":")][0]}'
V6 = "${[for ip in " + PORT + '.all_fixed_ips : ip if strcontains(ip, ":")][0]}'
_ADDRESS = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _host(**overrides: Any) -> Server:
    values: dict[str, Any] = {
        "name": "host-1",
        "provider": OS,
        "type": "small",
        "image": "debian-12",
        "ipv4": PrimaryIp("host-1-ip"),
    }
    values.update(overrides)
    return Server(**values)


def _edge(name: str = "edge-1", **overrides: Any) -> Server:
    values: dict[str, Any] = {
        "provider": HCLOUD,
        "type": "cax11",
        "image": "debian-12",
    }
    values.update(overrides)
    return Server(name, **values)


def _records(infra: Infrastructure) -> dict[str, dict[str, Any]]:
    return render(infra).config["resource"]["porkbun_dns_record"]


def _invalid(match: str) -> Any:
    return pytest.raises(InfraError, match=match)


def test_hetzner_only_declarations_keep_their_digest() -> None:
    """Golden: the digest before Porkbun existed (records resolve the same way)."""
    a = _edge(ipv4=PrimaryIp("edge-1-v4"), firewall=Firewall("edge", [Rule.tcp(443)]),
              ssh_keys=[KEY])  # fmt: skip
    b = _edge("edge-2")
    infra = Infrastructure("edge", servers=[a, b], records=[
        DnsRecord("example.com", "www", "A", servers=[b, a]),
        DnsRecord("example.com", "www", "AAAA", server=a),
        DnsRecord("example.com", "@", "TXT", value="v=spf1 -all", provider=HCLOUD),
        DnsRecord("example.com", "shop", "CNAME", value="www.example.com.",
                  provider=HCLOUD),
    ])  # fmt: skip
    assert render(infra).digest == (
        "sha256:f728b5381224240c2f5bb59ce4959529ef0a59dfaa182a6fbe602a0909fd8429"
    )
    assert DnsRecord("example.com", "www", "A", server=a).ttl == 300


def test_records_at_porkbun_for_an_openstack_server_on_a_port() -> None:
    host = _host()
    infra = Infrastructure("web", servers=[host], records=[
        DnsRecord("example.com", "www", "A", server=host, provider=DNS),
        DnsRecord("example.com", "www", "AAAA", server=host, provider=DNS),
        DnsRecord("example.com", "@", "TXT", value="v=spf1 -all", provider=DNS),
        DnsRecord("example.com", "shop", "CNAME", value="www.example.com",
                  provider=DNS, ttl=3600),
    ])  # fmt: skip
    records = _records(infra)
    assert len(records) == 4
    by_type = {body["type"]: (name, body) for name, body in records.items()}
    name, a = by_type["A"]
    assert name.startswith("record-example-com-www-a-")
    assert a == {"domain": "example.com", "subdomain": "www", "type": "A",
                 "content": V4, "ttl": 600}  # fmt: skip
    assert by_type["AAAA"][1]["content"] == V6
    assert by_type["TXT"][1] == {"domain": "example.com", "subdomain": "",
                                 "type": "TXT", "content": "v=spf1 -all", "ttl": 600}  # fmt: skip
    assert by_type["CNAME"][1]["content"] == "www.example.com"
    assert by_type["CNAME"][1]["ttl"] == 3600
    config = render(infra).config
    assert config["terraform"]["required_providers"]["porkbun"] == {
        "source": "registry.opentofu.org/marcfrederick/porkbun",
        "version": PORKBUN_PIN.version,
    }
    assert config["provider"]["porkbun"] == {}
    # The server's own output still reads the instance's address.
    assert "access_ip_v4" in json.dumps(config["output"])
    assert not _ADDRESS.search(json.dumps(records))


def test_records_at_porkbun_for_hetzner_servers_one_resource_per_answer() -> None:
    a, b = _edge(), _edge("edge-2")
    infra = Infrastructure("edge", servers=[a, b], records=[
        DnsRecord("example.com", "www", "A", servers=[b, a], provider=DNS),
    ])  # fmt: skip
    records = _records(infra)
    assert sorted(body["content"] for body in records.values()) == [
        "${hcloud_server.server-edge-1.ipv4_address}",
        "${hcloud_server.server-edge-2.ipv4_address}",
    ]
    owners = render(infra).resources
    assert {owners[f"porkbun_dns_record.{n}"] for n in records} == {
        ("porkbun", "record", "www.example.com/A")
    }


def test_mixed_providers_in_one_infrastructure() -> None:
    host, edge = _host(), _edge()
    infra = Infrastructure("web", servers=[host, edge], records=[
        DnsRecord("example.com", "www", "A", servers=[host, edge], provider=DNS),
        DnsRecord("example.net", "edge", "A", server=edge),  # at Hetzner
    ])  # fmt: skip
    rendered = render(infra)
    config = rendered.config
    assert set(config["terraform"]["required_providers"]) == {
        "hcloud", "openstack", "porkbun",
    }  # fmt: skip
    assert sorted(b["content"] for b in _records(infra).values()) == sorted(
        [V4, "${hcloud_server.server-edge-1.ipv4_address}"]
    )
    rrset = config["resource"]["hcloud_zone_rrset"]["record-example-net-edge-a"]
    assert rrset["records"] == [
        {"value": "${hcloud_server.server-edge-1.ipv4_address}"}
    ]
    for source in (
        "hetznercloud/hcloud",
        "terraform-provider-openstack/openstack",
        "marcfrederick/porkbun",
    ):
        assert f'provider "registry.opentofu.org/{source}"' in rendered.lock
    assert render(infra).config_text == rendered.config_text


def test_records_follow_a_replaced_server_through_references() -> None:
    def build(image: str, ipv4: Any) -> dict[str, dict[str, Any]]:
        host = _host(image=image, ipv4=ipv4)
        return _records(Infrastructure("web", servers=[host], records=[
            DnsRecord("example.com", "www", "A", server=host, provider=DNS)]))  # fmt: skip

    # On a port: the record names the port, not the server; replacing the
    # server (another image) changes nothing in the record.
    port = PrimaryIp("host-1-ip")
    assert build("debian-12", port) == build("debian-13", port)
    # Without a port: the instance's address; the record keeps its resource
    # and its content is the new instance's address after the replacement.
    plain = build("debian-12", True)
    assert plain == build("debian-13", True)
    ((_, body),) = plain.items()
    assert body["content"] == (
        "${openstack_compute_instance_v2.server-host-1.access_ip_v4}"
    )


def test_porkbun_validation() -> None:
    host = _host()
    with _invalid("holds no servers"):
        _host(provider=DNS)
    with _invalid("serves no DNS records here .*Porkbun"):
        DnsRecord("example.com", "www", "A", server=host)
    with _invalid("600..86400 at porkbun"):
        DnsRecord("example.com", "www", "A", server=host, provider=DNS, ttl=300)
    with _invalid("credentials"):
        Porkbun()
    with _invalid("credential name"):
        Porkbun(credentials="Not A Name")
    with _invalid("two different porkbun"):
        Infrastructure("web", servers=[host], records=[
            DnsRecord("example.com", "a", "A", server=host, provider=DNS),
            DnsRecord("example.com", "b", "A", server=host,
                      provider=Porkbun(credentials="other")),
        ])  # fmt: skip
    with pytest.raises(InfraError, match="not servers"):
        DNS.address(render(Infrastructure("web", servers=[host])).config, "ipv4")  # type: ignore[arg-type]


def test_credentials_go_to_the_environment_only() -> None:
    assert DNS.credential_env({"api_key": API_KEY, "secret_api_key": SECRET_KEY}) == {
        "PORKBUN_API_KEY": API_KEY,
        "PORKBUN_SECRET_API_KEY": SECRET_KEY,
    }
    with pytest.raises(InfraError) as caught:
        DNS.credential_env({"token": SECRET_KEY})
    assert caught.value.code == "infra-credential-invalid"
    assert SECRET_KEY not in str(caught.value)
    assert _secret_name("PORKBUN_API_KEY") and _secret_name("PORKBUN_SECRET_API_KEY")
    host = _host()
    infra = Infrastructure("web", servers=[host], records=[
        DnsRecord("example.com", "www", "A", server=host, provider=DNS)])  # fmt: skip
    rendered = render(infra)
    text = rendered.config_text + rendered.lock + json.dumps(infra.describe())
    for secret in (API_KEY, SECRET_KEY, "PORKBUN_", "apikey", "api_key"):
        assert secret not in text


def test_secrets_command_refuses_porkbun_keys_in_the_environment(
    homes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from piceli.k8s.cli import app

    monkeypatch.setenv("PORKBUN_SECRET_API_KEY", SECRET_KEY)
    result = CliRunner().invoke(app, ["secrets", "provider", "porkbun", "--prompt"],
                                input=f"{API_KEY}:{SECRET_KEY}\n")  # fmt: skip
    assert result.exit_code == 2
    assert "infra-credential-refused" in result.stdout
    assert SECRET_KEY not in result.stdout + result.stderr
    assert not (homes / "credentials" / "porkbun.json").exists()


def _workspace(tmp_path: Path) -> Workspace:
    host = _host()
    infra = Infrastructure("web", servers=[host], records=[
        DnsRecord("example.com", "www", "A", server=host, provider=DNS),
        DnsRecord("example.com", "@", "TXT", value="v=spf1 -all", provider=DNS),
    ], state_dir=tmp_path)  # fmt: skip
    return Workspace(infra, tmp_path, None, render(infra), {})  # type: ignore[arg-type]


def test_destroy_refuses_records_piceli_did_not_create(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    records = sorted(a for a in ws.rendered.resources if a.startswith("porkbun_"))
    assert len(records) == 2
    changes = [
        {"address": address, "mode": "managed",
         "change": {"actions": ["delete"],
                    "before": {"domain": "example.com", "notes": "", "id": 1}}}
        for address in records
    ]  # fmt: skip
    write_record(tmp_path, INVENTORY, {"created": records[:1]})
    found, foreign = changes_of(ws, {"resource_changes": changes})
    assert foreign == (records[1],)
    assert {c.provider for c in found} == {"porkbun"}
    write_record(tmp_path, INVENTORY, {"created": records})
    assert changes_of(ws, {"resource_changes": changes})[1] == ()
    # An address Piceli never rendered nor recorded is foreign.
    stray = "porkbun_dns_record.by-hand"
    write_record(tmp_path, INVENTORY, {"created": [*records, stray]})
    doomed = [{**changes[0], "address": stray}]
    assert changes_of(ws, {"resource_changes": doomed})[1] == (stray,)


def test_records_add_no_estimate_lines(tmp_path: Path) -> None:
    result = estimate(_workspace(tmp_path))
    assert result is not None
    assert [item["kind"] for item in result["items"]] == ["server", "ip"]


def _diagnostic(summary: str, detail: str) -> str:
    return json.dumps({"type": "diagnostic", "diagnostic": {
        "severity": "error", "summary": summary, "detail": detail}})  # fmt: skip


@pytest.mark.parametrize(
    "detail",
    [
        "POST https://api.porkbun.com/api/json/v3/dns/create/example.com: 400 "
        "Domain is not opted in to API access.",
        "porkbun: API_ACCESS_DISABLED",
    ],
)
def test_a_domain_without_api_access_has_its_own_code(
    tmp_path: Path, detail: str
) -> None:
    ws = _workspace(tmp_path)
    result = TofuResult(1, _diagnostic("Error Creating DNS Record", detail), "")
    with pytest.raises(InfraError) as caught:
        _check(result, "infra-tofu-failed", "plan", ws)
    assert caught.value.code == "infra-dns-api-disabled"
    assert "opted" not in str(caught.value) and "api.porkbun" not in str(caught.value)
    other = TofuResult(1, _diagnostic("Error Creating DNS Record", "rate limited"), "")
    with pytest.raises(InfraError) as caught:
        _check(other, "infra-tofu-failed", "plan", ws)
    assert caught.value.code == "infra-tofu-failed"


def test_pin_has_every_platform() -> None:
    assert sum(h.startswith("h1:") for h in PORKBUN_PIN.hashes) == 13
    assert sum(h.startswith("zh:") for h in PORKBUN_PIN.hashes) == 14
    host = _host()
    lock = render(Infrastructure("web", servers=[host], records=[
        DnsRecord("example.com", "www", "A", server=host, provider=DNS)])).lock  # fmt: skip
    assert 'provider "registry.opentofu.org/marcfrederick/porkbun"' in lock


@needs_tofu
@pytest.mark.skipif(
    os.environ.get("PICELI_TOFU_NETWORK") != "1",
    reason="downloads the pinned providers (set PICELI_TOFU_NETWORK=1)",
)
@pytest.mark.timeout(300)
def test_the_pinned_providers_validate_the_configuration(homes: Path) -> None:
    """``tofu init`` verifies the pinned hashes; ``tofu validate`` accepts every record."""
    from piceli.infra.machines.plan import workspace

    credentials.save_credential("web-state", "state-key", PASSPHRASE)
    credentials.save_credential("cloud", "provider-token", "id:secret")
    credentials.save_credential("hcloud", "provider-token", "h" * 64)
    credentials.save_credential("porkbun", "provider-token", f"{API_KEY}:{SECRET_KEY}")
    host, plain, edge = _host(), _host(name="host-2", ipv4=True), _edge()
    infra = Infrastructure(
        "web",
        servers=[host, plain, edge],
        records=[
            DnsRecord("example.com", "www", "A", servers=[host, plain, edge],
                      provider=DNS),
            DnsRecord("example.com", "www", "AAAA", server=host, provider=DNS),
            DnsRecord("example.com", "@", "TXT", value="v=spf1 -all", provider=DNS),
            DnsRecord("example.com", "shop", "CNAME", value="www.example.com",
                      provider=DNS),
        ],
        state_dir=homes / "state",
    )  # fmt: skip
    with workspace(infra) as ws:
        validated = ws.run("validate", "-json")
    assert validated.code == 0, validated.stdout
    assert json.loads(validated.stdout)["warning_count"] == 0, validated.stdout
    assert SECRET_KEY not in validated.stdout and API_KEY not in validated.stdout


# ------------------------------------------------------------ plan-time API access
CREDENTIAL = f"{API_KEY}:{SECRET_KEY}"
ENABLED = b'{"status": "SUCCESS", "cloudflare": "enabled", "records": []}'
DISABLED = b'{"status": "ERROR", "message": "Domain is not opted in to API access."}'


def _answers(**by_domain: tuple[int, bytes] | Exception) -> Any:
    """A transport answering per domain (``example_com`` for example.com)."""
    seen: list[tuple[str, dict[str, Any]]] = []

    def post(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
        domain = url.rsplit("/", 1)[1]
        seen.append((url, json.loads(body)))
        answer = by_domain[domain.replace(".", "_")]
        if isinstance(answer, Exception):
            raise answer
        return answer

    post.seen = seen  # type: ignore[attr-defined]
    return post


def test_api_access_asks_porkbun_for_each_domain() -> None:
    from piceli.infra.machines.porkbun import API, api_access

    post = _answers(example_com=(200, ENABLED), example_net=(200, ENABLED))
    assert api_access(["example.net", "example.com"], CREDENTIAL, post=post) == {
        "example.com": "enabled",
        "example.net": "enabled",
    }
    assert post.seen == [
        (f"{API}/dns/retrieve/example.com",
         {"apikey": API_KEY, "secretapikey": SECRET_KEY}),
        (f"{API}/dns/retrieve/example.net",
         {"apikey": API_KEY, "secretapikey": SECRET_KEY}),
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        ((400, DISABLED), "infra-dns-api-disabled"),
        ((200, b'{"status": "ERROR", "message": "API_ACCESS_DISABLED"}'),
         "infra-dns-api-disabled"),
        ((400, b'{"status": "ERROR", "message": "Invalid API key. (002)"}'),
         "infra-dns-check-failed"),
        ((503, b"<html>busy</html>"), "infra-dns-check-failed"),
        ((200, b"[]"), "infra-dns-check-failed"),
        (TimeoutError("timed out"), "infra-dns-check-failed"),
        (OSError("no route to host"), "infra-dns-check-failed"),
    ],
)  # fmt: skip
def test_api_access_refuses_what_it_could_not_confirm(answer: Any, code: str) -> None:
    from piceli.infra.machines.porkbun import api_access

    with pytest.raises(InfraError) as caught:
        api_access(["example.com"], CREDENTIAL, post=_answers(example_com=answer))
    assert caught.value.code == code
    text = str(caught.value) + " ".join(caught.value.details)
    assert "example.com" in str(caught.value)
    for private in (API_KEY, SECRET_KEY, "opted", "Invalid API key", "busy"):
        assert private not in text


def test_api_access_needs_the_key_pair() -> None:
    from piceli.infra.machines.porkbun import api_access

    for credential in (None, SECRET_KEY):
        with pytest.raises(InfraError) as caught:
            api_access(["example.com"], credential, post=_answers())
        assert caught.value.code in {
            "infra-dns-check-failed",
            "infra-credential-invalid",
        }
        assert SECRET_KEY not in str(caught.value)


def test_api_access_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default transport against a fake Porkbun API (never the real one)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from piceli.infra.machines import porkbun

    requests: list[tuple[str, dict[str, Any]]] = []

    class Fake(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, json.loads(body)))
            answer = DISABLED if self.path.endswith("/example.net") else ENABLED
            self.send_response(400 if answer is DISABLED else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/api/json/v3"
        monkeypatch.setattr(porkbun, "API", base)
        assert porkbun.api_access(["example.com"], CREDENTIAL) == {
            "example.com": "enabled"
        }
        with pytest.raises(InfraError) as caught:
            porkbun.api_access(["example.net"], CREDENTIAL)
        assert caught.value.code == "infra-dns-api-disabled"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=60)
    assert [path for path, _ in requests] == [
        "/api/json/v3/dns/retrieve/example.com",
        "/api/json/v3/dns/retrieve/example.net",
    ]
    assert requests[0][1] == {"apikey": API_KEY, "secretapikey": SECRET_KEY}


def _planning(tmp_path: Path, document: dict[str, Any], **tokens: str) -> Workspace:
    from .machines_support import ScriptedTofu

    ws = _workspace(tmp_path)
    ws.tofu = ScriptedTofu(document)  # type: ignore[assignment]
    ws.tokens = dict(tokens)
    return ws


def _record_changes(ws: Workspace, action: str = "create") -> dict[str, Any]:
    from .machines_support import planned

    records = sorted(a for a in ws.rendered.resources if a.startswith("porkbun_"))
    after = {"domain": "example.com", "subdomain": "www", "type": "A"}
    return planned(*((r, action, after) for r in records), infra="web")


@pytest.mark.parametrize("destroy", [False, True])
def test_a_plan_checks_api_access_of_the_domains_it_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, destroy: bool
) -> None:
    from piceli.infra.machines import porkbun
    from piceli.infra.machines.plan import plan

    ws = _planning(tmp_path, {}, porkbun=CREDENTIAL)
    document = _record_changes(ws, "delete" if destroy else "create")
    write_record(tmp_path, INVENTORY, {"created": sorted(ws.rendered.resources)})
    ws.tofu.plan = document  # type: ignore[attr-defined]
    post = _answers(example_com=(200, ENABLED))
    monkeypatch.setattr(porkbun, "_post", post)
    result = plan(ws, destroy=destroy)
    body = result.to_dict()
    conflicts = (
        {"check": "porkbun-record-conflicts", "ran": False,
         "why": "the plan creates no records at Porkbun"}
        if destroy
        else {"check": "porkbun-record-conflicts", "ran": True, "creates": 2,
              "foreign": 0, "conflicts": [], "warnings": []}
    )  # fmt: skip
    assert body["checks"] == [
        {"check": "porkbun-api-access", "ran": True,
         "domains": {"example.com": "enabled"}},
        conflicts,
    ]  # fmt: skip
    assert len(post.seen) == 1  # one call per domain, not per record
    assert SECRET_KEY not in json.dumps(body) and API_KEY not in json.dumps(body)
    # The check is not part of what is approved.
    monkeypatch.setattr(porkbun, "_post", _answers(example_com=(200, ENABLED)))
    assert plan(ws, destroy=destroy).plan_hash == result.plan_hash


def test_a_plan_fails_for_a_domain_without_api_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.infra.machines import porkbun
    from piceli.infra.machines.plan import plan

    ws = _planning(tmp_path, {}, porkbun=CREDENTIAL)
    ws.tofu.plan = _record_changes(ws)  # type: ignore[attr-defined]
    monkeypatch.setattr(porkbun, "_post", _answers(example_com=(400, DISABLED)))
    with pytest.raises(InfraError) as caught:
        plan(ws)
    assert caught.value.code == "infra-dns-api-disabled"
    assert "example.com" in str(caught.value)
    # Without a key pair the check cannot run: the plan does not pass it.
    ws.tokens = {}
    monkeypatch.setattr(porkbun, "_post", _answers(example_com=(200, ENABLED)))
    with pytest.raises(InfraError) as caught:
        plan(ws)
    assert caught.value.code == "infra-dns-check-failed"


def test_a_plan_without_record_changes_says_the_check_did_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.infra.machines import porkbun
    from piceli.infra.machines.plan import plan

    from .machines_support import planned

    ws = _planning(tmp_path, planned(("openstack_compute_instance_v2.server-host-1",
                                      "create", {})))  # fmt: skip
    monkeypatch.setattr(porkbun, "_post", _answers())  # any call fails the test
    assert plan(ws).to_dict()["checks"] == [
        {"check": "porkbun-api-access", "ran": False,
         "why": "no record changes at Porkbun", "domains": {}},
        {"check": "porkbun-record-conflicts", "ran": False,
         "why": "no record changes at Porkbun"},
    ]  # fmt: skip
