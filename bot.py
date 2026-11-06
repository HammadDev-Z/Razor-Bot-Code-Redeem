from __future__ import annotations

import contextlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from purchaser import Purchaser, blocked_reason, codes_messages, progress_text
from razer_session import PRODUCTS_BY_CODE, RazerSessionManager
from store import StateStore, parse_order


load_dotenv()
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
# HTTPX logs full Telegram request URLs, which contain the bot token. Never
# allow those request lines into console or redirected log files.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

EMAIL, PASSWORD, BACKUP_CODES, ADD_CODES = range(4)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CHECK_JOB_NAME = "purchase-check"
HELP_TEXT = (
    "/newaccount - save Razer email, password and backup codes\n"
    "/order 830x30 2320x20 - set what to buy, in that order (replaces the old order)\n"
    "/status - progress, codes left, next check\n"
    "/codes - the product codes bought so far for this order\n"
    "/checknow - check right now instead of waiting\n"
    "/addcodes - add more backup codes\n"
    "/resolve bought|notbought - settle an unconfirmed purchase\n"
    "/cancel - delete the account, order and saved login\n\n"
    "Product codes: " + ", ".join(PRODUCTS_BY_CODE)
)


def _store(context: ContextTypes.DEFAULT_TYPE) -> StateStore:
    return context.application.bot_data["store"]


def _purchaser(context: ContextTypes.DEFAULT_TYPE) -> Purchaser:
    return context.application.bot_data["purchaser"]


def _owner_ids() -> frozenset[int]:
    """Telegram user IDs allowed to use the bot: BOT_OWNER_ID, comma-separated."""
    parts = [part.strip() for part in os.getenv("BOT_OWNER_ID", "").split(",")]
    parts = [part for part in parts if part]
    if not parts or not all(part.isdigit() for part in parts):
        raise RuntimeError(
            "BOT_OWNER_ID must be set in .env to numeric Telegram user IDs, "
            "separated by commas (e.g. 111,222). The bot saves a Razer login "
            "and buys automatically, so it cannot be left open to everyone."
        )
    return frozenset(int(part) for part in parts)


async def _reject_if_needed(update: Update) -> bool:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None:
        return True
    if chat.type != "private":
        await message.reply_text("Use this bot only in a private chat.")
        return True
    if user.id not in _owner_ids():
        await message.reply_text("You are not authorized to use this bot.")
        return True
    return False


def _start_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.application.create_task(
        _purchaser(context).run_cycle(manual=True), name="manual-check"
    )


def _parse_backup_codes(value: str) -> list[str]:
    codes: list[str] = []
    for line in value.splitlines():
        compact = re.sub(r"[^A-Za-z0-9]", "", line)
        if len(compact) == 8:
            codes.append(compact.upper())
    if not codes:
        codes = [
            match.upper()
            for match in re.findall(
                r"(?<![A-Za-z0-9])[A-Za-z0-9]{8}(?![A-Za-z0-9])",
                value,
            )
        ]
    return list(dict.fromkeys(codes))


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_needed(update):
        return
    await update.effective_message.reply_text(HELP_TEXT)


# /newaccount conversation ------------------------------------------------


async def newaccount_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if await _reject_if_needed(update):
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text("Send your Razer account email:")
    return EMAIL


async def receive_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _reject_if_needed(update):
        return ConversationHandler.END
    email = (update.effective_message.text or "").strip()
    if not EMAIL_PATTERN.fullmatch(email):
        await update.effective_message.reply_text(
            "That email is not valid. Send the Razer account email again:"
        )
        return EMAIL
    context.user_data["razer_email"] = email
    await update.effective_message.reply_text(
        "Send your Razer password.\n"
        "For safety, the bot will immediately try to delete your password message."
    )
    return PASSWORD


async def receive_password(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if await _reject_if_needed(update):
        return ConversationHandler.END
    message = update.effective_message
    password = message.text or ""
    with contextlib.suppress(BadRequest, Forbidden):
        await message.delete()
    if not context.user_data.get("razer_email") or not password:
        context.user_data.clear()
        await update.effective_chat.send_message(
            "Email or password was missing. Use /newaccount to try again."
        )
        return ConversationHandler.END
    context.user_data["razer_password"] = password
    await update.effective_chat.send_message(
        "Send your Razer backup codes in one message, one code per line.\n"
        "Each purchase uses one code. The message will be deleted."
    )
    return BACKUP_CODES


async def receive_backup_codes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if await _reject_if_needed(update):
        return ConversationHandler.END
    message = update.effective_message
    codes = _parse_backup_codes(message.text or "")
    with contextlib.suppress(BadRequest, Forbidden):
        await message.delete()
    if not codes:
        await update.effective_chat.send_message(
            "I could not read any 8-character backup codes. Send them again, "
            "one per line:"
        )
        return BACKUP_CODES

    email = context.user_data.pop("razer_email", "")
    password = context.user_data.pop("razer_password", "")
    context.user_data.clear()
    store = _store(context)
    store.set_account(email, password, codes)
    text = f"Account saved with {len(codes)} backup codes."
    if store.state.order and not store.state.order_complete:
        text += "\nStarting a check now."
        _start_check(context)
    else:
        text += "\nNow set the order, e.g. /order 830x30 2320x20"
    await update.effective_chat.send_message(text)
    return ConversationHandler.END


# /addcodes conversation --------------------------------------------------


async def addcodes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _reject_if_needed(update):
        return ConversationHandler.END
    if not _store(context).state.has_account:
        await update.effective_message.reply_text("No account is set. Use /newaccount.")
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "Send the new backup codes in one message, one per line. "
        "The message will be deleted."
    )
    return ADD_CODES


