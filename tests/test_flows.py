import logging
import threading
import time

import hermes_yaml as yaml
from fakes import OWNER, FakeConnection, FakeCtx, _context, secrets
from hda import connection, flows
from hermes_cli.approval_transport import ApprovalRequest
from hermes_cli.plugins import PluginManager
from tools import approval_context, approval_prompt


def _req(allow_session=True, command="rm -rf /tmp/example", timeout_seconds=300):
    return ApprovalRequest.create(
        command=command, description="recursive delete", pattern_key="rm_recursive", pattern_keys=("rm_recursive",),
        session_key="session-a", surface="cli", allow_session=allow_session, allow_permanent=True,
        timeout_seconds=timeout_seconds,
    )


def _setup(monkeypatch, away=True, gateway=True):
    secrets(monkeypatch)
    monkeypatch.setattr(flows, "_pending", {})
    monkeypatch.setattr(flows, "_titles", flows.OrderedDict())
    fake = FakeConnection(gateway).install(monkeypatch)
    ctx = FakeCtx({"away": away})
    for name in ("_questions", "_starts", "_replies"):
        monkeypatch.setattr(flows, name, {})
    return ctx, fake, flows.make_present(ctx)


def _wait(condition, seconds=5):
    end = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < end, "timed out waiting"
        time.sleep(0.01)


def _custom_id(fake, choice):
    return next(cid for _, cid, _ in fake.sent[0][1] if cid.endswith(f":{choice}"))


class Running:
    """present() on its own thread, as the host runs it."""

    def __init__(self, present, request):
        self.result = self.error = None

        def target():
            try:
                self.result = present(request)
            except Exception as e:
                self.error = e

        self.thread = threading.Thread(target=target)
        self.thread.start()

    def finish(self):
        self.thread.join(5)
        assert not self.thread.is_alive()
        return self


def test_away_off_raises_and_sends_nothing(monkeypatch):
    _, fake, present = _setup(monkeypatch, away=False)
    try:
        present(_req())
        assert False, "expected PassThrough"
    except connection.PassThrough:
        pass
    assert fake.sent == []


