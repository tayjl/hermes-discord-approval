# Live test

Manual checks against a real Discord connection. The bullets below are copied verbatim from the former README "Live test" section.

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
