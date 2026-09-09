"""End-to-end wiring for a container that dies before the SDK can attach.

The expensive failure this covers: the container segfaults on arrival, the
host disappears with it, and the SDK spends five more minutes on SSH before
reporting a transport error. The call must instead end quickly and blame the
container, quoting its logs.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import mimiry._ssh as ssh_mod
import mimiry.function  # noqa: F401  (registers the submodule)
from mimiry._config import Config
from mimiry.exceptions import MimiryError, SessionFailed
from mimiry.function import FunctionConfig, _run_remote
from mimiry.image import Image

# ``mimiry.function`` the submodule is shadowed by the ``function`` decorator
# re-exported from the package, so reach it through sys.modules.
function_mod = sys.modules["mimiry.function"]


CRASH_LOG = "Segmentation fault (core dumped) | python3 -"


class _DeadSessionClient:
    """Reports a session that reached ``running`` with SSH published, then died."""

    def __init__(self):
        self.get_calls = 0
        self.terminated = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def create_session(self, payload):
        self.payload = payload
        return {"id": "sess-dead", "state": "submitted"}

    def get_session(self, session_id, *, events_tail=None):
        self.get_calls += 1
        # First two polls: up and reachable. After that: gone.
        if self.get_calls <= 2:
            return {
                "id": session_id,
                "state": "running",
                "ssh": {"host": "10.0.0.1", "port": 22, "username": "root"},
            }
        return {"id": session_id, "state": "exited", "stop_reason": "CONTAINER_EXITED"}

    def get_logs(self, session_id, *, tail=50, timestamps=False):
        return {"_status": 200, "logs": CRASH_LOG}

    def terminate_session(self, session_id):
        self.terminated = session_id


@pytest.fixture
def dead_session(monkeypatch, tmp_path):
    key = tmp_path / "id_test"
    key.write_text("private")
    (tmp_path / "id_test.pub").write_text("ssh-ed25519 AAAA test")
    cfg = Config(
        ssh_key_path=key,
        api_base="https://api.test",
        timeout_seconds=5,
        poll_interval_seconds=0.0,
    )
    client = _DeadSessionClient()

    monkeypatch.setattr(function_mod, "get_config", lambda: cfg)
    monkeypatch.setattr(function_mod, "get_token", lambda *a, **kw: "tok")
    monkeypatch.setattr(function_mod, "MimiryClient", lambda token: client)
    monkeypatch.setattr(
        function_mod, "preflight_gpu_availability", lambda *a, **kw: ["H100_80G_SXM"]
    )
    # sshd never answers: the host went away with the container.
    monkeypatch.setattr(
        ssh_mod.subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="ssh", timeout=30)
        ),
    )
    # A fake clock so the SSH retry loop's 300 s budget elapses instantly.
    # Without it a regression here does not fail the suite, it hangs it — the
    # five-minute wait is exactly the defect under test.
    clock = {"t": 0.0}

    def _sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(ssh_mod.time, "sleep", _sleep)
    monkeypatch.setattr(ssh_mod.time, "monotonic", lambda: clock["t"])
    import mimiry._session as session_mod

    monkeypatch.setattr(session_mod.time, "sleep", _sleep)
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: clock["t"])
    return client


def _square(x):
    return x * x


def test_dead_container_is_diagnosed_as_a_container_failure(dead_session):
    with pytest.raises(SessionFailed) as exc:
        _run_remote(_square, FunctionConfig(gpu="H100_80G_SXM"), (7,), {})
    msg = str(exc.value)
    # The container's own logs, not an SSH transport error.
    assert CRASH_LOG in msg
    assert "sshd not reachable" not in msg


def test_dead_container_does_not_wait_out_the_ssh_timeout(dead_session):
    # Fewer polls than the 300 s / 3 s retry loop would make: the wait ended
    # as soon as the session reported a terminal state.
    with pytest.raises(SessionFailed):
        _run_remote(_square, FunctionConfig(gpu="H100_80G_SXM"), (7,), {})
    assert dead_session.get_calls < 10


def test_a_mismatched_image_never_creates_a_session(dead_session):
    cfg = FunctionConfig(
        gpu="H100_80G_SXM",
        image=Image.from_registry("docker.io/pytorch/pytorch:2.4.0").python_version(
            "2.7"
        ),
    )
    with pytest.raises(MimiryError) as exc:
        _run_remote(_square, cfg, (7,), {})
    assert "2.7" in str(exc.value)
    # Nothing was submitted, so nothing was charged.
    assert dead_session.get_calls == 0
    assert not hasattr(dead_session, "payload")


def test_the_callers_python_version_is_sent_to_the_container(monkeypatch, dead_session):
    captured = {}
    monkeypatch.setattr(
        function_mod,
        "wait_for_started_or_terminal",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("stop here")),
    )
    with pytest.raises(RuntimeError):
        _run_remote(_square, FunctionConfig(gpu="H100_80G_SXM"), (7,), {})
    captured = dead_session.payload["environment_vars"]
    assert captured["MIMIRY_CALLER_PYTHON"] == function_mod.caller_python_version()
    # The preflight's result is the gpu.types preference list the API receives.
    assert dead_session.payload["gpu"]["types"] == ["H100_80G_SXM"]
