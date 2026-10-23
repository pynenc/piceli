"""The example of ``docs/single_nixos_host.md`` is valid (0.24.0).

The test imports the page's declaration as written and builds, offline, the
OpenTofu configuration ``piceli infra plan`` plans and the ``NixHost``'s
agent configuration and module: no network, no ``tofu``. The host is
bootstrapped onto a tailnet through secret user data: no public port 22.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from piceli.dev.model import DevBuilds
from piceli.hosts.model import agent_config
from piceli.hosts.module import render_module
from piceli.infra import Infrastructure, NixHost
from piceli.infra.machines.render import render

PAGE = Path(__file__).parents[3] / "docs" / "single_nixos_host.md"
_BLOCK = re.compile(r"```python\n(# web_1\.py\n.*?)```", re.S)
IMAGE_SHA256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


@pytest.fixture(autouse=True)
def _built_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What step 2's `dev nix build --image --artifacts-dir images` leaves."""
    images = tmp_path / "images"
    images.mkdir()
    (images / "web-1-image.image.json").write_text(
        json.dumps({"schema": "piceli.nix-image.v1", "sha256": IMAGE_SHA256,
                    "file": "r-1/piceli-nix-out/nixos.qcow2", "format": "qcow2"})
    )  # fmt: skip
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


#: The public key step 1's ``piceli secrets setup`` prints.
PUBLIC = "example-cache-1:jXbGkEMPpvXtlVqGBrqfsnU8Jyn1cZB9u0QhGhvsKP0="
_KEY_NAME = 'key_name="example-cache-1",'


def _example(tmp_path: Path, *, keyed: bool = True) -> ModuleType:
    """The page's declaration; ``keyed``: after step 1, with the public key
    in place of ``key_name=``."""
    found = _BLOCK.findall(PAGE.read_text(encoding="utf-8"))
    assert len(found) == 1, "the page has one `# web_1.py` block"
    assert found[0].count(_KEY_NAME) == 1, "the cache names its key first"
    text = found[0].replace(_KEY_NAME, f"{PUBLIC!r},") if keyed else found[0]
    path = tmp_path / ("web_1.py" if keyed else "web_1_new.py")
    path.write_text(text, encoding="utf-8")
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
    assert sorted({rule["port_range_min"] for rule in rules}) == [80, 443]
    assert all(rule["direction"] == "ingress" for rule in rules)
    records = resources["porkbun_dns_record"].values()
    assert sorted(record["type"] for record in records) == ["A", "AAAA"]
    assert all(record["domain"] == "example.org" for record in records)
    # The tailnet key: a sensitive variable, read at creation only.
    server = resources["openstack_compute_instance_v2"]["server-web-1"]
    assert server["flavor_name"] == "a1-ram2-disk20-perf1"
    assert server["user_data"] == "${var.user_data_web_1}"
    assert server["lifecycle"] == {"ignore_changes": ["user_data"]}
    assert rendered.config["variable"]["user_data_web_1"]["sensitive"] is True
    assert rendered.user_data == {
        "openstack_compute_instance_v2.server-web-1": ("web-1", "web-1-boot")
    }
    assert rendered.digest.startswith("sha256:")
    assert render(_example(tmp_path).infra).digest == rendered.digest  # stable


def test_the_example_as_written_waits_for_its_public_key(tmp_path: Path) -> None:
    from piceli.dev.model import DevError

    example = _example(tmp_path, keyed=False)
    assert example.cache.public_key is None
    assert example.cache.signing_credential == "example-cache-1"
    render(example.infra)  # the bucket needs no signing key
    with pytest.raises(DevError) as raised:
        agent_config(example.host)
    assert raised.value.code == "dev-nix-cache-key-unset"


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
    assert host.ssh_target(None).address == "web-1.tail-example.ts.net"
    assert render_module(host, "web_1.py:host").startswith(
        "# The Piceli host agent of web-1"
    )
    assert isinstance(example.builds, DevBuilds)  # no cluster for builds
    image = example.nixos
    assert image.sha256 == IMAGE_SHA256
    assert Path(str(image.file)) == Path("images/r-1/piceli-nix-out/nixos.qcow2")


def test_host_commands_find_the_host_by_name_with_the_pinned_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    import piceli.hosts.remote as remote
    from piceli.k8s.cli import app

    (tmp_path / "web_1.py").write_text(
        _BLOCK.findall(PAGE.read_text(encoding="utf-8"))[0], encoding="utf-8"
    )
    known = tmp_path / "state" / "piceli" / "infra" / "web-1" / "known_hosts"
    known.parent.mkdir(parents=True)
    known.write_text("web-1.tail-example.ts.net ssh-ed25519 AAAA\n")
    seen: list[dict[str, Any]] = []

    def read_status(target: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append({"address": target.address, **kwargs})
        return {"host": "web-1", "current": {"tag": "v1.0.0"}}

    monkeypatch.setattr(remote, "read_status", read_status)
    done = CliRunner().invoke(
        app, ["host", "wait", "web-1", "v1.0.0", "--infra", "web_1.py", "--json"]
    )
    assert done.exit_code == 0, done.stdout + done.stderr
    assert seen == [{"address": "web-1.tail-example.ts.net", "known_hosts": known}]
    with_attr = CliRunner().invoke(
        app, ["host", "status", "web-1", "--infra", "web_1.py:infra", "--json"]
    )
    assert with_attr.exit_code == 0, with_attr.stdout + with_attr.stderr
    assert seen[-1]["known_hosts"] == known


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


def test_the_page_bootstraps_over_the_tailnet() -> None:
    text = PAGE.read_text(encoding="utf-8")
    assert "Rule.tcp(22" not in text  # port 22 is never open to the internet
    assert "@example.org" not in text.replace("owner@example.org", "")
    assert "single-use, pre-authorized" in text and "one hour" in text
    assert "metadata service" in text
    assert 'authKeyFile = "/etc/ec2-metadata/user-data"' in text
    assert "virtualisation.amazon-init.enable = false" in text
    words = [" ".join(command[:2]) for command in _commands()]
    assert words.index("secrets user-data") < words.index("infra plan")
    assert words.index("infra apply") < words.index("infra register")
    assert words.index("infra register") < words.index("host secrets")


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
