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
        billing = {"provider": "verda", "hourly_rate": 1.85, "currency": "EUR"}
        if session_id in self.terminated:
            billing["final_cost"] = 0.308
            return {"id": session_id, "state": "terminated", "gpu_type": "A100_80G_SXM", "billing": billing}
        if self.die_after_calls is not None and self.calls_served >= self.die_after_calls:
            return {"id": session_id, "state": "exited", "error": "container died", "billing": billing}
        return {
            "id": session_id,
            "state": "running",
            "gpu_type": "A100_80G_SXM",
            "billing": billing,
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
        # Fake clocks everywhere a wait could otherwise burn wall time.
        import mimiry._session as session_mod

        clock = {"t": 0.0}

        def _sleep(seconds):
            clock["t"] += seconds

        for mod in (ssh_mod, session_mod, function_mod):
            monkeypatch.setattr(mod.time, "sleep", _sleep)
            monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(function_mod, "open_control_channel", lambda t: t)
        monkeypatch.setattr(function_mod, "close_control_channel", lambda t: None)
        return platform, container

    return make


def _square(x, power=2):
    return x**power


def test_map_uses_one_session_for_every_item(wire):
    platform, container = wire(_square)
    out, info = _run_map(_square, FunctionConfig(), [((2,), {}), ((3,), {"power": 3}), ((4,), {})])
    assert out == [4, 27, 16]
    assert info.session_id == "sess-1" and info.final_cost == 0.308 and info.gpu_type == "A100_80G_SXM"
    assert len(platform.created) == 1, "one session, not one per item"
    assert container.done, "the container was released"
    # The fake platform stays "running" after release, so the SDK must
    # terminate what auto_terminate did not.
    assert platform.terminated == ["sess-1"]


def test_public_map_goes_through_the_single_session_path(wire):
    import mimiry

    platform, _ = wire(_square)
    fn = mimiry.function()(_square)
    assert fn.map([2, 3, 4]) == [4, 9, 16]
    assert len(platform.created) == 1, "Function.map must not fall back to one session per item"
    assert fn.last_run.session_id == "sess-1"
    assert fn.last_run.final_cost == 0.308
    assert "running" in fn.last_run.phases


def test_map_ships_the_function_once_and_arguments_per_call(wire):
    platform, container = wire(_square)
    _run_map(_square, FunctionConfig(), [((5,), {})])
    env = platform.created[0]["environment_vars"]
    fn = cloudpickle.loads(base64.b64decode(env["MIMIRY_FN_PAYLOAD_B64"]))
    assert callable(fn) and not isinstance(fn, tuple), "worker mode carries fn alone"
    assert f"{ssh_mod.CALLS_DIR}/0.b64" in container.fs
    # The bootstrap that ran is the worker one: it polls the calls dir and
    # never writes the single-call result file.
    bootstrap = base64.b64decode(platform.created[0]["command"].split("echo ")[1].split(" |")[0]).decode()
    assert ssh_mod.CALLS_DIR in bootstrap
    assert f'"{ssh_mod.RESULT_FILE}"' not in bootstrap


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
    assert err.run is not None and err.run.session_id == "sess-1"
    assert [i for i, _ in err.failures] == [1]
    assert "three is bad" in str(err.failures[0][1])
    assert err.completed == 2
    assert len(platform.created) == 1


def test_map_keeps_finished_results_when_the_session_dies(wire):
    platform, _ = wire(_square, die_after_calls=2)
    with pytest.raises(MapError) as exc:
        _run_map(_square, FunctionConfig(), [((2,), {}), ((3,), {}), ((4,), {}), ((5,), {})])
    err = exc.value
    assert err.results == [4, 9, None, None]
    assert err.total == 4
    assert err.run is not None, "a session that died still reports what it cost"
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
    result, info = _run_remote(_square, FunctionConfig(), (7,), {})
    assert result == 49
    assert info.session_id == "sess-1" and info.hourly_rate == 1.85
    env = platform.created[0]["environment_vars"]
    fn, args, kwargs = cloudpickle.loads(base64.b64decode(env["MIMIRY_FN_PAYLOAD_B64"]))
    assert args == (7,)


def test_public_remote_records_last_run(wire):
    import hashlib
    import hmac as _hmac

    import mimiry

    platform, container = wire(_square)
    inner = container.run

    def run(args, **kw):
        cmd = args[-1]
        if cmd.startswith("test -f ") and ssh_mod.RESULT_FILE in cmd and "present" in cmd:
            wire_ = base64.b64encode(cloudpickle.dumps({"ok": True, "result": 9})).decode()
            sig = _hmac.new(HMAC.encode(), wire_.encode(), hashlib.sha256).hexdigest()
            container.fs[ssh_mod.RESULT_FILE] = (sig + "\n" + wire_).encode()
        return inner(args, **kw)

    ssh_mod.subprocess.run = run
    fn = mimiry.function()(_square)
    assert fn.last_run is None
    assert fn.remote(3) == 9
    assert fn.last_run.session_id == "sess-1"
    assert fn.last_run.final_cost == 0.308
    assert fn.last_run.duration is not None


def test_remote_records_last_run_even_when_the_call_raises(wire):
    import mimiry

    platform, _ = wire(_square, die_after_calls=0)
    fn = mimiry.function()(_square)
    with pytest.raises(SessionError):
        fn.remote(3)
    assert fn.last_run is not None and fn.last_run.session_id == "sess-1", (
        "a failed call still cost a session; the user must be able to see it"
    )


def test_map_that_never_started_still_reports_its_session(wire):
    import mimiry

    platform, _ = wire(_square, die_after_calls=0)
    fn = mimiry.function()(_square)
    with pytest.raises(SessionError) as exc:
        fn.map([2])
    assert exc.value.run is not None and exc.value.run.session_id == "sess-1"
    assert fn.last_run is not None and fn.last_run.session_id == "sess-1"


def test_a_live_session_is_terminated_when_the_attach_fails(wire, monkeypatch):
    # The session exists and is running, but the SSH step blows up. The SDK
    # must not walk away from a paid, running machine.
    platform, _ = wire(_square)
    monkeypatch.setattr(
        function_mod, "wait_for_sshd", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no route"))
    )
    with pytest.raises(RuntimeError):
        _run_remote(_square, FunctionConfig(), (2,), {})
    assert platform.terminated == ["sess-1"], "attach failed on a live session: terminate it"

    platform2, _ = wire(_square)
    monkeypatch.setattr(
        function_mod, "wait_for_sshd", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no route"))
    )
    with pytest.raises(RuntimeError):
        _run_map(_square, FunctionConfig(), [((2,), {})])
    assert platform2.terminated == ["sess-1"]


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
