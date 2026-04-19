from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from telegram.constants import ChatAction

from .emailer import smtp_send_email
from .intents import refers_to_last_attachment
from .router import classify, is_multiline_conversation
from .core_tool_executor import call_tool_skill
from .services import run_direct_shell, run_download_count, run_execute_last, run_git_commit_push, run_shell_with_autovenv
from .workflows import handle_attachment_arrival, handle_email_text


class _StateLike(Protocol):
    last_attachment_by_chat: dict[int, str]
    pending_action_by_chat: dict[int, dict]
    last_email_status_by_chat: dict[int, str]
    last_exec_by_chat: dict[int, str]


class _LogLike(Protocol):
    def warning(self, msg: str, *args, **kwargs) -> None: ...
    def exception(self, msg: str, *args, **kwargs) -> None: ...


CommandHandler = Callable[[Any], Awaitable[None]]
CommandRegistry = Mapping[str, tuple[CommandHandler, str | None, str | None]]
ReplyFn = Callable[[Any, str], Awaitable[None]]
CallSkillFn = Callable[[int, str, str, str, dict], Awaitable[str]]
AnalyzeAttachmentFn = Callable[[int, str, str], Awaitable[str]]
OpEventFn = Callable[..., None]


@dataclass
class ProcessTextDeps:
    state: _StateLike
    reply: ReplyFn
    call_skill: CallSkillFn
    mode_getter: Callable[[], str]
    list_cron_jobs: Callable[[], str]
    run_agent: Callable[[int, str], Awaitable[str]]
    owner_only: Callable[[Any], bool]
    panic_locked: Callable[[], bool]
    panic_bypass_text: Callable[[str], bool]
    command_aliases_getter: Callable[[], dict[str, str]]
    analyze_attachment: AnalyzeAttachmentFn
    op_event: OpEventFn
    log: _LogLike
    command_registry: CommandRegistry | None = None


def _looks_like_pipeline_contract_request(text: str) -> bool:
    t = (text or "")
    must = [
        "Final output ONLY",
        "RUN_STATUS=",
        "RAW_ROWS=",
        "CLEAN_ROWS=",
        "REJECTED_ROWS=",
        "OUTLIER_ROWS=",
        "SCHEMA_VALID=",
        "CONSISTENCY=",
        "RECOVERY_OK=",
        "PROOF_FILES_OK=",
        "OUTPUT_DIR=",
        "ERROR=",
    ]
    return all(k in t for k in must)


def _has_pipeline_kv_response(text: str) -> bool:
    t = (text or "")
    return all(k in t for k in ["RUN_STATUS=", "RAW_ROWS=", "CLEAN_ROWS=", "REJECTED_ROWS=", "OUTLIER_ROWS=", "SCHEMA_VALID=", "CONSISTENCY=", "RECOVERY_OK=", "PROOF_FILES_OK=", "OUTPUT_DIR=", "ERROR="])


def _pipeline_contract_fail_response(output_dir: str = "~/agent-test/stress-lab", reason: str = "execution_contract_not_satisfied") -> str:
    return "\n".join([
        "RUN_STATUS=FAIL",
        "RAW_ROWS=0",
        "CLEAN_ROWS=0",
        "REJECTED_ROWS=0",
        "OUTLIER_ROWS=0",
        "SCHEMA_VALID=0",
        "CONSISTENCY=DRIFT",
        "RECOVERY_OK=0",
        "PROOF_FILES_OK=0",
        f"OUTPUT_DIR={output_dir}",
        f"ERROR={reason}",
    ])


def _extract_generated_artifact_paths(text: str) -> list[Path]:
    if not text:
        return []

    cwd = Path.cwd().resolve()
    allowed_suffixes = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".zip"}

    def _normalize(raw: str) -> Path | None:
        token = (raw or "").strip().strip('"').strip("'")
        if not token:
            return None
        p = Path(token)
        if not p.is_absolute():
            p = (cwd / p).resolve()
        else:
            p = p.resolve()
        if not p.exists() or not p.is_file():
            return None
        if p.suffix.lower() not in allowed_suffixes:
            return None
        try:
            p.relative_to(cwd)
        except Exception:
            return None
        return p

    found: list[Path] = []
    # machine-readable keys first
    for m in re.finditer(r"(?im)^(?:PLOT_PATH|PLOTPATH|ZIP_PATH|ARTIFACT_PATH|FILE_PATH)\s*=\s*(.+)$", text):
        p = _normalize(m.group(1))
        if p and p not in found:
            found.append(p)

    # generic fallback: any in-repo .zip path mentioned in text
    for m in re.finditer(r"(?i)([\w./-]+\.zip)\b", text):
        p = _normalize(m.group(1))
        if p and p not in found:
            found.append(p)

    return found


