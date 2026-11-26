import asyncio
import json
import threading
from types import SimpleNamespace

import hermes_constants
from gateway import session_server_requests as ssr
from gateway.session_authorities import owner_scope
from gateway.session_events import SessionEvents
from hda import flows
from hermes_constants import hermes_home_key
from test_flows import _setup, _wait


def _hooks(monkeypatch, away=True, terminal=False):
    ctx, fake, _ = _setup(monkeypatch, away=away)
    monkeypatch.setattr(flows, "_lookup_title", lambda session_id: "My session")
    monkeypatch.setattr(flows, "_batches", {})
    if not terminal:
        monkeypatch.setattr(ssr, "_open", object())  # no terminal record, so the plain cards run
    on_prompt, on_settled = flows.make_prompt_hooks(ctx)
    return fake, on_prompt, on_settled


def _batch(on_prompt, srq="srq-a", *questions):
    on_prompt(session_id="s1", kind="clarify", prompt_id=srq, params={"questions": list(questions)})


def _q(qid, question):
    return {"qid": qid, "question": question, "choices": ["Red", "Blue"]}


def test_two_questions_give_two_cards_in_order_with_a_counter_and_no_buttons(monkeypatch):
    fake, on_prompt, _ = _hooks(monkeypatch)
    _batch(on_prompt, "srq-a", _q("q0", "Red or blue?"), _q("q1", "Small or large?"))
    assert fake.sent == [
        (flows.framed(flows.QUESTION_CARD.format(title="My session", question="(1 of 2) Red or blue?"), flows.ANSWER_IN_TERMINAL), []),
        (flows.framed(flows.QUESTION_CARD.format(title="My session", question="(2 of 2) Small or large?"), flows.ANSWER_IN_TERMINAL), []),
    ]
    assert fake.selects == []
    assert set(flows._questions) == {"srq-a.q0", "srq-a.q1"}


def test_one_question_has_no_counter_and_non_dicts_are_skipped(monkeypatch):
    fake, on_prompt, _ = _hooks(monkeypatch)
    _batch(on_prompt, "srq-a", "junk", _q("q0", "Red or blue?"), None)
    assert fake.sent[0][0] == flows.framed(flows.QUESTION_CARD.format(title="My session", question="Red or blue?"), flows.ANSWER_IN_TERMINAL)
    assert len(fake.sent) == 1


def test_secrets_are_redacted_on_the_card(monkeypatch):
    fake, on_prompt, _ = _hooks(monkeypatch)
    secret = "sk-" + "a1B2c3D4" * 4
    _batch(on_prompt, "srq-a", _q("q0", f"key {secret}?"))
    assert secret not in fake.sent[0][0]


def test_settle_closes_every_card_of_the_batch_and_only_that_batch(monkeypatch):
    fake, on_prompt, on_settled = _hooks(monkeypatch)
    _batch(on_prompt, "srq-a", _q("q0", "one"), _q("q1", "two"))
    _batch(on_prompt, "srq-ab", _q("q0", "other"))
    on_settled(prompt_id="srq-a")
    assert [m for m, _ in fake.edits] == [1, 2]
    assert all(flows.CLOSED in text for _, text in fake.edits)
    assert set(flows._questions) == {"srq-ab.q0"}


def test_another_profiles_prompt_sends_nothing(monkeypatch, tmp_path):
    fake, on_prompt, _ = _hooks(monkeypatch)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path / "other")
    _batch(on_prompt, "srq-a", _q("q0", "one"))
    assert fake.sent == [] and flows._questions == {}


def test_away_off_sends_nothing(monkeypatch):
    fake, on_prompt, _ = _hooks(monkeypatch, away=False)
    _batch(on_prompt, "srq-a", _q("q0", "one"))
    assert fake.sent == []


def test_worst_case_card_with_status_fits_discord(monkeypatch):
    fake, on_prompt, _ = _hooks(monkeypatch)
    monkeypatch.setattr(flows, "_lookup_title", lambda session_id: "t" * 100)
    _batch(on_prompt, "srq-a", *[_q(f"q{n}", "q" * 5000) for n in range(5)])
    assert len(fake.sent) == 5
    assert all(len(text) < 2000 for text, _ in fake.sent)
    assert all(len(flows.with_status(text, flows.CLOSED)) < 2000 for text, _ in fake.sent)


