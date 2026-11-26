import asyncio
import logging
import threading
import time

import discord
from fakes import OWNER, FakeConnection, FakeCtx, secrets
from hda import connection, flows


class FakeClient:
    """Stands in for discord.Client: never touches the network."""
    instances = []
    start_script = None  # async fn(client, call_number), default blocks forever

    def __init__(self, intents=None):
        self.intents = intents
        self.handlers = {}
        self.presence_calls = 0
        self.closed = False
        FakeClient.instances.append(self)

    def event(self, fn):
        self.handlers[fn.__name__] = fn
        return fn

    async def change_presence(self, **kw):
        self.presence_calls += 1

    async def start(self, token, reconnect=True):
        if FakeClient.start_script:
            return await FakeClient.start_script(self, len(FakeClient.instances))
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


def _patch_client(monkeypatch, script=None):
    FakeClient.instances = []
    FakeClient.start_script = script
    monkeypatch.setattr(discord, "Client", FakeClient)
    monkeypatch.setattr(connection, "_conn", None)


def _wait_for(cond, seconds=3):
    end = time.monotonic() + seconds
    while not cond() and time.monotonic() < end:
        time.sleep(0.01)
    assert cond()


def test_get_from_two_threads_gives_one_client_and_one_thread(monkeypatch):
    secrets(monkeypatch)
    _patch_client(monkeypatch)
    got = []
    threads = [threading.Thread(target=lambda: got.append(connection.get())) for _ in range(2)]
    try:
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert got[0] is got[1]
        _wait_for(lambda: len(FakeClient.instances) == 1)
        assert [t.name for t in threading.enumerate()].count("hda-discord") == 1
    finally:
        connection.stop()
    assert "hda-discord" not in [t.name for t in threading.enumerate()]
    assert FakeClient.instances[0].closed


def test_client_has_no_intents_and_never_changes_presence(monkeypatch):
    secrets(monkeypatch)
    _patch_client(monkeypatch)
    try:
        connection.get()
        _wait_for(lambda: FakeClient.instances)
    finally:
        connection.stop()
    client = FakeClient.instances[0]
    assert client.intents == discord.Intents.none()
    assert client.presence_calls == 0


def test_backoff_doubles_to_the_cap_and_resets_after_ready(monkeypatch):
    secrets(monkeypatch)

    async def script(client, call):
        if call == 9:
            await client.handlers["on_ready"]()
        raise ConnectionError("down")

    _patch_client(monkeypatch, script)
    sleeps = []
    conn = connection.Connection("fake-token")

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 10:
            conn._stopping = True

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(conn._main())
    assert sleeps == [5, 10, 20, 40, 80, 160, 300, 300, 5, 10]


def test_ensure_does_nothing_outside_the_gateway(monkeypatch):
    secrets(monkeypatch)
    _patch_client(monkeypatch)
    monkeypatch.setattr(connection, "in_gateway", lambda: False)
    connection.ensure()
    assert connection._conn is None and FakeClient.instances == []


def test_ensure_does_nothing_without_exactly_one_user(monkeypatch):
    secrets(monkeypatch, user="1,2")
    _patch_client(monkeypatch)
    monkeypatch.setattr(connection, "in_gateway", lambda: True)
    connection.ensure()
    assert connection._conn is None


def test_gateway_status_is_logged_once(monkeypatch):
    import gateway.status
    monkeypatch.setattr(gateway.status, "owns_gateway_runtime_lock", lambda: True)
    monkeypatch.setattr(connection, "_gateway", False)
    monkeypatch.setattr(connection, "_gateway_logged", None)
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    connection.logger.addHandler(handler)
    old_level = connection.logger.level
    connection.logger.setLevel(logging.INFO)
    try:
        assert connection.in_gateway() is True
        assert connection.in_gateway() is True
    finally:
        connection.logger.removeHandler(handler)
        connection.logger.setLevel(old_level)
    assert len(records) == 1 and records[0].levelno == logging.INFO
    assert "is the gateway" in records[0].getMessage()


def test_router_ignores_non_hda_custom_ids(monkeypatch):
    secrets(monkeypatch)
    assert FakeConnection().click("clarify:abc:0") == []
    assert FakeConnection().click("clarify:abc:0", user_id="999") == []


