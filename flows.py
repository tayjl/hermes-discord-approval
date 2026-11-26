"""Strings and the /away and /ping command handlers."""

import functools
import json
import logging
import re
import threading
from collections import OrderedDict
from time import monotonic

from . import connection
from .connection import PassThrough

logger = logging.getLogger(__name__)


def _logged(hook):
    """Hermes logs a failing hook at DEBUG only: make it visible. Type only, never the message."""
    @functools.wraps(hook)
    def wrapper(*args, **kwargs):
        try:
            return hook(*args, **kwargs)
        except Exception as e:
            logger.warning("discord-approval: %s failed (%s)", hook.__name__, type(e).__name__)
    return wrapper

AWAY_ON = "Away is on. Approvals and questions go to your Discord DMs."
AWAY_OFF = "Away is off. Prompts go back to the terminal."
PING_ON = "Long-turn pings are on. Turns of 3 minutes or more send you a DM."
PING_OFF = "Long-turn pings are off."
PING_ALSO_ON = "Pings are on too."
USAGE_AWAY = "Use /away, /away on or /away off."
USAGE_PING = "Use /ping, /ping on or /ping off."
NOT_ALLOWED = "Only the owner can use this."
SAVE_FAILED = "Could not save the setting."

# Same look as the daily digest. Escape codes count toward Discord's 2000 characters.
_YELLOW, _RESET, _REVERSED = "\x1b[1;38;2;255;216;0m", "\x1b[0m", "\x1b[7;37m"
_RULE = "\u2500"
_GREY = "\x1b[38;5;245m"


def _header(title):
    return (f"```ansi\n{_YELLOW}\u256d{_RULE * 36}\u256e\n"
            f"{_REVERSED}{'-' * 7}{_RESET}{_YELLOW}{title}{_REVERSED}{'-' * (31 - len(title))}{_RESET}{_YELLOW}\n"
            f"\u2570{_RULE * 36}\u256f{_RESET}\n```")


HEADER = _header("HERMES DISCORD APPROVAL")
RESPONSE_HEADER = _header("HERMES DISCORD RESPONSE")
FOOTER = f"```ansi\n{_YELLOW}\u2570{_RULE * 36}\u256f{_RESET}\n```"
CODE_BOX = f"```ansi\n{_YELLOW}\u250c{_RULE * 37}{_RESET}\n{{code}}\n{_YELLOW}\u2514{_RULE * 37}{_RESET}\n```"

APPROVAL_CARD = HEADER + "\n**Approval needed** | __{title}__\n{code_box}\nWhy: {description}\nAnswer within {window} or it is denied."
CUT_SHORT = "(cut short)"
BTN_ONCE = "Allow once"
BTN_SESSION = "Allow for this session"
BTN_DENY = "Deny"
ALLOWED_ONCE = "Allowed once."
ALLOWED_SESSION = "Allowed for this session."
DENIED = "Denied."
TIMED_OUT = "No answer in time. Denied."
MOVED_TO_TERMINAL = "Away is off. This one moved to the terminal."
CLOSED = "This request is no longer open."
NOT_YOURS = "This is not for you."
UNTITLED = "Untitled session"
SUBAGENT_WAITING = HEADER + "\n**Waiting in the terminal** | __{title}__\n{title} needs approval in the terminal."

QUESTION_CARD = HEADER + "\n**Question** | __{title}__\n{question}"
ANSWER_BUTTON = "Answer"
SELECT_HINT = "Pick one or more"
MODAL_TITLE = "Your answer"
MODAL_LABEL = "Answer"
ANSWERED = "Answered: {answer}"
EMPTY_ANSWER = "Type an answer first."

TOO_LONG = "Too long to approve from here. Deny it or turn away off to see it in the terminal."

PING = HEADER + "\n**{outcome}** after {duration} | __{title}__"
OUTCOME = {"completed": "Done", "failed": "Failed", "interrupted": "Stopped"}

PING_AFTER_S = 180
DEADLINE_MARGIN_S = 15
READY_WAIT_S = 10
REPLY_LINES = 5
REPLY_CHARS = 500

SURFACE = "transport:discord-away"
COMMAND_CHARS = 1000
TITLE_CHARS = 100
DESCRIPTION_CHARS = 200  # keeps the card under Discord's 2000 character limit
MAX_TITLES = 64
QUESTION_CHARS = 1000
LABEL_CHARS = 80
ANSWER_CHARS = 200
MODAL_CHARS = 1000
MAX_LABELS = 24
ANSWER_ID = "answer"
MAX_AGE_S = 24 * 3600  # turns that never end are dropped after this

