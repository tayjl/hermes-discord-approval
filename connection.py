"""The one discord.py client of this process: own thread and loop, start/stop, interaction router."""

import asyncio
import logging
import threading

import discord
from agent.secret_scope import get_secret

logger = logging.getLogger(__name__)

BACKOFF_START_S = 5
BACKOFF_MAX_S = 300
PREFIX = "hda:"

_lock = threading.Lock()
_conn = None
_token = None  # set by capture_token(), never logged
_user_ids = None  # raw DISCORD_ALLOWED_USERS, set by capture_token()
_gateway = False  # cached once True: the lock may be claimed after the plugin loads
_gateway_logged = None
_warned_user = False

# kind ("a", "c", "cs", "cm") -> async handler(interaction, parts), added by flows.py.
# A handler acks first and calls close_card() when the id is unknown or finished.
ROUTES = {}


class PassThrough(RuntimeError):
    """Raised so the host falls back to its normal prompt."""


def in_gateway():
    global _gateway, _gateway_logged
    if _gateway:
        return True
    try:
        from gateway.status import owns_gateway_runtime_lock
        _gateway = bool(owns_gateway_runtime_lock())
    except Exception:
        pass
    if _gateway_logged != _gateway:
        _gateway_logged = _gateway
        logger.info("discord-approval: this process %s the gateway", "is" if _gateway else "is not")
    return _gateway


def user_id():
    """The single allowed Discord user id as a string, or None (one warning) if not exactly one."""
    global _warned_user
    raw = _user_ids if _user_ids is not None else get_secret("DISCORD_ALLOWED_USERS")  # worker threads have no secret scope
    ids = [i.strip() for i in (raw or "").split(",") if i.strip()]
    if len(ids) == 1 and ids[0].isdigit():
        return ids[0]
    if not _warned_user:
        _warned_user = True
        logger.warning("discord-approval: DISCORD_ALLOWED_USERS must hold exactly one user id, staying inactive")
    return None


class Connection:
    def __init__(self, token):
        self._token = token  # read by the caller: this thread has no secret scope
        self.loop = None
        self.client = None
        self.ready_event = threading.Event()
        self.failed = False  # no usable token: retrying can't help, so ready() raises at once
        self.delay = BACKOFF_START_S
        self._stopping = False
        self._task = None
        self._thread = threading.Thread(target=self._run, name="hda-discord", daemon=True)

    def _run(self):
        try:
            asyncio.run(self._main())
        except Exception as e:
            logger.warning("discord-approval: connection thread crashed (%s)", type(e).__name__)

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        token = self._token
        try:
            if not token:
                self._fail("no token")
                return
            while not self._stopping:
                client = self.client = discord.Client(intents=discord.Intents.none())

                @client.event
                async def on_ready():
                    self.delay = BACKOFF_START_S
                    self.ready_event.set()
                    logger.info("discord-approval: connected")

                client.event(on_interaction)
                try:
                    await client.start(token, reconnect=True)
                except discord.LoginFailure as e:
                    self._fail(type(e).__name__)
                    return
                except Exception as e:
                    # Type only: never put anything from the login path in a log line.
                    logger.warning("discord-approval: connection failed (%s), retrying in %ss", type(e).__name__, self.delay)
                self.ready_event.clear()
                await asyncio.sleep(self.delay)
                self.delay = min(self.delay * 2, BACKOFF_MAX_S)
        except asyncio.CancelledError:
            pass
        finally:
            if self.client is not None:
                try:
                    await self.client.close()
                except Exception:
                    pass

    def _fail(self, why):
        self.failed = True
        self.ready_event.set()  # wakes anyone waiting in ready()
        logger.warning("discord-approval: cannot log in (%s), not retrying", why)

    def close(self):
        self._stopping = True
        if self._thread.is_alive():  # after a failed login the loop is already closed
            if self.loop is not None and self._task is not None:
                self.loop.call_soon_threadsafe(self._task.cancel)
            self._thread.join(5)

    def ready(self, timeout):
        if not self.ready_event.wait(timeout) or self.failed:
            raise PassThrough("discord login failed" if self.failed else "discord not ready")
        return self.client

    def submit(self, coro):
        """Run on the plugin loop without waiting. Returns a concurrent future."""
        if self.loop is None:
            coro.close()
            raise PassThrough("discord not started")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro, timeout):
        return self.submit(coro).result(timeout)