async def _maybe_send_generated_artifacts(update, result_text: str, log, *, user_text: str = "") -> None:
    try:
        if not getattr(update, "message", None):
            return

        artifacts = _extract_generated_artifact_paths(result_text)

        # Deterministic fallback: if user asked to send/share here and result mentions a zip
        # but no concrete path was extracted, pick newest zip in cwd.
        ul = (user_text or "").lower()
        wants_send_here = (("send" in ul) or ("share" in ul) or ("upload" in ul)) and (("here" in ul) or ("telegram" in ul) or ("chat" in ul))
        if not artifacts and wants_send_here and ".zip" in (result_text or "").lower():
            zips = sorted(Path.cwd().glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
            if zips:
                artifacts = [zips[0].resolve()]

        # Contract: zip delivery requires explicit in-chat send/share intent.
        if artifacts and not wants_send_here:
            artifacts = [p for p in artifacts if p.suffix.lower() != ".zip"]

        for p in artifacts[:3]:
            caption = f"Generated plot: {p}" if p.suffix.lower() in {'.png', '.jpg', '.jpeg', '.svg', '.webp', '.gif'} else f"Generated artifact: {p}"
            with p.open("rb") as fh:
                await update.message.reply_document(document=fh, filename=p.name, caption=caption)
    except Exception as exc:
        log.warning("Failed to send generated artifact: %s", exc)


def _is_ambiguous_confirmation(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"do it", "you do it", "do", "it", "yes", "yes!", "ok", "okay", "sure"}


def resolve_command_alias(raw_text: str, aliases: dict[str, str]) -> tuple[str, bool, str | None]:
    text = (raw_text or "").strip()
    if not text.startswith("/"):
        return raw_text, False, None
    parts = text.split(maxsplit=1)
    cmd = parts[0][1:].strip().lower()
    rest = parts[1] if len(parts) > 1 else ""
    target = (aliases or {}).get(cmd)
    if not target:
        return raw_text, False, None
    resolved = str(target).strip().lower()
    rewritten = f"/{resolved}" + (f" {rest}" if rest else "")
    return rewritten, True, resolved


async def _dispatch_command_if_any(*, update, text: str, chat_id: int, state, reply, call_skill, mode_getter, op_event, aliases: dict, registry: dict, log) -> bool:
    lower = text.lower()
    if lower.startswith("/exec "):
        op_event("route_selected", chat_id=chat_id, route="cmd_exec")
        cmd = text[6:].strip()
        if not cmd:
            await reply(update, "Use: `/exec <command>`")
            return True
        state.last_exec_by_chat[chat_id] = cmd
        await update.message.chat.send_action(ChatAction.TYPING)
        result = await run_shell_with_autovenv(
            user_text=text,
            command=cmd,
            chat_id=chat_id,
            call_skill=call_skill,
            mode_getter=mode_getter,
        )
        await reply(update, result)
        await _maybe_send_generated_artifacts(update, result, log, user_text=text)
        return True

    cmd_token = lower.split()[0] if lower.startswith("/") else ""
    if cmd_token in registry:
        handler, event_name, route_name = registry[cmd_token]
        if event_name and route_name:
            op_event(event_name, chat_id=chat_id, route=route_name)
        await handler(update)
        return True

    if text.startswith("/"):
        cmd = text.split()[0]
        key = cmd.lstrip("/").lower()
        sug = aliases.get(key)
        op_event("unknown_command", chat_id=chat_id, command=cmd, status="error", metadata={"suggested": sug or ""})
        if sug:
            await reply(update, f"⛔ Unknown command: `{cmd}`\nDid you mean `/{sug}` ?")
        else:
            await reply(update, f"⛔ Unknown command: `{cmd}`\nUse `/help`.")
        return True

    return False


async def _dispatch_workflow_if_any(*, update, text: str, chat_id: int, state, reply) -> bool:
    wf = handle_email_text(text, chat_id, state)
    if not wf.get("handled"):
        return False

    if wf.get("reply"):
        await reply(update, wf["reply"])
        return True

    if wf.get("send_now"):
        try:
            sent = smtp_send_email(
                os.getenv("GMAIL_EMAIL") or os.getenv("GMAIL_USER") or "",
                os.getenv("GMAIL_APP_PASSWORD") or "",
                wf["to"], wf["subject"], wf["body"], wf.get("attachment"),
            )
            state.last_email_status_by_chat[chat_id] = sent
            state.last_email_payload_by_chat[chat_id] = {
                "to": wf["to"],
                "subject": wf["subject"],
                "body": wf["body"],
                "attachment": wf.get("attachment"),
            }
            await reply(update, sent)
        except Exception as exc:
            state.last_email_status_by_chat[chat_id] = f"EMAIL_FAILED {str(exc)[:180]}"
            await reply(update, f"⚠️ Email send failed: {str(exc)[:180]}")
        return True

    return False


async def _dispatch_intent_if_any(*, update, text: str, chat_id: int, state, reply, run_agent, list_cron_jobs, call_skill, mode_getter, op_event, log) -> bool:
    intent = classify(text)
    intent_name = intent["name"]
    op_event("route_selected", chat_id=chat_id, route=f"intent:{intent_name}")

    async def _handle_cron_list() -> None:
        await reply(update, list_cron_jobs())

    async def _handle_download_count() -> None:
        await update.message.chat.send_action(ChatAction.TYPING)
        await run_download_count(lambda m: reply(update, m))

    async def _handle_git_commit_push() -> None:
        await update.message.chat.send_action(ChatAction.TYPING)
        await run_git_commit_push(text=text, chat_id=chat_id, state=state, call_skill=call_skill, mode_getter=mode_getter, reply=lambda m: reply(update, m))

    async def _handle_lan_clients() -> None:
        await update.message.chat.send_action(ChatAction.TYPING)
        candidates = [
            (Path.cwd().parent / "network-audits" / "latest_devices.json").resolve(),
            (Path.cwd().parent / "network-audits" / "latest_report.txt").resolve(),
        ]

        json_path = candidates[0]
        if json_path.exists():
            try:
                import json

                data = json.loads(json_path.read_text(encoding="utf-8", errors="ignore"))
                rows = data if isinstance(data, list) else data.get("devices", []) if isinstance(data, dict) else []
                out = []
                for d in rows:
                    if not isinstance(d, dict):
                        continue
                    ip = str(d.get("ip") or d.get("ip_address") or d.get("ipv4") or "").strip()
                    host = str(d.get("hostname") or d.get("name") or "").strip() or "-"
                    mac = str(d.get("mac") or d.get("mac_address") or "").strip() or "-"
                    if ip and not ip.startswith("127."):
                        out.append(f"{ip} | {host} | {mac}")
                if out:
                    await reply(update, "LAN clients (IP | hostname | MAC):\n" + "\n".join(out[:100]))
                    return
            except Exception:
                pass

        txt_path = candidates[1]
        if txt_path.exists():
            raw = txt_path.read_text(encoding="utf-8", errors="ignore")
            await reply(update, "LAN report (latest):\n" + raw[:3500])
            return

        await reply(update, "No LAN audit artifacts found. Run a network audit first, then ask again.")

    async def _handle_web_search() -> None:
        await update.message.chat.send_action(ChatAction.TYPING)
        result = await run_agent(chat_id, text)
        await reply(update, result)

    async def _handle_execute_last() -> None:
        await update.message.chat.send_action(ChatAction.TYPING)
        await run_execute_last(text=text, chat_id=chat_id, state=state, call_skill=call_skill, mode_getter=mode_getter, reply=lambda m: reply(update, m))

    async def _handle_direct_shell() -> None:
        await update.message.chat.send_action(ChatAction.TYPING)
        await run_direct_shell(text=text, chat_id=chat_id, state=state, call_skill=call_skill, mode_getter=mode_getter, reply=lambda m: reply(update, m))

    intent_registry: dict[str, tuple[Callable[[], Awaitable[None]], Callable[[], bool] | None]] = {
        "cron_list": (_handle_cron_list, None),
        "download_count": (_handle_download_count, None),
        "lan_clients": (_handle_lan_clients, None),
        "web_search": (_handle_web_search, None),
        "git_commit_push": (_handle_git_commit_push, lambda: chat_id in state.last_exec_by_chat),
        "execute_last": (_handle_execute_last, lambda: chat_id in state.last_exec_by_chat),
        "direct_shell": (_handle_direct_shell, None),
    }

    entry = intent_registry.get(intent_name)
    if entry:
        handler, guard = entry
        if guard is None or bool(guard()):
            await handler()
            return True

    await update.message.chat.send_action(ChatAction.TYPING)

    if _looks_like_pipeline_contract_request(text):
        # For strict pipeline contract prompts, force an execution attempt first.
        forced = f"{text}\n\nExecution policy: You MUST execute. Do not return templates/placeholders. Return exact KV only."
        result = await run_agent(chat_id, forced)
        if not _has_pipeline_kv_response(result):
            result = _pipeline_contract_fail_response(reason="execution_contract_not_satisfied")
    else:
        result = await run_agent(chat_id, text)

    await reply(update, result)
    await _maybe_send_generated_artifacts(update, result, log, user_text=text)
    return True


async def process_media_message(*, update, state, artifact_dir: str, owner_only, reply, log) -> bool:
    if not owner_only(update):
        return True
    msg = update.message
    if not msg:
        return True
    chat_id = update.effective_chat.id

    media_obj = None
    suffix = ""
    if msg.photo:
        media_obj = msg.photo[-1]
        suffix = ".jpg"
    elif msg.document:
        media_obj = msg.document
        name = msg.document.file_name or ""
        suffix = Path(name).suffix or ""
    if not media_obj:
        return False

    try:
        tg_file = await media_obj.get_file()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        inbox = Path(artifact_dir) / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        local_path = inbox / f"chat{chat_id}_{ts}{suffix}"
        await tg_file.download_to_drive(custom_path=str(local_path))
        state.last_attachment_by_chat[chat_id] = str(local_path)

        req = handle_attachment_arrival(chat_id, str(local_path), state)
        if req:
            try:
                sent = smtp_send_email(
                    os.getenv("GMAIL_EMAIL") or os.getenv("GMAIL_USER") or "",
                    os.getenv("GMAIL_APP_PASSWORD") or "",
                    req["to"], req["subject"], req["body"], req["attachment"],
                )
                state.last_email_status_by_chat[chat_id] = sent
                state.last_email_payload_by_chat[chat_id] = {"to": req["to"], "subject": req["subject"], "body": req["body"], "attachment": req["attachment"]}
                await reply(update, f"Attachment stored: {local_path}\n{sent}")
            except Exception as exc:
                state.last_email_status_by_chat[chat_id] = f"EMAIL_FAILED {str(exc)[:180]}"
                await reply(update, f"Attachment stored: {local_path}\n⚠️ Email send failed: {str(exc)[:180]}")
            return True

        await reply(update, f"Attachment stored: {local_path}")
        return True
    except Exception as exc:
        log.exception("Failed to download attachment")
        await reply(update, f"⚠️ Could not store attachment: {str(exc)[:200]}")
        return True


async def process_text_message(
    *,
    update,
    text: str,
    chat_id: int,
    deps: ProcessTextDeps,
) -> bool:
    state = deps.state
    reply = deps.reply
    call_skill = deps.call_skill
    mode_getter = deps.mode_getter
    list_cron_jobs = deps.list_cron_jobs
    run_agent = deps.run_agent
    command_aliases_getter = deps.command_aliases_getter
    analyze_attachment = deps.analyze_attachment
    op_event = deps.op_event
    log = deps.log

    if not deps.owner_only(update):
        return True

    if deps.panic_locked() and not deps.panic_bypass_text(text):
        deps.op_event("route_selected", chat_id=chat_id, route="panic_block", status="blocked")
        await deps.reply(update, "⛔ Panic-lock active. Allowed commands: /panicstatus, /panicunlock <code>, /status, /health, /checkup, /help")
        return True

    if refers_to_last_attachment(text):
        if chat_id in state.last_attachment_by_chat:
            attachment_path = state.last_attachment_by_chat[chat_id]
            attachment_context = ""
            if callable(analyze_attachment):
                try:
                    attachment_context = (await analyze_attachment(chat_id, attachment_path, text)) or ""
                except Exception:
                    attachment_context = ""
            if attachment_context:
                text = f"{text}\n\nAttachment context (auto):\n{attachment_context}"
            else:
                text = f"{text}\n\nUse this local attachment path: {attachment_path}"
        elif chat_id in state.pending_action_by_chat:
            await reply(update, "Got it — please send the actual photo/file attachment now.")
            return True

    aliases = command_aliases_getter() or {}
    text, alias_changed, resolved_command = resolve_command_alias(text, aliases)
    lower = text.lower()
    if alias_changed:
        op_event("command_alias_applied", chat_id=chat_id, command=resolved_command or "", metadata={"rewritten": text})

    # Guard generic confirmations: without explicit workflow context, ask for clarification
    # instead of allowing latent side-effects via generic agent interpretation.
    if _is_ambiguous_confirmation(text):
        await reply(update, "What should I do exactly? Please specify the action.")
        return True

    # Keep long pasted prompts in conversation lane (avoid accidental side-effect workflows).
    # Alias rewrite is intentionally done first so multiline alias commands still route correctly.
    if is_multiline_conversation(text):
        await update.message.chat.send_action(ChatAction.TYPING)
        result = await run_agent(chat_id, text)
        await reply(update, result)
        return True

    if await _dispatch_workflow_if_any(
        update=update,
        text=text,
        chat_id=chat_id,
        state=state,
        reply=reply,
    ):
        return True

    registry = deps.command_registry or {}
    if await _dispatch_command_if_any(
        update=update,
        text=text,
        chat_id=chat_id,
        state=state,
        reply=reply,
        call_skill=call_skill,
        mode_getter=mode_getter,
        op_event=op_event,
        aliases=aliases,
        registry=registry,
        log=log,
    ):
        return True

    return await _dispatch_intent_if_any(
        update=update,
        text=text,
        chat_id=chat_id,
        state=state,
        reply=reply,
        run_agent=run_agent,
        list_cron_jobs=list_cron_jobs,
        call_skill=call_skill,
        mode_getter=mode_getter,
        op_event=op_event,
        log=log,
    )