LOCAL_PLATFORMS = {"cli", "local", "desktop", "dashboard", "tui"}


def _allowed(source, platform):
    if platform == "discord":
        return source is not None and str(source.user_id) == connection.user_id()
    return platform in LOCAL_PLATFORMS


def _state(label, on):
    value = f"{_YELLOW}on{_RESET}" if on else f"{_GREY}off{_RESET}"
    return f"{label} : {value}"


def _local_panel(ctx):
    """Same rounded rich panel as the claude-swap plugin, with the skin's colours."""
    import io
    import shutil
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    try:
        from hermes_cli.skin_engine import get_active_skin
        skin = get_active_skin()
        accent, border, head = (skin.get_color("ui_accent", "#FFBF00"), skin.get_color("banner_border", "#CD7F32"),
                                skin.get_color("banner_title", "#FFD700"))
    except Exception:
        accent, border, head = "#FFBF00", "#CD7F32", "#FFD700"
    body = Text()
    for n, (label, key) in enumerate((("away-mode", "away"), ("ping-mode", "ping"))):
        on = ctx.get_config(key, False)
        body.append(("\n" if n else "") + f"{label} : ")
        body.append("on" if on else "off", style=f"bold {accent}" if on else "dim")
    buf = io.StringIO()
    # legacy_windows=False: otherwise rich swaps the rounded corners for square ones
    console = Console(file=buf, force_terminal=True, color_system="truecolor",
                      width=shutil.get_terminal_size().columns, legacy_windows=False)
    console.print(Panel(body, title=Text("HERMES DISCORD RESPONSE", style=f"bold {head}"), border_style=border,
                        padding=(0, 2), expand=False))
    # A reset before the bold title is redundant (the border style has no attributes to clear). Resets after
    # it stay, or the bold would leak into the border. The TUI collapses replies over 400 characters.
    return re.sub(r"\x1b\[0m(?=\x1b\[1;)", "", buf.getvalue().rstrip("\n"))


def _status_panel(ctx, platform, change=None):
    tail = f"\n{change}" if change else ""
    if platform in LOCAL_PLATFORMS:
        return f"{_local_panel(ctx)}{tail}"
    lines = "\n".join((_state("away-mode", ctx.get_config("away", False)),
                       _state("ping-mode", ctx.get_config("ping", False))))
    return f"{RESPONSE_HEADER}\n{CODE_BOX.format(code=lines)}{tail}"


def make_command(ctx, key, on_text, off_text, usage):
    def handler(raw_args, source=None, platform=None):
        if not _allowed(source, platform):
            return NOT_ALLOWED
        arg = (raw_args or "").strip().lower()
        if arg not in ("", "on", "off"):
            return usage
        value = not ctx.get_config(key, False) if arg == "" else arg == "on"
        try:
            ctx.set_config(key, value)
            if key == "away" and value:
                ctx.set_config("ping", True)
        except PermissionError:
            return SAVE_FAILED
        if value:
            connection.ensure()
        change = on_text if value else off_text
        # The local panel already shows ping-mode, and the note would push the reply past the TUI's 400 characters.
        also = key == "away" and value and platform not in LOCAL_PLATFORMS
        return _status_panel(ctx, platform, f"{change} {PING_ALSO_ON}" if also else change)
    return handler


class Entry:
    def __init__(self, text, choices):
        self.text = text
        self.choices = choices
        self.message = None
        self.event = threading.Event()
        self.choice = None
        self.done = False  # first resolution wins


_lock = threading.Lock()
_pending = {}  # request_id -> Entry, removed by the post_approval_response hook
_titles = OrderedDict()  # request_id -> session title, filled by the pre hook


def _redact(text):
    from agent.redact import redact_approval_display
    return redact_approval_display(text, force=True, prose=True)


def _lookup_title(session_id):
    try:
        from gateway.session_context import get_session_env
        from hermes_constants import get_hermes_home
        from persistence_assembly import open_persistence_reader
        with open_persistence_reader(get_hermes_home()) as persistence:
            title = persistence.sessions.get_title(session_id or get_session_env("HERMES_SESSION_ID"))
        return _redact(title)[:TITLE_CHARS] if title else UNTITLED
    except Exception:
        return UNTITLED


