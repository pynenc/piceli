"""Build and install distributable assets, then serve them without Node on PATH."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

INSTALLED_CHECK = r"""
import gzip
import json
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

installed = Path(sys.argv[1])
sys.path.insert(0, str(installed))
import piceli
from fastapi.testclient import TestClient
from piceli.server.app import create_app
from piceli.services.query import QueryService

assert Path(piceli.__file__).resolve().is_relative_to(installed)

class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
        self.base = None
    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "base":
            self.base = attrs.get("href")
        if tag == "script" and attrs.get("src"):
            self.urls.append(attrs["src"])
        if tag == "link" and attrs.get("rel") in {"stylesheet", "icon"}:
            self.urls.append(attrs["href"])

origin = "http://127.0.0.1:8000"
evidence = []
for prefix in ("", "/piceli"):
    with TestClient(create_app(QueryService([]), origin=origin, url_prefix=prefix), base_url=origin) as client:
        root = client.get(prefix + "/")
        assert root.status_code == 200, root.text
        assert "text/html" in root.headers["content-type"]
        parsed = Assets()
        parsed.feed(root.text)
        assert parsed.urls, "Wheel contains no frontend script/style links"
        assert any(urlsplit(url).path.endswith(".js") for url in parsed.urls)
        base = urljoin(origin + prefix + "/", parsed.base or "")
        compressed_js = 0
        for reference in parsed.urls:
            url = urljoin(base, reference)
            assert urlsplit(url).netloc == urlsplit(origin).netloc, "Runtime CDN dependency"
            assert urlsplit(url).path.startswith(prefix + "/")
            asset = client.get(url)
            assert asset.status_code == 200, url
            assert "text/html" not in asset.headers["content-type"], "Asset route returned HTML fallback"
            if urlsplit(url).path.endswith(".js"):
                compressed_js += len(gzip.compress(asset.content))
        assert compressed_js < 350_000, compressed_js
        for route in ("/applications", "/applications/shop/resources?resource=sample"):
            response = client.get(prefix + route)
            assert response.status_code == 200
            assert response.text == root.text
        assert client.get(prefix + "/assets/missing.js").status_code == 404
        assert client.get(prefix + "/api/v1/not-a-route").status_code == 404
        assert client.get(prefix + "/api/v1/applications").json()["items"] == []
        evidence.append({"prefix": prefix, "scripts_and_styles": len(parsed.urls), "gzip_js_bytes": compressed_js})
print(json.dumps(evidence))
"""


def _run(
    command: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.fixture(scope="module")
def distributions() -> Iterator[tuple[Path, Path, Path]]:
    with tempfile.TemporaryDirectory(prefix="piceli-ui-package-") as directory:
        temporary = Path(directory)
        artifacts = temporary / "dist"
        _run(
            [
                "uv",
                "--no-cache",
                "build",
                "--offline",
                "--no-build-isolation",
                "--python",
                sys.executable,
                "--out-dir",
                str(artifacts),
            ],
            cwd=ROOT,
        )
        (wheel,) = artifacts.glob("*.whl")
        (sdist,) = artifacts.glob("*.tar.gz")
        yield temporary, wheel, sdist


@pytest.mark.timeout(120)
def test_installed_wheel_serves_offline_assets_deep_links_and_url_prefix(
    distributions: tuple[Path, Path, Path],
) -> None:
    temporary, wheel, _ = distributions
    installed = temporary / "installed"
    _run(
        [
            "uv",
            "--no-cache",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--target",
            str(installed),
            str(wheel),
        ],
        cwd=temporary,
    )
    # The child can import locked Python dependencies but cannot find Node or npm.
    result = _run(
        [sys.executable, "-I", "-c", INSTALLED_CHECK, str(installed)],
        cwd=temporary,
        env={**os.environ, "PATH": "", "PYTHONPATH": ""},
    )
    assert [row["prefix"] for row in json.loads(result.stdout)] == ["", "/piceli"]


def test_sdist_includes_prebuilt_assets_without_frontend_node_modules(
    distributions: tuple[Path, Path, Path],
) -> None:
    _, _, sdist = distributions
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
    assert any(name.endswith("/piceli/server/static/index.html") for name in names)
    assert any(
        "/piceli/server/static/assets/" in name and name.endswith(".js")
        for name in names
    )
    assert not any("/node_modules/" in name for name in names)
