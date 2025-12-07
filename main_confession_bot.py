import json
import os
import logging
from functools import wraps
from datetime import datetime
from pytz import utc
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Load environment variables
load_dotenv()

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== Config =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@weirdo_confessions")
try:
    ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))
except Exception:
    ADMIN_GROUP_ID = -100
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8000))

DATA_FILE = "confessions_store.json"
ADMIN_ALIAS = "Admin"
MAX_BATCH_APPROVAL = 15

# ===== Persistent Storage =====
store = {"next_id": 1, "pending": {}, "posted": {}, "user_profiles": {}}

def load_store():
    global store
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                loaded_data = json.load(f)
                store.update(loaded_data)
        except json.JSONDecodeError:
            logger.warning("Failed to load data store. Starting fresh.")

def save_store():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save data store: {e}")

# ===== Access Control =====
def is_admin_chat(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != ADMIN_GROUP_ID:
            if update.message:
                await update.message.reply_text("🚫 Admins only.")
            return
        return await func(update, context)
    return wrapper

# ===== Helpers =====
def get_user_alias(user_id: int) -> str:
    return store["user_profiles"].get(str(user_id), "Anonymous")

def get_confession_text(conf_id: int) -> str:
    conf = store["posted"].get(str(conf_id))
    if not conf: return "⚠️ Confession not found."
    return f"*Confession #{conf_id}* (by {conf.get('user_alias','Anonymous')})\n\n{conf['text']}"

def get_confession_markup(conf_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("💬 Add Comment", callback_data=f"add_comment|{conf_id}"),
            InlineKeyboardButton("👁️ Browse Comments", callback_data=f"browse_comments|{conf_id}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def send_confession_options(update: Update, context: ContextTypes.DEFAULT_TYPE, conf_id: int):
    text = get_confession_text(conf_id)
    markup = get_confession_markup(conf_id)
    if update.callback_query and update.callback_query.message:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)

async def submit_pending_confession(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    conf_id = store['next_id']
    pending_id = f"p{conf_id}"
    user_id = update.effective_user.id
    user_alias = get_user_alias(user_id)
    store["next_id"] += 1
    store["pending"][pending_id] = {"id": conf_id, "text": text, "from_user": user_id, "user_alias": user_alias}
    save_store()

    keyboard = [[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve|{pending_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject|{pending_id}")
    ]]
    try:
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=f"🆕 *Pending Confession #{conf_id}* (ID: {pending_id} | Alias: {user_alias})\n\n{text}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        await update.message.reply_text("✅ Your confession has been submitted for admin review.")
    except Exception as e:
        logger.error(f"Failed to submit confession: {e}")
        await update.message.reply_text("⚠️ Failed to submit confession.")

# ===== Handlers =====
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == Chat.PRIVATE:
        alias = get_user_alias(update.effective_user.id)
        msg = f"👋 Welcome! Your alias: *{alias}*\nSend a message here to confess anonymously."
        await update.message.reply_text(msg, parse_mode="Markdown")

async def set_alias_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setalias <nickname>")
        return
    alias = " ".join(context.args).strip()
    store["user_profiles"][str(update.effective_user.id)] = alias
    save_store()
    await update.message.reply_text(f"✅ Alias set to *{alias}*", parse_mode="Markdown")

async def confess_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📝 Send your anonymous confession now.")

async def handle_confession(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text: return
    await submit_pending_confession(update, context, text)

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")

    if data[0] == "add_comment":
        conf_id = int(data[1])
        context.user_data[update.effective_user.id] = {"state": "awaiting_reply", "conf_id": conf_id}
        await query.edit_message_text(f"Send your comment for Confession #{conf_id}")
        return

    # Admin approve/reject
    if query.message.chat.id != ADMIN_GROUP_ID: return
    if data[0] == "approve":
        pending = store["pending"].pop(data[1], None)
        if pending:
            conf_id = pending["id"]
            store["posted"][str(conf_id)] = {"text": pending["text"], "user_alias": pending["user_alias"], "replies": [], "channel_message_id": None}
            save_store()
            bot_username = (await context.bot.get_me()).username
            keyboard = [[InlineKeyboardButton("💬 Add/View Comments", url=f"https://t.me/{bot_username}?start=comment_{conf_id}")]]
            sent = await context.bot.send_message(CHANNEL_ID, f"*#{conf_id} Confession* - {pending['text']}", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
            store["posted"][str(conf_id)]["channel_message_id"] = sent.message_id
            save_store()
            await query.edit_message_text(f"✅ Approved and posted as #{conf_id}")

# ===== Admin Commands Example =====
@is_admin_chat
async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not store["pending"]:
        await update.message.reply_text("✅ No pending confessions.")
        return
    msg = ""
    for p in store["pending"].values():
        msg += f"ID {p['id']} | {p['text'][:30]}...\n"
    await update.message.reply_text(msg)

# ===== Webhook Setup =====
async def set_bot_commands(application: Application):
    commands = [
        BotCommand("start", "Welcome message"),
        BotCommand("confess", "Send anonymous confession"),
        BotCommand("setalias", "Set alias"),
        BotCommand("pending", "List pending confessions")
    ]
    await application.bot.set_my_commands(commands)

def main():
    load_store()
    app = Application.builder().token(BOT_TOKEN).post_init(set_bot_commands).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("confess", confess_command))
    app.add_handler(CommandHandler("setalias", set_alias_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_confession))
    app.add_handler(CallbackQueryHandler(handle_callbacks))

    if WEBHOOK_URL:
        path = "/webhook/" + BOT_TOKEN
        app.run_webhook(listen="0.0.0.0", port=PORT, urlpath=path, webhook_url=WEBHOOK_URL + path)
        logger.info(f"Webhook running at {WEBHOOK_URL + path}")
    else:
        app.run_polling()
        logger.warning("Webhook URL not set. Running in polling mode.")

if __name__ == "__main__":
    main()