# --- full mode: the terminal's own record, driven through the real session_server_requests ---

ALLOW = lambda *a: True  # the terminal's own lock, as the Ink panel makes it
QUESTIONS = [{"qid": "q0", "question": "Red or blue?", "choices": ["Red", "Blue"], "multi_select": False},
             {"qid": "q1", "question": "Small or large?", "choices": ["Small", "Large"], "multi_select": False}]


class _Terminal:
    def __init__(self, questions=QUESTIONS, timeout=10, hook_params=None):
        self.frames, self.out = [], {}
        frames = self.frames

        class Peer:
            def write(self, frame):
                frames.append(frame)
                return True

            def close(self):
                pass
        self.events = SessionEvents()
        self.events.fanout.attach(Peer())
        qids = [q["qid"] for q in questions]
        self.worker = threading.Thread(target=lambda: self.out.update(result=ssr.ask_viewer_result(
            self.events, "s1", "clarify", {"questions": questions}, timeout=timeout, qids=qids)))
        self.worker.start()
        _wait(lambda: self.frames)
        self.rid = self.frames[0]["id"]
        self.hook_params = hook_params or {"questions": questions}

    def cid(self, qid, what, kind="c"):
        return f"hda:{kind}:{self.rid}.{qid}:{what}"

    def result(self):
        self.worker.join(5)
        return self.out["result"]

    def end(self):
        """Withdraw and wait, so the asker thread is done before the next test patches the module."""
        ssr.withdraw(self.events, "s1", "clarify")
        self.worker.join(5)


def _full(monkeypatch, **kw):
    fake, on_prompt, on_settled = _hooks(monkeypatch, terminal=True)
    term = _Terminal(**kw)
    on_prompt(session_id="s1", kind="clarify", prompt_id=term.rid, params=term.hook_params)
    return fake, term, on_settled


def _status(calls):
    return calls[0][1]


def test_full_cards_have_real_controls_in_order(monkeypatch):
    fake, term, _ = _full(monkeypatch)
    assert [len(buttons) for _, buttons in fake.sent] == [3, 3]  # two choices and Answer
    assert "(1 of 2) Red or blue?" in fake.sent[0][0] and "(2 of 2) Small or large?" in fake.sent[1][0]
    assert fake.sent[0][1][1][1] == term.cid("q0", 1)
    assert set(flows._questions) == {f"{term.rid}.q0", f"{term.rid}.q1"}
    term.end()


def test_partial_then_full_lock_gives_the_waiter_both_answers(monkeypatch):
    fake, term, on_settled = _full(monkeypatch)
    assert flows.ANSWERED.format(answer="Blue") in _status(fake.click(term.cid("q0", 1)))
    assert term.out == {} and ssr._open[term.rid].locked == {"q0": "Blue"}
    assert flows.ANSWERED.format(answer="Small") in _status(fake.click(term.cid("q1", 0)))
    assert term.result() == {"answers": {"q0": "Blue", "q1": "Small"}, "outcome": "submitted"}
    on_settled(prompt_id=term.rid)
    assert fake.edits == [] and flows._batches == {} and flows._questions == {}


def test_terminal_lock_first_refuses_the_tap_and_keeps_its_value(monkeypatch):
    fake, term, on_settled = _full(monkeypatch)
    ssr.lock_answer(term.rid, "q0", "Red", session_ok=ALLOW)
    assert flows.ANSWERED_IN_TERMINAL in _status(fake.click(term.cid("q0", 1)))
    assert ssr._open[term.rid].locked == {"q0": "Red"}
    fake.click(term.cid("q1", 0))
    assert term.result()["answers"] == {"q0": "Red", "q1": "Small"}
    on_settled(prompt_id=term.rid)
    assert fake.edits == []


def test_terminal_relock_after_discord_switches_the_card_at_settle(monkeypatch):
    fake, term, on_settled = _full(monkeypatch)
    fake.click(term.cid("q0", 1))
    ssr.lock_answer(term.rid, "q0", "Red", session_ok=ALLOW)
    fake.click(term.cid("q1", 0))
    assert term.result()["answers"] == {"q0": "Red", "q1": "Small"}
    on_settled(prompt_id=term.rid)
    assert [(m, flows.ANSWERED_IN_TERMINAL in text) for m, text in fake.edits] == [(1, True)]


