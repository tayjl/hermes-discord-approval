# Hermes Discord Approval

Approve commands and answer your agent's questions from your phone.

When you step away, this plugin sends approval prompts and agent questions to your Discord DM. It can also DM you after long turns.

## Quick setup

Paste this prompt into Hermes to begin setup:

```text
Set up the discord-approval plugin for me. Do this in the default profile only.

1. Clone https://github.com/tayjl/hermes-discord-approval and note the local path.
2. Link the repo into the default profile's plugins dir as "discord-approval".
   On Windows, use a junction: mklink /J <hermes home>\plugins\discord-approval <repo path>
   Elsewhere, use a symlink.
3. Check that DISCORD_BOT_TOKEN and DISCORD_ALLOWED_USERS are set in the default profile's env.
   DISCORD_ALLOWED_USERS must be exactly one Discord user id. If either is missing, ask me for it.
4. Run: hermes plugins enable discord-approval
5. Run: hermes config set security.approval.transport discord-away
6. Run: hermes config set security.approval.transport_fallback builtin
7. Run: hermes gateway restart
8. Tell me to type /away in a terminal session to try it.
```

### Manual install

Run these in the default profile only. Set `DISCORD_BOT_TOKEN` and `DISCORD_ALLOWED_USERS` (exactly one Discord user id) first. Hermes disables the plugin if either is missing.

```
mklink /J <hermes home>\plugins\discord-approval <path to this repo>
hermes plugins enable discord-approval
hermes config set security.approval.transport discord-away
hermes config set security.approval.transport_fallback builtin
hermes gateway restart
```

Do not enable the plugin in other profiles.

## What you get

- **Approvals.** With away on, a flagged command shows a DM card with Allow once, Allow for this session and Deny. There is no "always" button. An unanswered card is denied after the timeout. The card also warns you if a command was cut short or has hidden secrets or odd characters.
- **Questions.** With away on, clarify questions also arrive as a DM. Tap a choice, pick from a menu, or tap Answer and type your own. Questions from the TUI, CLI or Desktop work too. If you answer in both places, the terminal answer wins.
- **Long-turn pings.** With ping on, a turn that takes 3 minutes or more sends you a DM with the outcome, the duration and the first lines of the reply.
- **Commands.** `/away [on|off]` and `/ping [on|off]` work in the terminal, the dashboard and your Discord DM. With no argument, they toggle. Turning away on also turns ping on.

The plugin only runs inside the gateway process. Anywhere else, the normal prompt shows.

## If something breaks

Switch approvals back to the terminal. No restart is needed:

```
hermes config set security.approval.transport builtin
```

To remove the plugin fully, also run `hermes plugins disable discord-approval`. Keep `transport_fallback` set to `builtin`. Without it, an approval the plugin can't handle is denied instead of shown in the terminal.

## Limitations

- Subagents can't ask questions. Hermes blocks clarify for them.
- Questions in a Discord session stay in the channel. They are not sent to your DM.
- The TUI keeps a batched question open until the whole batch is done. Press Tab to move on.
- Not covered: the action gate, MCP elicitation, and sudo or secret prompts.
- Subagent approvals (for example, from the devops profile) use the same card. If the request can't be found, you get a DM with no buttons that says to answer in the terminal.
- The plugin relies on a few Hermes internals. If they change, cards fall back to plain ones that say to answer in the terminal.
- A missing or wrong bot token is logged once and not retried. Approvals go to the terminal until the gateway restarts.
- Whoever owns the Discord account can answer any approval from this profile's sessions, including group-chat parent sessions.

## Development

Tested with Hermes 2026.9.24 and discord.py 2.7.1.

Run the tests with the Hermes venv Python. No real Discord connection is opened:

```
<hermes venv python> tests/run.py
```

The manual live-test checklist is in [docs/live-test.md](docs/live-test.md).
