import asyncio
import logging
from types import SimpleNamespace

import discord
from fakes import OWNER, FakeConnection, FakeCtx, secrets
from hda import connection, flows
from test_clarify_batch import _batch, _hooks, _q
from test_clarify_ping import _ping, _turn
from test_flows import Running, _custom_id, _req, _setup, _wait

CHANNEL = "1575066493924933712"
MENTION = f"<@{OWNER}>\n"


def _use(monkeypatch, ctx, channel=CHANNEL):
    ctx.config["channel"] = channel
    monkeypatch.setattr(flows, "_ctx", ctx)


# --- routing in flows: the channel is read per send ---

def test_an_approval_card_goes_to_the_channel_and_a_click_settles_it(monkeypatch):
    ctx, fake, present = _setup(monkeypatch)
    _use(monkeypatch, ctx)
    run = Running(present, _req())
    _wait(lambda: fake.sent)
    assert fake.routes == [(CHANNEL, True)]
    assert fake.click(_custom_id(fake, "once"), content=MENTION + fake.sent[0][0])[0][1].startswith(MENTION)  # a click edit keeps it
    assert run.finish().result is not None


def test_no_channel_setting_keeps_the_dm_and_a_change_applies_to_the_next_send(monkeypatch):
    ctx, fake, present = _setup(monkeypatch)
    _use(monkeypatch, ctx, channel="")
    run = Running(present, _req())
    _wait(lambda: fake.sent)
    ctx.config["channel"] = CHANNEL
    run2 = Running(present, _req())
    _wait(lambda: len(fake.sent) == 2)
    assert fake.routes == [("", True), (CHANNEL, True)]  # mention is only used by send() when the channel is used
    for r, cid in ((run, _custom_id(fake, "deny")), (run2, fake.sent[1][1][-1][1])):
        fake.click(cid)
        r.finish()


def test_a_channel_setting_that_is_not_digits_means_dm(monkeypatch):
    ctx, fake, present = _setup(monkeypatch)
    _use(monkeypatch, ctx, channel="#general")
    assert flows._channel() == ""
    _use(monkeypatch, ctx, channel=f" {CHANNEL} ")
    assert flows._channel() == CHANNEL


def test_batch_mentions_message_one_only_and_the_footer_has_none(monkeypatch):
    fake, on_prompt, _ = _hooks(monkeypatch)
    _use(monkeypatch, FakeCtx({"away": True}))
    _batch(on_prompt, "srq-a", _q("q0", "one"), _q("q1", "two"))
    assert fake.routes == [(CHANNEL, True), (CHANNEL, False), (CHANNEL, False)]
    assert [t for t, _ in fake.sent][2] == flows.FOOTER  # layout unchanged: header on 1, footer alone last
    assert fake.sent[0][0].startswith(flows.HEADER) and not fake.sent[1][0].startswith(flows.HEADER)


def test_a_ping_goes_to_the_channel_with_a_mention(monkeypatch):
    fake, now, hooks = _ping(monkeypatch)
    _use(monkeypatch, FakeCtx())
    _turn(hooks, now, 181)
    assert fake.routes == [(CHANNEL, True)]


# --- connection.send: destination, fallback, allowed mentions ---

class _Collect(logging.Handler):
    def __init__(self, records):
        super().__init__()
        self.records = records

    def emit(self, record):
        self.records.append(record)


class Dm:
    def __init__(self):
        self.calls = []

    async def send(self, text, view=None):
        self.calls.append(text)
        return "dm"


class Target:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    async def send(self, text, view=None, allowed_mentions=None):
        if self.fail:
            raise self.fail
        self.calls.append((text, allowed_mentions))
        return "msg"


def _client(monkeypatch, channel, user):
    seen = []

    class Client:
        async def fetch_channel(self, channel_id):
            seen.append(("channel", channel_id))
            if isinstance(channel, Exception):
                raise channel
            return channel

        async def fetch_user(self, user_id):
            seen.append(("user", user_id))
            return user

    secrets(monkeypatch)
    monkeypatch.setattr(connection, "get", lambda: SimpleNamespace(client=Client()))
    return seen


def test_send_posts_to_the_channel_with_only_the_owner_allowed_to_be_pinged(monkeypatch):
    channel, user = Target(), Dm()
    seen = _client(monkeypatch, channel, user)
    assert asyncio.run(connection.send("card", channel=CHANNEL, mention=True)) == "msg"
    assert seen == [("channel", int(CHANNEL))] and user.calls == []
    text, allowed = channel.calls[0]
    assert text == f"<@{OWNER}>\ncard"
    assert [u.id for u in allowed.users] == [int(OWNER)]
    assert allowed.everyone is False and allowed.roles is False and allowed.replied_user is False
    asyncio.run(connection.send("footer", channel=CHANNEL))
    assert channel.calls[1][0] == "footer" and channel.calls[1][1].everyone is False  # no mention, same restrictions


def test_send_without_a_channel_dms_with_no_mention(monkeypatch):
    channel, user = Target(), Dm()
    _client(monkeypatch, channel, user)
    assert asyncio.run(connection.send("card", mention=True)) == "dm"
    assert user.calls == ["card"] and channel.calls == []


def test_send_falls_back_to_the_dm_once_when_the_channel_fails(monkeypatch):
    for failure in (discord.Forbidden(SimpleNamespace(status=403, reason="x"), "secret detail"), RuntimeError("gone")):
        for broken in ("fetch", "send"):
            user, records = Dm(), []
            channel = failure if broken == "fetch" else Target(failure)
            _client(monkeypatch, channel, user)
            handler = _Collect(records)
            connection.logger.addHandler(handler)
            try:
                assert asyncio.run(connection.send("card", channel=CHANNEL, mention=True)) == "dm"
            finally:
                connection.logger.removeHandler(handler)
            assert user.calls == ["card"]  # no mention in the DM
            warnings = [r for r in records if r.levelname == "WARNING"]
            assert len(warnings) == 1 and "secret detail" not in warnings[0].getMessage()


