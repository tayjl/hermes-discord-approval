import contextlib
import threading
import time

from fakes import FakeCtx
from hda import connection, flows
from test_flows import _setup

SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"


def _clarify(monkeypatch, question="Which colour?", choices=("Red", "Blue"), multi=False, away=True):
    """A real clarify_gateway entry with its waiter on a thread, plus the prompt hooks."""
    from tools import clarify_gateway
    ctx, fake, _ = _setup(monkeypatch, away=away)
    prompt_id = f"p{time.monotonic_ns()}"
    clarify_gateway.register(prompt_id, "session-a", question, list(choices), multi, authority={})
    got = []
    waiter = threading.Thread(target=lambda: got.append(clarify_gateway.wait_for_response(prompt_id, 5)))
    waiter.start()
    on_prompt, on_settled = flows.make_prompt_hooks(ctx)
    monkeypatch.setattr(flows, "_lookup_title", lambda session_id: "My session")
    on_prompt(session_id="s1", kind="clarify", prompt_id=prompt_id)

    def answer():
        waiter.join(5)
        return got[0] if got else None

    return prompt_id, fake, answer, on_settled


def test_clarify_choices_give_buttons_and_a_click_answers_the_waiter(monkeypatch):
    prompt_id, fake, answer, _ = _clarify(monkeypatch)
    text, buttons = fake.sent[0]
    assert text == flows.QUESTION_CARD.format(title="My session", question="Which colour?")
    assert [(label, cid) for label, cid, _ in buttons] == [
        ("Red", f"hda:c:{prompt_id}:0"), ("Blue", f"hda:c:{prompt_id}:1"), (flows.ANSWER_BUTTON, f"hda:c:{prompt_id}:answer"),
    ]
    assert fake.click(f"hda:c:{prompt_id}:1", content="card") == [
        ("edit_message", "card\n\n" + flows.ANSWERED.format(answer="Blue"), None)]
    assert answer() == "Blue"
    assert fake.click(f"hda:c:{prompt_id}:0", content="card")[0][1].endswith(flows.CLOSED)  # second click


def test_clarify_forged_choice_index_is_closed(monkeypatch):
    prompt_id, fake, answer, _ = _clarify(monkeypatch)
    assert fake.click(f"hda:c:{prompt_id}:9")[0][1].endswith(flows.CLOSED)
    assert fake.click(f"hda:c:{prompt_id}:-1")[0][1].endswith(flows.CLOSED)
    fake.click(f"hda:c:{prompt_id}:0")
    assert answer() == "Red"


def test_clarify_multi_select_menu_sends_a_json_list(monkeypatch):
    prompt_id, fake, answer, _ = _clarify(monkeypatch, choices=("a", "b", "c"), multi=True)
    assert [label for label, _, _ in fake.sent[0][1]] == [flows.ANSWER_BUTTON]
    assert fake.selects == [(flows.SELECT_HINT, f"hda:cs:{prompt_id}", [("a", "0"), ("b", "1"), ("c", "2")])]
    assert fake.select(f"hda:cs:{prompt_id}", ["7"])[0][1].endswith(flows.CLOSED)  # not an option
    assert fake.select(f"hda:cs:{prompt_id}", [])[0][1].endswith(flows.CLOSED)
    calls = fake.select(f"hda:cs:{prompt_id}", ["0", "2"], content="card")
    assert calls == [("edit_message", "card\n\n" + flows.ANSWERED.format(answer='["a", "c"]'), None)]
    assert answer() == '["a", "c"]'


def test_clarify_open_question_opens_a_modal_and_passes_text_through(monkeypatch):
    prompt_id, fake, answer, _ = _clarify(monkeypatch, choices=())
    assert [label for label, _, _ in fake.sent[0][1]] == [flows.ANSWER_BUTTON]
    assert fake.click(f"hda:c:{prompt_id}:answer") == [("send_modal", f"hda:cm:{prompt_id}", flows.MODAL_TITLE)]
    text = "  green  " + "g" * 300
    calls = fake.submit_modal(f"hda:cm:{prompt_id}", text, content="card")
    assert calls == [("edit_message", "card\n\n" + flows.ANSWERED.format(answer=text.strip()[:200]), None)]
    assert answer() == text.strip()  # the agent gets it whole, trimmed