def test_router_refuses_other_users_without_routing(monkeypatch):
    secrets(monkeypatch)
    hits = []

    async def route(interaction, parts):
        hits.append(parts)

    monkeypatch.setattr(connection, "ROUTES", {"a": route})
    calls = FakeConnection().click("hda:a:req:once", user_id="999")
    assert calls == [("send_message", flows.NOT_YOURS, True)] and hits == []


def test_router_closes_unknown_kind_with_no_view(monkeypatch):
    secrets(monkeypatch)
    monkeypatch.setattr(connection, "ROUTES", {})
    calls = FakeConnection().click("hda:a:req:once", content="Approval needed")
    assert calls == [("edit_message", flows.with_status("Approval needed", flows.CLOSED), None)]


def test_router_passes_owner_clicks_to_the_route(monkeypatch):
    secrets(monkeypatch)
    hits = []

    async def route(interaction, parts):
        hits.append(parts)

    monkeypatch.setattr(connection, "ROUTES", {"a": route})
    assert FakeConnection().click(f"hda:a:req:once", user_id=OWNER) == []
    assert hits == [["req", "once"]]


def test_gateway_false_is_not_cached(monkeypatch):
    import gateway.status
    monkeypatch.setattr(connection, "_gateway", False)
    monkeypatch.setattr(gateway.status, "owns_gateway_runtime_lock", lambda: False)
    assert connection.in_gateway() is False
    monkeypatch.setattr(gateway.status, "owns_gateway_runtime_lock", lambda: True)
    assert connection.in_gateway() is True


def test_submit_logs_a_failed_background_send(monkeypatch):
    import concurrent.futures

    class Conn:
        def submit(self, coro):
            future = concurrent.futures.Future()
            future.set_exception(ValueError("private detail"))
            return future

    monkeypatch.setattr(connection, "get", lambda: Conn())
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    connection.logger.addHandler(handler)
    try:
        connection.submit(None)
    finally:
        connection.logger.removeHandler(handler)
    assert len(records) == 1 and "ValueError" in records[0].getMessage() and "private" not in records[0].getMessage()


def test_thread_gets_the_token_from_the_caller_and_never_reads_secrets(monkeypatch):
    caller = threading.current_thread()
    tokens = []

    def scoped_secret(name, default=None):
        if threading.current_thread() is not caller:  # stands in for UnscopedSecretError
            raise RuntimeError("unscoped")
        return "tok"

    async def script(client, call):
        await asyncio.Event().wait()

    class TokenClient(FakeClient):
        async def start(self, token, reconnect=True):
            tokens.append(token)
            await super().start(token, reconnect)

    _patch_client(monkeypatch, script)
    monkeypatch.setattr(connection, "get_secret", scoped_secret)
    monkeypatch.setattr(discord, "Client", TokenClient)
    try:
        connection.get()
        _wait_for(lambda: tokens)
    finally:
        connection.stop()
    assert tokens == ["tok"]


def test_dead_thread_is_logged_and_replaced_on_next_get(monkeypatch):
    secrets(monkeypatch)
    _patch_client(monkeypatch)

    def broken(intents=None):
        raise ValueError("secret detail")

    monkeypatch.setattr(discord, "Client", broken)
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    connection.logger.addHandler(handler)
    try:
        first = connection.get()
        first._thread.join(3)
        assert not first._thread.is_alive()
        assert len(records) == 1 and "ValueError" in records[0].getMessage() and "secret" not in records[0].getMessage()
        monkeypatch.setattr(discord, "Client", FakeClient)
        second = connection.get()
        assert second is not first
        assert connection.get() is second  # a live one is kept
        _wait_for(lambda: FakeClient.instances)
    finally:
        connection.logger.removeHandler(handler)
        connection.stop()


def test_hook_failures_are_logged_with_the_type_only():
    @flows._logged
    def hook(**_):
        raise KeyError("private detail")

    records = []
    handler = logging.Handler()
    handler.emit = records.append
    flows.logger.addHandler(handler)
    try:
        assert hook(a=1) is None
    finally:
        flows.logger.removeHandler(handler)
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    assert "KeyError" in records[0].getMessage() and "private" not in records[0].getMessage()


