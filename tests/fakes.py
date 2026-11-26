"""Test helpers. _manifest, _context and _request are copied from hermes-agent tests/hermes_cli/test_approval_transport.py:17-43."""
import asyncio
import inspect
from types import SimpleNamespace

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

from hda import connection

OWNER = "123456789012345678"


def _manifest(name: str = "fixture-approval") -> PluginManifest:
    return PluginManifest(
        name=name,
        version="1.0.0",
        description="fixture",
        source="user",
        key=name,
    )


def _context(manager: PluginManager, name: str = "fixture-approval") -> PluginContext:
    return PluginContext(_manifest(name), manager)


def _request():
    from hermes_cli.approval_transport import ApprovalRequest

    return ApprovalRequest.create(
        command="rm -rf /tmp/example",
        description="recursive delete",
        pattern_key="rm_recursive",
        pattern_keys=("rm_recursive",),
        session_key="session-a",
        surface="cli",
        allow_session=True,
        allow_permanent=True,
    )


def secrets(monkeypatch, user=OWNER, token="fake-token"):
    # Any name other than the user list is the bot token.
    monkeypatch.setattr(connection, "_user_ids", None)  # a captured value from an earlier register() would hide the patched secret
    monkeypatch.setattr(connection, "get_secret", lambda name, default=None: user if name == "DISCORD_ALLOWED_USERS" else token)


class FakeCtx:
    """Config backed by a dict. Records register_* and on_unload calls."""

    def __init__(self, config=None):
        self.config = dict(config or {})
        self.commands = {}
        self.unload = []
        self.transports = {}
        self.hooks = {}
        self.set_error = None

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        if self.set_error:
            raise self.set_error
        self.config[key] = value

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = handler

    def register_approval_transport(self, name, present_fn):
        self.transports[name] = present_fn

    def register_hook(self, name, callback, **kwargs):
        self.hooks[name] = callback

    def on_unload(self, callback):
        self.unload.append(callback)


class FakeResponse:
    def __init__(self):
        self.calls = []

    async def send_message(self, content, ephemeral=False):
        self.calls.append(("send_message", content, ephemeral))

    async def edit_message(self, content, view="unset"):
        self.calls.append(("edit_message", content, view))

    async def send_modal(self, modal):
        self.calls.append(("send_modal", modal.custom_id, modal.title))


def interaction(custom_id, user_id=OWNER, content="card text"):
    return SimpleNamespace(
        data={"custom_id": custom_id},
        user=SimpleNamespace(id=int(user_id)),
        message=SimpleNamespace(content=content),
        response=FakeResponse(),
    )


def _finish(value):
    return asyncio.run(value) if inspect.iscoroutine(value) else value


class FakeConnection:
    """Same interface as connection.py. click() and submit_modal() call the real router."""

    def __init__(self, gateway=True):
        self.gateway = gateway
        self.ensured = 0
        self.ready_ok = True
        self.sent = []  # (text, buttons) per DM
        self.selects = []  # the select menu of each DM that has one
        self.edits = []  # (message, text) per card edit

    def install(self, monkeypatch):
        """Replace the real send and thread helpers: run/submit run a coroutine to the end on the spot."""
        for name in ("in_gateway", "ensure", "ready", "send_dm", "edit_card"):
            monkeypatch.setattr(connection, name, getattr(self, name))
        monkeypatch.setattr(connection, "run", lambda value, timeout: _finish(value))
        monkeypatch.setattr(connection, "submit", _finish)
        return self

    def ready(self, timeout):
        if not self.ready_ok:
            raise connection.PassThrough("discord not ready")

    async def send_dm(self, text, buttons=(), select=None):
        self.sent.append((text, list(buttons)))
        if select:
            self.selects.append(select)
        return len(self.sent)  # stands in for the message

    async def edit_card(self, message, text):
        self.edits.append((message, text))

    def in_gateway(self):
        return self.gateway

    def ensure(self):
        self.ensured += 1

    def click(self, custom_id, user_id=OWNER, content="card text"):
        i = interaction(custom_id, user_id, content)
        asyncio.run(connection.on_interaction(i))
        return i.response.calls

    def select(self, custom_id, values, user_id=OWNER, content="card text"):
        i = interaction(custom_id, user_id, content)
        i.data["values"] = values
        asyncio.run(connection.on_interaction(i))
        return i.response.calls

    def submit_modal(self, custom_id, text, user_id=OWNER, content="card text"):
        i = interaction(custom_id, user_id, content)
        i.data["components"] = [{"type": 1, "components": [{"type": 4, "custom_id": "answer", "value": text}]}]
        asyncio.run(connection.on_interaction(i))
        return i.response.calls
