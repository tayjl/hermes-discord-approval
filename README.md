# Hermes Discord Approval

A Hermes plugin that sends approval prompts and agent questions to your Discord DM while you are away. It can also DM you a short report after a long turn.

## What it does

- **Approvals.** With away on, a flagged command shows up as a DM card with Allow once, Allow for this session and Deny buttons. There is no "always" button. If you don't answer in time, the card says so and the command is denied. The plugin never removes a button Hermes allows. A card shows a warning line above the buttons when the command is cut short, has a secret hidden in it, has invisible or control characters in it, or (for a subagent) also changes a file the card can't show. More than one warning can show. A command is cut at a little over 1400 characters on a plain card. The most is 1500, and a long title or several warnings leave less. Control and invisible characters show as visible escapes such as `\r` or `\u202e`.
- **Questions.** With away on, the agent's clarify questions also show up as a DM. Tap a choice, pick from a menu, or tap Answer and type your own. Questions asked from the TUI, CLI or Desktop work too, and the terminal answer wins. When the agent asks several at once, the first card has the header and the session title, the next cards start with "(N of M)" and the question, and a closing line comes last as its own message. In the classic CLI, a question you answer in Discord stays on screen in the terminal. Once you answer a batch question in Discord, finish the batch in Discord. The terminal closes when the last one is answered. To use the terminal, answer there from the start, and the terminal answer wins.
- **Long-turn pings.** With ping on, a turn that takes 3 minutes or more sends you a DM with the outcome, how long it took and the first lines of the reply.
- **Commands.** `/away [on|off]` and `/ping [on|off]` work in the terminal, the dashboard and from your own Discord DM. With no argument they toggle. Turning away on also turns pings on. Each reply is a small panel showing away-mode and ping-mode. In the terminal a line below it says what changed. In Discord the panel is the whole reply.

It only works inside the gateway process. Anywhere else it does nothing and the normal prompt shows.

## Install

You run these yourself, in the default profile only. Do not enable the plugin in other profiles.

Set `DISCORD_BOT_TOKEN` and `DISCORD_ALLOWED_USERS` (exactly one Discord user id) in the default profile's environment first. Hermes disables the plugin if either is missing.

```
# junction so the repo is the plugin dir (cmd.exe)
mklink /J <hermes home>\plugins\discord-approval <path to this repo>
hermes plugins enable discord-approval
hermes config set security.approval.transport discord-away
hermes config set security.approval.transport_fallback builtin
hermes gateway restart
```

## Recovery

If approvals start getting denied or the plugin breaks:

```
hermes config set security.approval.transport builtin
```

Hermes reads this per request, so it takes effect without a restart. To remove the plugin fully, also run `hermes plugins disable discord-approval`.

Keep `transport_fallback` set to `builtin`. Without it, an approval the plugin can't handle is denied instead of shown in the terminal.

## Known gaps

- Approvals from profile-routed subagents (for example the orchestrator delegating to devops) get the same card. The buttons answer that exact request, and Hermes closes the card when it times out or is answered in the terminal. If the pending request can't be found, you get a DM with no buttons that says it needs approval in the terminal. In smart approval mode most subagent commands are decided without asking, so you will see few of these.
- Subagents can't ask questions: Hermes blocks clarify for them.
- Questions in a Discord session are not sent here. The gateway already shows them in the channel.
- In the TUI, a question answered in Discord still shows as open until the whole batch is done. Press Tab to move to the next question.
- Not covered: the action gate, MCP elicitation, and sudo and secret prompts.
- While away is off, every approval logs two warning lines. This is expected.
- It shares the gateway's bot token with its own client. That client asks for no gateway intents and backs off between retries, so it stays light. The gateway adapter has no `on_interaction`, so each click is handled once, here. Long uptimes and frequent gateway restarts are untested.
- A missing or wrong bot token is logged once and not retried. Approvals then go straight to the terminal until the gateway restarts.
- Logs carry exception types only, never message text. Each click on an approval card is logged as the first 8 characters of its request id and the choice.
- The Discord owner can answer any approval of this profile's sessions, including group-chat parent sessions.
- Batched questions and subagent approvals read a few Hermes internals (`gateway.session_server_requests`, the pending approval queue in `tools.approval`). If they change, the cards fall back to plain ones that say to answer in the terminal.

## Tested with