def test_first_clarify_after_a_restart_waits_for_the_connection_and_sends(monkeypatch):
    from tools import clarify_gateway
    sent = []

    class Client(FakeClient):
        async def fetch_user(self, user_id):
            async def send(text, view=None):
                sent.append((user_id, text))
            return type("User", (), {"send": staticmethod(send)})

    async def script(client, call):
        await asyncio.sleep(0.2)  # the connection is not up when the hook fires
        await client.handlers["on_ready"]()
        await asyncio.Event().wait()

    secrets(monkeypatch)
    _patch_client(monkeypatch, script)
    monkeypatch.setattr(discord, "Client", Client)
    monkeypatch.setattr(connection, "_gateway", True)
    monkeypatch.setattr(flows, "_questions", {})
    monkeypatch.setattr(flows, "_lookup_title", lambda session_id: "T")
    clarify_gateway.register("pw", "session-a", "Q?", ["x"], authority={})
    try:
        assert connection._conn is None
        flows._open_question("s1", "pw")
        _wait_for(lambda: sent)
    finally:
        clarify_gateway._entries.pop("pw", None)
        connection.stop()
    assert sent[0][0] == int(OWNER) and "Q?" in sent[0][1]


def test_warm_start_runs_once_when_the_gateway_lock_is_first_seen(monkeypatch):
    secrets(monkeypatch)
    fake = FakeConnection(gateway=False).install(monkeypatch)
    monkeypatch.setattr(flows, "_warmed", False)
    pre_turn = flows.make_ping_hooks(FakeCtx({"away": True}))[0]
    pre_turn(session_id="s", turn_id="t1")
    assert fake.ensured == 0  # not the gateway yet
    fake.gateway = True
    pre_turn(session_id="s", turn_id="t2")
    pre_turn(session_id="s", turn_id="t3")
    assert fake.ensured == 1


def test_warm_start_skips_when_away_and_ping_are_off(monkeypatch):
    secrets(monkeypatch)
    fake = FakeConnection().install(monkeypatch)
    monkeypatch.setattr(flows, "_warmed", False)
    flows.make_ping_hooks(FakeCtx())[0](session_id="s", turn_id="t")
    assert fake.ensured == 0


def test_token_captured_at_register_is_used_from_an_unscoped_thread(monkeypatch):
    tokens = []

    async def script(client, call):
        await asyncio.Event().wait()

    class TokenClient(FakeClient):
        async def start(self, token, reconnect=True):
            tokens.append(token)
            await super().start(token, reconnect)

    _patch_client(monkeypatch, script)
    monkeypatch.setattr(discord, "Client", TokenClient)
    monkeypatch.setattr(connection, "_token", None)
    monkeypatch.setattr(connection, "get_secret", lambda name, default=None: "captured")
    connection.capture_token()

    def unscoped(name, default=None):
        raise RuntimeError("unscoped")

    monkeypatch.setattr(connection, "get_secret", unscoped)
    worker = threading.Thread(target=connection.get)
    try:
        worker.start()
        worker.join(3)
        _wait_for(lambda: tokens)
    finally:
        connection.stop()
    assert tokens == ["captured"]


def test_swallowed_prompt_failures_and_a_missing_entry_are_logged(monkeypatch):
    secrets(monkeypatch)
    fake = FakeConnection().install(monkeypatch)
    fake.ready_ok = False
    monkeypatch.setattr(flows, "_questions", {})
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    flows.logger.addHandler(handler)
    try:
        on_prompt = flows.make_prompt_hooks(FakeCtx({"away": True}))[0]
        on_prompt(session_id="s", kind="clarify", prompt_id="srq-none")
        on_prompt(session_id="s", kind="approval", prompt_id="x")
    finally:
        flows.logger.removeHandler(handler)
    messages = [r.getMessage() for r in records if r.levelno == logging.WARNING]
    assert any("no pending clarify entry" in m for m in messages)
    assert any("approval prompt not sent" in m and "not ready" in m for m in messages)


def test_user_id_captured_at_register_is_read_without_a_secret_scope(monkeypatch):
    monkeypatch.setattr(connection, "_user_ids", None)
    monkeypatch.setattr(connection, "get_secret", lambda name, default=None: OWNER)
    connection.capture_token()

    def unscoped(name, default=None):
        raise RuntimeError("unscoped")

    monkeypatch.setattr(connection, "get_secret", unscoped)
    found = []
    worker = threading.Thread(target=lambda: found.append(connection.user_id()))
    worker.start()
    worker.join(3)
    assert found == [OWNER]