def test_card_has_once_session_deny_and_never_always(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    request = _req()
    assert "always" in request.allowed_choices
    run = Running(present, request)
    _wait(lambda: fake.sent)
    text, buttons = fake.sent[0]
    assert [(label, cid) for label, cid, _ in buttons] == [
        (flows.BTN_ONCE, f"hda:a:{request.request_id}:once"),
        (flows.BTN_SESSION, f"hda:a:{request.request_id}:session"),
        (flows.BTN_DENY, f"hda:a:{request.request_id}:deny"),
    ]
    assert "rm -rf /tmp/example" in text and "recursive delete" in text and flows.UNTITLED in text
    assert "Answer within 5 minutes or it is denied." in text
    # a forged always click is refused like any unknown choice
    assert fake.click(f"hda:a:{request.request_id}:always")[0][1].endswith(flows.CLOSED)
    fake.click(_custom_id(fake, "deny"))
    run.finish()


def test_one_shot_request_has_only_once_and_deny(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    run = Running(present, _req(allow_session=False))
    _wait(lambda: fake.sent)
    assert [label for label, _, _ in fake.sent[0][1]] == [flows.BTN_ONCE, flows.BTN_DENY]
    fake.click(_custom_id(fake, "deny"))
    run.finish()


def test_click_once_responds_and_edits_card_without_view(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    request = _req()
    run = Running(present, request)
    _wait(lambda: fake.sent)
    calls = fake.click(_custom_id(fake, "once"), content="card")
    assert calls == [("edit_message", "card\n\n" + flows.ALLOWED_ONCE, None)]
    decision = run.finish().result
    assert (decision.request_id, decision.request_digest, decision.choice) == (request.request_id, request.digest, "once")


def test_click_session_and_deny_edit_text(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    run = Running(present, _req())
    _wait(lambda: fake.sent)
    assert fake.click(_custom_id(fake, "session"), content="c")[0][1] == "c\n\n" + flows.ALLOWED_SESSION
    assert run.finish().result.choice == "session"
    fake.sent.clear()
    run = Running(present, _req())
    _wait(lambda: fake.sent)
    assert fake.click(_custom_id(fake, "deny"), content="c")[0][1] == "c\n\n" + flows.DENIED
    assert run.finish().result.choice == "deny"


def test_deadline_is_shorter_than_host_timeout_and_denies(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    ticks = iter(range(0, 1000, 5))  # a fake clock: 5 s per look
    monkeypatch.setattr(flows, "monotonic", lambda: next(ticks))
    decision = present(_req(timeout_seconds=20))
    assert decision.choice == "deny"
    assert "Answer within 5 seconds" in fake.sent[0][0]  # max(10, 20 - 15) minus the 5 s the clock showed before the card
    assert fake.edits == [(1, fake.sent[0][0] + "\n\n" + flows.TIMED_OUT)]


def test_away_flipping_off_mid_wait_raises_and_moves_card(monkeypatch):
    ctx, fake, present = _setup(monkeypatch)
    run = Running(present, _req())
    _wait(lambda: fake.sent)
    ctx.config["away"] = False
    assert isinstance(run.finish().error, connection.PassThrough)
    assert fake.edits == [(1, fake.sent[0][0] + "\n\n" + flows.MOVED_TO_TERMINAL)]
    # the card is closed, so a late click is refused
    assert fake.click(_custom_id(fake, "once"), content="x")[0][1].endswith(flows.CLOSED)


def test_other_user_gets_not_yours_and_nothing_resolves(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    run = Running(present, _req())
    _wait(lambda: fake.sent)
    assert fake.click(_custom_id(fake, "once"), user_id="999") == [("send_message", flows.NOT_YOURS, True)]
    time.sleep(0.7)
    assert run.thread.is_alive() and fake.edits == []
    fake.click(_custom_id(fake, "deny"))
    run.finish()


def test_second_click_and_unknown_id_are_closed(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    run = Running(present, _req())
    _wait(lambda: fake.sent)
    fake.click(_custom_id(fake, "once"))
    run.finish()
    assert fake.click(_custom_id(fake, "deny"), content="c") == [("edit_message", "c\n\n" + flows.CLOSED, None)]
    assert fake.click("hda:a:unknown:once", content="c") == [("edit_message", "c\n\n" + flows.CLOSED, None)]


def test_post_response_closes_a_still_open_card_and_wakes_present(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    request = _req()
    run = Running(present, request)
    _wait(lambda: fake.sent)
    flows.post_approval_response(request_id=request.request_id, surface=flows.SURFACE, choice="transport_timeout")
    assert run.finish().result.choice == "deny"
    assert fake.edits == [(1, fake.sent[0][0] + "\n\n" + flows.CLOSED)]
    flows.post_approval_response(request_id=request.request_id, surface=flows.SURFACE)  # second call is harmless
    assert len(fake.edits) == 1


def test_not_in_gateway_raises_and_sends_nothing(monkeypatch):
    _, fake, present = _setup(monkeypatch, gateway=False)
    try:
        present(_req())
        assert False, "expected PassThrough"
    except connection.PassThrough:
        pass
    assert fake.sent == []


def test_connection_not_ready_raises(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    fake.ready_ok = False
    try:
        present(_req())
        assert False, "expected PassThrough"
    except connection.PassThrough:
        pass
    assert fake.sent == [] and flows._pending == {}


def test_failed_send_raises_and_leaves_nothing_pending(monkeypatch):
    _, fake, present = _setup(monkeypatch)

    def boom(text, buttons=()):
        raise RuntimeError("discord down")

    monkeypatch.setattr(connection, "send_dm", boom)
    try:
        present(_req())
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
    assert flows._pending == {}


def test_long_command_is_clipped_and_backticks_neutralised(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    request = _req(command="```" + "x" * 3000)
    run = Running(present, request)
    _wait(lambda: fake.sent)
    text, buttons = fake.sent[0]
    assert flows.CUT_SHORT in text and "x" * 1500 not in text and len(text) < 2000
    assert text.count("```") == 2  # only the card's own fence
    assert text.endswith(flows.TOO_LONG)
    assert [label for label, _, _ in buttons] == [flows.BTN_DENY]  # the hidden tail can't be approved
    assert fake.click(f"hda:a:{request.request_id}:once")[0][1].endswith(flows.CLOSED)
    fake.click(_custom_id(fake, "deny"))
    run.finish()


def test_command_at_the_limit_is_not_clipped(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    run = Running(present, _req(command="y" * flows.COMMAND_CHARS))
    _wait(lambda: fake.sent)
    assert flows.TOO_LONG not in fake.sent[0][0] and len(fake.sent[0][1]) == 3
    fake.click(_custom_id(fake, "deny"))
    run.finish()


def test_four_or_more_backticks_cannot_close_the_fence(monkeypatch):
    for ticks in (4, 7):
        _, fake, present = _setup(monkeypatch)
        run = Running(present, _req(command="a" + "`" * ticks + "\n**Why:** fine"))
        _wait(lambda: fake.sent)
        assert fake.sent[0][0].count("```") == 2, ticks
        fake.click(_custom_id(fake, "deny"))
        run.finish()


def test_slow_ready_and_send_count_against_the_deadline(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    now = [0]

    def clock():  # every look takes 5 s
        now[0] += 5
        return now[0] - 5

    async def slow_send(text, buttons=(), select=None):
        now[0] += 8
        fake.sent.append((text, list(buttons)))
        return 1

    monkeypatch.setattr(flows, "monotonic", clock)
    monkeypatch.setattr(connection, "send_dm", slow_send)
    monkeypatch.setattr(connection, "ready", lambda timeout: now.__setitem__(0, now[0] + 8))
    decision = present(_req(timeout_seconds=40))  # window 25 s from the start
    assert decision.choice == "deny"
    assert "Answer within 12 seconds" in fake.sent[0][0]  # 25 - 13 spent before the card
    assert now[0] < 40  # the wait stopped 25 s from the start, not 25 s after the send


def test_title_from_pre_hook_is_in_the_card(monkeypatch):
    ctx, fake, present = _setup(monkeypatch)
    monkeypatch.setattr(flows, "_lookup_title", lambda session_id: f"Title of {session_id}")
    request = _req()
    flows.make_pre_approval_request(ctx)(request_id=request.request_id, session_id="s1", surface=flows.SURFACE)
    run = Running(present, request)
    _wait(lambda: fake.sent)
    assert "Session: Title of s1\n" in fake.sent[0][0] and flows._titles == {}
    fake.click(_custom_id(fake, "deny"))
    run.finish()


def test_title_lookup_failure_gives_untitled_and_titles_are_bounded(monkeypatch):
    ctx, _, _ = _setup(monkeypatch)
    pre = flows.make_pre_approval_request(ctx)
    assert flows._lookup_title("no-such-session") == flows.UNTITLED  # the temp home has no session store
    pre(request_id="other", session_id="x", surface="cli")
    assert flows._titles == {}
    ctx.config["away"] = False
    pre(request_id="off", session_id="x", surface=flows.SURFACE)  # away is off: no lookup
    assert flows._titles == {}
    ctx.config["away"] = True
    for i in range(flows.MAX_TITLES + 10):
        pre(request_id=str(i), session_id="x", surface=flows.SURFACE)
    assert len(flows._titles) == flows.MAX_TITLES and "0" not in flows._titles


def test_approval_prompt_notice_sends_text_without_buttons(monkeypatch):
    ctx, fake, _ = _setup(monkeypatch)
    monkeypatch.setattr(flows, "_lookup_title", lambda session_id: "My session")
    notice, _ = flows.make_prompt_hooks(ctx)
    notice(session_id="s1", kind="sudo", prompt_id="p")
    assert fake.sent == []
    notice(session_id="s1", kind="approval", prompt_id="p")
    assert fake.sent == [(flows.SUBAGENT_WAITING.format(title="My session"), [])]
    ctx.config["away"] = False
    notice(session_id="s1", kind="approval", prompt_id="p")
    assert len(fake.sent) == 1


# Spike 002's five tests, run against the real present() through the real host code.

def _host_setup(monkeypatch, present, fallback="builtin"):
    from tools import approval

    manager = PluginManager()
    _context(manager, "discord-approval").register_approval_transport("discord-away", present)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval, "detect_hardline_command", lambda c: (False, ""))
    monkeypatch.setattr(approval, "_check_sudo_stdin_guard", lambda c: (False, ""))
    monkeypatch.setattr(approval, "_match_user_deny_rule", lambda c: None)
    monkeypatch.setattr(approval, "_command_matches_permanent_allowlist", lambda c: False)
    monkeypatch.setattr(approval, "detect_dangerous_command", lambda c: (True, "danger", "dangerous"))
    monkeypatch.setattr(approval, "get_current_session_key", lambda *a, **k: "session-a")
    monkeypatch.setattr(approval, "is_approved", lambda *a: False)
    monkeypatch.setattr(approval_prompt, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(approval_context, "_get_approval_transport_config", lambda: ("discord-away", fallback))
    return approval


def _host_run(approval):
    calls = []

    def builtin(*a, **k):
        calls.append("builtin")
        return "once"

    return calls, approval.check_all_command_guards("rm -rf /tmp/x", "local", approval_callback=builtin)


def _click_when_sent(fake, choice, after=0.0):
    def target():
        _wait(lambda: fake.sent)
        time.sleep(after)
        fake.click(_custom_id(fake, choice))

    threading.Thread(target=target).start()


def test_spike_away_off_falls_through_to_builtin(monkeypatch):
    _, fake, present = _setup(monkeypatch, away=False)
    calls, result = _host_run(_host_setup(monkeypatch, present))
    assert calls == ["builtin"] and result["approved"] is True and fake.sent == []


def test_spike_away_on_replaces_builtin_and_deny_blocks(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    approval = _host_setup(monkeypatch, present)
    _click_when_sent(fake, "once")
    calls, result = _host_run(approval)
    assert calls == [] and result["approved"] is True
    assert fake.edits == [] and len(fake.sent) == 1  # the click edited the card itself
    fake.sent.clear()
    _click_when_sent(fake, "deny")
    calls, result = _host_run(approval)
    assert calls == [] and result["approved"] is False


def test_spike_flip_off_mid_wait_surfaces_builtin(monkeypatch):
    ctx, fake, present = _setup(monkeypatch)
    approval = _host_setup(monkeypatch, present)
    threading.Thread(target=lambda: (_wait(lambda: fake.sent), ctx.config.update(away=False))).start()
    calls, result = _host_run(approval)
    assert calls == ["builtin"] and result["approved"] is True
    assert fake.edits[-1][1].endswith(flows.MOVED_TO_TERMINAL)


def test_spike_without_fallback_a_raise_denies(monkeypatch):
    _, fake, present = _setup(monkeypatch, away=False)
    calls, result = _host_run(_host_setup(monkeypatch, present, fallback=None))
    assert calls == [] and result["approved"] is False


def test_spike_transport_config_is_read_per_request(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"security": {"approval": {"transport": "discord-away", "transport_fallback": "builtin"}}}))
    assert approval_context._get_approval_transport_config() == ("discord-away", "builtin")
    time.sleep(0.05)
    cfg.write_text(yaml.safe_dump({"security": {"approval": {"transport": "builtin"}}}))
    assert approval_context._get_approval_transport_config()[0] == "builtin"


def test_present_failure_is_logged_by_type_only_and_reraised(monkeypatch):
    _, _, present = _setup(monkeypatch)
    monkeypatch.setattr(connection, "ready", lambda timeout: (_ for _ in ()).throw(ValueError("secret-token")))
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    flows.logger.addHandler(handler)
    try:
        present(_req())
        assert False, "expected ValueError"
    except ValueError:
        pass
    finally:
        flows.logger.removeHandler(handler)
    warnings = [r.getMessage() for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "ValueError" in warnings[0] and "secret-token" not in warnings[0]
