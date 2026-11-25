"""The names a client bundle shows to the operator who installs it.

A bundle is read by someone who installs an app, not by someone who knows the
tool that wrote it. :class:`Naming` holds the three names its owner may
choose: the prefix of the input variables of ``prepare.sh``, the tool named in
generated comments, and the domain of the annotation and label keys. The
defaults are the ones every earlier bundle used, so a bundle rendered without
options does not change.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

DEFAULT_INPUT_PREFIX = "PICELI_INPUT_"
DEFAULT_TOOL_NAME = "piceli bundle"
DEFAULT_DOMAIN = "piceli.io"

_PREFIX = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")
_TOOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}$")
_LABEL = r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"^{_LABEL}(\.{_LABEL})*$")
#: Keys whose value is a map of label or annotation keys.
_KEY_MAPS = frozenset({"labels", "annotations", "matchLabels", "selector"})
_FIELD_PATH = re.compile(r"^(metadata\.(?:annotations|labels)\[')piceli\.io/")


@dataclass(frozen=True)
class Naming:
    """The operator-facing names of a bundle (see the module docstring).

    :param input_prefix: Prefix of the environment variables ``prepare.sh``
        reads (``<prefix><NAME>=path``): capital letters, digits and ``_``.
    :param tool_name: The tool named in generated comments (``written by
        <tool_name>``).
    :param annotation_domain: Domain of the annotation and label keys the
        bundle writes (``<domain>/bundle``, ``safety.<domain>/allow-<rule>``):
        a DNS name in lower case.
    :raises ValueError: a name is not valid; the text says which.
    """

    input_prefix: str = DEFAULT_INPUT_PREFIX
    tool_name: str = DEFAULT_TOOL_NAME
    annotation_domain: str = DEFAULT_DOMAIN

    def __post_init__(self) -> None:
        if not _PREFIX.fullmatch(self.input_prefix):
            raise ValueError(
                "--input-prefix is capital letters, digits and '_', starting "
                "with a letter, at most 40 characters (for example SHOP_INPUT_)"
            )
        if not _TOOL.fullmatch(self.tool_name):
            raise ValueError(
                "--tool-name is plain text (letters, digits, space, '.', '_', "
                "'-'), at most 60 characters"
            )
        domain = self.annotation_domain
        if (
            not _DOMAIN.fullmatch(domain)
            or len(domain) > 240
            or domain in {"kubernetes.io", "k8s.io"}
            or domain.endswith((".kubernetes.io", ".k8s.io"))
        ):
            raise ValueError(
                "--annotation-domain is a DNS name in lower case (for example "
                "shop.example), not a Kubernetes domain"
            )

    @property
    def product(self) -> str:
        """The first label of the domain: ``piceli`` for ``piceli.io``."""
        return self.annotation_domain.split(".")[0]

    def key(self, name: str) -> str:
        """The annotation or label key ``<domain>/<name>``."""
        return f"{self.annotation_domain}/{name}"

    @property
    def safety_prefix(self) -> str:
        """``safety.<domain>/allow-``: the annotations that waive a rule."""
        return f"safety.{self.annotation_domain}/allow-"

    @property
    def image_host(self) -> str:
        """The never-resolvable host of the images in the base."""
        return f"images.{self.product}.invalid"

    @property
    def schema(self) -> str:
        """The ``schema`` of ``bundle.json``."""
        return f"{self.product}.client-bundle.v1"

    @property
    def secrets_template(self) -> str:
        """The chart template holding the generated Secrets."""
        return f"templates/{self.product}-secrets.yaml"

    @property
    def tool_with_helm(self) -> str:
        """The tool as the Helm files name it (``piceli bundle --helm``)."""
        if self.tool_name == DEFAULT_TOOL_NAME:
            return f"{DEFAULT_TOOL_NAME} --helm"
        return self.tool_name

    def field_path(self, annotation: str) -> str:
        """``metadata.annotations['<domain>/<annotation>']``."""
        return f"metadata.annotations['{self.key(annotation)}']"

    def apply(self, manifest: Any) -> Any:
        """``manifest`` with every ``piceli.io/`` label and annotation key (and
        every ``safety.piceli.io/`` key) under the chosen domain.

        Keys are renamed in every ``labels``, ``annotations``, ``matchLabels``
        and ``selector`` map (so a selector keeps matching its pods), and the
        ``fieldPath`` of an environment variable that reads one. Run it after
        the safety gate, which reads the default keys. The input is not
        changed.
        """
        if self.annotation_domain == DEFAULT_DOMAIN:
            return manifest
        return self._walk(manifest, False)

    def _key(self, key: str) -> str:
        if key.startswith(f"{DEFAULT_DOMAIN}/"):
            return self.annotation_domain + key[len(DEFAULT_DOMAIN) :]
        if key.startswith(f"safety.{DEFAULT_DOMAIN}/"):
            return "safety." + self.annotation_domain + key[len(DEFAULT_DOMAIN) + 7 :]
        return key

    def _walk(self, value: Any, keys: bool) -> Any:
        if isinstance(value, dict):
            return {
                (self._key(key) if keys and isinstance(key, str) else key): self._walk(
                    child, key in _KEY_MAPS
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [self._walk(child, False) for child in value]
        if isinstance(value, str):
            return _FIELD_PATH.sub(rf"\g<1>{self.annotation_domain}/", value)
        return value
