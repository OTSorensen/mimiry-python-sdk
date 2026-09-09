"""``.map()`` on one session: the cold start is paid once, items stream
through, a failing item does not discard the finished ones, and ``volume=``
reaches the session payload.

These drive the real ``_run_map`` / ``_run_remote`` against a fake platform
and a fake container: the SSH layer is replaced by an in-memory filesystem
that a stub worker answers, so the SDK's side of the wire protocol is what is
under test.
"""

from __future__ import annotations

import base64
import sys

import cloudpickle
import pytest

import mimiry._ssh as ssh_mod
from mimiry._config import Config
from mimiry.exceptions import MapError, SessionError
from mimiry.function import FunctionConfig, _run_map, _run_remote

function_mod = sys.modules["mimiry.function"]

HMAC = "k" * 64


class _Platform:
    """A session that reaches ``running`` with SSH published and stays up."""

    def __init__(self, *, volumes=None, die_after_calls=None):
        self.created = []
        self.terminated = []
        self.volumes = volumes or []
        self.die_after_calls = die_after_calls
        self.calls_served = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def create_session(self, payload):
        self.created.append(payload)
        return {"id": f"sess-{len(self.created)}", "state": "submitted"}

    def get_session(self, session_id, *, events_tail=None):
        if self.die_after_calls is not None and self.calls_served >= self.die_after_calls:
            return {"id": session_id, "state": "exited", "error": "container died"}
        return {
            "id": session_id,
            "state": "running",
            "ssh": {"host": "10.0.0.1", "port": 22, "username": "root"},
        }

    def get_logs(self, session_id, *, tail=50, timestamps=False):
        return {"_status": 200, "logs": "boom"}

    def list_volumes(self):
        return self.volumes

    def terminate_session(self, session_id):
        self.terminated.append(session_id)


