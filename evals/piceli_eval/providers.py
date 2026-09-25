"""Model providers for the eval, over plain HTTP (standard library only).

A model is named ``provider:model``:

- ``anthropic:<model>`` needs ``ANTHROPIC_API_KEY``
- ``openai:<model>`` needs ``OPENAI_API_KEY``; ``OPENAI_BASE_URL`` points it at any
  OpenAI-compatible server (a local model server, a gateway)
- ``gemini:<model>`` needs ``GEMINI_API_KEY``
- ``mock:reference`` / ``mock:naive`` replay canned answers from
  ``evals/mock_responses/`` and need nothing; they validate the harness itself

Keys are read from the environment only, sent only to their provider, and never
printed, logged or written to results.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MOCK_DIR = Path(__file__).resolve().parent.parent / "mock_responses"
TIMEOUT_SECONDS = 600
MAX_TOKENS = 16000

KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


class ProviderUnavailableError(RuntimeError):
    """The provider cannot run here (missing key or unknown provider)."""


class ProviderError(RuntimeError):
    """The provider was called and failed."""


@dataclass
class Message:
    """One conversation turn: ``role`` is ``user`` or ``assistant``."""

    role: str
    content: str


class Provider:
    """Base class: ``complete`` returns the model's text answer."""

    name = "base"

    def __init__(self, model: str) -> None:
        self.model = model

    @property
    def label(self) -> str:
        """``provider:model`` as used on the command line and in results."""
        return f"{self.name}:{self.model}"

    def complete(self, system: str, messages: list[Message], task_id: str) -> str:
        """Return the assistant's answer to ``messages``."""
        raise NotImplementedError


def _require_key(provider: str) -> str:
    env = KEY_ENV[provider]
    key = os.environ.get(env, "")
    if not key:
        raise ProviderUnavailableError(f"{env} is not set")
    return key


def _post_json(
    url: str, headers: dict[str, str], body: dict[str, Any], secret: str
) -> dict[str, Any]:
    """POST ``body``; errors never include request headers or the key."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data: dict[str, Any] = json.loads(response.read())
            return data
    except urllib.error.HTTPError as error:
        detail = error.read()[:500].decode(errors="replace")
        message = f"HTTP {error.code}: {detail}".replace(secret, "[redacted]")
        raise ProviderError(message) from None
    except urllib.error.URLError as error:
        raise ProviderError(f"connection failed: {error.reason}") from None


class AnthropicProvider(Provider):
    """Anthropic Messages API."""

    name = "anthropic"

    def complete(self, system: str, messages: list[Message], task_id: str) -> str:
        """Call ``POST /v1/messages``."""
        key = _require_key(self.name)
        base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        data = _post_json(
            f"{base.rstrip('/')}/v1/messages",
            {"x-api-key": key, "anthropic-version": "2023-06-01"},
            {
                "model": self.model,
                "max_tokens": MAX_TOKENS,
                "system": system,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
            },
            key,
        )
        if data.get("stop_reason") == "refusal":
            return "[refused]"
        return "".join(
            b.get("text", "")
            for b in data.get("content", [])
            if b.get("type") == "text"
        )


class OpenAIProvider(Provider):
    """OpenAI Chat Completions API, or any server compatible with it."""

    name = "openai"

    def complete(self, system: str, messages: list[Message], task_id: str) -> str:
        """Call ``POST /chat/completions``."""
        key = _require_key(self.name)
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        data = _post_json(
            f"{base.rstrip('/')}/chat/completions",
            {"authorization": f"Bearer {key}"},
            {
                "model": self.model,
                "messages": [{"role": "system", "content": system}]
                + [{"role": m.role, "content": m.content} for m in messages],
            },
            key,
        )
        choices = data.get("choices") or [{}]
        content = choices[0].get("message", {}).get("content")
        return str(content or "")


class GeminiProvider(Provider):
    """Google Gemini ``generateContent`` API."""

    name = "gemini"

    def complete(self, system: str, messages: list[Message], task_id: str) -> str:
        """Call ``POST /v1beta/models/{model}:generateContent``."""
        key = _require_key(self.name)
        base = os.environ.get(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com"
        )
        data = _post_json(
            f"{base.rstrip('/')}/v1beta/models/{self.model}:generateContent",
            {"x-goog-api-key": key},
            {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [
                    {
                        "role": "model" if m.role == "assistant" else "user",
                        "parts": [{"text": m.content}],
                    }
                    for m in messages
                ],
            },
            key,
        )
        candidates = data.get("candidates") or [{}]
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)


class MockProvider(Provider):
    """Replays ``mock_responses/<variant>/<task id>.md`` (same answer every turn)."""

    name = "mock"

    def __init__(self, model: str) -> None:
        super().__init__(model)
        if not model or not (MOCK_DIR / model).is_dir():
            raise ProviderUnavailableError(f"no mock variant {model!r} in {MOCK_DIR}")

    def complete(self, system: str, messages: list[Message], task_id: str) -> str:
        """Return the canned answer, or an empty answer when there is none."""
        path = MOCK_DIR / self.model / f"{task_id}.md"
        return path.read_text() if path.is_file() else ""


PROVIDERS: dict[str, type[Provider]] = {
    cls.name: cls
    for cls in (AnthropicProvider, OpenAIProvider, GeminiProvider, MockProvider)
}


def make_provider(spec: str) -> Provider:
    """Build a provider from ``provider:model``; raises if it cannot run here."""
    provider, _, model = spec.partition(":")
    if provider not in PROVIDERS or not model:
        raise ProviderUnavailableError(
            f"unknown model {spec!r}; use one of {sorted(PROVIDERS)} as 'provider:model'"
        )
    if provider in KEY_ENV:
        _require_key(provider)
    return PROVIDERS[provider](model)


def secret_values() -> list[str]:
    """Values of the provider keys present, to scrub from anything written."""
    return [v for env in KEY_ENV.values() if (v := os.environ.get(env))]
