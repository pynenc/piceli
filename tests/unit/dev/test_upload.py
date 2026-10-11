"""Uploads in chunks: progress, a retried dropped connection, a named cause."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from piceli.dev.cluster import upload_chunks
from piceli.dev.model import DevError


class Pod:
    """Runs the chunk scripts against a local directory standing for /work."""

    def __init__(self, drops: int = 0) -> None:
        self.parts: dict[str, bytes] = {}
        self.drops = drops
        self.calls = 0
        self.assembled: bytes | None = None
        self.sha: str | None = None

    def __call__(self, command: list[str], stdin: Iterator[bytes] | None) -> int:
        self.calls += 1
        script = command[2]
        if stdin is not None:
            if self.drops:
                self.drops -= 1
                raise DevError(
                    "dev-upload-failed",
                    "exec in the run's pod failed: OSError: Connection reset",
                )
            name = script.rsplit("/work/upload/", 1)[1]
            self.parts[name] = b"".join(stdin)
            return 0
        self.assembled = b"".join(self.parts[k] for k in sorted(self.parts))
        if "printf %s " in script:
            self.sha = script.split("printf %s ")[1].split(" ")[0]
        return 0


def test_parts_assemble_to_the_archive_with_progress(tmp_path: Path) -> None:
    archive = tmp_path / "a"
    archive.write_bytes(bytes(range(256)) * 40)  # 10240 bytes
    pod = Pod()
    seen: list[tuple[int, int]] = []
    report = upload_chunks(
        pod,
        archive,
        "f" * 64,
        lambda s, t, _e: seen.append((s, t)),
        chunk_bytes=4096,
        sleep=lambda _s: None,
    )
    assert pod.assembled == archive.read_bytes() and pod.sha == "f" * 64
    assert seen == [(4096, 10240), (8192, 10240), (10240, 10240)]
    assert report["chunks"] == 3 and report["retries"] == 0 and report["bytes"] == 10240


def test_a_dropped_connection_retries_the_chunk(tmp_path: Path) -> None:
    archive = tmp_path / "a"
    archive.write_bytes(b"x" * 9000)
    pod = Pod(drops=2)
    waits: list[float] = []
    report = upload_chunks(
        pod, archive, None, lambda *_a: None, chunk_bytes=4096, sleep=waits.append
    )
    assert pod.assembled == archive.read_bytes()
    assert report["retries"] == 2 and waits == [1, 2]


def test_a_link_that_stays_down_names_the_chunk_and_the_cause(tmp_path: Path) -> None:
    archive = tmp_path / "a"
    archive.write_bytes(b"x" * 9000)
    with pytest.raises(DevError) as raised:
        upload_chunks(
            Pod(drops=100),
            archive,
            None,
            lambda *_a: None,
            chunk_bytes=4096,
            sleep=lambda _s: None,
        )
    assert raised.value.code == "dev-upload-failed"
    message = str(raised.value)
    assert (
        "chunk 1 of 3" in message
        and "Connection reset" in message
        and "0 of 9000 bytes" in message
    )