def make_pre_approval_request(ctx):
    @_logged
    def pre_approval_request(request_id=None, session_id=None, surface=None, **_):
        # present() runs on a worker thread with no session context, so the title is read here.
        if surface != SURFACE or not ctx.get_config("away", False):
            return
        title = _lookup_title(session_id)
        with _lock:
            _titles[request_id] = title
            while len(_titles) > MAX_TITLES:
                _titles.popitem(last=False)
    return pre_approval_request


@_logged
def post_approval_response(request_id=None, surface=None, **_):
    # Still open here means the host gave up first (interrupted, busy or timed out).
    if surface != SURFACE:
        return
    with _lock:
        entry = _pending.pop(request_id, None)
    if entry is not None:
        _settle(entry, "deny", CLOSED)


def _window(seconds):
    if seconds >= 60:
        minutes = round(seconds / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{round(seconds)} seconds"


def approval_card(request, title, window):
    """The card text and whether the command was clipped."""
    # No escape or control characters: a command must not fake colours or close the box.
    command = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", request.command)
    command = re.sub(r"`(?=``)", "`\u200b", command)  # a zero-width space breaks every run of 3 backticks
    clipped = len(command) > COMMAND_CHARS
    if clipped:
        command = f"{command[:COMMAND_CHARS]}\n{CUT_SHORT}"
    text = APPROVAL_CARD.format(title=title, code_box=CODE_BOX.format(code=command), window=window,
                                description=_redact(request.description)[:DESCRIPTION_CHARS])
    return (f"{text}\n{TOO_LONG}" if clipped else text), clipped


def framed(text, status=None):
    """The footer goes last, so a status line sits above it."""
    return f"{text}\n\n**{status}**\n{FOOTER}" if status else f"{text}\n{FOOTER}"


def with_status(content, status):
    return framed(content.removesuffix(f"\n{FOOTER}"), status)


def _settle(entry, choice, line):
    """Resolve an open card once. False if something resolved it first."""
    with _lock:
        if entry.done:
            return False
        entry.done, entry.choice = True, choice
    entry.event.set()
    if entry.message is not None:
        try:
            connection.submit(connection.edit_card(entry.message, framed(entry.text, line)))
        except Exception:
            logger.warning("discord-approval: could not edit the card")
    return True


async def on_approval_click(interaction, parts):
    request_id, choice = parts[0], parts[-1]
    with _lock:
        entry = _pending.get(request_id)
        won = entry is not None and not entry.done and choice in entry.choices
        if won:
            entry.done, entry.choice = True, choice
    if not won:
        await connection.close_card(interaction)
        return
    entry.event.set()
    line = {"once": ALLOWED_ONCE, "session": ALLOWED_SESSION, "deny": DENIED}[choice]
    try:
        await interaction.response.edit_message(content=with_status(interaction.message.content, line), view=None)
    except Exception as e:
        # The choice stands, so say so: the card keeps its buttons and a second click gets CLOSED.
        logger.warning("discord-approval: choice taken but the card edit failed (%s)", type(e).__name__)


connection.ROUTES["a"] = on_approval_click


def make_present(ctx):
    def present(request):
        # Hermes reports any exception as a bare "error", so say what it was. Type only, never the message.
        try:
            return _present(request)
        except PassThrough as e:
            logger.info("discord-approval: present passed through (%s)", e)  # our own fixed text
            raise
        except Exception as e:
            logger.warning("discord-approval: present failed (%s)", type(e).__name__)
            raise

    def _present(request):
        start = monotonic()  # the host's clock started about now, so every deadline counts from here
        if not ctx.get_config("away", False) or not connection.in_gateway() or not connection.user_id():
            raise PassThrough("away is off")
        connection.ready(READY_WAIT_S)
        deadline = start + max(10, request.timeout_seconds - DEADLINE_MARGIN_S)  # ends before the host gives up
        with _lock:
            title = _titles.pop(request.request_id, UNTITLED)
        text, clipped = approval_card(request, title, _window(max(1, deadline - monotonic())))
        # A clipped command hides its tail, so it can only be denied here.
        choices = ["deny"] if clipped else [c for c in ("once", "session", "deny") if c in request.allowed_choices]  # never "always"
        entry = Entry(text, choices)
        buttons = [(label, f"{connection.PREFIX}a:{request.request_id}:{c}", style) for c, label, style in (
            ("once", BTN_ONCE, "success"), ("session", BTN_SESSION, "secondary"), ("deny", BTN_DENY, "danger"),
        ) if c in choices]
        with _lock:
            _pending[request.request_id] = entry
        try:
            entry.message = connection.run(connection.send_dm(framed(entry.text), buttons), 10)
        except Exception:
            with _lock:
                _pending.pop(request.request_id, None)
            raise
        while not entry.event.wait(0.5):
            if not ctx.get_config("away", False):
                if _settle(entry, None, MOVED_TO_TERMINAL):
                    raise PassThrough("away turned off")
            elif monotonic() > deadline:
                _settle(entry, "deny", TIMED_OUT)
        return request.respond(entry.choice)
    return present


_questions = {}  # prompt_id -> Entry, removed when answered or settled


def _on():
    return connection.in_gateway() and connection.user_id()


_warmed = False


def _warm(ctx):
    """The gateway lock is claimed after the plugin loads, so start the connection at the first hook that sees it."""
    global _warmed
    if _warmed or not connection.in_gateway():
        return
    _warmed = True
    if ctx.get_config("away", False) or ctx.get_config("ping", False):
        connection.ensure()


def _send_soon(coro):
    """For hooks on the agent's own thread: never block it. Send now when connected, else once the connection is up."""
    try:
        connection.ready(0)
    except PassThrough:
        def wait_then_send():
            try:
                connection.ready(READY_WAIT_S)
                connection.submit(coro)
            except PassThrough as e:
                coro.close()
                logger.warning("discord-approval: message not sent (%s)", e)
        threading.Thread(target=wait_then_send, name="hda-send", daemon=True).start()
    else:
        connection.submit(coro)


async def _send_question(entry, buttons, select):
    entry.message = await connection.send_dm(framed(entry.text), buttons, select)
    if entry.done:  # settled while the card was still being sent
        await connection.edit_card(entry.message, framed(entry.text, CLOSED))


def _open_question(session_id, prompt_id):
    from tools import clarify_gateway
    pending = clarify_gateway.get_pending(prompt_id)
    if pending is None:
        logger.warning("discord-approval: no pending clarify entry for prompt %s, not sent", str(prompt_id)[:12])
        return
    question = _redact(pending.question)[:QUESTION_CHARS]
    labels = [_redact(c)[:LABEL_CHARS] for c in pending.choices or []]
    if len(labels) > MAX_LABELS or not all(label.strip() for label in labels):
        labels = []  # Discord allows 24 choices and no empty label, so the card falls back to Answer only
    cid = f"{connection.PREFIX}c:{prompt_id}"
    select = None
    if pending.multi_select and labels:
        buttons = []
        select = (SELECT_HINT, f"{connection.PREFIX}cs:{prompt_id}", list(zip(labels, map(str, range(len(labels))))))
    else:
        buttons = [(label, f"{cid}:{n}", "primary") for n, label in enumerate(labels)]
    buttons.append((ANSWER_BUTTON, f"{cid}:answer", "secondary"))
    # entry.choices keeps the real labels: the card shows redacted ones, the answer must be exact.
    entry = Entry(QUESTION_CARD.format(title=_lookup_title(session_id), question=question), list(pending.choices or []))
    with _lock:
        _questions[prompt_id] = entry
    try:
        connection.ready(READY_WAIT_S)  # the first use after a restart starts the connection and waits for it
        connection.submit(_send_question(entry, buttons, select))
    except Exception:
        with _lock:
            _questions.pop(prompt_id, None)
        raise


def _open_entry(prompt_id):
    with _lock:
        entry = _questions.get(prompt_id)
        return entry if entry is not None and not entry.done else None


async def _answer(interaction, prompt_id, answer):
    from tools import clarify_gateway
    with _lock:
        entry = _questions.pop(prompt_id, None)
        if entry is not None:
            entry.done = True
    ok = entry is not None and clarify_gateway.resolve_gateway_clarify(prompt_id, answer)
    line = ANSWERED.format(answer=answer[:ANSWER_CHARS]) if ok else CLOSED
    await interaction.response.edit_message(content=with_status(interaction.message.content, line), view=None)


def _answer_modal(prompt_id):
    import discord
    modal = discord.ui.Modal(title=MODAL_TITLE, custom_id=f"{connection.PREFIX}cm:{prompt_id}")
    field = discord.ui.TextInput(label=MODAL_LABEL, style=discord.TextStyle.paragraph, max_length=MODAL_CHARS, custom_id=ANSWER_ID)
    modal.add_item(field)
    return modal, field


async def on_clarify_click(interaction, parts):
    prompt_id, what = parts[0], parts[-1]
    entry = _open_entry(prompt_id)
    if entry is None:
        await connection.close_card(interaction)
    elif what == "answer":
        modal, _ = _answer_modal(prompt_id)
        await interaction.response.send_modal(modal)
    elif what.isascii() and what.isdigit() and int(what) < len(entry.choices):
        await _answer(interaction, prompt_id, entry.choices[int(what)])
    else:
        await connection.close_card(interaction)


async def on_clarify_select(interaction, parts):
    entry = _open_entry(parts[0])
    values = interaction.data.get("values") or []
    if entry is None or not values or not all(v.isascii() and v.isdigit() and int(v) < len(entry.choices) for v in values):
        await connection.close_card(interaction)
        return
    await _answer(interaction, parts[0], json.dumps([entry.choices[int(v)] for v in values], ensure_ascii=False))


async def on_clarify_modal(interaction, parts):
    if _open_entry(parts[0]) is None:
        await connection.close_card(interaction)
        return
    # Rebuild the modal and let discord.py fill the TextInput from the submitted payload.
    modal, field = _answer_modal(parts[0])
    modal._refresh(interaction, interaction.data["components"], {})
    answer = field.value.strip()
    if not answer:
        await interaction.response.send_message(EMPTY_ANSWER, ephemeral=True)  # the card stays open
        return
    await _answer(interaction, parts[0], answer)


connection.ROUTES["c"] = on_clarify_click
connection.ROUTES["cs"] = on_clarify_select
connection.ROUTES["cm"] = on_clarify_modal


def make_prompt_hooks(ctx):
    @_logged
    def on_session_prompt(session_id=None, kind=None, prompt_id=None, **_):
        _warm(ctx)
        if kind not in ("approval", "clarify") or not ctx.get_config("away", False) or not _on():
            return
        try:
            if kind == "clarify":
                _open_question(session_id, prompt_id)
            else:
                # Profile-routed subagents use the builtin queue, so all we can do is say one is waiting.
                # This runs on the hook worker thread, so waiting for the connection is safe.
                connection.ready(READY_WAIT_S)
                connection.submit(connection.send_dm(framed(SUBAGENT_WAITING.format(title=_lookup_title(session_id)))))
        except PassThrough as e:
            logger.warning("discord-approval: %s prompt not sent (%s)", kind, e)

    @_logged
    def on_session_prompt_settled(prompt_id=None, **_):
        with _lock:
            entry = _questions.pop(prompt_id, None)
        if entry is not None:
            _settle(entry, None, CLOSED)

    return on_session_prompt, on_session_prompt_settled


_starts = {}  # turn_id -> (monotonic start, session_id), top-level turns only
_replies = {}  # turn_id -> (monotonic time, reply)


def _duration(seconds):
    minutes = int(seconds // 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


def _excerpt(reply):
    lines = [line.strip() for line in _redact(reply).splitlines() if line.strip()][:REPLY_LINES]
    return "\n".join(f"> {line}" for line in "\n".join(lines)[:REPLY_CHARS].splitlines())


def make_ping_hooks(ctx):
    @_logged
    def pre_llm_call(session_id=None, turn_id=None, parent_session_id=None, **_):
        _warm(ctx)
        now = monotonic()
        with _lock:
            for table in (_starts, _replies):
                for key in [k for k, v in table.items() if now - v[0] > MAX_AGE_S]:
                    del table[key]
            if turn_id and not parent_session_id:
                _starts[turn_id] = (now, session_id)

    @_logged
    def post_llm_call(turn_id=None, assistant_response=None, **_):
        with _lock:
            if turn_id in _starts:
                _replies[turn_id] = (monotonic(), str(assistant_response or ""))

    @_logged
    def on_session_end(turn_id=None, completed=None, failed=None, interrupted=None, **_):
        with _lock:
            started = _starts.pop(turn_id, None)
            reply = _replies.pop(turn_id, (0, ""))[1]
        if started is None or not ctx.get_config("ping", False) or not _on():
            return
        seconds = monotonic() - started[0]
        if seconds < PING_AFTER_S:
            return
        outcome = "interrupted" if interrupted else "failed" if failed else "completed"
        text = PING.format(outcome=OUTCOME[outcome], duration=_duration(seconds), title=_lookup_title(started[1]))
        excerpt = _excerpt(reply)
        try:
            _send_soon(connection.send_dm(framed(f"{text}\n{excerpt}" if excerpt else text)))
        except PassThrough as e:
            logger.warning("discord-approval: ping not sent (%s)", e)

    return pre_llm_call, post_llm_call, on_session_end