def test_clarify_blank_modal_answer_leaves_the_card_open(monkeypatch):
    prompt_id, fake, answer, _ = _clarify(monkeypatch, choices=())
    assert fake.submit_modal(f"hda:cm:{prompt_id}", " \n ") == [("send_message", flows.EMPTY_ANSWER, True)]
    assert flows._open_entry(prompt_id) is not None
    fake.submit_modal(f"hda:cm:{prompt_id}", "ok")
    assert answer() == "ok"


def test_clarify_too_many_or_empty_labels_fall_back_to_answer_only(monkeypatch):
    for choices in ([str(n) for n in range(25)], ["Red", "  "]):
        for multi in (False, True):
            prompt_id, fake, answer, _ = _clarify(monkeypatch, choices=choices, multi=multi)
            assert [label for label, _, _ in fake.sent[-1][1]] == [flows.ANSWER_BUTTON]
            assert fake.selects == []
            fake.submit_modal(f"hda:cm:{prompt_id}", "free text")
            assert answer() == "free text"


def test_clarify_non_ascii_digit_index_is_closed_not_raised(monkeypatch):
    prompt_id, fake, answer, _ = _clarify(monkeypatch)
    assert fake.click(f"hda:c:{prompt_id}:²")[0][1].endswith(flows.CLOSED)
    assert fake.select(f"hda:cs:{prompt_id}", ["²"])[0][1].endswith(flows.CLOSED)
    fake.click(f"hda:c:{prompt_id}:0")


def test_clarify_resolver_false_closes_the_card(monkeypatch):
    from tools import clarify_gateway
    prompt_id, fake, answer, _ = _clarify(monkeypatch)
    assert clarify_gateway.resolve_gateway_clarify(prompt_id, "answered in the CLI")
    assert fake.click(f"hda:c:{prompt_id}:0", content="card") == [("edit_message", "card\n\n" + flows.CLOSED, None)]
    assert fake.submit_modal(f"hda:cm:{prompt_id}", "late", content="card")[0][1].endswith(flows.CLOSED)
    assert answer() == "answered in the CLI"


def test_clarify_resolver_returning_false_is_closed_not_answered(monkeypatch):
    from tools import clarify_gateway
    prompt_id, fake, answer, _ = _clarify(monkeypatch)
    real = clarify_gateway.resolve_gateway_clarify
    monkeypatch.setattr(clarify_gateway, "resolve_gateway_clarify", lambda *a: False)
    assert fake.click(f"hda:c:{prompt_id}:0", content="card") == [("edit_message", "card\n\n" + flows.CLOSED, None)]
    real(prompt_id, "x")


def test_clarify_ignored_when_away_off_or_wrong_kind_or_unknown(monkeypatch):
    _, fake, _, _ = _clarify(monkeypatch, away=False)
    assert fake.sent == []
    prompt_id, fake, answer, _ = _clarify(monkeypatch)
    fake.sent.clear()
    on_prompt, _ = flows.make_prompt_hooks(FakeCtx({"away": True}))
    on_prompt(session_id="s1", kind="sudo", prompt_id=prompt_id)
    on_prompt(session_id="s1", kind="clarify", prompt_id="unknown")
    assert fake.sent == []
    fake.click(f"hda:c:{prompt_id}:0")


def test_clarify_not_in_gateway_sends_nothing(monkeypatch):
    from tools import clarify_gateway
    ctx, fake, _ = _setup(monkeypatch, gateway=False)
    clarify_gateway.register("pg", "session-a", "Q?", ["x"], authority={})
    try:
        flows.make_prompt_hooks(ctx)[0](session_id="s1", kind="clarify", prompt_id="pg")
    finally:
        clarify_gateway._entries.pop("pg", None)
    assert fake.sent == []