def capture_token():
    """Read the token and user id once where a secret scope exists (register), for threads that have none."""
    global _token, _user_ids, _conn
    try:
        _token = get_secret("DISCORD_BOT_TOKEN") or _token
        _user_ids = get_secret("DISCORD_ALLOWED_USERS") or _user_ids
    except Exception as e:
        logger.warning("discord-approval: could not read the secrets at load (%s)", type(e).__name__)
    with _lock:
        if _conn is not None and _conn.failed and _conn._token != _token:
            _conn = None  # a corrected token gets a fresh login without a restart


def get():
    global _conn
    with _lock:
        if _conn is None or not (_conn._thread.is_alive() or _conn.failed):  # a crashed thread is replaced, a failed login is not
            _conn = Connection(_token or get_secret("DISCORD_BOT_TOKEN"))
            _conn._thread.start()
            logger.info("discord-approval: connection thread started")
        return _conn


def ensure():
    """Start the connection early. Does nothing outside the gateway or without a valid config."""
    if in_gateway() and user_id() and (_token or get_secret("DISCORD_BOT_TOKEN")):
        get()


def ready(timeout):
    return get().ready(timeout)


def run(coro, timeout):
    return get().run(coro, timeout)


def _log_failure(future):
    if not future.cancelled() and future.exception() is not None:
        logger.warning("discord-approval: background send failed (%s)", type(future.exception()).__name__)


def submit(coro):
    """Fire and forget: failures are logged, not raised."""
    future = get().submit(coro)
    future.add_done_callback(_log_failure)
    return future


async def send_dm(text, buttons=(), select=None):
    """DM the user. buttons: (label, custom_id, style name) tuples. select: (placeholder, custom_id, [(label, value)]), pick one or more. Returns the message."""
    view = discord.ui.View(timeout=None)
    if select:
        placeholder, select_id, options = select
        view.add_item(discord.ui.Select(
            custom_id=select_id, placeholder=placeholder, min_values=1, max_values=len(options),
            options=[discord.SelectOption(label=label, value=value) for label, value in options]))
    for label, custom_id, style in buttons:
        view.add_item(discord.ui.Button(label=label, custom_id=custom_id, style=getattr(discord.ButtonStyle, style)))
    user = await get().client.fetch_user(int(user_id()))  # type: ignore[union-attr, arg-type]
    return await user.send(text, view=view)


async def edit_card(message, text):
    """Final state of a card: new text, buttons removed."""
    await message.edit(content=text, view=None)


def stop():
    global _conn
    with _lock:
        conn, _conn = _conn, None
    if conn is not None:
        conn.close()


async def close_card(interaction):
    """Final state for an unknown or finished request. The edit is the ack."""
    from .flows import CLOSED, with_status
    message = interaction.message
    await interaction.response.edit_message(content=with_status(message.content, CLOSED), view=None)


async def on_interaction(interaction):
    custom_id = (interaction.data or {}).get("custom_id") or ""
    if not custom_id.startswith(PREFIX):
        return  # not ours, e.g. the gateway's own clarify cards
    from .flows import NOT_YOURS
    if str(interaction.user.id) != user_id():
        await interaction.response.send_message(NOT_YOURS, ephemeral=True)
        return
    parts = custom_id[len(PREFIX):].split(":")
    handler = ROUTES.get(parts[0])
    if handler is None:
        await close_card(interaction)
        return
    try:
        await handler(interaction, parts[1:])
    except Exception:
        logger.exception("discord-approval: interaction handler failed")
