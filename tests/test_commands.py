import re

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


def _plain(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_away_sets_and_turns_pings_on_with_it(monkeypatch):
    ctx, fake = _setup(monkeypatch)
    away = _cli(ctx, "away")
    on = _plain(away("on"))
    assert f"{flows.AWAY_ON} {flows.PING_ALSO_ON}" in on
    assert "away-mode : on" in on and "ping-mode : on" in on
    assert ctx.config == {"away": True, "ping": True}
    off = _plain(away(" OFF "))
    assert off.endswith(flows.AWAY_OFF) and "away-mode : off" in off and "ping-mode : on" in off
    assert ctx.config == {"away": False, "ping": True}
    assert fake.ensured == 1


def test_bare_command_toggles_and_shows_the_panel(monkeypatch):
    ctx, fake = _setup(monkeypatch)
    away = _cli(ctx, "away")
    on = _plain(away(""))
    assert "away-mode : on" in on and "ping-mode : on" in on and on.endswith(flows.PING_ALSO_ON)
    assert ctx.config == {"away": True, "ping": True} and fake.ensured == 1
    off = _plain(away(""))
    assert "away-mode : off" in off and off.endswith(flows.AWAY_OFF)
    assert ctx.config == {"away": False, "ping": True}
    ping = _cli(ctx, "ping")
    assert "ping-mode : off" in _plain(ping("")) and ctx.config["ping"] is False


def test_local_replies_stay_under_the_tui_collapse_limit(monkeypatch):
    ctx, _ = _setup(monkeypatch)
    for platform in ("cli", "tui"):
        for name in ("away", "ping"):
            for arg in ("on", "off", "", ""):
                reply = ctx.commands[name](arg, platform=platform)
                assert len(reply) < 400
                assert "\u250c" in reply and "\u2514" in reply and "\u256d" in reply and "\u2570" in reply


def test_discord_reply_is_fenced_ansi_and_local_reply_is_not(monkeypatch):
    ctx, _ = _setup(monkeypatch)
    owner = type("S", (), {"user_id": int(OWNER)})()
    discord = ctx.commands["ping"]("on", source=owner, platform="discord")
    assert discord.startswith("```ansi\n") and discord.count("```") == 4
    assert "HERMES DISCORD RESPONSE" in discord and "\x1b[1;38;2;255;216;0mon" in discord
    for platform in ("cli", "tui"):
        local = ctx.commands["ping"]("off", platform=platform)
        assert "```" not in local and "HERMES DISCORD RESPONSE" in local
        assert "\x1b[38;5;245moff" in local  # off is dim and still spelled out
        assert flows.PING_OFF in local


def test_bad_argument_gives_usage_and_no_write(monkeypatch):
    ctx, _ = _setup(monkeypatch)
    assert _cli(ctx, "away")("maybe") == flows.USAGE_AWAY
    assert _cli(ctx, "ping")("maybe") == flows.USAGE_PING
    assert ctx.config == {}


def test_ping_toggles(monkeypatch):
    ctx, _ = _setup(monkeypatch)
    ping = _cli(ctx, "ping")
    assert flows.PING_ON in ping("on") and ctx.config == {"ping": True}
    assert flows.PING_OFF in ping("off") and ctx.config == {"ping": False}


def test_discord_other_user_is_refused(monkeypatch):
    ctx, fake = _setup(monkeypatch)
    other = type("S", (), {"user_id": "999"})()
    assert ctx.commands["away"]("on", source=other, platform="discord") == flows.NOT_ALLOWED
    assert ctx.config == {} and fake.ensured == 0


def test_discord_owner_and_local_platforms_work(monkeypatch):
    ctx, _ = _setup(monkeypatch)
    owner = type("S", (), {"user_id": int(OWNER)})()
    assert flows.AWAY_ON in ctx.commands["away"]("on", source=owner, platform="discord")
    for platform in ("cli", "local", "desktop", "dashboard", "tui"):
        assert flows.AWAY_OFF in ctx.commands["away"]("off", platform=platform)


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
