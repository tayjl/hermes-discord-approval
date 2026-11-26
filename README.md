# Discord approval

A Hermes plugin that sends approval prompts and agent questions to your Discord DM while you are away. It can also DM you a short report after a long turn.

## What it does

- **Approvals.** With away on, a flagged command shows up as a DM card with Allow once, Allow for this session and Deny buttons. There is no "always" button. If you don't answer in time, the card says so and the command is denied. A command too long to show in full can only be denied.
- **Questions.** With away on, the agent's clarify questions also show up as a DM. Tap a choice, pick from a menu, or tap Answer and type your own.
- **Long-turn pings.** With ping on, a turn that takes 3 minutes or more sends you a DM with the outcome, how long it took and the first lines of the reply.
- **Commands.** `/away [on|off]` and `/ping [on|off]` work in the terminal, the dashboard and from your own Discord DM. With no argument they toggle.

It only works inside the gateway process. Anywhere else it does nothing and the normal prompt shows.

## Install

You run these yourself, in the default profile only. Do not enable the plugin in other profiles.

Set `DISCORD_BOT_TOKEN` and `DISCORD_ALLOWED_USERS` (exactly one Discord user id) in the default profile's environment first. Hermes disables the plugin if either is missing.

```
# junction so the repo is the plugin dir (cmd.exe)
mklink /J C:\Users\tayle\AppData\Local\hermes\plugins\discord-approval C:\Development\hermes-discord-approval
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

- Approvals from profile-routed subagents (for example the orchestrator delegating to devops) don't get buttons. You get a DM saying the session needs approval in the terminal.
- Questions in a Discord session are not sent here. The gateway already shows them in the channel.
- Not covered: the action gate, MCP elicitation, and sudo and secret prompts.
- While away is off, every approval logs two warning lines. This is expected.
- It shares the gateway's bot token. Long uptimes and frequent gateway restarts are untested.

## Tests

```
"C:/Users/tayle/AppData/Local/hermes/installs/b00dc2f46aaceabe/environments/521c54cb71104fa5b8bc69b525c91501/venv/Scripts/python.exe" tests/run.py
```

No real Discord connection is opened. Test files use pytest style, so `pytest` also works if you add it.

## Live test

Start in a classic CLI session on the gateway, with the DM with the bot open on your phone.

- **L1:** Type `/away`. You should see "Away is on. Approvals and questions go to your Discord DMs." Type `/away` again for "Away is off. Prompts go back to the terminal." Type `/away on`. In Discord, DM the bot `/away off` from your account and you should see the off message. Check `gateway.log` for the plugin's "in gateway" line.
- **L2:** With away on, ask Hermes to run any command the guard flags, for example `git push --dry-run` in C:/Development/hermes-discord-approval. A DM card appears and the CLI shows a spinner. Tap **Allow once**. The command runs and the card says "Allowed once."
- **L3:** Repeat L2 and tap **Deny**. Hermes reports the command was blocked.
- **L4:** Repeat L2 and don't tap. Before the approval window ends, the card says "No answer in time. Denied."
- **L5:** Repeat L2. While the card waits, type `/away off` in the CLI. Within about 1 second the card says "Away is off. This one moved to the terminal." and the normal CLI prompt appears. Answer it there.
- **L6:** With away on, ask "ask me which colour I prefer, red or blue, using clarify". The DM shows Red, Blue and Answer buttons. Tap Blue and the agent continues with blue. Repeat and tap **Answer**, type "green" in the box, and the agent gets "green". Repeat and answer in the CLI instead, and the card says "This request is no longer open."
- **L7 (subagent, the push case):** With away on, have the orchestrator delegate a flagged command to the devops profile. Expected: the CLI shows the normal approval card and the DM says "<title> needs approval in the terminal." with no buttons. Note what actually happens. If the DM card has buttons instead, the plain child path was taken, so tap Allow once.
- **L8:** Type `/ping on`, then ask for something that takes 3 minutes or more, for example "wait 200 seconds using the terminal, then say done". A DM arrives saying "**Done** after 3 min: <title>" with the first lines of the reply. A short turn sends nothing. Interrupt a long turn and the DM says "Stopped".
- **L9 (restart):** Leave an approval card open, run `hermes gateway restart`, then tap the old card. It loses its buttons and says "This request is no longer open." Discord may show "This interaction failed" for a click made before the plugin reconnects, which takes about 3 seconds after the gateway is up.
- **L10:** Type `/away off`, trigger L2 again, and the normal CLI prompt shows with no DM. Two warning lines in the log are expected.
- **L11 (recovery):** Run `hermes config set security.approval.transport builtin` and trigger L2. The normal prompt shows. Set it back to `discord-away`.
- **L12 (long command):** With away on, trigger a flagged command longer than 1500 characters. The card shows the start of it with "(cut short)" and only a Deny button.