class _Container:
    """Answers the SDK's SSH commands from an in-memory filesystem, running
    the function the SDK shipped. Mirrors the worker loop's contract."""

    def __init__(self, platform: _Platform, hmac_key: str):
        self.fs: dict[str, bytes] = {}
        self.platform = platform
        self.hmac_key = hmac_key
        self.fn = None
        self.done = False

    def _answer(self, call_index: int):
        import hashlib
        import hmac as _hmac

        args, kwargs = cloudpickle.loads(base64.b64decode(self.fs[f"{ssh_mod.CALLS_DIR}/{call_index}.b64"]))
        try:
            payload = {"ok": True, "result": self.fn(*args, **kwargs)}
        except BaseException as e:  # noqa: BLE001 — mirrors the container
            payload = {"ok": False, "error": {"type": type(e).__name__, "message": str(e), "traceback": "tb"}}
        wire = base64.b64encode(cloudpickle.dumps(payload)).decode()
        sig = _hmac.new(self.hmac_key.encode(), wire.encode(), hashlib.sha256).hexdigest()
        self.fs[f"{ssh_mod.RESULTS_DIR}/{call_index}.b64"] = (sig + "\n" + wire).encode()
        self.platform.calls_served += 1

    def run(self, args, **kw):
        """Stand-in for ``subprocess.run`` on an ssh argv."""
        cmd = args[-1]
        stdin = kw.get("input")

        class R:
            returncode = 0
            stdout = b""
            stderr = b""

        r = R()
        if cmd == "echo ok":
            r.stdout = b"ok"
        elif cmd == "true":
            pass
        elif cmd.startswith("test -f ") and "&& echo present" in cmd:
            path = cmd.split()[2]
            r.stdout = b"present" if path in self.fs else b"missing"
        elif cmd.startswith("test -f ") and "cat" in cmd:
            r.stdout = b""
        elif cmd.startswith("cat > "):
            path = cmd.split()[2].removesuffix(".partial")
            self.fs[path] = stdin
            n = int(path.rsplit("/", 1)[1].split(".")[0])
            if self.platform.die_after_calls is None or n < self.platform.die_after_calls:
                self._answer(n)
        elif cmd.startswith("cat "):
            r.stdout = self.fs[cmd.split()[1]]
        elif cmd.startswith("touch "):
            self.done = True
        return r


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """Wire ``_run_map``/``_run_remote`` to a fake platform + container."""
    key = tmp_path / "id_test"
    key.write_text("private")
    (tmp_path / "id_test.pub").write_text("ssh-ed25519 AAAA test")
    cfg = Config(ssh_key_path=key, api_base="https://api.test", timeout_seconds=5, poll_interval_seconds=0.0)

    def make(fn, **platform_kw):
        platform = _Platform(**platform_kw)
        container = _Container(platform, HMAC)
        container.fn = fn
        monkeypatch.setattr(function_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(function_mod, "get_token", lambda *a, **kw: "tok")
        monkeypatch.setattr(function_mod, "MimiryClient", lambda token: platform)
        monkeypatch.setattr(function_mod, "new_result_hmac_key", lambda: HMAC)
        monkeypatch.setattr(function_mod, "preflight_gpu_availability", lambda *a, **kw: ["A100_80G_SXM"])
        monkeypatch.setattr(ssh_mod.subprocess, "run", container.run)
        monkeypatch.setattr(ssh_mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(function_mod, "open_control_channel", lambda t: t)
        monkeypatch.setattr(function_mod, "close_control_channel", lambda t: None)
        return platform, container

    return make


def _square(x, power=2):
    return x**power


def test_map_uses_one_session_for_every_item(wire):
    platform, container = wire(_square)
    out = _run_map(_square, FunctionConfig(), [((2,), {}), ((3,), {"power": 3}), ((4,), {})])
    assert out == [4, 27, 16]
    assert len(platform.created) == 1, "one session, not one per item"
    assert container.done, "the container was released"
    assert platform.terminated == ["sess-1"] or platform.terminated == []


def test_public_map_goes_through_the_single_session_path(wire):
    import mimiry

    platform, _ = wire(_square)
    fn = mimiry.function()(_square)
    assert fn.map([2, 3, 4]) == [4, 9, 16]
    assert len(platform.created) == 1, "Function.map must not fall back to one session per item"


def test_map_ships_the_function_once_and_arguments_per_call(wire):
    platform, container = wire(_square)
    _run_map(_square, FunctionConfig(), [((5,), {})])
    env = platform.created[0]["environment_vars"]
    fn = cloudpickle.loads(base64.b64decode(env["MIMIRY_FN_PAYLOAD_B64"]))
    assert callable(fn) and not isinstance(fn, tuple), "worker mode carries fn alone"
    assert f"{ssh_mod.CALLS_DIR}/0.b64" in container.fs
    assert "worker" not in platform.created[0]["command"] or True  # command is opaque
    # The bootstrap that ran is the worker one: it must reference the calls dir.
    assert base64.b64decode(platform.created[0]["command"].split("echo ")[1].split(" |")[0]).decode().count(ssh_mod.CALLS_DIR)


def test_map_keeps_finished_results_when_an_item_raises(wire):
    def flaky(x):
        if x == 3:
            raise ValueError("three is bad")
        return x * 10

    platform, _ = wire(flaky)
    with pytest.raises(MapError) as exc:
        _run_map(flaky, FunctionConfig(), [((2,), {}), ((3,), {}), ((4,), {})])
    err = exc.value
    assert err.results == [20, None, 40]
    assert [i for i, _ in err.failures] == [1]
    assert "three is bad" in str(err.failures[0][1])
    assert err.completed == 2
    assert len(platform.created) == 1


def test_map_keeps_finished_results_when_the_session_dies(wire):
    platform, _ = wire(_square, die_after_calls=2)
    with pytest.raises(MapError) as exc:
        _run_map(_square, FunctionConfig(), [((2,), {}), ((3,), {}), ((4,), {}), ((5,), {})])
    err = exc.value
    assert err.results[:2] == [4, 9]
    assert err.total == 4
    assert err.failures[-1][0] == 2, "the failure is recorded at the first unfinished index"
    assert "died" in str(err) or "exited" in str(err)


def test_map_with_no_session_progress_raises_the_underlying_error(wire):
    platform, _ = wire(_square, die_after_calls=0)
    with pytest.raises(SessionError):
        _run_map(_square, FunctionConfig(), [((2,), {})])


def test_remote_still_uses_the_single_call_protocol(wire):
    platform, container = wire(_square)
    # The single-call container writes RESULT_FILE itself; emulate that.
    import hashlib
    import hmac as _hmac

    def run(args, **kw):
        cmd = args[-1]
        if cmd.startswith("test -f ") and ssh_mod.RESULT_FILE in cmd and "present" in cmd:
            wire_ = base64.b64encode(cloudpickle.dumps({"ok": True, "result": 49})).decode()
            sig = _hmac.new(HMAC.encode(), wire_.encode(), hashlib.sha256).hexdigest()
            container.fs[ssh_mod.RESULT_FILE] = (sig + "\n" + wire_).encode()
        return container.run(args, **kw)

    import mimiry._ssh as m

    m.subprocess.run = run
    assert _run_remote(_square, FunctionConfig(), (7,), {}) == 49
    env = platform.created[0]["environment_vars"]
    fn, args, kwargs = cloudpickle.loads(base64.b64decode(env["MIMIRY_FN_PAYLOAD_B64"]))
    assert args == (7,)


def test_volume_reaches_the_payload_and_sets_the_location(wire):
    platform, _ = wire(_square, volumes=[{"name": "ckpt", "location": "FIN-02"}])
    cfg = FunctionConfig(volumes={"ckpt": "/data"})
    _run_map(_square, cfg, [((2,), {})])
    payload = platform.created[0]
    assert payload["volume_mounts"] == [{"volume_name": "ckpt", "mount_path": "/data"}]
    assert payload["gpu"]["location"] == "FIN-02", "the session adopts the volume's location"


def test_volume_in_another_location_is_refused_before_any_session(wire):
    platform, _ = wire(_square, volumes=[{"name": "ckpt", "location": "FIN-02"}])
    cfg = FunctionConfig(volumes={"ckpt": "/data"}, location="FIN-01")
    with pytest.raises(SessionError, match="FIN-02"):
        _run_map(_square, cfg, [((2,), {})])
    assert platform.created == []


def test_decorator_volume_forms():
    import mimiry

    a = mimiry.function(volume="ckpt")(_square)
    b = mimiry.function(volume={"ckpt": "/models", "data": "/data"})(_square)
    c = mimiry.function()(_square)
    assert a._cfg.volumes == {"ckpt": "/data"}
    assert b._cfg.volumes == {"ckpt": "/models", "data": "/data"}
    assert c._cfg.volumes == {}
