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

from mimiry.exceptions import MimiryError


@dataclass
class Image:
    uri: str
    _apt_packages: list[str] = field(default_factory=list)
    _pip_packages: list[str] = field(default_factory=list)
    _env: dict[str, str] = field(default_factory=dict)
    _python_version: str | None = None

    @classmethod
    def from_registry(cls, uri: str) -> "Image":
        """Reference a public container image by URI. v1 supports any image the
        Mimiry compute backend's registry-resolver accepts (e.g. nvcr.io, docker.io).
        """
        return cls(uri=uri)

    def python_version(self, version: str) -> "Image":
        """Declare the Python this image ships (``\"3.11\"``), enabling a local
        compatibility check before a session is ever created.

        A remote call ships the caller's function as a cloudpickle blob, and
        those blobs do not load on a different Python minor version — the
        container dies on arrival. Declaring the version turns that into an
        error raised before anything is paid for. Undeclared images are still
        checked, but only once the container is running.
        """
        self._python_version = _normalize_python_version(version)
        return self

    @property
    def declared_python_version(self) -> str | None:
        """The ``major.minor`` declared via :meth:`python_version`, if any."""
        return self._python_version

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


def preflight_python_version(image: Image, caller_version: str) -> None:
    """Refuse a remote call whose caller Python cannot match the image's.

    Only images that declared their Python (:meth:`Image.python_version`) can
    be judged here; an undeclared image is checked inside the container
    instead, which costs a provisioning round-trip. Declaring it is what moves
    the failure to before the money is spent.
    """
    declared = image.declared_python_version
    if declared is None or declared == caller_version:
        return
    raise MimiryError(
        f"Python version mismatch: you are calling from Python {caller_version}, "
        f"but image {image.uri!r} declares Python {declared}. Your function travels "
        f"as a cloudpickle blob, which does not load across minor versions — the "
        f"container would crash on arrival and you would still be charged for it. "
        f"Run your script on Python {declared}, or pick an image that ships "
        f"Python {caller_version}."
    )


def _normalize_python_version(version: str) -> str:
    """Reduce a declared version to ``major.minor``, which is the granularity
    that decides whether a cloudpickle payload loads. ``\"3.11.9\"`` and
    ``\"3.11\"`` are the same container as far as this check is concerned.
    """
    parts = str(version).strip().split(".")
    if len(parts) < 2 or not all(p.isdigit() for p in parts[:2]):
        raise ValueError(
            f"python_version must look like '3.11' (got: {version!r})"
        )
    return f"{int(parts[0])}.{int(parts[1])}"
