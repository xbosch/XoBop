from __future__ import annotations


def build_unknown_dispatch(*, command_dispatch: dict, ctx):
    return {k: (lambda u, fn=fn: fn(u, ctx)) for k, fn in command_dispatch.items()}


def build_media_processor(*, process_media_message_fn, state, artifact_dir: str, owner_only, reply, log):
    return lambda u: process_media_message_fn(
        update=u,
        state=state,
        artifact_dir=artifact_dir,
        owner_only=owner_only,
        reply=reply,
        log=log,
    )