Hermes release 2026.9.24 (it reports version 0.0.0, so the manifest has no minimum version) and discord.py 2.7.1.

## Tests

```
<hermes venv python> tests/run.py
```

No real Discord connection is opened. Test files use pytest style, so `pytest` also works if you add it.

## Live test

Start in a classic CLI session on the gateway, with the DM with the bot open on your phone.

- **L1:** Type `/away`. You should see "Away is on. Approvals and questions go to your Discord DMs." Type `/away` again for "Away is off. Prompts go back to the terminal." Type `/away on`. In Discord DM the bot `/away off` from your account and you should see the panel with away-mode off. Check `gateway.log` for the plugin's "in gateway" line.
- **L2:** With away on, ask Hermes to run any command the guard flags, for example `git push --dry-run` in a git repo. A DM card appears and the CLI shows a spinner. Tap **Allow once**. The command runs and the card says "Allowed once."
- **L3:** Repeat L2 and tap **Deny**. Hermes reports the command was blocked.
- **L4:** Repeat L2 and don't tap. Before the approval window ends, the card says "No answer in time. Denied."
- **L5:** Repeat L2. While the card waits, type `/away off` in the CLI. Within about 1 second the card says "Away is off. This one moved to the terminal." and the normal CLI prompt appears. Answer it there.
- **L6:** With away on, ask "ask me which colour I prefer, red or blue, using clarify". The DM shows Red, Blue and Answer buttons. Tap Blue and the agent continues with blue. Repeat and tap **Answer**, type "green" in the box, and the agent gets "green". Repeat and answer in the CLI instead, and the card says "This request is no longer open."
- **L7 (subagent, the push case):** With away on, have the orchestrator delegate a flagged command to the devops profile. Expected: the CLI shows the normal approval card and one DM shows the same card with Allow once, Allow for this session and Deny. Tap **Allow once**. The command runs, the card says "Allowed once." and the CLI prompt closes. Repeat and answer in the CLI instead: the DM says "This request is no longer open." Repeat with a command longer than 1400 characters and the card shows a warning and keeps every button. Nothing is sent for an approval that moved to the terminal because of `/away off`.
- **L8:** Type `/ping on`, then ask for something that takes 3 minutes or more, for example "wait 200 seconds using the terminal, then say done". A DM arrives saying "**Done** after 3 min | <title>" with the first lines of the reply. A short turn sends nothing. Interrupt a long turn and the DM says "Stopped".
- **L9 (restart):** Leave an approval card open, run `hermes gateway restart`, then tap the old card. It loses its buttons and says "This request is no longer open." Discord may show "This interaction failed" for a click made before the plugin reconnects, which takes about 3 seconds after the gateway is up.
- **L10:** Type `/away off`, trigger L2 again, and the normal CLI prompt shows with no DM. Two warning lines in the log are expected.
- **L11 (recovery):** Run `hermes config set security.approval.transport builtin` and trigger L2. The normal prompt shows. Set it back to `discord-away`.
- **L12 (long command):** With away on, trigger a flagged command longer than 1400 characters. The card shows the start of it with "(cut short)", a warning line and every button.
- **L12b (secret in a command):** Trigger a flagged command that contains a token. The card hides the token, shows a warning that part of it is hidden and keeps every button.
- **L13 (Ink TUI):** With away on, ask "use clarify to ask me two questions at once: red or blue, and small or large". Three DMs arrive. The first has the header and "(1 of 2)", the second has only "(2 of 2)" and its question, and the third is only the closing line, below the buttons. Tap Blue on card 1 and it says "Answered: Blue" with no closing line added to the card. The TUI still shows question 1 open, which is expected. Answer question 2 in the TUI. The agent gets both answers and the TUI panel closes.
- **L14 (Ink TUI):** Answer question 1 in the TUI, then tap a choice on card 1 in Discord. It replies "Answered in the terminal." At settle the card switches to "Answered in the terminal."
- **L15 (Ink TUI):** Tap Blue, then answer Red for question 1 in the TUI and finish. The agent gets Red, and the card switches to "Answered in the terminal."
- **L16 (classic CLI):** Answer both questions in Discord. The CLI panel closes after the last answer, and the agent gets both answers.
- **L17 (expiry):** Run `hermes config set agent.clarify_timeout 30`, answer one question in Discord and wait. The open card says "This request is no longer open.", and the agent reports one answer. Reset the timeout, then repeat with Ctrl+C.
