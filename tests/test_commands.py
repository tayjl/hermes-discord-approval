from fakes import OWNER, FakeConnection, FakeCtx, secrets
from hda import connection, flows, register


def _setup(monkeypatch, config=None):
    secrets(monkeypatch)
    monkeypatch.setattr(connection, "_token", None)  # register() captures the token
    fake = FakeConnection()
    monkeypatch.setattr(connection, "ensure", fake.ensure)
    ctx = FakeCtx(config)
    register(ctx)
    return ctx, fake


def _cli(ctx, name):
    return lambda args: ctx.commands[name](args, platform="cli")


def test_register_registers_commands_transport_and_hooks(monkeypatch):
    ctx, _ = _setup(monkeypatch)
    assert sorted(ctx.commands) == ["away", "ping"]
    assert list(ctx.transports) == ["discord-away"]
    assert sorted(ctx.hooks) == [
        "on_session_end", "on_session_prompt", "on_session_prompt_settled", "post_approval_response",
        "post_llm_call", "pre_approval_request", "pre_llm_call",
    ]
    assert ctx.unload == [connection.stop]


def test_register_warm_starts_only_when_a_setting_is_on(monkeypatch):
    _, fake = _setup(monkeypatch)
    assert fake.ensured == 0
    _, fake = _setup(monkeypatch, {"ping": True})
    assert fake.ensured == 1


def test_away_toggles_and_sets(monkeypatch):
    ctx, fake = _setup(monkeypatch)
    away = _cli(ctx, "away")
    assert away("") == flows.AWAY_ON and ctx.config["away"] is True
    assert away("") == flows.AWAY_OFF and ctx.config["away"] is False
    assert away("on") == flows.AWAY_ON and ctx.config["away"] is True
    assert away(" OFF ") == flows.AWAY_OFF and ctx.config["away"] is False
    assert fake.ensured == 2  # only the two "on" results


def test_bad_argument_gives_usage_and_no_write(monkeypatch):
    ctx, _ = _setup(monkeypatch)
    assert _cli(ctx, "away")("maybe") == flows.USAGE_AWAY
    assert _cli(ctx, "ping")("maybe") == flows.USAGE_PING
    assert ctx.config == {}


def test_ping_toggles(monkeypatch):
    ctx, _ = _setup(monkeypatch)
    ping = _cli(ctx, "ping")
    assert ping("") == flows.PING_ON and ctx.config["ping"] is True
    assert ping("off") == flows.PING_OFF and ctx.config["ping"] is False


def test_discord_other_user_is_refused(monkeypatch):
    ctx, fake = _setup(monkeypatch)
    other = type("S", (), {"user_id": "999"})()
    assert ctx.commands["away"]("on", source=other, platform="discord") == flows.NOT_ALLOWED
    assert ctx.config == {} and fake.ensured == 0


def test_discord_owner_and_local_platforms_work(monkeypatch):
    ctx, _ = _setup(monkeypatch)
    owner = type("S", (), {"user_id": int(OWNER)})()
    assert ctx.commands["away"]("on", source=owner, platform="discord") == flows.AWAY_ON
    for platform in ("cli", "local", "desktop", "dashboard", "tui"):
        assert ctx.commands["away"]("off", platform=platform) == flows.AWAY_OFF


def test_missing_platform_is_refused(monkeypatch):
    ctx, _ = _setup(monkeypatch)
    assert ctx.commands["away"]("on", platform=None) == flows.NOT_ALLOWED
    assert ctx.commands["away"]("on", platform="") == flows.NOT_ALLOWED


def test_other_platform_is_refused(monkeypatch):
    ctx, _ = _setup(monkeypatch)
    assert ctx.commands["away"]("on", platform="telegram") == flows.NOT_ALLOWED


def test_invalid_owner_config_refuses_discord(monkeypatch):
    secrets(monkeypatch, user="1,2")
    monkeypatch.setattr(connection, "_token", None)
    ctx = FakeCtx()
    register(ctx)
    anyone = type("S", (), {"user_id": "1"})()
    assert ctx.commands["away"]("on", source=anyone, platform="discord") == flows.NOT_ALLOWED


def test_save_failure_is_reported(monkeypatch):
    ctx, fake = _setup(monkeypatch)
    ctx.set_error = PermissionError("managed")
    assert _cli(ctx, "away")("on") == flows.SAVE_FAILED
    assert fake.ensured == 0
