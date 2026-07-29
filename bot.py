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
logger = logging.getLogger(__name__)

EMAIL, PASSWORD = range(2)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
    email = context.user_data.pop("razer_email", "")
    context.user_data.clear()

    with contextlib.suppress(BadRequest, Forbidden):
        await message.delete()

    if not email or not password:
        await update.effective_chat.send_message(
            "Email or password was missing. Use /login to try again."
        )
        return ConversationHandler.END

    status = await update.effective_chat.send_message("Logging in to Razer ID...")
    try:
        await _sessions(context).login(
            update.effective_user.id,
            email,
            password,
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
            "Razer login successful. The authenticated session is ready."
        )
    finally:
        password = ""
    return ConversationHandler.END


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
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(login_conversation)
    application.add_handler(CommandHandler("logout", logout_command))
    return application


def main() -> None:
    build_application().run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

