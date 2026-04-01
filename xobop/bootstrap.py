from __future__ import annotations

from telegram.ext import Application, CommandHandler, MessageHandler, filters


def build_app(*, token: str, post_init, post_shutdown) -> Application:
    return (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )


def register_handlers(*, app: Application, commands: dict[str, object], cmd_unknown, handle_media_message, handle_message, on_error) -> None:
    command_order = [
        "remember", "delfact", "facts", "memories", "memadd", "memdel",
        "addtask", "donetask", "tasks", "mode", "promptshow", "promptset",
        "promptproposal", "promptapprove", "promptreject", "quiethours", "quietoff",
        "quietflush", "status", "health", "checkup", "state_check", "state_repair",
        "op_sanity", "diagnose", "improve_propose", "improve_list", "improve_compile",
        "improve_artifacts", "improve_simulate", "improve_runs", "improve_compare",
        "improve_policy", "improve_promote", "improve_autopromote", "improve_funnel",
        "improve_eval_report", "improve_weekly_report", "improve_phase", "improve_log_rollback",
        "evolve_capture", "evolve_memory", "plan", "go", "topskills", "exec",
        "panicstatus", "paniclock", "panicunlock", "skills", "skillon", "skilloff",
        "clearctx", "help", "cronadd", "cronrm", "cronlist", "cronon", "cronoff",
        "cronrun", "cronruns",
    ]

    for name in command_order:
        app.add_handler(CommandHandler(name, commands[name]))

    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, handle_media_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(on_error)
