"""Image builder. v1: thin wrapper around a container image URI plus optional
``apt_install`` / ``pip_install`` prefix commands.

This is intentionally simple — v1 has no Dockerfile build pipeline. The
``pip_install``/``apt_install`` directives are executed inside the container
at command start, prefixing the user's actual command. Slow (re-installs on
every invocation) but works against today's API without backend changes.

When v2 lands per-region image caching, this class can grow a ``.build()``
method that resolves to a content-hashed pre-built image.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field


@dataclass
class Image:
    uri: str
    _apt_packages: list[str] = field(default_factory=list)
    _pip_packages: list[str] = field(default_factory=list)
    _env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_registry(cls, uri: str) -> "Image":
        """Reference a public container image by URI. v1 supports any image the
        Mimiry compute backend's registry-resolver accepts (e.g. nvcr.io, docker.io).
        """
        return cls(uri=uri)

    def pip_install(self, *packages: str) -> "Image":
        """Append pip packages to install at container start. Accepts version
        specifiers (``"torch>=2.3"``) and extras (``"requests[socks]"``).
        """
        self._pip_packages.extend(packages)
        return self

    def apt_install(self, *packages: str) -> "Image":
        """Append apt packages to install at container start."""
        self._apt_packages.extend(packages)
        return self

    def env(self, **kwargs: str) -> "Image":
        """Pin environment variables for the running container."""
        self._env.update(kwargs)
        return self

    def install_prefix(self) -> str:
        """Render the shell snippet that prepares the container before the user's
        command runs. Returns an empty string when nothing needs installing.
        """
        parts: list[str] = []
        if self._apt_packages:
            quoted = " ".join(shlex.quote(p) for p in self._apt_packages)
            parts.append(f"apt-get update -qq && apt-get install -y -q {quoted} >/dev/null")
        if self._pip_packages:
            quoted = " ".join(shlex.quote(p) for p in self._pip_packages)
            # Invoke pip via ``python3 -m pip`` (a bare ``pip`` is often absent on
            # minimal images, e.g. nvidia/cuda runtimes). ``--break-system-packages``
            # is required on Ubuntu 24.04+ (PEP 668) and ignored by older pip — the
            # bootstrap installs into a single-purpose container, so there's no
            # system Python to protect. The bootstrap guarantees python3 + pip exist
            # before this prefix runs (see build_bootstrap_script ordering).
            parts.append(
                f"python3 -m pip install --break-system-packages --quiet --no-input {quoted}"
            )
        return " && ".join(parts)

    @property
    def env_vars(self) -> dict[str, str]:
        return dict(self._env)


def normalize_image(image: "Image | str") -> Image:
    """Accept either an Image instance or a bare URI string."""
    if isinstance(image, Image):
        return image
    return Image.from_registry(image)
