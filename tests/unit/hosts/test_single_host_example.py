"""The example of ``docs/single_nixos_host.md`` is valid (0.24.0).

The test imports the page's declaration as written and builds, offline, the
OpenTofu configuration ``piceli infra plan`` plans and the ``NixHost``'s
agent configuration and module: no network, no ``tofu``.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

from piceli.hosts.model import agent_config
from piceli.hosts.module import render_module
from piceli.infra import Cluster, Infrastructure, NixHost
from piceli.infra.machines.render import render

PAGE = Path(__file__).parents[3] / "docs" / "single_nixos_host.md"
_BLOCK = re.compile(r"```python\n(# web_1\.py\n.*?)```", re.S)


def _example(tmp_path: Path) -> ModuleType:
    found = _BLOCK.findall(PAGE.read_text(encoding="utf-8"))
    assert len(found) == 1, "the page has one `# web_1.py` block"
    path = tmp_path / "web_1.py"
    path.write_text(found[0], encoding="utf-8")
    spec = importlib.util.spec_from_file_location("single_host_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_example_renders_the_infrastructure(tmp_path: Path) -> None:
    example = _example(tmp_path)
    assert isinstance(example.infra, Infrastructure)
    rendered = render(example.infra)
    resources = rendered.config["resource"]
    assert set(resources) == {
        "openstack_images_image_v2", "openstack_networking_port_v2",
        "openstack_networking_secgroup_v2", "openstack_networking_secgroup_rule_v2",
        "openstack_compute_keypair_v2", "openstack_compute_instance_v2",
        "openstack_objectstorage_container_v1", "porkbun_dns_record",
    }  # fmt: skip
    rules = resources["openstack_networking_secgroup_rule_v2"].values()
    assert sorted({rule["port_range_min"] for rule in rules}) == [22, 80, 443]
    assert all(rule["direction"] == "ingress" for rule in rules)
    records = resources["porkbun_dns_record"].values()
    assert sorted(record["type"] for record in records) == ["A", "AAAA"]
    assert all(record["domain"] == "example.org" for record in records)
    assert rendered.digest.startswith("sha256:")
    assert render(_example(tmp_path).infra).digest == rendered.digest  # stable


def test_the_example_declares_the_host_and_its_builds(tmp_path: Path) -> None:
    example = _example(tmp_path)
    host = example.host
    assert isinstance(host, NixHost)
    config = agent_config(host)
    assert config["follow"] == {"kind": "tag", "pattern": "v*", "prereleases": False}
    assert config["cache"]["objects"]["bucket"] == example.nix_bucket.name
    assert [check["kind"] for check in config["checks"]] == ["unit", "http"]
    assert config["report"]["endpoint"] == "https://otel.example.org:4318/v1/logs"
    # The host's secrets are sops-nix's: the cache's read keys, the headers.
    assert config["read_credentials_file"] == "/run/secrets/piceli/cache.env"
    assert config["report"]["headers_file"] == "/run/secrets/piceli/otlp-headers"
    assert host.age_identity == "web-1-age"
    assert host.age_key_file == "/var/lib/sops-nix/key.txt"
    assert "web-1-age" not in json.dumps(config)  # names stay off the host
    assert "nix-cache-write" not in json.dumps(config)
    assert host.ssh_target(None).address == "example.org"
    assert render_module(host, "web_1.py:host").startswith(
        "# The Piceli host agent of web-1"
    )
    assert isinstance(example.builds, Cluster) and example.builds.dev is not None


def _commands() -> list[list[str]]:
    """Every ``piceli ...`` command of the page's shell blocks, as words."""
    text = PAGE.read_text(encoding="utf-8")
    lines: list[list[str]] = []
    for block in re.findall(r"```sh\n(.*?)```", text, re.S):
        joined = block.replace("\\\n", " ")
        for line in joined.splitlines():
            line = line.split("#", 1)[0]
            for part in re.split(r"\|", line):
                words = part.split()
                if "piceli" in words:
                    lines.append(words[words.index("piceli") + 1 :])
    return lines


def test_the_page_puts_no_secret_on_the_host_by_hand() -> None:
    text = PAGE.read_text(encoding="utf-8")
    assert "/etc/piceli-host" not in text and "install -m 600" not in text
    assert 'sops.secrets."piceli/cache.env"' in text
    words = [" ".join(command[:2]) for command in _commands()]
    for command in ("secrets age", "host secrets", "host rollback", "host pin",
                    "host unpin"):  # fmt: skip
        assert command in words, command


def test_the_page_uses_real_commands_and_options() -> None:
    import typer

    from piceli.k8s.cli import app

    root = typer.main.get_command(app)
    commands = _commands()
    assert len(commands) >= 12
    for words in commands:
        command, rest = root, list(words)
        while hasattr(command, "commands"):  # a group: the next word is its command
            assert rest, words
            found = command.commands.get(rest.pop(0))
            assert found is not None, f"no command: piceli {' '.join(words)}"
            command = found
        options = {opt for param in command.params for opt in param.opts}
        for word in rest:
            if word.startswith("--"):
                assert word in options, f"piceli {' '.join(words)}: {word}"