def test_classic_cli_frame_switches_both_cards(monkeypatch):
    fake, term, on_settled = _full(monkeypatch)
    fake.click(term.cid("q0", 1))
    assert ssr.resolve({"jsonrpc": "2.0", "id": term.rid, "result": {"answers": {"q0": "x", "q1": None}}})
    assert term.result()["answers"] == {"q0": "x", "q1": None}
    on_settled(prompt_id=term.rid)
    assert sorted(m for m, _ in fake.edits) == [1, 2]
    assert all(flows.ANSWERED_IN_TERMINAL in text for _, text in fake.edits)


def test_cancel_all_frame_closes_the_open_cards_and_keeps_the_discord_one(monkeypatch):
    fake, term, on_settled = _full(monkeypatch)
    fake.click(term.cid("q0", 1))
    assert ssr.resolve({"jsonrpc": "2.0", "id": term.rid, "result": {}})
    on_settled(prompt_id=term.rid)
    assert [(m, flows.CLOSED in text) for m, text in fake.edits] == [(2, True)]


def test_timeout_keeps_the_locks_and_closes_the_open_cards(monkeypatch):
    fake, term, on_settled = _full(monkeypatch, timeout=0.5)
    fake.click(term.cid("q0", 1))
    assert term.result() == {"answers": {"q0": "Blue"}, "outcome": "timed_out"}
    on_settled(prompt_id=term.rid)
    assert [(m, flows.CLOSED in text) for m, text in fake.edits] == [(2, True)]


def test_withdraw_gives_closed_only(monkeypatch):
    fake, term, on_settled = _full(monkeypatch)
    fake.click(term.cid("q0", 1))
    ssr.withdraw(term.events, "s1", "clarify")
    assert term.result() is None
    on_settled(prompt_id=term.rid)
    assert [(m, flows.CLOSED in text) for m, text in fake.edits] == [(2, True)]


def test_another_user_is_refused_and_nothing_locks(monkeypatch):
    fake, term, _ = _full(monkeypatch)
    assert fake.click(term.cid("q0", 1), user_id="999") == [("send_message", flows.NOT_YOURS, True)]
    assert ssr._open[term.rid].locked == {} and f"{term.rid}.q0" in flows._questions
    term.end()


def test_forged_ids_give_closed(monkeypatch):
    fake, term, _ = _full(monkeypatch)
    assert flows.CLOSED in _status(fake.click("hda:c:srq-forged.q0:0"))
    assert flows.CLOSED in _status(fake.click(f"hda:c:{term.rid}.zz:0"))
    assert ssr._open[term.rid].locked == {}
    term.end()


def test_a_batch_that_is_gone_gives_closed(monkeypatch):
    fake, term, _ = _full(monkeypatch)
    ssr.withdraw(term.events, "s1", "clarify")  # the card is still ours until the settled hook runs
    assert flows.CLOSED in _status(fake.click(term.cid("q0", 1)))


def test_secrets_are_redacted_on_the_card_and_the_locked_answer_is_exact(monkeypatch):
    secret = "sk-" + "a1B2c3D4" * 4
    real = [{"qid": "q0", "question": f"key {secret}?", "choices": [secret, "No"], "multi_select": False}]
    shown = [{**real[0], "question": "key [redacted]?", "choices": ["[redacted]", "No"]}]
    fake, term, _ = _full(monkeypatch, questions=real, hook_params={"questions": shown})
    assert secret not in fake.sent[0][0] and all(secret not in label for label, _, _ in fake.sent[0][1])
    fake.click(term.cid("q0", 0))
    assert term.result()["answers"] == {"q0": secret}


def test_multi_select_locks_a_json_list(monkeypatch):
    fake, term, _ = _full(monkeypatch, questions=[{**QUESTIONS[0], "multi_select": True}])
    assert len(fake.selects) == 1
    fake.select(term.cid("q0", "", kind="cs"), ["0", "1"])
    assert term.result()["answers"] == {"q0": '["Red", "Blue"]'}


