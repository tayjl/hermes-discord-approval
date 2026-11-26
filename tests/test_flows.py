import asyncio
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
    monkeypatch.setattr(flows, "_passed", flows.OrderedDict())
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
    assert flows.CLOSED in fake.click(f"hda:a:{request.request_id}:always")[0][1]
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
    assert calls == [("edit_message", flows.with_status("card", flows.ALLOWED_ONCE), None)]
    decision = run.finish().result
    assert (decision.request_id, decision.request_digest, decision.choice) == (request.request_id, request.digest, "once")


def test_click_session_and_deny_edit_text(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    run = Running(present, _req())
    _wait(lambda: fake.sent)
    assert fake.click(_custom_id(fake, "session"), content="c")[0][1] == flows.with_status("c", flows.ALLOWED_SESSION)
    assert run.finish().result.choice == "session"
    fake.sent.clear()
    run = Running(present, _req())
    _wait(lambda: fake.sent)
    assert fake.click(_custom_id(fake, "deny"), content="c")[0][1] == flows.with_status("c", flows.DENIED)
    assert run.finish().result.choice == "deny"


def test_deadline_is_shorter_than_host_timeout_and_denies(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    ticks = iter(range(0, 1000, 5))  # a fake clock: 5 s per look
    monkeypatch.setattr(flows, "monotonic", lambda: next(ticks))
    decision = present(_req(timeout_seconds=20))
    assert decision.choice == "deny"
    assert "Answer within 5 seconds" in fake.sent[0][0]  # max(10, 20 - 15) minus the 5 s the clock showed before the card
    assert fake.edits == [(1, flows.with_status(fake.sent[0][0], flows.TIMED_OUT))]


def test_away_flipping_off_mid_wait_raises_and_moves_card(monkeypatch):
    ctx, fake, present = _setup(monkeypatch)
    run = Running(present, _req())
    _wait(lambda: fake.sent)
    ctx.config["away"] = False
    assert isinstance(run.finish().error, connection.PassThrough)
    assert fake.edits == [(1, flows.with_status(fake.sent[0][0], flows.MOVED_TO_TERMINAL))]
    # the card is closed, so a late click is refused
    assert flows.CLOSED in fake.click(_custom_id(fake, "once"), content="x")[0][1]


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
    assert fake.click(_custom_id(fake, "deny"), content="c") == [("edit_message", flows.with_status("c", flows.CLOSED), None)]
    assert fake.click("hda:a:unknown:once", content="c") == [("edit_message", flows.with_status("c", flows.CLOSED), None)]


def test_post_response_closes_a_still_open_card_and_wakes_present(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    request = _req()
    run = Running(present, request)
    _wait(lambda: fake.sent)
    flows.post_approval_response(request_id=request.request_id, surface=flows.SURFACE, choice="transport_timeout")
    assert run.finish().result.choice == "deny"
    assert fake.edits == [(1, flows.with_status(fake.sent[0][0], flows.CLOSED))]
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
    assert flows.CUT_SHORT in text and "x" * flows.COMMAND_CHARS not in text and len(text) < 2000
    assert text.count("```") == 6  # header, code box and footer only
    assert text.removesuffix(f"\n{flows.FOOTER}").endswith(flows.TOO_LONG)
    assert [label for label, _, _ in buttons] == [flows.BTN_ONCE, flows.BTN_SESSION, flows.BTN_DENY]  # still approvable
    assert text.count(flows.TOO_LONG) == 1
    assert flows.CLOSED not in fake.click(f"hda:a:{request.request_id}:once")[0][1]
    run.finish()


def test_worst_case_cards_and_their_edited_form_fit_discord(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    title = "T" * flows.TITLE_CHARS
    monkeypatch.setattr(flows, "_lookup_title", lambda session_id: title)
    request = ApprovalRequest.create(
        command="`" * 5000, description="d" * 1000, pattern_key="k", pattern_keys=("k",),  # backticks double once broken up
        session_key="session-a", surface="cli", allow_session=True, allow_permanent=True, timeout_seconds=300)
    flows._titles[request.request_id] = title
    run = Running(present, request)
    _wait(lambda: fake.sent)
    text = fake.sent[0][0]
    assert flows.TOO_LONG in text and len(text) < 2000
    assert len(flows.with_status(text, flows.TIMED_OUT)) < 2000
    fake.click(_custom_id(fake, "deny"))
    run.finish()
    card = flows.framed(flows.QUESTION_CARD.format(title=title, question="q" * flows.QUESTION_CHARS))
    assert len(flows.with_status(card, flows.ANSWERED.format(answer="a" * flows.ANSWER_CHARS))) < 2000


def test_the_card_with_every_warning_and_the_longest_command_fits_discord():
    # Escapes make each character 4 long, so the clip must count the displayed text.
    command = "\x1b" + "x" * 5000 + " Authorization: Bearer " + SECRET  # a secret drops the escapes, so the length is plain text
    text = flows.approval_card(command, "d" * 1000, "T" * flows.TITLE_CHARS, "300 seconds", edit=True)
    for line in (flows.TOO_LONG, flows.HIDDEN, flows.CONTROL, flows.EDIT):
        assert line in text
    longest = max(flows.STATUS_LINES, key=len)
    assert len(flows.with_status(flows.framed(text), longest)) < 2000
    assert flows.COMMAND_CHARS == 1500 and flows.TITLE_CHARS == 100 and flows.DESCRIPTION_CHARS == 200


def test_a_plain_command_fills_the_room_and_clips_past_it():
    card = lambda n: flows.approval_card("x" * n, "why", "Title", "300 seconds")
    assert flows.COMMAND_CHARS == 1500
    full = card(1400)  # the header, footer and a reserved status line leave room for a little over 1400
    assert "x" * 1400 in full and flows.CUT_SHORT not in full and flows.TOO_LONG not in full
    assert len(flows.with_status(flows.framed(full), max(flows.STATUS_LINES, key=len))) < 2000
    clipped = card(1501)
    assert "x" * 1501 not in clipped and flows.CUT_SHORT in clipped and flows.TOO_LONG in clipped
    assert "x" * 1300 in clipped and len(flows.with_status(flows.framed(clipped), max(flows.STATUS_LINES, key=len))) < 2000


def test_a_long_title_and_description_shrink_the_command_room_only_when_needed():
    text = flows.approval_card("\x1b" + "x" * 3000 + " Authorization: Bearer " + SECRET, "d" * 1000, "T" * flows.TITLE_CHARS, "300 seconds", edit=True)
    assert flows.CUT_SHORT in text and "x" * 1400 not in text  # clipped well below 1500 shown characters
    assert len(flows.with_status(flows.framed(text), max(flows.STATUS_LINES, key=len))) < 2000


def test_a_long_command_of_control_characters_without_a_secret_is_clipped_with_escapes():
    units = lambda s: len(s.encode("utf-16-le")) // 2
    text = flows.approval_card("\x1b" * 500, "d" * 1000, "T" * flows.TITLE_CHARS, "300 seconds", edit=True)
    assert flows.CUT_SHORT in text and flows.CONTROL in text and flows.HIDDEN not in text
    assert "\\x1b" in text and "\x1b\x1b" not in text  # the card's own colour codes use single ESC
    assert units(flows.with_status(flows.framed(text), max(flows.STATUS_LINES, key=len))) < 2000


def test_command_cannot_fake_colours_or_close_the_box(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    run = Running(present, _req(command="a\x1b[31mred\x1b[0m\x07\tb\r\nc"))
    _wait(lambda: fake.sent)
    text = fake.sent[0][0]
    assert "a\\x1b[31mred\\x1b[0m\\x07\tb\nc" in text  # shown as escapes, tab and CRLF (as a newline) stay
    assert flows.CONTROL in text and [label for label, _, _ in fake.sent[0][1]] == [flows.BTN_ONCE, flows.BTN_SESSION, flows.BTN_DENY]
    fake.click(_custom_id(fake, "deny"))
    run.finish()


def test_command_at_the_limit_is_not_clipped(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    run = Running(present, _req(command="y" * 1300))
    _wait(lambda: fake.sent)
    assert flows.TOO_LONG not in fake.sent[0][0] and len(fake.sent[0][1]) == 3
    fake.click(_custom_id(fake, "deny"))
    run.finish()


def test_four_or_more_backticks_cannot_close_the_fence(monkeypatch):
    for ticks in (4, 7):
        _, fake, present = _setup(monkeypatch)
        run = Running(present, _req(command="a" + "`" * ticks + "\n**Why:** fine"))
        _wait(lambda: fake.sent)
        assert fake.sent[0][0].count("```") == 6, ticks
        fake.click(_custom_id(fake, "deny"))
        run.finish()


def test_slow_ready_and_send_count_against_the_deadline(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    now = [0]

    def clock():  # every look takes 5 s, for this thread only: a leftover thread must not move it
        if threading.current_thread() is not threading.main_thread():
            return time.monotonic()
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
    assert "**Approval needed** | __Title of s1__\n" in fake.sent[0][0] and flows._titles == {}
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


def _notice(monkeypatch, away=True):
    ctx, fake, present = _setup(monkeypatch, away=away)
    monkeypatch.setattr(flows, "_lookup_title", lambda session_id: "My session")
    notice, settled = flows.make_prompt_hooks(ctx)
    return ctx, fake, present, notice, settled


def test_approval_prompt_notice_sends_text_without_buttons_once(monkeypatch):
    ctx, fake, _, notice, _ = _notice(monkeypatch)
    notice(session_id="s1", kind="sudo", prompt_id="p")
    assert fake.sent == []
    notice(session_id="s1", kind="approval", prompt_id="p")
    assert fake.sent == [(flows.framed(flows.SUBAGENT_WAITING.format(title="My session")), [])]
    notice(session_id="s1", kind="approval", prompt_id="p")  # one DM per prompt
    assert len(fake.sent) == 1
    ctx.config["away"] = False
    notice(session_id="s1", kind="approval", prompt_id="other")
    assert len(fake.sent) == 1


def test_the_waiting_notice_is_edited_when_the_prompt_settles(monkeypatch):
    _, fake, _, notice, settled = _notice(monkeypatch)
    notice(session_id="s1", kind="approval", prompt_id="p")
    settled(prompt_id="p")
    assert fake.edits == [(1, flows.with_status(fake.sent[0][0], flows.CLOSED))]
    settled(prompt_id="p")
    assert len(fake.edits) == 1


def test_the_waiting_notice_only_comes_from_the_profile_that_loaded_the_plugin(monkeypatch, tmp_path):
    import hermes_constants
    _, fake, _, notice, _ = _notice(monkeypatch)
    # A routed subagent's approval is queued on its parent's session, so it arrives in the parent's profile scope.
    notice(session_id="parent", kind="approval", prompt_id="p1")
    assert len(fake.sent) == 1
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path / "other")
    notice(session_id="theirs", kind="approval", prompt_id="p2")
    assert len(fake.sent) == 1


def test_an_approval_present_gave_back_gets_no_waiting_notice(monkeypatch):
    ctx, fake, present, notice, _ = _notice(monkeypatch, away=False)
    request = _req()
    try:
        present(request)  # away is off: the host falls back to its own prompt and announces the same id
    except connection.PassThrough:
        pass
    ctx.config["away"] = True
    notice(session_id="s1", kind="approval", prompt_id=request.request_id)
    assert fake.sent == []
    notice(session_id="s1", kind="approval", prompt_id="someone else's")
    assert len(fake.sent) == 1


def test_the_passed_through_ids_are_bounded(monkeypatch):
    ctx, fake, present, _, _ = _notice(monkeypatch, away=False)
    for _ in range(flows.MAX_TITLES + 5):
        try:
            present(_req())
        except connection.PassThrough:
            pass
    assert len(flows._passed) == flows.MAX_TITLES


ALL = ["once", "deny", "session", "always"]
SECRET = "sk-" + "a1B2c3D4" * 4


def _queue(monkeypatch, *ids, command="rm -rf /tmp/example"):
    """Pending builtin-queue approvals of one parent session, as Hermes holds them."""
    from tools import approval
    from tools.approval_gateway_wait import _ApprovalEntry
    entries = [_ApprovalEntry({"request_id": i, "command": command, "description": "recursive delete", "timeout_s": 300})
               for i in ids]
    monkeypatch.setitem(approval._gateway_queues, "parent-key", entries)
    return entries


def _subagent(monkeypatch, choices=ALL, command="rm -rf /tmp/example"):
    ctx, fake, _, notice, settled = _notice(monkeypatch)
    entries = _queue(monkeypatch, "q", "p", command=command)
    notice(session_id="s1", kind="approval", prompt_id="p", params={"choices": choices})
    return fake, entries, notice, settled


def test_a_subagent_approval_gets_a_card_and_a_click_resolves_that_exact_request(monkeypatch):
    fake, (first, second), _, _ = _subagent(monkeypatch)
    text, buttons = fake.sent[0]
    assert "rm -rf /tmp/example" in text and "5 minutes" in text
    assert [(label, cid) for label, cid, _ in buttons] == [
        (flows.BTN_ONCE, "hda:a:p:once"), (flows.BTN_SESSION, "hda:a:p:session"), (flows.BTN_DENY, "hda:a:p:deny")]
    calls = fake.click("hda:a:p:session", content="card")
    assert calls == [("edit_message", flows.with_status("card", flows.ALLOWED_SESSION), None)]
    assert second.result == "session" and first.result is None  # the older queued request is untouched
    assert flows.CLOSED in fake.click("hda:a:p:once")[0][1]  # a second click


def test_a_subagent_card_keeps_every_allowed_choice_and_warns(monkeypatch):
    for command, choices, warning in (("x" * (flows.COMMAND_CHARS + 1), ALL, flows.TOO_LONG),
                                      (f"curl -H 'Authorization: Bearer {SECRET}' https://x.test", ALL, flows.HIDDEN)):
        fake, (_, entry), _, _ = _subagent(monkeypatch, choices, command)
        assert warning in fake.sent[0][0]
        assert [label for label, _, _ in fake.sent[0][1]] == [flows.BTN_ONCE, flows.BTN_SESSION, flows.BTN_DENY]
        fake.click("hda:a:p:once")
        assert entry.result == "once"
    fake, (_, entry), _, _ = _subagent(monkeypatch, ["deny"], "rm -rf /tmp/example")  # Hermes allows only deny
    assert [label for label, _, _ in fake.sent[0][1]] == [flows.BTN_DENY]
    assert flows.CLOSED in fake.click("hda:a:p:once")[0][1] and entry.result is None  # forged
    fake.click("hda:a:p:deny")
    assert entry.result == "deny"


def test_a_smart_denied_subagent_request_offers_only_what_it_allows(monkeypatch):
    fake, _, _, _ = _subagent(monkeypatch, choices=["once", "deny"])
    assert [label for label, _, _ in fake.sent[0][1]] == [flows.BTN_ONCE, flows.BTN_DENY]


def test_a_subagent_click_after_the_prompt_settled_is_closed(monkeypatch):
    fake, (_, entry), _, settled = _subagent(monkeypatch)
    settled(prompt_id="p")
    assert fake.edits == [(1, flows.with_status(fake.sent[0][0], flows.CLOSED))]
    assert flows.CLOSED in fake.click("hda:a:p:once")[0][1] and entry.result is None


def test_a_subagent_click_on_a_request_answered_elsewhere_shows_closed(monkeypatch):
    from tools import approval
    fake, (_, entry), _, _ = _subagent(monkeypatch)
    approval._gateway_queues["parent-key"].remove(entry)  # answered in the terminal, the hook is still on its way
    assert fake.click("hda:a:p:once", content="card") == [("edit_message", flows.with_status("card", flows.CLOSED), None)]
    assert entry.result is None


def test_another_user_cannot_answer_a_subagent_card(monkeypatch):
    fake, (_, entry), _, _ = _subagent(monkeypatch)
    assert fake.click("hda:a:p:once", user_id="999")[0][1] == flows.NOT_YOURS and entry.result is None


def test_a_subagent_approval_that_cannot_be_found_falls_back_to_the_waiting_notice(monkeypatch):
    _, fake, _, notice, _ = _notice(monkeypatch)
    notice(session_id="s1", kind="approval", prompt_id="p", params={"choices": ALL})  # not in the queue
    _queue(monkeypatch, "q")
    notice(session_id="s1", kind="approval", prompt_id="q", params=None)  # no choices to honour
    assert fake.sent == [(flows.framed(flows.SUBAGENT_WAITING.format(title="My session")), [])] * 2


def test_a_first_dm_that_fails_leaves_nothing_registered(monkeypatch):
    _, fake, _, notice, _ = _notice(monkeypatch)

    async def down(text, buttons=(), select=None):
        raise RuntimeError("discord down")

    monkeypatch.setattr(connection, "send_dm", down)
    notice(session_id="s1", kind="approval", prompt_id="p")
    assert flows._questions == {}


def test_away_flipping_off_mid_wait_leaves_nothing_pending(monkeypatch):
    ctx, fake, present = _setup(monkeypatch)
    run = Running(present, _req())
    _wait(lambda: fake.sent)
    ctx.config["away"] = False
    assert isinstance(run.finish().error, connection.PassThrough)
    assert flows._pending == {} and len(flows._passed) == 1


def test_a_click_after_the_timeout_is_closed_and_the_denial_stands(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    ticks = iter(range(0, 1000, 5))
    monkeypatch.setattr(flows, "monotonic", lambda: next(ticks))
    request = _req(timeout_seconds=20)
    assert present(request).choice == "deny"
    assert flows.CLOSED in fake.click(f"hda:a:{request.request_id}:once", content="c")[0][1]
    assert flows._pending == {}


def test_a_failed_card_edit_after_a_click_still_takes_the_buttons_off(monkeypatch):
    from fakes import interaction
    _, fake, present = _setup(monkeypatch)
    run = Running(present, _req())
    _wait(lambda: fake.sent)

    async def refuse(content, view="unset"):
        raise RuntimeError("discord down")

    i = interaction(_custom_id(fake, "once"))
    i.response.edit_message = refuse
    queued = []
    monkeypatch.setattr(connection, "submit", queued.append)  # on the real loop this schedules the edit, so do the same
    asyncio.run(connection.on_interaction(i))
    for coro in queued:
        asyncio.run(coro)
    assert run.finish().result.choice == "once"
    assert fake.edits == [(1, flows.with_status(fake.sent[0][0], flows.ALLOWED_ONCE))]


def test_a_command_with_a_secret_is_redacted_and_warns(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    secret = "sk-" + "a1B2c3D4" * 4
    run = Running(present, _req(command=f"curl -H 'Authorization: Bearer {secret}' https://x.test"))
    _wait(lambda: fake.sent)
    text, buttons = fake.sent[0]
    assert secret not in text and flows.HIDDEN in text and len(text) < 2000
    assert [label for label, _, _ in buttons] == [flows.BTN_ONCE, flows.BTN_SESSION, flows.BTN_DENY]
    fake.click(_custom_id(fake, "deny"))
    run.finish()


def test_a_plain_command_has_no_warning(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    run = Running(present, _req())
    _wait(lambda: fake.sent)
    assert not any(w in fake.sent[0][0] for w in (flows.TOO_LONG, flows.HIDDEN, flows.CONTROL, flows.EDIT))
    assert len(fake.sent[0][1]) == 3
    fake.click(_custom_id(fake, "deny"))
    run.finish()


def test_benign_commands_have_no_warning_and_hidden_characters_do(monkeypatch):
    for command in ("cat C:/Users/me/AppData/x.txt", "curl https://example.com/a?b=1&c=2", "git log --author=a@b.com",
                    "git push --dry-run origin main", 'npx supabase db query --linked --read-only "select 1"',
                    "echo héllo 😀"):
        text = flows.approval_card(command, "why", "t", "5 minutes")
        assert flows.HIDDEN not in text and flows.CONTROL not in text and command in text, command
        assert flows.approval_buttons("r", ["once", "session", "deny", "always"])[0] == ["once", "session", "deny"]
    text = flows.approval_card("echo a\u200db", "why", "t", "5 minutes")  # a joiner is invisible in a command
    assert flows.CONTROL in text


def test_control_characters_warn_and_stay_approvable(monkeypatch):
    for command in ("echo safe\rrm -rf /", "echo ok\x1b[2K rm -rf x"):
        text = flows.approval_card(command, "why", "t", "5 minutes")
        assert flows.CONTROL in text and flows.HIDDEN not in text, command
    _, fake, present = _setup(monkeypatch)
    run = Running(present, _req(command="echo safe\rrm -rf /"))
    _wait(lambda: fake.sent)
    assert [label for label, _, _ in fake.sent[0][1]] == [flows.BTN_ONCE, flows.BTN_SESSION, flows.BTN_DENY]
    fake.click(_custom_id(fake, "deny"))
    run.finish()
    text = flows.approval_card("a\n\tb", "why", "t", "5 minutes")  # newline and tab are shown, so they are fine
    assert flows.CONTROL not in text


def test_hidden_characters_show_as_escapes_and_warn():
    text = flows.approval_card("echo safe\rrm -rf /", "why", "t", "5 minutes")
    assert flows.CONTROL in text and "echo safe\\rrm -rf /" in text and "\r" not in text
    text = flows.approval_card("echo a\u202eb\x1b[2K", "why", "t", "5 minutes")
    assert flows.CONTROL in text and "echo a\\u202eb\\x1b[2K" in text and "\u202e" not in text
    for char in "\u2028\u2029\u200b\u200f\u2066\ufeff\u00ad":
        assert flows.CONTROL in flows.approval_card(f"echo a{char}b", "why", "t", "5 minutes"), hex(ord(char))
    text = flows.approval_card("echo a\n" + "`" * 3, "why", "t", "5 minutes")  # the plugin's own break is not a hidden character
    assert flows.CONTROL not in text


def test_a_control_character_after_sk_does_not_hide_the_secret_tail():
    body = "a1B2c3D4" * 4
    for char in "\x1b\x00\u200b\u00ad\u202e":
        text = flows.approval_card(f"echo sk-{char}{body}", "why", "t", "5 minutes")
        assert body not in text and flows.HIDDEN in text and flows.CONTROL in text, hex(ord(char))
    for command in (f"KEY=sk-{body}", f"curl -H 'Authorization: Bearer sk-{body}'", f"OPENAI_API_KEY={body}"):
        text = flows.approval_card(command, "why", "t", "5 minutes")
        assert body not in text and flows.HIDDEN in text, command


def test_a_decoy_secret_padded_with_control_characters_does_not_leak_a_second_secret():
    body = "abcdefghijklmnopqrstuvwxyz0123456789"
    for decoy in (f"API_KEY={SECRET}", f"mysql --password={SECRET}", f"Authorization: Bearer {SECRET}", f'{{"password": "{SECRET}"}}'):
        for command in (f"{decoy}{chr(27) * 30} ; export K=sk-proj-\x1b{body}",
                        f"export K=sk-proj-\x1b{body} ; {decoy}{chr(27) * 30}"):
            text = flows.approval_card(command, "why", "t", "5 minutes")
            assert body not in text and flows.HIDDEN in text and flows.CONTROL in text, command


def test_card_length_stays_under_the_limit_in_utf16_units():
    units = lambda s: len(s.encode("utf-16-le")) // 2
    text = flows.approval_card("😀" * 1400, "d" * 1000, "T" * flows.TITLE_CHARS, "300 seconds", edit=True)
    assert flows.TOO_LONG in text and units(text) < 2000
    assert all(units(flows.with_status(text, line)) < 2000 for line in flows.STATUS_LINES)


def test_crlf_multiline_indented_command_stays_approvable(monkeypatch):
    _, fake, present = _setup(monkeypatch)
    command = "cat <<EOF\r\n        indented\r\n\t\tmore\r\nEOF"
    text = flows.approval_card(command, "why", "t", "5 minutes")
    assert flows.CONTROL not in text and flows.HIDDEN not in text and "\r" not in text and "        indented\n" in text
    run = Running(present, _req(command=command))
    _wait(lambda: fake.sent)
    assert len(fake.sent[0][1]) == 3
    fake.click(_custom_id(fake, "deny"))
    run.finish()


def test_description_loses_control_characters_and_backtick_runs():
    text = flows.approval_card("ls", "d\x1b[31m```x", "t", "5 minutes")
    why = text.split("Why: ")[1]
    assert "\x1b" not in why and "```" not in why


def test_a_subagent_card_with_an_edit_warns_and_keeps_the_allowed_choices(monkeypatch):
    ctx, fake, _, notice, _ = _notice(monkeypatch)
    entries = _queue(monkeypatch, "p")
    entries[0].data["edit"] = {"command": "ls"}
    notice(session_id="s1", kind="approval", prompt_id="p", params={"choices": ["once", "deny"]})  # Hermes allows once and deny for an edit
    assert flows.EDIT in fake.sent[0][0]
    assert [label for label, _, _ in fake.sent[0][1]] == [flows.BTN_ONCE, flows.BTN_DENY]


def test_several_warnings_show_together(monkeypatch):
    ctx, fake, _, notice, _ = _notice(monkeypatch)
    entries = _queue(monkeypatch, "p")
    entries[0].data.update(command="x" * flows.COMMAND_CHARS + "\rcurl -H 'Authorization: Bearer " + SECRET + "' u", edit={"command": "ls"})
    notice(session_id="s1", kind="approval", prompt_id="p", params={"choices": ALL})
    text = fake.sent[0][0]
    assert all(w in text for w in (flows.TOO_LONG, flows.CONTROL, flows.EDIT))
    assert len(fake.sent[0][1]) == 3


def test_a_click_is_logged_by_id_and_choice_only(monkeypatch):
    fake, _, _, _ = _subagent(monkeypatch)
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    flows.logger.addHandler(handler)
    old = flows.logger.level
    flows.logger.setLevel(logging.INFO)
    try:
        fake.click("hda:a:p:deny")
    finally:
        flows.logger.removeHandler(handler)
        flows.logger.setLevel(old)
    messages = [r.getMessage() for r in records]
    assert any("approval p answered deny" in m for m in messages) and not any("rm -rf" in m for m in messages)


def test_a_title_cannot_carry_newlines_ansi_or_backtick_runs(monkeypatch):
    import contextlib
    import persistence_assembly
    from types import SimpleNamespace
    title = "line one\n\x1b[31mred```break"
    reader = SimpleNamespace(sessions=SimpleNamespace(get_title=lambda session_id: title))
    monkeypatch.setattr(persistence_assembly, "open_persistence_reader", lambda home: contextlib.nullcontext(reader))
    shown = flows._lookup_title("s1")
    assert "\n" not in shown and "\x1b" not in shown and "```" not in shown and shown.startswith("line one ") and shown.endswith("break")


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
    assert flows.MOVED_TO_TERMINAL in fake.edits[-1][1]


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
