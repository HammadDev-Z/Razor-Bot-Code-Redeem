from __future__ import annotations

import contextlib
import logging
import os
import re

from dotenv import load_dotenv
from telegram import Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from razer_session import (
    InvalidCredentialsError,
    RazerLoginError,
    RazerSessionManager,
    VerificationRequiredError,
)


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

EMAIL, PASSWORD, BACKUP_CODES, PRODUCT_CODE = range(4)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PRODUCTS_BY_CODE = {
    "830": "Yalla Ludo - USD 2 Diamonds",
    "2320": "Yalla Ludo - USD 5 Diamonds",
    "5k": "Yalla Ludo - USD 10 Diamonds",
    "13k": "Yalla Ludo - USD 25 Diamonds",
    "27k": "Yalla Ludo - USD 50 Diamonds",
    "56k": "Yalla Ludo - USD 100 Diamonds",
}


def _sessions(context: ContextTypes.DEFAULT_TYPE) -> RazerSessionManager:
    return context.application.bot_data["razer_sessions"]


async def _reject_if_needed(update: Update) -> bool:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None:
        return True
    if chat.type != "private":
        await message.reply_text("Use this command only in a private chat.")
        return True
    owner_value = os.getenv("BOT_OWNER_ID", "").strip()
    if owner_value and owner_value.isdigit() and user.id != int(owner_value):
        await message.reply_text("You are not authorized to use this bot.")
        return True
    return False


async def start_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if await _reject_if_needed(update):
        return
    await update.effective_message.reply_text(
        "Use /login to sign in to Razer ID.\n"
        "Use /logout to close the active Razer session."
    )


async def login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if await _reject_if_needed(update):
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text("Send your Razer account email:")
    return EMAIL


async def receive_email(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
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
            "Email or password was missing. Use /login to try again."
        )
        return ConversationHandler.END

    context.user_data["razer_password"] = password
    password = ""
    await update.effective_chat.send_message(
        "Send all 10 Razer backup codes in one message, one code per line.\n"
        "Only the first code will be used. The message will be deleted."
    )
    return BACKUP_CODES


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
    return codes


async def receive_backup_codes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if await _reject_if_needed(update):
        return ConversationHandler.END
    message = update.effective_message
    codes = _parse_backup_codes(message.text or "")
    with contextlib.suppress(BadRequest, Forbidden):
        await message.delete()
    if len(codes) < 10:
        await update.effective_chat.send_message(
            "I could not read 10 backup codes. Send all 10 again, one per line."
        )
        return BACKUP_CODES

    email = context.user_data.pop("razer_email", "")
    password = context.user_data.pop("razer_password", "")
    context.user_data.clear()
    first_backup_code = codes[0]
    codes.clear()
    status = await update.effective_chat.send_message("Logging in to Razer ID...")
    next_state = ConversationHandler.END
    try:
        await _sessions(context).login(
            update.effective_user.id,
            email,
            password,
            first_backup_code,
        )
    except InvalidCredentialsError:
        await status.edit_text("Login failed: incorrect email or password.")
    except VerificationRequiredError as exc:
        await status.edit_text(f"Login needs verification: {exc}")
    except RazerLoginError as exc:
        await status.edit_text(f"Login failed: {exc}")
    except Exception:
        logger.exception("Unexpected Razer login failure")
        await status.edit_text("Login failed because of an unexpected browser error.")
    else:
        await status.edit_text(
            "Razer login successful. The Yalla Ludo product menu is ready.\n\n"
            "Send a product code:\n"
            "830, 2320, 5k, 13k, 27k, or 56k"
        )
        next_state = PRODUCT_CODE
    finally:
        password = ""
        first_backup_code = ""
    return next_state


async def receive_product_code(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if await _reject_if_needed(update):
        return ConversationHandler.END
    normalized = re.sub(
        r"\s+",
        "",
        (update.effective_message.text or "").casefold(),
    )
    product_name = PRODUCTS_BY_CODE.get(normalized)
    if product_name is None:
        await update.effective_message.reply_text("Unknown product code.")
        return PRODUCT_CODE

    try:
        await _sessions(context).select_product(
            update.effective_user.id,
            product_name,
        )
    except RazerLoginError as exc:
        await update.effective_message.reply_text(f"Selection failed: {exc}")
        return PRODUCT_CODE
    await update.effective_message.reply_text(f"Selected: {product_name}.")
    return PRODUCT_CODE


async def logout_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if await _reject_if_needed(update):
        return
    closed = await _sessions(context).logout(update.effective_user.id)
    await update.effective_message.reply_text(
        "Razer session closed." if closed else "There is no active Razer session."
    )


async def cancel_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text("Login cancelled.")
    return ConversationHandler.END


async def post_init(application: Application) -> None:
    await application.bot_data["razer_sessions"].start()


async def post_shutdown(application: Application) -> None:
    await application.bot_data["razer_sessions"].stop()


def build_application() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing from .env")

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
    application.bot_data["razer_sessions"] = RazerSessionManager()

    login_conversation = ConversationHandler(
        entry_points=[CommandHandler("login", login_command)],
        states={
            EMAIL: [
                CommandHandler("cancel", cancel_command),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_email),
            ],
            PASSWORD: [
                CommandHandler("cancel", cancel_command),
                MessageHandler(filters.TEXT, receive_password),
            ],
            BACKUP_CODES: [
                CommandHandler("cancel", cancel_command),
                MessageHandler(filters.TEXT, receive_backup_codes),
            ],
            PRODUCT_CODE: [
                CommandHandler("cancel", cancel_command),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_product_code),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(login_conversation)
    application.add_handler(CommandHandler("logout", logout_command))
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