def test_a_status_edit_keeps_the_mention_once(monkeypatch):
    secrets(monkeypatch)
    edits = []

    async def edit(**kw):
        edits.append(kw)
    asyncio.run(connection.edit_card(SimpleNamespace(content=MENTION + "card", edit=edit), "card\n\n**Denied.**"))
    asyncio.run(connection.edit_card(SimpleNamespace(content=MENTION + "card", edit=edit), MENTION + "card\n\n**Denied.**"))
    asyncio.run(connection.edit_card(SimpleNamespace(content="card", edit=edit), "card\n\n**Denied.**"))  # a DM card
    assert [e["content"] for e in edits] == [MENTION + "card\n\n**Denied.**"] * 2 + ["card\n\n**Denied.**"]
    assert edits[0]["allowed_mentions"].everyone is False and edits[0]["view"] is None


# --- length: the mention is inside the 2000-unit budget ---

def test_cards_with_the_mention_and_the_longest_status_fit_discord(monkeypatch):
    longest = max(flows.STATUS_LINES, key=flows._units)
    for ch in ("a", "\U0001F600"):
        card = flows.framed(flows.approval_card(ch * 5000, "d" * 500, "t" * 100, "5 minutes"))
        assert flows._units(MENTION + flows.with_status(card, longest)) <= 2000
        question = flows._card("t" * 100, ch * 5000)
        assert flows._units(MENTION + flows.with_status(question, longest)) <= 2000
    assert flows._units("<@" + "9" * 20 + ">\n") <= flows.MENTION_CHARS  # even a 20 digit id


# --- review fixes ---

def test_register_wires_the_channel_setting_to_flows(monkeypatch):
    import hda
    monkeypatch.setattr(flows, "_ctx", None)
    monkeypatch.setattr(connection, "capture_token", lambda: None)
    hda.register(FakeCtx({"channel": CHANNEL}))
    assert flows._channel() == CHANNEL


def _owner_only(allowed):
    assert [u.id for u in allowed.users] == [int(OWNER)] and allowed.everyone is False and allowed.roles is False


def test_every_click_edit_restricts_mentions_and_keeps_the_single_mention(monkeypatch):
    ctx, fake, present = _setup(monkeypatch)
    run = Running(present, _req())
    _wait(lambda: fake.sent)
    evil = MENTION + "card @everyone <@&5>"
    i = _click(fake, _custom_id(fake, "once"), evil)
    run.finish()
    assert i.response.allowed and all(a is not None for a in i.response.allowed)
    _owner_only(i.response.allowed[0])
    assert i.response.calls[0][1].count(MENTION) == 1 and i.response.calls[0][1].startswith(MENTION)
    closed = _click(fake, "hda:a:unknown:once", evil)  # close_card
    _owner_only(closed.response.allowed[0])
    assert closed.response.calls[0][1].count(MENTION) == 1


def _click(fake, custom_id, content, extra=None):
    from fakes import interaction
    i = interaction(custom_id, content=content)
    i.data.update(extra or {})
    asyncio.run(connection.on_interaction(i))
    return i


def test_clarify_select_and_modal_edits_restrict_mentions(monkeypatch):
    from test_clarify_ping import _clarify
    pid, fake, answer, _ = _clarify(monkeypatch, choices=("Red", "Blue"), multi=True)
    sel = _click(fake, fake.selects[0][1], MENTION + "card @here", {"values": ["0"]})
    _owner_only(sel.response.allowed[0])
    pid, fake, answer, _ = _clarify(monkeypatch, choices=("Red",))
    modal = _click(fake, f"hda:c:{pid}:answer", MENTION + "card")
    sub = _click(fake, f"hda:m:{pid}", MENTION + "card @everyone",
                 {"components": [{"type": 1, "components": [{"type": 4, "custom_id": "answer", "value": "x"}]}]})
    assert modal.response.calls[0][0] == "send_modal"
    _owner_only(sub.response.allowed[0])


def test_a_batch_stays_in_one_destination_and_warns_once(monkeypatch):
    user, records = Dm(), []
    ch = Target(RuntimeError("gone"))
    _client(monkeypatch, ch, user)
    handler = _Collect(records)
    connection.logger.addHandler(handler)
    route = {}
    try:
        for n in range(3):
            asyncio.run(connection.send(f"m{n}", channel=CHANNEL, mention=n == 0, route=route))
    finally:
        connection.logger.removeHandler(handler)
    assert user.calls == ["m0", "m1", "m2"]
    assert len([r for r in records if r.levelname == "WARNING"]) == 1


def test_send_batch_shares_one_route_across_cards_and_footer(monkeypatch):
    fake, on_prompt, _ = _hooks(monkeypatch)
    routes = []
    real = fake.send

    async def spy(text, buttons=(), select=None, channel="", mention=False, route=None):
        routes.append(route)
        return await real(text, buttons, select, channel, mention)

    monkeypatch.setattr(flows.connection, "send", spy)
    _use(monkeypatch, FakeCtx({"away": True}))
    _batch(on_prompt, "srq-a", _q("q0", "one"), _q("q1", "two"))
    assert len(routes) == 3 and routes[0] is not None and all(r is routes[0] for r in routes)