async def receive_added_codes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if await _reject_if_needed(update):
        return ConversationHandler.END
    message = update.effective_message
    codes = _parse_backup_codes(message.text or "")
    with contextlib.suppress(BadRequest, Forbidden):
        await message.delete()
    if not codes:
        await update.effective_chat.send_message(
            "I could not read any 8-character backup codes. Send them again:"
        )
        return ADD_CODES
    store = _store(context)
    added = store.add_codes(codes)
    text = f"Added {added} codes. {len(store.state.backup_codes)} codes available."
    if blocked_reason(store.state) is None:
        text += "\nStarting a check now."
        _start_check(context)
    await update.effective_chat.send_message(text)
    return ConversationHandler.END


async def cancel_conversation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


# Plain commands ----------------------------------------------------------


async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_needed(update):
        return
    store = _store(context)
    text = " ".join(context.args or [])
    if not text:
        current = progress_text(store.state) or "No order is set."
        await update.effective_message.reply_text(
            f"{current}\n\nSet a new order like: /order 830x30 2320x20"
        )
        return
    if _purchaser(context).running:
        await update.effective_message.reply_text(
            "A check is running. Try again when it finishes (see /status)."
        )
        return
    try:
        items = parse_order(text)
    except ValueError as exc:
        await update.effective_message.reply_text(
            f"{exc}\nExample: /order 830x30 2320x20\n"
            f"Product codes: {', '.join(PRODUCTS_BY_CODE)}"
        )
        return
    # A new order cancels the previous one and its saved product codes. Hand
    # over any codes it collected first, so none are lost with it.
    previous = progress_text(store.state)
    for text in codes_messages(
        store.state, "Codes from the previous order (cancelled by the new order):"
    ):
        await update.effective_message.reply_text(text)
    store.set_order(items)
    reply = ""
    if previous:
        reply = "Previous order cancelled:\n" + previous + "\n\n"
    reply += "New order, bought in this order:\n" + progress_text(store.state)
    if blocked_reason(store.state) is None:
        reply += "\n\nStarting a check now."
        _start_check(context)
    elif not store.state.has_account:
        reply += "\n\nNow add the account with /newaccount."
    await update.effective_message.reply_text(reply)


async def codes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_needed(update):
        return
    messages = codes_messages(_store(context).state, "Codes bought for this order:")
    if not messages:
        await update.effective_message.reply_text("No product codes saved yet.")
        return
    for text in messages:
        await update.effective_message.reply_text(text)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_needed(update):
        return
    state = _store(context).state
    purchaser = _purchaser(context)
    lines = [f"Account: {state.email or 'none'}"]
    lines.append("Order:\n" + (progress_text(state) or "none"))
    lines.append(f"Backup codes left: {len(state.backup_codes)}")
    balance = "unknown" if state.last_balance is None else f"{state.last_balance:.2f}"
    lines.append(f"Razer Gold (last seen): {balance}")
    lines.append(f"Last check: {state.last_check_at or 'never'}")
    jobs = context.job_queue.get_jobs_by_name(CHECK_JOB_NAME) if context.job_queue else []
    if jobs and jobs[0].next_t:
        lines.append(f"Next check: {jobs[0].next_t.astimezone():%Y-%m-%d %H:%M %Z}")
    if purchaser.running:
        lines.append("State: a check is running now.")
    else:
        lines.append(f"State: {blocked_reason(state) or 'active'}")
    await update.effective_message.reply_text("\n".join(lines))


async def checknow_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if await _reject_if_needed(update):
        return
    if _purchaser(context).running:
        await update.effective_message.reply_text("A check is already running.")
        return
    store = _store(context)
    if store.state.paused:
        # Sending /checknow is how the user acknowledges a pause notice.
        store.state.paused = ""
        store.state.last_notice = ""
        store.save()
    await update.effective_message.reply_text("Checking now...")
    _start_check(context)


