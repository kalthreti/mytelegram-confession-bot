import json
import os
import logging
import asyncio
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from pytz import utc
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables from .env file (for local testing)
load_dotenv()

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== CONFIGURATION & ENVIRONMENT VARIABLES (Passed by Render) =====
# Fetch sensitive data from environment variables provided by Render
BOT_TOKEN = os.getenv("BOT_TOKEN", "8394081800:AAHqaAOPyOu1O7xQAJj84JSeh1mCBF0EZlQ")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@weirdo_confessions")
# Convert admin ID to integer
try:
    ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "-1003301880047"))
except ValueError:
    logger.error("ADMIN_GROUP_ID environment variable is not a valid integer.")
    ADMIN_GROUP_ID = -100 # Default to a harmless invalid ID if parsing fails

# Webhook configuration (required for Render)
WEBHOOK_URL = os.getenv("WEBHOOK_URL") # Provided by Render once deployed (e.g., https://my-app.onrender.com)
PORT = int(os.getenv("PORT", "8000")) # Provided by Render

DATA_FILE = "confessions_store.json"
ADMIN_ALIAS = "Admin"
MAX_BATCH_APPROVAL = 15 
# =================================================

# ===== Persistent Storage (Local JSON) - RENDER NOTE =====
# IMPORTANT: Data stored in confessions_store.json may be lost if the service restarts 
# or scales down, as Render's free tier uses ephemeral storage. For production, consider 
# using a persistent database like Redis or PostgreSQL (available on Render).
store: dict = {"next_id": 1, "pending": {}, "posted": {}, "user_profiles": {}}

def load_store():
    """Loads state from JSON file on startup."""
    global store
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                loaded_data = json.load(f)
                store.update({"next_id": 1, "pending": {}, "posted": {}, "user_profiles": {}})
                store.update(loaded_data)
                
                # Robustness checks for existing data structure
                for conf_id in store["posted"]:
                    conf = store["posted"][conf_id]
                    if "replies" not in conf: conf["replies"] = []
                    for i, reply in enumerate(conf["replies"]):
                        if "reply_id" not in reply: reply["reply_id"] = int(conf_id) * 1000 + i 
                        if "voters" not in reply: reply["voters"] = {}
                        if "user_alias" not in reply: reply["user_alias"] = "Anonymous"
                    if "user_alias" not in conf: conf["user_alias"] = "Anonymous"
                            
                logger.info("Successfully loaded data store.")
        except json.JSONDecodeError:
            logger.warning(f"Could not decode {DATA_FILE}. Starting with fresh store.")
    else:
        logger.info("Data file not found. Initializing new store.")

def save_store():
    """Saves the current state of the bot's store to the JSON file."""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save data store: {e}")

# ===== Access Control Decorator =====

def is_admin_chat(func):
    """Decorator to restrict command access to the specified ADMIN_GROUP_ID."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id: int = update.effective_chat.id
        
        if chat_id != ADMIN_GROUP_ID:
            logger.info(f"Access denied for chat ID {chat_id} to command {func.__name__}")
            
            if update.message:
                await update.message.reply_text("🚫 This command can only be used by administrators.")
            return

        return await func(update, context)
    return wrapper # Note: Removed 'await' here as it's a decorator wrapper

# ===== Public Interaction Helpers (Logic remains same) =====

def get_user_alias(user_id: int) -> str:
    """Retrieves the stored nickname or returns a default 'Anonymous'."""
    return store["user_profiles"].get(str(user_id), "Anonymous")

def get_confession_options_text(conf_id: int) -> str:
    """Generates the full text for the main confession view."""
    confession_data = store["posted"].get(str(conf_id))
    if not confession_data:
        return "⚠️ Confession not found."
    
    conf_alias = confession_data.get("user_alias", "Anonymous")
    
    text = f"*Confession #{conf_id}* (by {conf_alias})\n\n{confession_data['text']}\n"
    
    return text

def get_confession_options_markup(conf_id: int) -> InlineKeyboardMarkup:
    """Generates the main keyboard for the initial confession view."""
    keyboard = [
        [
            InlineKeyboardButton("💬 Add Comment", callback_data=f"add_comment|{conf_id}"),
            InlineKeyboardButton("👁️ Browse Comments", callback_data=f"browse_comments|{conf_id}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def send_confession_options(update: Update, context: ContextTypes.DEFAULT_TYPE, conf_id: int) -> None:
    """Handles sending or editing the message to display the main confession view."""
    text = get_confession_options_text(conf_id)
    markup = get_confession_options_markup(conf_id)

    if update.callback_query and update.callback_query.message:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        logger.warning("Could not send confession options: Missing update context.")

async def submit_pending_confession(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Submits a new, original confession for admin review."""
    
    conf_id: int = store['next_id']
    pending_id: str = f"p{conf_id}"
    user_id = update.effective_user.id
    user_alias = get_user_alias(user_id)

    store["next_id"] += 1 

    store["pending"][pending_id] = {
        "id": conf_id,
        "text": text, 
        "from_user": user_id,
        "user_alias": user_alias
    }
    save_store()

    keyboard = [
        [
            InlineKeyboardButton("✅ Approve Confession", callback_data=f"approve|{pending_id}"),
            InlineKeyboardButton("❌ Reject Confession", callback_data=f"reject|{pending_id}"),
        ]
    ]
    
    try:
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=f"🆕 *Pending Confession #{conf_id}* (ID: {pending_id} | Alias: {user_alias})\n\n{text}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        await update.message.reply_text(
            "✅ Your confession has been received and is pending admin approval. You will not receive a further notification if it is posted."
        )
    except Exception as e:
        logger.error(f"Failed to send moderation message to admin group {ADMIN_GROUP_ID}: {e}")
        await update.message.reply_text(
            "⚠️ Error: Could not submit to the admin group. Please contact the administrator."
        )
        # Rollback ID and store
        store["next_id"] -= 1
        del store["pending"][pending_id]
        save_store()
        return