def test_clarify_settled_closes_an_open_card_once(monkeypatch):
    from tools import clarify_gateway
    prompt_id, fake, answer, on_settled = _clarify(monkeypatch)
    on_settled(prompt_id=prompt_id, reason="timeout")
    assert fake.edits == [(1, fake.sent[0][0] + "\n\n" + flows.CLOSED)]
    assert fake.click(f"hda:c:{prompt_id}:0", content="card")[0][1].endswith(flows.CLOSED)
    on_settled(prompt_id=prompt_id, reason="timeout")
    assert len(fake.edits) == 1
    clarify_gateway.resolve_gateway_clarify(prompt_id, "x")


def test_clarify_settled_after_an_answer_edits_nothing(monkeypatch):
    prompt_id, fake, answer, on_settled = _clarify(monkeypatch)
    fake.click(f"hda:c:{prompt_id}:0")
    on_settled(prompt_id=prompt_id, reason="answered")
    assert fake.edits == [] and answer() == "Red"


def test_clarify_other_user_cannot_answer(monkeypatch):
    prompt_id, fake, answer, _ = _clarify(monkeypatch)
    assert fake.click(f"hda:c:{prompt_id}:0", user_id="999") == [("send_message", flows.NOT_YOURS, True)]
    fake.click(f"hda:c:{prompt_id}:1")
    assert answer() == "Blue"


def test_clarify_card_redacts_question_and_labels_but_answers_exactly(monkeypatch):
    prompt_id, fake, answer, _ = _clarify(monkeypatch, question=f"Use key {SECRET}?", choices=(f"key {SECRET}", "no"))
    text, buttons = fake.sent[0]
    assert SECRET not in text and all(SECRET not in label for label, _, _ in buttons)
    fake.click(f"hda:c:{prompt_id}:0")
    assert answer() == f"key {SECRET}"  # the agent gets its own label back


def test_clarify_long_question_and_labels_are_clipped(monkeypatch):
    prompt_id, fake, answer, _ = _clarify(monkeypatch, question="q" * 5000, choices=("L" * 300,))
    text, buttons = fake.sent[0]
    assert len(text) < 2000 and len(buttons[0][0]) == flows.LABEL_CHARS
    fake.click(f"hda:c:{prompt_id}:0")


def test_clarify_send_failure_leaves_nothing_open(monkeypatch):
    from tools import clarify_gateway
    ctx, fake, _ = _setup(monkeypatch)
    clarify_gateway.register("pf", "session-a", "Q?", ["x"], authority={})

    def refuse(coro):
        coro.close()
        raise connection.PassThrough("discord not started")

    monkeypatch.setattr(connection, "submit", refuse)
    try:
        flows.make_prompt_hooks(ctx)[0](session_id="s1", kind="clarify", prompt_id="pf")
    finally:
        clarify_gateway._entries.pop("pf", None)
    assert flows._questions == {}


# Ping

def _ping(monkeypatch, ping=True, gateway=True):
    ctx, fake, _ = _setup(monkeypatch, gateway=gateway)
    ctx.config["ping"] = ping
    now = [1000.0]
    monkeypatch.setattr(flows, "monotonic", lambda: now[0])
    monkeypatch.setattr(flows, "_lookup_title", lambda session_id: f"Title {session_id}")
    return fake, now, flows.make_ping_hooks(ctx)


def _turn(hooks, now, seconds, reply="All done.", turn="t1", parent="", completed=True, failed=False, interrupted=False):
    pre, post, finish = hooks
    pre(session_id="s1", turn_id=turn, parent_session_id=parent)
    post(turn_id=turn, assistant_response=reply)
    now[0] += seconds
    finish(session_id="s1", turn_id=turn, completed=completed, failed=failed, interrupted=interrupted)