def test_session_ok_runs_under_the_terminals_lock(monkeypatch):
    fake, term, _ = _full(monkeypatch)
    held, real = [], ssr.lock_answer

    def spy(request_id, question_id, answer, *, session_ok):
        return real(request_id, question_id, answer, session_ok=lambda sid, ev: held.append(ssr._lock.locked()) or session_ok(sid, ev))
    monkeypatch.setattr(ssr, "lock_answer", spy)
    fake.click(term.cid("q0", 1))
    assert held == [True]
    term.end()


def test_no_terminal_record_sends_nothing(monkeypatch):
    fake, on_prompt, _ = _hooks(monkeypatch, terminal=True)
    _batch(on_prompt, "srq-gone", _q("q0", "one"))
    assert fake.sent == [] and flows._questions == {}


def test_a_qid_with_a_colon_sends_nothing(monkeypatch):
    fake, term, _ = _full(monkeypatch, questions=[{**QUESTIONS[0], "qid": "q:0"}])
    assert fake.sent == [] and flows._questions == {}
    term.end()


def test_another_sessions_prompt_sends_nothing(monkeypatch):
    fake, on_prompt, _ = _hooks(monkeypatch, terminal=True)
    term = _Terminal()
    on_prompt(session_id="other", kind="clarify", prompt_id=term.rid, params=term.hook_params)
    assert fake.sent == [] and flows._questions == {}
    term.end()


def test_a_failed_send_forgets_every_card(monkeypatch):
    fake, on_prompt, _ = _hooks(monkeypatch, terminal=True)
    fake.ready_ok = False
    term = _Terminal()
    on_prompt(session_id="s1", kind="clarify", prompt_id=term.rid, params=term.hook_params)
    assert flows._questions == {} and flows._batches == {}
    term.end()


def test_a_changed_internal_api_falls_back_to_the_plain_cards(monkeypatch):
    fake, on_prompt, _ = _hooks(monkeypatch)  # ssr._open is no longer a mapping
    _batch(on_prompt, "srq-a", _q("q0", "one"))
    assert fake.sent[0][1] == [] and flows.ANSWER_IN_TERMINAL in fake.sent[0][0]


def test_the_profile_override_is_set_inside_owner_scope(monkeypatch, tmp_path):
    # Importing gateway.run relaunches the interpreter, so a stand-in with the same body (gateway/run.py _profile_runtime_scope).
    import contextlib
    import sys
    from agent.runtime_scope import bind_state, scope_for_profile

    @contextlib.contextmanager
    def scope(home, hydrate_secrets=True):
        tokens = bind_state(scope_for_profile(home), secrets=None, hydrate_secrets=False, terminal_policy=None)
        try:
            yield
        finally:
            tokens.reset()
    home = tmp_path / "other"
    home.mkdir()
    authority = SimpleNamespace(profile_id=str(home), runner=SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True)))
    before = flows._home()
    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(_profile_runtime_scope=scope))
    with owner_scope(authority):
        assert flows._home() == hermes_home_key(home) != before
    assert flows._home() == before


def test_a_card_still_being_sent_when_the_terminal_answers_ends_as_answered_in_the_terminal(monkeypatch):
    fake, _, _ = _hooks(monkeypatch)
    entry = flows.Entry("Question?", [])
    flows._settle(entry, None, flows.ANSWERED_IN_TERMINAL)  # no message yet, so nothing to edit
    assert fake.edits == []
    asyncio.run(flows._send_question(entry, [], None))
    assert fake.edits == [(1, flows.framed("Question?", flows.ANSWERED_IN_TERMINAL))]


def test_the_same_multi_select_answer_from_the_terminal_keeps_the_discord_card(monkeypatch):
    fake, term, on_settled = _full(monkeypatch)
    fake.click(term.cid("q0", 1))
    flows._batches[term.rid][1]["q0"].sent = json.dumps(["Red", "Blue"])  # as a select menu would lock it
    ssr.lock_answer(term.rid, "q0", '["Red","Blue"]', session_ok=ALLOW)  # same picks, the terminal's spacing
    fake.click(term.cid("q1", 0))
    on_settled(prompt_id=term.rid)
    assert fake.edits == []