# ===== Handlers (Including previous admin commands and callbacks) =====

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Greets the user, handles deep links for commenting, and explains the anonymous process (private chat only)."""
    if update.effective_chat.type == Chat.PRIVATE:
        
        if context.args and context.args[0].startswith("comment_"):
            try:
                conf_id_str = context.args[0].split('_')[1]
                conf_id = int(conf_id_str)
                await send_confession_options(update, context, conf_id)
                return
            except (IndexError, ValueError) as e:
                logger.error(f"Error processing deep link: {e}")

        user_alias = get_user_alias(update.effective_user.id)
        
        welcome_message = (
            f"👋 *Welcome to the weirdo confession bot!* 🤫\n\n"
            f"Your current nickname(Alias) is: *{user_alias}*\n"
            "This bot allows you to share your thoughts, secrets, and stories completely anonymously in our channel (weirdo confession)....hey am tired of hearing bulshit get smt weird👽\n\n"
            "*Here are the Rules & Guidelines:*\n"
            "1. *Alias:* Use the `/setalias <name>` command to choose a stable nickname for your posts and comments.\n"
            "2. *Anonymity:* Your Telegram user ID is never revealed. Only your chosen alias is displayed.\n"
            "3. *Submission:* Use the `/confess` command, or simply send your message in this chat. It will be sent for review.\n"
            "4. *Review:* All confessions are reviewed by administrators before being posted to the channel.\n"
            "5. *NO Hate Speech:* Submissions must not contain hate speech, bullying, harassment, or discrimination.\n" 
            "6. *Respect Rights:* Do not submit content that violates the rights of any individual, including privacy or intellectual property.\n" 
            "7. *Interaction:* Use the buttons below posts in the channel to leave anonymous comments and react to other confessions.\n\n"
            "Ready to share? Tap `/confess` or just start typing!"
        )

        await update.message.reply_text(welcome_message, parse_mode="Markdown")

async def set_alias_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Allows the user to set a persistent nickname (alias)."""
    if update.effective_chat.type != Chat.PRIVATE:
        await update.message.reply_text("This command works only in a private chat with the bot.")
        return

    if not context.args:
        current_alias = get_user_alias(update.effective_user.id)
        await update.message.reply_text(
            f"📝 Your current alias is: *{current_alias}*\n"
            "Usage: `/setalias <new_nickname>`. Your nickname must be between 3 and 20 characters long and contain only letters, numbers, and spaces."
        )
        return

    new_alias = " ".join(context.args).strip()
    
    if len(new_alias) < 3 or len(new_alias) > 20:
        await update.message.reply_text("❌ Nickname must be between 3 and 20 characters long.")
        return
        
    if not all(c.isalnum() or c.isspace() for c in new_alias):
        await update.message.reply_text("❌ Nickname can only contain letters, numbers, and spaces.")
        return

    user_id_str = str(update.effective_user.id)
    store["user_profiles"][user_id_str] = new_alias
    save_store()

    await update.message.reply_text(
        f"✅ Your new alias has been set to: *{new_alias}*. This will be used for all future posts and comments.",
        parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Provides a list of commands and general usage instructions."""
    help_text = (
        "*Welcome to the Confession Bot!* 🤫\n\n"
        "**Public User Commands (Private Chat Only):**\n"
        "1. `/setalias <name>`: Set your persistent nickname/alias.\n" 
        "2. `/confess`: Start submitting your anonymous confession.\n"
        "3. `/feedback`: Send anonymous feedback or suggestions directly to the admins.\n"
        "4. `/start`: Get the detailed welcome message and rules.\n"
        "5. `/help`: Show this command summary.\n"
        "6. `/cancel`: Cancel a pending comment or feedback submission.\n\n"
        "**How to Interact:**\n"
        "Find a confession in the public channel and click the 'Add / View Comments' button to interact."
    )
    
    if update.effective_chat.id == ADMIN_GROUP_ID:
        help_text += (
            "\n\n*Admin Commands (Admin Chat Only):*\n"
            "1. `/pending`: List all confessions awaiting approval.\n"
            f"2. `/approve_batch [N]`: Approve and post the next N (max {MAX_BATCH_APPROVAL}) pending confessions.\n" 
            f"3. `/reply <id> <message>`: Post an auto-approved anonymous comment to confession `<id>` (as `{ADMIN_ALIAS}`).\n"
            "4. `/stats`: Show comment and vote statistics for all posted confessions.\n"
            "5. `/deleteconfession <id>`: Permanently delete a posted confession and all its comments.\n"
            "6. `/deletecomment <id> <index>`: Delete a specific comment by confession `<id>` and comment `<index>` (1-based).\n"
            "7. `/reset_counter`: **DANGER** Clears ALL data and resets the counter to 1."
        )

    await update.message.reply_text(help_text, parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancels a pending comment or feedback submission."""
    user_id = update.effective_user.id
    if user_id in context.user_data:
        # Check for state stored under user_id key
        state_data = context.user_data.get(user_id, {})
        state = state_data.get('state')
        
        if state in ['awaiting_reply', 'awaiting_feedback']:
            del context.user_data[user_id]
            await update.message.reply_text(f"❌ Cancelled your {state.split('_')[1]} submission.")
            return

    await update.message.reply_text("There is no active submission to cancel.")

async def confess_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompts the user to send their confession message."""
    if update.effective_chat.type == Chat.PRIVATE:
        user_alias = get_user_alias(update.effective_user.id)
        await update.message.reply_text(
            f"📝 Please send your anonymous confession message now. Your current alias is *{user_alias}*."
        )
    else:
        await update.message.reply_text("This command works only in a private chat with the bot.")

async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initiates the feedback submission state for the user."""
    if update.effective_chat.type == Chat.PRIVATE:
        user_id = update.effective_user.id
        context.user_data[user_id] = {'state': 'awaiting_feedback'}
        await update.message.reply_text(
            "📝 You are now submitting *anonymous feedback* to the admins. "
            "Please send your message now."
        )
    else:
        await update.message.reply_text("This command works only in a private chat with the bot.")

async def handle_confession(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receives a text message from a private chat and handles it as a comment, feedback, or new confession."""
    user_id = update.effective_user.id
    user_alias = get_user_alias(user_id)
    text: str = update.message.text.strip() if update.message.text else ""
    
    if not text:
        await update.message.reply_text("Please send text only for your submission.")
        return

    # User data is stored under the user_id key in the application context for session management
    user_state = context.user_data.get(user_id)
    
    if user_state and user_state.get('state') == 'awaiting_reply':
        conf_id_to_reply_to = user_state.get('conf_id')
        del context.user_data[user_id]
        
        if conf_id_to_reply_to is None:
            await update.message.reply_text("⚠️ Cannot submit comment: Invalid context.")
            return
            
        conf_key = str(conf_id_to_reply_to)
        
        if conf_key not in store["posted"]:
            await update.message.reply_text("⚠️ Cannot submit comment: The original confession no longer exists.")
            return

        reply_id = store['next_id']
        store["next_id"] += 1

        posted_confession = store["posted"][conf_key]
        if "replies" not in posted_confession:
              posted_confession["replies"] = []

        posted_confession["replies"].append({
            "reply_id": reply_id,
            "text": text,
            "user_alias": user_alias,
            "approved_time": update.message.date.astimezone(utc).isoformat(),
            "voters": {}
        })
        save_store()
        
        await update.message.reply_text(
            f"✅ Your comment to Confession #{conf_id_to_reply_to} (as *{user_alias}*) has been posted ",
            parse_mode="Markdown"
        )
        return

    if user_state and user_state.get('state') == 'awaiting_feedback':
        del context.user_data[user_id]
        
        try:
            await context.bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                text=f"✉️ *New Anonymous Feedback*\n\n{text}",
                parse_mode="Markdown",
            )
            await update.message.reply_text("✅ Your feedback has been sent to the administrators. Thank Благодарю!")
        except Exception as e:
            logger.error(f"Failed to send feedback to admin group {ADMIN_GROUP_ID}: {e}")
            await update.message.reply_text(
                "⚠️ Error: Could not send feedback to the admin group. Please try again later."
            )
        return

    # Default: Treat message as a new confession
    await submit_pending_confession(update, context, text)


async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles inline button presses."""
    query = update.callback_query
    await query.answer()

    action_data = query.data.split("|")
    
    # --- 1. Public Interaction Callbacks ---
    if action_data[0] in ["view_confession", "add_comment"]:
        action, data_id = action_data[0], action_data[1]
        try:
            conf_id = int(data_id)
        except (IndexError, ValueError):
            await query.edit_message_text("Invalid confession ID.")
            return

        confession_data = store["posted"].get(str(conf_id))

        if not confession_data:
            await query.edit_message_text("⚠️ Confession not found.")
            return
            
        if action == "view_confession":
            await send_confession_options(update, context, conf_id)
            return
        
        elif action == "add_comment":
            user_id = update.effective_user.id
            user_alias = get_user_alias(user_id)
            
            # Store state under user_id key
            context.user_data[user_id] = {'state': 'awaiting_reply', 'conf_id': conf_id}

            response_text = (
                f"** u r submitting a comment to Confession #{conf_id}** (as *{user_alias}*)\n\n" 
                f" \n> {confession_data['text']}\n\n"
                f"Please send your comment "
            )
            await query.edit_message_text(response_text, parse_mode="Markdown")
            return
    
    # Handle Comment Voting
    elif action_data[0] == "vote_comment":
        if len(action_data) != 4: return
        _, conf_id_str, reply_id_str, vote_type = action_data
        
        user_id = str(update.effective_user.id)
        current_vote = vote_type
        
        try:
            conf_id = int(conf_id_str)
            reply_id = int(reply_id_str)
        except ValueError:
            await query.answer("Invalid Confession or Reply ID.")
            return
            
        conf_key = str(conf_id)
        if conf_key not in store["posted"]:
            await query.answer("Confession not found.")
            return

        replies = store["posted"][conf_key]["replies"]
        
        target_reply = None
        comment_index = -1
        for i, r in enumerate(replies):
             if r.get('reply_id') == reply_id:
                 target_reply = r
                 comment_index = i + 1
                 break

        if not target_reply:
            await query.answer("Comment not found.")
            return

        voters = target_reply.get('voters', {}) 
        previous_vote = voters.get(user_id)
        
        if previous_vote == current_vote:
            await query.answer(f"You already voted '{current_vote}' on this comment.")
            return
        
        if previous_vote:
            voters[user_id] = current_vote
            message_text = f"Vote changed to '{current_vote}'!"
        else:
            voters[user_id] = current_vote
            message_text = f"{current_vote.capitalize()} counted!"
            
        target_reply['voters'] = voters
        save_store()
        
        likes = sum(1 for vote in voters.values() if vote == 'like')
        dislikes = sum(1 for vote in voters.values() if vote == 'dislike')
        
        updated_keyboard = [
            [
                InlineKeyboardButton(f"👍 Like ({likes})", callback_data=f"vote_comment|{conf_id}|{reply_id}|like"),
                InlineKeyboardButton(f"👎 Dislike ({dislikes})", callback_data=f"vote_comment|{conf_id}|{reply_id}|dislike"),
            ]
        ]
        updated_markup = InlineKeyboardMarkup(updated_keyboard)
        
        comment_author_alias = target_reply.get('user_alias', 'Anonymous') 
        comment_text = f"*Comment {comment_index}* (by {comment_author_alias} | ID: {reply_id}):\n\n{target_reply['text']}" 
        
        if query.message.text and query.message.text.startswith("*Comment"):
            await query.edit_message_text(
                comment_text, 
                parse_mode="Markdown", 
                reply_markup=updated_markup
            )
        
        await query.answer(message_text)
        return

    # Handle Browse Comments
    elif action_data[0] == "browse_comments":
        _, data_id = action_data
        user_chat_id = query.message.chat.id
        
        try:
            conf_id = int(data_id)
        except ValueError:
            await query.edit_message_text("Invalid confession ID.")
            return

        confession_data = store["posted"].get(str(conf_id))

        if not confession_data:
            await query.edit_message_text("⚠️ Confession not found.")
            return

        replies = confession_data.get("replies", [])
        
        # Edit the button message to show the comment count
        await query.edit_message_text(
            f"📝 *Displaying {len(replies)} Comments for Confession #{conf_id}* (Scroll Down for the list)", 
            parse_mode="Markdown"
        )
        
        # Setup navigation for the end of the comment stream
        channel_message_id = confession_data.get("channel_message_id")
        
        if channel_message_id:
            channel_username = CHANNEL_ID.lstrip('@')
            back_button = InlineKeyboardButton(
                "📢 View Confession in Channel", 
                url=f"https://t.me/{channel_username}/{channel_message_id}"
            )
            final_text = "*_End of Comments List. Click the button below to go directly to the original Confession post._*"
        else:
            # Fallback for old posts without channel_message_id
            back_button = InlineKeyboardButton(
                "⬅️ Back to Confession Options (Private)", 
                callback_data=f"view_confession|{conf_id}"
            )
            final_text = "*_End of Comments List. Click 'Back to Confession Options (Private)' to return to the main post options._*"

        add_comment_button = InlineKeyboardButton("💬 Add New Comment", callback_data=f"add_comment|{conf_id}")
        
        navigation_keyboard = [
            [back_button, add_comment_button]
        ]
        final_markup = InlineKeyboardMarkup(navigation_keyboard)
        
        # Send each comment as a separate message
        if replies:
            for i, reply in enumerate(replies, 1):
                reply_id = reply.get("reply_id", int(data_id) * 1000 + i) 
                
                voters = reply.get('voters', {})
                likes = sum(1 for vote in voters.values() if vote == 'like')
                dislikes = sum(1 for vote in voters.values() if vote == 'dislike')
                
                comment_author_alias = reply.get('user_alias', 'Anonymous') 

                comment_text = f"*Comment {i}* (by {comment_author_alias} | ID: {reply_id}):\n\n{reply['text']}" 

                comment_keyboard = [
                    [
                        InlineKeyboardButton(f"👍 Like ({likes})", callback_data=f"vote_comment|{conf_id}|{reply_id}|like"),
                        InlineKeyboardButton(f"👎 Dislike ({dislikes})", callback_data=f"vote_comment|{conf_id}|{reply_id}|dislike"),
                    ]
                ]
                
                await context.bot.send_message(
                    chat_id=user_chat_id,
                    text=comment_text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(comment_keyboard)
                )
                
            # Send final navigation message
            await context.bot.send_message(
                chat_id=user_chat_id,
                text=final_text,
                parse_mode="Markdown",
                reply_markup=final_markup
            )

        else:
            # Handle case with no comments
            response_text = f"📝 *Comments for Confession #{conf_id}*\n\n"
            response_text += "_No comments yet. Be the first one to share your thoughts._\n\n"
            
            await context.bot.send_message(
                chat_id=user_chat_id,
                text=response_text, 
                parse_mode="Markdown",
                reply_markup=final_markup
            )
            # Remove the message that previously held the 'Browse Comments' button
            if query.message:
                 await query.delete_message()
            return


    # --- 2. Admin Action Block ---
    
    if query.message.chat.id != ADMIN_GROUP_ID:
        return
        
    # --- Handle Confession Approval/Rejection (Single Item) ---
    if action_data[0] in ["approve", "reject"]:
        if len(action_data) != 2: return
        action, data_id = action_data

        if data_id not in store["pending"]:
            await query.edit_message_text(query.message.text + "\n\n⚠️ *Already Handled*", parse_mode="Markdown")
            return
            
        pending_item = store["pending"][data_id]
        conf_id = pending_item["id"]
        conf_alias = pending_item["user_alias"]
        
        success_text = query.message.text + f"\n\n✅ *Approved by {update.effective_user.first_name}*"

        if action == "reject":
            del store["pending"][data_id]
            save_store()
            await query.edit_message_text(
                query.message.text + f"\n\n❌ *Rejected by {update.effective_user.first_name}*", 
                parse_mode="Markdown"
            )
            
        elif action == "approve":
            conf = store["pending"].pop(data_id)
            
            temp_conf_data = {
                "text": conf["text"], 
                "user_alias": conf_alias,
                "post_time": update.callback_query.message.date.astimezone(utc).isoformat(), 
                "replies": [], 
                "channel_message_id": None
            }
            store["posted"][str(conf_id)] = temp_conf_data
            save_store()

            bot_info = await context.bot.get_me()
            bot_username = bot_info.username

            reply_keyboard = [[
                InlineKeyboardButton(
                    "💬 Add / View Comments", 
                    # Use the bot's username and the 'start' parameter for deep linking
                    url=f"https://t.me/{bot_username}?start=comment_{conf_id}"
                )
            ]]
            post_text = f"*#{conf_id} Confession* - Posted by: {conf_alias}\n\n{conf['text']}"
            try:
                sent_message = await context.bot.send_message(
                    chat_id=CHANNEL_ID, 
                    text=post_text, 
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(reply_keyboard)
                )
                
                # Store the channel message ID for deep linking
                store["posted"][str(conf_id)]["channel_message_id"] = sent_message.message_id
                save_store()
                
                await query.edit_message_text(
                    success_text + f" and Posted as #{conf_id}", 
                    parse_mode="Markdown"
                )

            except Exception as e:
                logger.error(f"Failed to post confession #{conf_id} to channel {CHANNEL_ID}: {e}")
                
                # Rollback changes if posting fails
                store["pending"][data_id] = conf
                del store["posted"][str(conf_id)]
                # store["next_id"] is left alone to avoid reuse in a tight loop if multiple failures occur
                save_store()

                error_message = f"❌ *POSTING FAILED* Confession #{conf_id}.\n\nError: The bot could not post to the channel. **Check if the bot is an administrator in {CHANNEL_ID} with the 'Post Messages' permission.**"
                await query.edit_message_text(
                    success_text + f"\n\n{error_message}", 
                    parse_mode="Markdown"
                )
    
    # --- Handle Batch Approval Callback ---
    elif action_data[0] == "approve_batch":
        try:
            N = int(action_data[1])
        except (IndexError, ValueError):
            await query.edit_message_text("❌ Error: Invalid batch size argument in callback.")
            return

        batch_size = min(N, MAX_BATCH_APPROVAL)
        
        pending_keys = list(store["pending"].keys())[:batch_size]
        approved_count = 0
        failed_count = 0
        
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username

        for data_id in pending_keys:
            pending_item = store["pending"].get(data_id)
            if not pending_item: continue

            conf_id = pending_item["id"]
            conf_alias = pending_item["user_alias"]

            conf = store["pending"].pop(data_id)
            temp_conf_data = {
                "text": conf["text"], 
                "user_alias": conf_alias,
                "post_time": datetime.now(utc).isoformat(), 
                "replies": [], 
                "channel_message_id": None
            }
            store["posted"][str(conf_id)] = temp_conf_data
            
            reply_keyboard = [[
                InlineKeyboardButton(
                    "💬 Add / View Comments", 
                    url=f"https://t.me/{bot_username}?start=comment_{conf_id}"
                )
            ]]
            post_text = f"*#{conf_id} Confession* - Posted by: {conf_alias}\n\n{conf['text']}"
            
            try:
                sent_message = await context.bot.send_message(
                    chat_id=CHANNEL_ID, 
                    text=post_text, 
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(reply_keyboard)
                )
                store["posted"][str(conf_id)]["channel_message_id"] = sent_message.message_id
                approved_count += 1
            except Exception as e:
                logger.error(f"Batch post failed for #{conf_id} to channel {CHANNEL_ID}: {e}")
                
                # Rollback changes if posting fails
                store["pending"][data_id] = conf
                del store["posted"][str(conf_id)]
                failed_count += 1
        
        save_store()
        
        total_pending_after = len(store["pending"])
        
        batch_summary = (
            f"✅ *Batch Approval Complete* (by {update.effective_user.first_name})\n"
            f"Approved: **{approved_count}**\n"
            f"Failed: **{failed_count}** (Check bot permissions in channel)\n"
            f"Pending remaining: **{total_pending_after}**\n"
            f"Run `/pending` to review the rest."
        )

        await query.edit_message_text(batch_summary, parse_mode="Markdown")
        
        return

# ===== ADMIN COMMANDS (Logic remains same) =====

@is_admin_chat
async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists all pending confessions."""
    pending_list = store["pending"].values()
    if not pending_list:
        await update.message.reply_text("✅ No confessions are currently awaiting approval.")
        return

    message = "*Pending Confessions Queue:*\n\n"
    for conf in pending_list:
        conf_id = conf["id"]
        pending_id = f"p{conf_id}"
        alias = conf["user_alias"]
        message += f"**ID #{conf_id}** (Alias: {alias}) | Key: `{pending_id}`\n"
        message += f"Excerpt: _{conf['text'][:50]}..._\n"
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve|{pending_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject|{pending_id}"),
            ]
        ]
        await update.message.reply_text(message, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        message = ""

    if message:
        await update.message.reply_text(message, parse_mode="Markdown")
        
@is_admin_chat
async def approve_batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompts for batch approval of N (max 15) pending confessions."""
    if not context.args:
        N = 5
    else:
        try:
            N = int(context.args[0])
            if N <= 0: raise ValueError
        except ValueError:
            await update.message.reply_text(f"❌ Please provide a positive number for the batch size (max {MAX_BATCH_APPROVAL}). E.g., `/approve_batch 10`.")
            return
            
    N = min(N, MAX_BATCH_APPROVAL)

    pending_count = len(store["pending"])
    if pending_count == 0:
        await update.message.reply_text("✅ No pending confessions to approve.")
        return
        
    actual_count = min(N, pending_count)
    
    keyboard = [
        [InlineKeyboardButton(f"🚀 Confirm Batch Approve ({actual_count} items)", callback_data=f"approve_batch|{N}")]
    ]
    
    await update.message.reply_text(
        f"You are about to approve and post the next **{actual_count}** pending confessions.\n"
        f"There are **{pending_count}** total pending confessions.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

@is_admin_chat
async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to post an anonymous comment as 'Admin'."""
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/reply <confession_id> <message>`. The message will be posted anonymously as 'Admin'.")
        return

    try:
        conf_id = int(context.args[0])
        conf_key = str(conf_id)
        reply_text = " ".join(context.args[1:])
    except ValueError:
        await update.message.reply_text("❌ Invalid Confession ID format.")
        return

    if conf_key not in store["posted"]:
        await update.message.reply_text(f"⚠️ Confession #{conf_id} not found in posted list.")
        return
        
    reply_id = store['next_id']
    store["next_id"] += 1
    
    posted_confession = store["posted"][conf_key]
    if "replies" not in posted_confession:
        posted_confession["replies"] = []

    posted_confession["replies"].append({
        "reply_id": reply_id,
        "text": reply_text,
        "user_alias": ADMIN_ALIAS, 
        "approved_time": update.message.date.astimezone(utc).isoformat(),
        "voters": {}
    })
    save_store()
    
    await update.message.reply_text(
        f"✅ Your comment (ID: {reply_id}) has been posted to Confession #{conf_id} as *{ADMIN_ALIAS}*.",
        parse_mode="Markdown"
    )

@is_admin_chat
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows comment and vote statistics."""
    stats = []
    total_comments = 0
    
    for conf_id_str, conf in store["posted"].items():
        conf_id = int(conf_id_str)
        replies = conf.get("replies", [])
        comment_count = len(replies)
        total_comments += comment_count
        
        total_likes = 0
        total_dislikes = 0
        for reply in replies:
            voters = reply.get('voters', {})
            total_likes += sum(1 for vote in voters.values() if vote == 'like')
            total_dislikes += sum(1 for vote in voters.values() if vote == 'dislike')
            
        stats.append({
            "id": conf_id,
            "comments": comment_count,
            "likes": total_likes,
            "dislikes": total_dislikes
        })
        
    if not stats:
        await update.message.reply_text("No posted confessions yet to generate statistics.")
        return

    stats.sort(key=lambda x: x["comments"], reverse=True)
    
    response = "*Confession Bot Statistics*\n\n"
    response += f"Total Confessions Posted: **{len(store['posted'])}**\n"
    response += f"Total Comments Submitted: **{total_comments}**\n"
    response += f"Next Confession ID: **{store['next_id']}**\n\n"
    response += "*Top Confessions by Interaction:*\n"
    
    for stat in stats[:10]:
        response += (
            f"**#{stat['id']}**: "
            f"{stat['comments']} Comments, {stat['likes']} 👍, {stat['dislikes']} 👎\n"
        )
        
    await update.message.reply_text(response, parse_mode="Markdown")

@is_admin_chat
async def delete_confession_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Permanently deletes a posted confession."""
    if len(context.args) != 1:
        await update.message.reply_text("Usage: `/deleteconfession <confession_id>`")
        return

    try:
        conf_id_str = context.args[0]
        conf_id = int(conf_id_str)
    except ValueError:
        await update.message.reply_text("❌ Invalid Confession ID format.")
        return

    if conf_id_str not in store["posted"]:
        await update.message.reply_text(f"⚠️ Confession #{conf_id} not found in posted list.")
        return

    deleted_conf = store["posted"].pop(conf_id_str)
    save_store()
    
    await update.message.reply_text(
        f"✅ Confession #{conf_id} and all {len(deleted_conf.get('replies', []))} comments have been permanently deleted from the bot's database."
    )
    
@is_admin_chat
async def delete_comment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deletes a specific comment by ID and index."""
    if len(context.args) != 2:
        await update.message.reply_text("Usage: `/deletecomment <confession_id> <comment_index>` (index is 1-based, see `browse_comments` for IDs)")
        return

    try:
        conf_id_str = context.args[0]
        conf_id = int(conf_id_str)
        comment_index = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid ID or Index format.")
        return

    if conf_id_str not in store["posted"]:
        await update.message.reply_text(f"⚠️ Confession #{conf_id} not found.")
        return

    replies = store["posted"][conf_id_str].get("replies", [])
    
    if 1 <= comment_index <= len(replies):
        deleted_reply = replies.pop(comment_index - 1)
        save_store()
        
        await update.message.reply_text(
            f"✅ Comment index *{comment_index}* (ID: {deleted_reply.get('reply_id')}) on Confession #{conf_id} has been deleted.\n"
            f"Content: _{deleted_reply['text'][:50]}..._"
            , parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"⚠️ Invalid comment index *{comment_index}* for Confession #{conf_id}. There are only {len(replies)} comments.")

@is_admin_chat
async def reset_counter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """DANGEROUS: Clears all data and resets the counter."""
    keyboard = [
        [InlineKeyboardButton("⚠️ CONFIRM DELETE ALL DATA ⚠️", callback_data="confirm_reset")]
    ]
    await update.message.reply_text(
        "🚨 **DANGER ZONE** 🚨\n\n"
        "Are you absolutely sure you want to reset the bot?\n"
        "This will *PERMANENTLY DELETE* all data. **This action cannot be undone.**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def confirm_reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles confirmation of the dangerous reset command."""
    query = update.callback_query
    await query.answer()

    if query.message.chat.id != ADMIN_GROUP_ID: return

    global store
    store = {"next_id": 1, "pending": {}, "posted": {}, "user_profiles": {}}
    save_store()

    await query.edit_message_text(
        "✅ **ALL BOT DATA HAS BEEN PERMANENTLY DELETED.**\n"
        "The confession counter has been reset to 1. The bot is now running with a fresh slate.",
        parse_mode="Markdown"
    )

# ===== Main Webhook Function & Setup (Updated for Render) =====

async def set_bot_commands(application: Application) -> None:
    """Sets the visible command list for the bot."""
    commands = [
        BotCommand("start", "Get welcome message & rules"),
        BotCommand("help", "Show command list"),
        BotCommand("confess", "Submit an anonymous confession"),
        BotCommand("setalias", "Set your anonymous nickname/alias"),
        BotCommand("feedback", "Send anonymous feedback to admins"),
        BotCommand("cancel", "Cancel current comment/feedback submission"),
    ]
    await application.bot.set_my_commands(commands)

def main() -> None:
    """Start the bot using Webhooks for deployment."""
    load_store()
    
    # Initialize Application
    application = Application.builder().token(BOT_TOKEN).post_init(set_bot_commands).build()

    # Handlers Setup 
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("confess", confess_command))
    application.add_handler(CommandHandler("setalias", set_alias_command))
    application.add_handler(CommandHandler("feedback", feedback_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_confession))
    
    # Admin Handlers
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("approve_batch", approve_batch_command))
    application.add_handler(CommandHandler("reply", reply_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("deleteconfession", delete_confession_command))
    application.add_handler(CommandHandler("deletecomment", delete_comment_command))
    application.add_handler(CommandHandler("reset_counter", reset_counter_command))
    
    # Callback Handlers
    application.add_handler(CallbackQueryHandler(confirm_reset_callback, pattern='^confirm_reset$'))
    application.add_handler(CallbackQueryHandler(handle_callbacks))

    # --- Webhook Deployment Logic for Render ---
    if WEBHOOK_URL and BOT_TOKEN:
        # We need a secure, specific path for the webhook
        webhook_path = "/webhook/" + BOT_TOKEN
        
        # Start the bot as a webhook web service
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            urlpath=webhook_path,
            webhook_url=WEBHOOK_URL + webhook_path
        )
        logger.info(f"Bot started via webhook on port {PORT} at path {webhook_path}.")
        logger.info(f"Full Webhook URL: {WEBHOOK_URL + webhook_path}")
    else:
        # Fallback to polling for local testing if WEBHOOK_URL is missing
        logger.warning("WEBHOOK_URL not set. Falling back to local Polling mode. DO NOT USE FOR DEPLOYMENT.")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()