def test_ping_after_181_seconds_has_outcome_duration_title_and_quoted_lines(monkeypatch):
    fake, now, hooks = _ping(monkeypatch)
    _turn(hooks, now, 181, "one\n\n  two  \nthree\nfour\nfive\nsix\nseven")
    assert fake.sent == [(flows.PING.format(outcome="Done", duration="3 min", title="Title s1")
                          + "\n> one\n> two\n> three\n> four\n> five", [])]
    assert flows._starts == {} and flows._replies == {}


def test_ping_sends_nothing_below_the_limit_when_off_or_outside_the_gateway(monkeypatch):
    fake, now, hooks = _ping(monkeypatch)
    _turn(hooks, now, 179)
    assert fake.sent == [] and flows._starts == {}
    fake, now, hooks = _ping(monkeypatch, ping=False)
    _turn(hooks, now, 500)
    assert fake.sent == [] and flows._starts == {} and flows._replies == {}
    fake, now, hooks = _ping(monkeypatch, gateway=False)
    _turn(hooks, now, 500)
    assert fake.sent == []


def test_ping_never_tracks_child_turns(monkeypatch):
    fake, now, hooks = _ping(monkeypatch)
    _turn(hooks, now, 500, parent="s0")
    assert flows._starts == {} and flows._replies == {} and fake.sent == []


def test_ping_outcome_prefers_interrupted_then_failed(monkeypatch):
    fake, now, hooks = _ping(monkeypatch)
    _turn(hooks, now, 200, turn="a", completed=False, failed=True, interrupted=True)
    _turn(hooks, now, 200, turn="b", completed=False, failed=True)
    _turn(hooks, now, 200, turn="c")
    assert [text.split("**")[1] for text, _ in fake.sent] == ["Stopped", "Failed", "Done"]


def test_ping_duration_format_and_missing_reply(monkeypatch):
    assert flows._duration(180) == "3 min" and flows._duration(3599) == "59 min"
    assert flows._duration(3600) == "1 h 0 min" and flows._duration(3900) == "1 h 5 min"
    fake, now, hooks = _ping(monkeypatch)
    _turn(hooks, now, 200, reply=None)
    assert fake.sent == [(flows.PING.format(outcome="Done", duration="3 min", title="Title s1"), [])]


def test_ping_excerpt_is_capped(monkeypatch):
    fake, now, hooks = _ping(monkeypatch)
    _turn(hooks, now, 200, "word " * 400)
    quoted = fake.sent[0][0].split("\n", 1)[1]
    assert quoted.startswith("> word") and len(quoted) <= flows.REPLY_CHARS + 2


def test_ping_drops_turns_older_than_a_day_on_the_next_turn(monkeypatch):
    fake, now, (pre, post, _) = _ping(monkeypatch)
    pre(session_id="s1", turn_id="old", parent_session_id="")
    post(turn_id="old", assistant_response="x")
    now[0] += flows.MAX_AGE_S + 1
    pre(session_id="s1", turn_id="new", parent_session_id="")
    assert list(flows._starts) == ["new"] and flows._replies == {}


def test_secrets_are_redacted_in_the_title_and_the_ping_excerpt(monkeypatch):
    import persistence_assembly

    class Sessions:
        def get_title(self, session_id):
            return f"Deploy with {SECRET}"

    @contextlib.contextmanager
    def reader(home):
        yield type("P", (), {"sessions": Sessions()})()

    monkeypatch.setattr(persistence_assembly, "open_persistence_reader", reader)
    title = flows._lookup_title("s1")
    assert SECRET not in title and title.startswith("Deploy with sk-")
    fake, now, hooks = _ping(monkeypatch)
    monkeypatch.setattr(flows, "_lookup_title", lambda session_id: title)
    _turn(hooks, now, 200, f"key is {SECRET}")
    text = fake.sent[0][0]
    assert SECRET not in text and "> key is sk-" in text
