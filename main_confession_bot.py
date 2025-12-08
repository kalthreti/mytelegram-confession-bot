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

# Load environment variables (useful for local testing)
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

# --- PERSISTENCE FIX ---
# Set the base path for persistent data storage. 
# We default to /data, which MUST be mapped to a Railway Volume.
DATA_PATH = os.getenv("DATA_PATH", "/data") 
DATA_FILE = os.path.join(DATA_PATH, "confessions_store.json")
# -----------------------

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
            logger.info(f"Successfully loaded data store from {DATA_FILE}.")
        except json.JSONDecodeError:
            logger.warning(f"Failed to load data store from {DATA_FILE}. Starting fresh.")

def save_store():
    # Ensure the directory exists before saving
    os.makedirs(DATA_PATH, exist_ok=True)
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
        logger.debug(f"Successfully saved data store to {DATA_FILE}.")
    except Exception as e:
        logger.error(f"Failed to save data store: {e}")

# ===== Access Control (is_admin_chat decorator) =====
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
        await update.message.reply_text("⚠️ Failed to submit confession. Check `ADMIN_GROUP_ID`.")

# ===== Handlers (Including simple state handling for comments) =====
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == Chat.PRIVATE:
        alias = get_user_alias(update.effective_user.id)
        msg = f"👋 Welcome! Your current alias: *{alias}*\nSend a message here to confess anonymously or use /confess."
        await update.message.reply_text(msg, parse_mode="Markdown")
    
    # Simple Deeplink/Start Parameter handling (e.g., from channel button)
    if context.args and context.args[0].startswith("comment_"):
        try:
            conf_id = int(context.args[0].split('_')[1])
            await send_confession_options(update, context, conf_id)
        except (IndexError, ValueError):
            pass # Ignore bad start parameters

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

# State machine for receiving the actual comment text after button click
async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id

    # 1. Check if the user is in the 'awaiting_reply' state
    user_data = context.user_data.get(user_id, {})
    if user_data.get("state") == "awaiting_reply":
        conf_id = user_data["conf_id"]
        
        # Simple placeholder for comment storage
        conf = store["posted"].get(str(conf_id))
        if conf:
            conf.setdefault("replies", []).append({
                "alias": get_user_alias(user_id),
                "text": text,
                "timestamp": datetime.now(utc).isoformat()
            })
            save_store()
            await update.message.reply_text(f"✅ Your comment has been saved for Confession #{conf_id}.")
            
            # Clear the state
            context.user_data[user_id] = {}
            return
        
    # 2. If not in a special state, treat it as a new confession submission
    if not text: return
    await submit_pending_confession(update, context, text)

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")
    user_id = update.effective_user.id

    if data[0] == "add_comment":
        conf_id = int(data[1])
        # Set the user state to awaiting_reply
        context.user_data[user_id] = {"state": "awaiting_reply", "conf_id": conf_id}
        await query.edit_message_text(f"📝 Send your comment text now for Confession #{conf_id}.")
        return

    if data[0] == "browse_comments":
        conf_id = int(data[1])
        conf = store["posted"].get(str(conf_id))
        if not conf or not conf.get("replies"):
            await query.edit_message_text(f"Confession #{conf_id} has no comments yet.", reply_markup=get_confession_markup(conf_id))
            return
        
        comment_list = [f"*{r['alias']}*: {r['text'][:50]}..." for r in conf['replies']]
        
        reply_text = f"💬 *Comments for Confession #{conf_id}*\n\n" + "\n---\n".join(comment_list)
        await query.edit_message_text(reply_text, parse_mode="Markdown", reply_markup=get_confession_markup(conf_id))
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
            
            # Use the alias in the post header
            post_header = f"*{pending['user_alias']}'s Confession #{conf_id}*"
            sent = await context.bot.send_message(CHANNEL_ID, f"{post_header}\n\n{pending['text']}", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
            
            store["posted"][str(conf_id)]["channel_message_id"] = sent.message_id
            save_store()
            await query.edit_message_text(f"✅ Approved and posted as #{conf_id}")
        else:
            await query.edit_message_text("⚠️ Confession already processed.")

    if data[0] == "reject":
        if store["pending"].pop(data[1], None):
            save_store()
            await query.edit_message_text("❌ Rejected and removed.")
        else:
            await query.edit_message_text("⚠️ Confession already processed.")


# ===== Admin Commands Example =====
@is_admin_chat
async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not store["pending"]:
        await update.message.reply_text("✅ No pending confessions.")
        return
    msg = "*Pending Confessions List*\n\n"
    for p in store["pending"].values():
        msg += f"ID: {p['id']} (Alias: {p['user_alias']}) - {p['text'][:50]}...\n"
    
    # Add buttons to quickly approve the first few
    keyboard = []
    pending_ids = list(store["pending"].keys())[:MAX_BATCH_APPROVAL]
    for pending_id in pending_ids:
        conf_id = store["pending"][pending_id]["id"]
        keyboard.append([InlineKeyboardButton(f"✅ #{conf_id}", callback_data=f"approve|{pending_id}")])

    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)


# ===== Command Setup (Moved out of main for post_init) =====
async def set_bot_commands(application: Application):
    commands = [
        BotCommand("start", "Welcome message & check alias"),
        BotCommand("confess", "Send anonymous confession"),
        BotCommand("setalias", "Set or change your alias/nickname"),
        BotCommand("pending", "Admin: List pending confessions (in admin group)")
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands set.")

def main():
    load_store() # Load data store first
    
    # post_init runs after bot is initialized but before polling/webhook starts
    app = Application.builder().token(BOT_TOKEN).post_init(set_bot_commands).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("confess", confess_command))
    app.add_handler(CommandHandler("setalias", set_alias_command))
    app.add_handler(CommandHandler("pending", pending_command))
    # This handler catches all non-command text, including new confessions AND comments
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_text_messages))
    app.add_handler(CallbackQueryHandler(handle_callbacks))

    # Deployment logic (prefers webhook but defaults to polling)
    if WEBHOOK_URL:
        # Webhook setup is usually for platforms like Heroku/FastAPI environments
        path = "/webhook/" + BOT_TOKEN
        app.run_webhook(listen="0.0.0.0", port=PORT, urlpath=path, webhook_url=WEBHOOK_URL + path)
        logger.info(f"Webhook running at {WEBHOOK_URL + path}")
    else:
        # Polling setup for platforms like Railway/Fly.io/local
        logger.warning("Webhook URL not set. Running in polling mode.")
        app.run_polling()
        
if __name__ == "__main__":
    main()
