import asyncio
import logging
import threading
import time

import discord
from fakes import OWNER, FakeConnection, secrets
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
    conn = connection.Connection()

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
    assert calls == [("edit_message", "Approval needed\n\n" + flows.CLOSED, None)]


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
