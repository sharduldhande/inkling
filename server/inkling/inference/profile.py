"""Route each turn to the adapter its user's inference_profile names (D7).

The profile lives in the database (`users.inference_profile`, §4.1) and points
at filesystem credential locations — never secret values (§2). It is read in
its own short transaction at turn time, then the transaction closes before the
model is invoked (§5.3): profile changes take effect on the next turn with no
restart, and no DB connection is held during inference.
"""

import logging

from inkling.db.client import Database
from inkling.inference.gateway import (
    InferenceError,
    OnTextDelta,
    ToolSchema,
    TurnResult,
)

log = logging.getLogger(__name__)


class ProfileRoutingGateway:
    def __init__(self, db: Database):
        self.db = db

    async def run_turn(
        self,
        *,
        user_id: str,
        system: str,
        context: str,
        tools: list[ToolSchema],
        on_text_delta: OnTextDelta,
    ) -> TurnResult:
        async with self.db.with_tenant(user_id) as conn:
            cur = await conn.execute(
                "SELECT inference_profile FROM users WHERE id = %s", (user_id,)
            )
            row = await cur.fetchone()
        if row is None:
            raise InferenceError(f"no users row for {user_id!r}", retryable=False)

        profile = row["inference_profile"] or {}
        kind = profile.get("type")
        model = profile.get("model")  # optional per-user override

        if kind == "subscription":
            from inkling.inference.anthropic_sdk import AnthropicSDKGateway

            adapter = AnthropicSDKGateway(profile["config_dir"], model=model)
        elif kind == "api_key":
            from inkling.inference.anthropic_api import AnthropicAPIGateway

            if model:
                adapter = AnthropicAPIGateway(profile["key_file"], model=model)
            else:
                adapter = AnthropicAPIGateway(profile["key_file"])
        elif kind == "codex_exec":
            from inkling.inference.codex_exec import CodexExecGateway

            adapter = CodexExecGateway(profile["profile_dir"], model=model)
        else:
            raise InferenceError(
                f"unknown inference profile type {kind!r} for {user_id!r}",
                retryable=False,
            )

        return await adapter.run_turn(
            user_id=user_id,
            system=system,
            context=context,
            tools=tools,
            on_text_delta=on_text_delta,
        )
