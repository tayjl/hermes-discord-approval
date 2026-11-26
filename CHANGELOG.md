# Changelog

## Unreleased

- New `channel` setting: post cards and pings to a server channel instead of your DM, with an @mention of you (first message of a batch only). Empty keeps the DM. Falls back to the DM, with one warning, if the channel can't be used. Only you can be pinged. A batch never splits between channel and DM. Settling a card by click keeps the mention and still blocks @everyone and roles. Cards keep 24 characters free for the mention.

## 1.0.0

- Branded Hermes Discord Approval everywhere. Command replies use the same header as approval cards.
- Batched questions: header on the first card, only "(N of M) question" on the rest, and a closing line comes last as its own message. Status edits keep this layout.
- A routed subagent's approval now gets a real card with buttons that answer that exact request, not a notice. It follows the same rules as other cards (never "always", only what the request allows, a warning line for a clipped or redacted command or an edit). It is sent once, only for the profile that loaded the plugin, never for an approval handed back to the terminal, and Hermes closes it when the prompt ends. If the pending request can't be found, the old notice with no buttons is sent.
- Hermes shows invisible and control characters in a command as escapes such as `\r`, `\x1b` or `\u202e` and names them in the card's Why line. The plugin escapes and warns about any raw ones it still gets. Windows line endings count as plain newlines. A command that holds a secret shows without escapes. Descriptions lose control characters and backtick runs.
- A first DM that fails to send no longer leaves its question or approval registered.
- A corrected bot token replaces a failed login when the plugin is reloaded, without a gateway restart.
- Commands are redacted before they reach Discord. A redacted or clipped command gets a warning line and keeps every button Hermes allows. A command is cut at a little over 1400 characters on a plain card. The most is 1500, and a long title or several warnings leave less. The plugin never removes an approval option on its own.
- Session titles lose newlines, escape codes and backtick runs. Titles still show up to 100 characters and descriptions up to 200, as before.
- A custom id longer than Discord allows, or with a ":" in its key, falls back to a plain card.
- The answer box is read from the raw interaction payload, not private discord.py API.
- A failing `lock_answer` closes the card instead of dropping the click. Settling tolerates changed Hermes internals.
- A failed card edit after a click falls back to a plain edit so the buttons go away.
- Approvals no longer stay in the pending table when away turns off mid-wait.
- Open questions older than the age limit are closed at the next turn.
- A missing or wrong bot token is logged once, not retried, and approvals pass to the terminal at once.
- Version 1.0.0, MIT license.
