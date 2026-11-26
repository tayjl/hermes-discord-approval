"""Discord approval plugin: approvals and questions to your DM while away."""

from . import connection, flows


def register(ctx):
    ctx.register_command(
        "away", flows.make_command(ctx, "away", flows.AWAY_ON, flows.AWAY_OFF, flows.USAGE_AWAY),
        description="Send approvals and questions to Discord", args_hint="[on|off]",
    )
    ctx.register_command(
        "ping", flows.make_command(ctx, "ping", flows.PING_ON, flows.PING_OFF, flows.USAGE_PING),
        description="DM me after long turns", args_hint="[on|off]",
    )
    ctx.register_approval_transport("discord-away", flows.make_present(ctx))
    ctx.register_hook("pre_approval_request", flows.make_pre_approval_request(ctx))
    ctx.register_hook("post_approval_response", flows.post_approval_response)
    on_prompt, on_settled = flows.make_prompt_hooks(ctx)
    ctx.register_hook("on_session_prompt", on_prompt)
    ctx.register_hook("on_session_prompt_settled", on_settled)
    pre_turn, post_turn, turn_end = flows.make_ping_hooks(ctx)
    ctx.register_hook("pre_llm_call", pre_turn)
    ctx.register_hook("post_llm_call", post_turn)
    ctx.register_hook("on_session_end", turn_end)
    ctx.on_unload(connection.stop)
    if ctx.get_config("away", False) or ctx.get_config("ping", False):
        connection.ensure()