async def resolve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_needed(update):
        return
    store = _store(context)
    state = store.state
    if state.pending is None:
        await update.effective_message.reply_text("There is nothing to resolve.")
        return
    if _purchaser(context).running:
        await update.effective_message.reply_text(
            "A check is running. Try again when it finishes."
        )
        return
    answer = (context.args or [""])[0].casefold()
    if answer not in {"bought", "notbought"}:
        await update.effective_message.reply_text(
            "Send /resolve bought if the purchase is in your Razer transaction "
            "history, or /resolve notbought if it is not."
        )
        return
    if answer == "bought":
        item = state.find_item(state.pending.code)
        if item is not None and item.remaining:
            item.done += 1
    state.pending = None
    state.last_notice = ""
    store.save()
    reply = "Recorded.\n" + progress_text(state)
    if blocked_reason(state) is None:
        reply += "\n\nStarting a check now."
        _start_check(context)
    await update.effective_message.reply_text(reply)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_needed(update):
        return
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes, delete everything", callback_data="wipe:yes"),
                InlineKeyboardButton("No", callback_data="wipe:no"),
            ]
        ]
    )
    await update.effective_message.reply_text(
        "Delete the account, saved login, backup codes, order and progress?",
        reply_markup=keyboard,
    )


async def cancel_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if await _reject_if_needed(update):
        return
    if query.data != "wipe:yes":
        await query.edit_message_text("Nothing was deleted.")
        return
    running = _purchaser(context).running
    _store(context).clear()
    text = "Deleted the account, saved login, backup codes, order and progress."
    if running:
        text += (
            "\nA check was running; it stops after its current step and "
            "records nothing."
        )
    await query.edit_message_text(text)


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _purchaser(context).run_cycle()


async def post_init(application: Application) -> None:
    await application.bot_data["sessions"].start()


async def post_shutdown(application: Application) -> None:
    await application.bot_data["sessions"].stop()


def build_application() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing from .env")
    owner_ids = _owner_ids()
    interval_minutes = int(os.getenv("CHECK_INTERVAL_MINUTES", "20"))

    builder = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    proxy = os.getenv("TELEGRAM_PROXY_URL", "").strip()
    if proxy:
        builder = builder.proxy(proxy).get_updates_proxy(proxy)
    application = builder.build()
    if application.job_queue is None:
        raise RuntimeError(
            'Install the scheduler: pip install "python-telegram-bot[job-queue]"'
        )

    async def notify(text: str) -> None:
        # Everyone listed gets every message. One person who has not started
        # the bot yet (Telegram refuses) must not stop the others getting it.
        for owner_id in sorted(owner_ids):
            try:
                await application.bot.send_message(owner_id, text)
            except TelegramError:
                logger.exception("Could not send a notification to %s", owner_id)

    store = StateStore.from_env()
    sessions = RazerSessionManager()
    application.bot_data["store"] = store
    application.bot_data["sessions"] = sessions
    def restart_check_timer() -> None:
        """Next automatic check = one full interval after this check ended.

        Called after every check, so a /checknow (or any manual check) also
        pushes the timer back instead of letting it fire soon after.
        """
        assert application.job_queue is not None
        due = datetime.now(timezone.utc) + timedelta(minutes=interval_minutes)
        for job in application.job_queue.get_jobs_by_name(CHECK_JOB_NAME):
            job.job.modify(next_run_time=due)

    application.bot_data["purchaser"] = Purchaser(
        store,
        sessions,
        notify,
        interval_minutes,
        on_cycle_done=restart_check_timer,
        refresh_at=int(os.getenv("BACKUP_CODES_REFRESH_AT", "2")),
        notify_every_check=os.getenv("NOTIFY_EVERY_CHECK", "true").strip().lower()
        in {"1", "true", "yes", "on"},
    )

    cancel = CommandHandler("cancel", cancel_conversation)
    text_only = filters.TEXT & ~filters.COMMAND
    application.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("newaccount", newaccount_command),
                CommandHandler("addcodes", addcodes_command),
            ],
            states={
                EMAIL: [cancel, MessageHandler(text_only, receive_email)],
                PASSWORD: [cancel, MessageHandler(text_only, receive_password)],
                BACKUP_CODES: [cancel, MessageHandler(text_only, receive_backup_codes)],
                ADD_CODES: [cancel, MessageHandler(text_only, receive_added_codes)],
            },
            fallbacks=[cancel],
        )
    )
    # Outside a conversation, /cancel means "delete everything" (with a
    # confirmation button); inside one, the conversation handles it first.
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(cancel_confirmed, pattern=r"^wipe:"))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("order", order_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("codes", codes_command))
    application.add_handler(CommandHandler("checknow", checknow_command))
    application.add_handler(CommandHandler("resolve", resolve_command))

    application.job_queue.run_repeating(
        scheduled_check,
        interval=interval_minutes * 60,
        first=60,
        name=CHECK_JOB_NAME,
        job_kwargs={"max_instances": 1, "coalesce": True},
    )
    return application


def main() -> None:
    # Telegram can be temporarily unreachable on some host networks. Keep the
    # process alive and retry bootstrap instead of exiting on the first timeout.
    build_application().run_polling(
        drop_pending_updates=True,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()
