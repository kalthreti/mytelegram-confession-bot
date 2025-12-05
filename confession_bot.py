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

# --- Logging Setup ---
# Configure basic logging to see bot activity and errors
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== CONFIGURATION =====
# NOTE: Ensure you replace the placeholders below with your actual values!
BOT_TOKEN = "8394081800:AAHqaAOPyOu1O7xQAJj84JSeh1mCBF0EZlQ"  # <-- REQUIRED: **REPLACE THIS WITH YOUR NEW, VALID BOT TOKEN**
CHANNEL_ID = "@weirdo_confessions"        # Your public channel username (e.g., @mychannel)
ADMIN_GROUP_ID = -1003301880047 # <-- REQUIRED: Your admin group/chat ID (numerical, starts with -100)
DATA_FILE = "confessions_store.json"      # File for persistent data storage
ADMIN_ALIAS = "Admin" # Alias used when an admin replies via /reply command
# =========================

# ===== Persistent Storage (Local JSON) =====
# Global dictionary to hold the bot's state (confession IDs, pending/posted data)
# ADDED 'user_profiles' dictionary to store user aliases
store: dict = {"next_id": 1, "pending": {}, "posted": {}, "user_profiles": {}}

def load_store():
    """Loads state from JSON file on startup."""
    global store
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                loaded_data = json.load(f)
                # Ensure core keys exist and update with loaded data
                store.update({"next_id": 1, "pending": {}, "posted": {}, "user_profiles": {}})
                store.update(loaded_data)
                
                # Robustness check and initialization for existing confessions and replies
                for conf_id in store["posted"]:
                    conf = store["posted"][conf_id]
                    if "replies" not in conf:
                        conf["replies"] = []
                    
                    # Ensure all replies also have a unique ID and initialize the new 'voters' map
                    for i, reply in enumerate(conf["replies"]):
                        if "reply_id" not in reply:
                            # Use a temporary ID structure if reply_id is missing for old data
                            reply["reply_id"] = int(conf_id) * 1000 + i 
                        
                        if "voters" not in reply:
                            reply["voters"] = {}
                        
                        # Data Migration: Ensure existing replies have an alias (default to 'Anonymous')
                        if "user_alias" not in reply:
                            reply["user_alias"] = "Anonymous"
                            
                    # Data Migration: Ensure existing confessions have an alias (default to 'Anonymous')
                    if "user_alias" not in conf:
                        conf["user_alias"] = "Anonymous"
                            
                logger.info("Successfully loaded data store.")
        except json.JSONDecodeError:
            logger.warning(f"Could not decode {DATA_FILE}. Starting with fresh store.")
    else:
        logger.info("Data file not found. Initializing new store.")

def save_store():
    """Saves the current state of the bot's store to the JSON file."""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            # Use ensure_ascii=False for proper display of non-Latin characters
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save data store: {e}")

# ===== Access Control Decorator =====

def is_admin_chat(func):
    """Decorator to restrict command access to the specified ADMIN_GROUP_ID."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id: int = update.effective_chat.id
        
        # Check against the actual configured ADMIN_GROUP_ID
        if chat_id != ADMIN_GROUP_ID:
            logger.info(f"Access denied for chat ID {chat_id} to command {func.__name__}")
            
            if update.message:
                await update.message.reply_text("🚫 This command can only be used by administrators.")
            return

        return await func(update, context)
    return wrapper

# ===== Public Interaction Helpers =====

def get_user_alias(user_id: int) -> str:
    """Retrieves the stored nickname or returns a default 'Anonymous'."""
    # Use str(user_id) as the key for consistency with JSON keys
    return store["user_profiles"].get(str(user_id), "Anonymous")

def get_confession_options_text(conf_id: int) -> str:
    """Generates the full text for the main confession view, *without* confession votes."""
    confession_data = store["posted"].get(str(conf_id))
    if not confession_data:
        return "⚠️ Confession not found."
    
    # Confession now includes the user's alias (or Anonymous)
    conf_alias = confession_data.get("user_alias", "Anonymous")
    
    text = f"*Confession #{conf_id}* (by {conf_alias})\n\n{confession_data['text']}\n"
    
    return text

def get_confession_options_markup(conf_id: int) -> InlineKeyboardMarkup:
    """
    Generates the main keyboard for the initial confession view. 
    Includes only Add/Browse Comment buttons.
    """
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

    # Determine if we should send a new message or edit the existing one (for deep links vs callbacks)
    if update.callback_query and update.callback_query.message:
        # This is where 'view_confession' action takes place, editing the message the button was on
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        # Fallback for complex scenarios where we have neither a message nor a query message
        logger.warning("Could not send confession options: Missing update context.")


# ===== Handler Helpers (Submission) =====

async def submit_pending_confession(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Submits a new, original confession for admin review."""
    
    # 1. Get user details and alias
    conf_id: int = store['next_id']
    pending_id: str = f"p{conf_id}"
    user_id = update.effective_user.id
    user_alias = get_user_alias(user_id) # <-- NEW: Get alias

    # 2. Generate unique ID and save pending data
    store["next_id"] += 1 

    store["pending"][pending_id] = {
        "id": conf_id,
        "text": text, 
        "from_user": user_id,
        "user_alias": user_alias # <-- NEW: Store alias in pending item
    }
    save_store()

    # 3. Create inline keyboard for moderators (Confession approval)
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve Confession", callback_data=f"approve|{pending_id}"),
            InlineKeyboardButton("❌ Reject Confession", callback_data=f"reject|{pending_id}"),
        ]
    ]
    
    # 4. Send to admin group
    try:
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            # Updated: Display the alias in the admin review message
            text=f"🆕 *Pending Confession #{conf_id}* (ID: {pending_id} | Alias: {user_alias})\n\n{text}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        # 5. Confirm receipt to the user
        await update.message.reply_text(
            "✅ Your confession has been received and is pending admin approval. You will not receive a further notification if it is posted."
        )
    except Exception as e:
        logger.error(f"Failed to send moderation message to admin group {ADMIN_GROUP_ID}: {e}")
        await update.message.reply_text(
            "⚠️ Error: Could not submit to the admin group. Please contact the administrator."
        )
        # Revert counter if we fail to notify the admin
        store["next_id"] -= 1
        del store["pending"][pending_id]
        save_store()
        return

# ===== Handlers (Asynchronous Functions) =====

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

        user_alias = get_user_alias(update.effective_user.id) # <-- NEW: Fetch alias
        
        welcome_message = (
            f"👋 *Welcome to the weirdo confession bot!* 🤫\n\n"
            f"Your current nickname(Alias) is: *{user_alias}*\n" # <-- NEW: Display alias
            "This bot allows you to share your thoughts, secrets, and stories completely anonymously in our channel (weirdo confession)....hey am tired of hearing bulshit get smt weird👽\n\n"
            "*Here are the Rules & Guidelines:*\n"
            "1. *Alias:* Use the `/setalias <name>` command to choose a stable nickname for your posts and comments.\n"
            "2. *Anonymity:* Your Telegram user ID is never revealed. Only your chosen alias is displayed.\n"
            "3. *Submission:* Use the `/confess` command, or simply send your message in this chat. It will be sent for review.\n"
            "4. *Review:* All confessions are reviewed by administrators before being posted to the channel.\n"
            "5. *NO Hate Speech:* Submissions must not contain hate speech, bullying, harassment, or discrimination.\n" # <-- NEW RULE
            "6. *Respect Rights:* Do not submit content that violates the rights of any individual, including privacy or intellectual property.\n" # <-- NEW RULE
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
        
    # Basic sanitization (allowing letters, numbers, spaces)
    if not all(c.isalnum() or c.isspace() for c in new_alias):
        await update.message.reply_text("❌ Nickname can only contain letters, numbers, and spaces.")
        return

    # Save the new alias
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
        "1. `/setalias <name>`: Set your persistent nickname/alias.\n" # <-- NEW
        "2. `/confess`: Start submitting your anonymous confession.\n"
        "3. `/feedback`: Send anonymous feedback or suggestions directly to the admins.\n"
        "4. `/start`: Get the detailed welcome message and rules.\n"
        "5. `/help`: Show this command summary.\n"
        "6. `/cancel`: Cancel a pending comment or feedback submission.\n\n"
        "**How to Interact:**\n"
        "Find a confession in the public channel and click the 'Add / View Comments' button to interact."
    )
    
    # Check if the user is an admin to display additional commands
    if update.effective_chat.id == ADMIN_GROUP_ID:
        help_text += (
            "\n\n*Admin Commands (Admin Chat Only):*\n"
            "1. `/pending`: List all confessions awaiting approval.\n"
            "2. `/approve_batch [N]`: Approve and post the next N (max 15) pending confessions.\n" 
            "3. `/reply <id> <message>`: Post an auto-approved anonymous comment to confession `<id>`.\n"
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
        state = context.user_data.get(user_id, {}).get('state')
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
        # Set the state for awaiting feedback
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
    user_alias = get_user_alias(user_id) # <-- NEW: Get alias for comments
    text: str = update.message.text.strip() if update.message.text else ""
    
    if not text:
        await update.message.reply_text("Please send text only for your submission.")
        return

    user_state = context.user_data.get(user_id)
    
    # --- Check for pending reply state (Add Comment) and handle it ---
    if user_state and user_state.get('state') == 'awaiting_reply':
        conf_id_to_reply_to = user_state['conf_id']
        del context.user_data[user_id] # Clear state immediately
        
        conf_key = str(conf_id_to_reply_to)
        
        if conf_key not in store["posted"]:
            await update.message.reply_text("⚠️ Cannot submit comment: The original confession no longer exists.")
            return

        # Auto-Approve Logic: Add directly to store
        reply_id = store['next_id']
        store["next_id"] += 1

        posted_confession = store["posted"][conf_key]
        if "replies" not in posted_confession:
              posted_confession["replies"] = [] # Initialize if somehow missing

        posted_confession["replies"].append({
            "reply_id": reply_id,
            "text": text,
            "user_alias": user_alias, # <-- NEW: Store alias
            "approved_time": update.message.date.astimezone(utc).isoformat(),
            "voters": {}
        })
        save_store()
        
        await update.message.reply_text(
            f"✅ Your comment to Confession #{conf_id_to_reply_to} (as *{user_alias}*) has been posted ",
            parse_mode="Markdown"
        )
        return
    # --------------------------------------

    # --- Check for pending feedback state and handle it ---
    if user_state and user_state.get('state') == 'awaiting_feedback':
        del context.user_data[user_id] # Clear state
        
        # Forward feedback to Admin Group
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
    # --------------------------------------

    # --- Original Confession Submission Logic ---
    await submit_pending_confession(update, context, text)


async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles inline button presses (Admin Approve/Reject/Batch actions OR Public Add/Browse Comment/Vote actions)."""
    query = update.callback_query
    await query.answer() # Acknowledge the press

    action_data = query.data.split("|")
    
    # --- 1. Public Interaction Callbacks (from private chat) ---
    
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
            user_alias = get_user_alias(user_id) # <-- NEW: Get alias for context
            
            context.user_data[user_id] = {'state': 'awaiting_reply', 'conf_id': conf_id}

            response_text = (
                f"** u r submitting a comment to Confession #{conf_id}** (as *{user_alias}*)\n\n" # <-- UPDATED
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
        
        comment_author_alias = target_reply.get('user_alias', 'Anonymous') # <-- NEW
        comment_text = f"*Comment {comment_index}* (by {comment_author_alias}):\n\n{target_reply['text']}" # <-- UPDATED
        
        # Check if the message hasn't been modified by another action (e.g., admin delete)
        if query.message.text.startswith("*Comment"):
            await query.edit_message_text(
                comment_text, 
                parse_mode="Markdown", 
                reply_markup=updated_markup
            )
        
        await query.answer(message_text)
        return

    # Handle Browse Comments: Now sends multiple messages
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
        
        # 1. Edit the original message to an acknowledgment/header
        await query.edit_message_text(
            f"📝 *Displaying {len(replies)} Comments for Confession #{conf_id}* (Scroll Down for the list)", 
            parse_mode="Markdown"
        )
        
        # --- Determine final navigation buttons ---
        channel_message_id = confession_data.get("channel_message_id")
        
        if channel_message_id:
            channel_username = CHANNEL_ID.lstrip('@')
            back_button = InlineKeyboardButton(
                "📢 View Confession in Channel", 
                url=f"https://t.me/{channel_username}/{channel_message_id}"
            )
            final_text = "*_End of Comments List. Click the button below to go directly to the original Confession post._*"
        else:
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
        # ---------------------------------------------------
        
        # 2. Loop and send each comment as a separate message
        if replies:
            for i, reply in enumerate(replies, 1):
                # Ensure a stable reply_id, fall back if missing
                reply_id = reply.get("reply_id", int(data_id) * 1000 + i) 
                
                voters = reply.get('voters', {})
                likes = sum(1 for vote in voters.values() if vote == 'like')
                dislikes = sum(1 for vote in voters.values() if vote == 'dislike')
                
                comment_author_alias = reply.get('user_alias', 'Anonymous') # <-- NEW: Get alias

                # Comment text content
                comment_text = f"*Comment {i}* (by {comment_author_alias}):\n\n{reply['text']}" # <-- UPDATED

                # Comment specific keyboard
                comment_keyboard = [
                    [
                        InlineKeyboardButton(f"👍 Like ({likes})", callback_data=f"vote_comment|{conf_id}|{reply_id}|like"),
                        InlineKeyboardButton(f"👎 Dislike ({dislikes})", callback_data=f"vote_comment|{conf_id}|{reply_id}|dislike"),
                    ]
                ]
                
                # Send the new message for this comment
                await context.bot.send_message(
                    chat_id=user_chat_id,
                    text=comment_text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(comment_keyboard)
                )
                
            # 3. Send a final navigational message after all comments
            await context.bot.send_message(
                chat_id=user_chat_id,
                text=final_text,
                parse_mode="Markdown",
                reply_markup=final_markup
            )

        else:
            # If no replies, edit the original message (A) to inform the user and show navigation
            response_text = f"📝 *Comments for Confession #{conf_id}*\n\n"
            response_text += "_No comments yet. Be the first one to share your thoughts._\n\n"
            
            await query.edit_message_text(
                response_text, 
                parse_mode="Markdown",
                reply_markup=final_markup
            )
        return


    # --- 2. Admin Action Block (Only runs if not a public action) ---
    
    if query.message.chat.id != ADMIN_GROUP_ID:
        # Ignore if not an admin action and not one of the public actions handled above
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
            # 1. Move from pending to posted
            conf = store["pending"].pop(data_id)
            
            temp_conf_data = {
                "text": conf["text"], 
                "user_alias": conf_alias,
                "post_time": update.callback_query.message.date.astimezone(utc).isoformat(), 
                "replies": [], 
                "channel_message_id": None # Will be set after successful post
            }
            store["posted"][str(conf_id)] = temp_conf_data
            save_store()

            # 2. Post to the public channel
            bot_info = await context.bot.get_me()
            bot_username = bot_info.username

            reply_keyboard = [[
                InlineKeyboardButton(
                    "💬 Add / View Comments", 
                    url=f"https://t.me/{bot_username}?start=comment_{conf_id}"
                )
            ]]
            # Updated: Display the alias in the final channel post
            post_text = f"*#{conf_id} Confession* - Posted by: {conf_alias}\n\n{conf['text']}"
            try:
                sent_message = await context.bot.send_message(
                    chat_id=CHANNEL_ID, 
                    text=post_text, 
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(reply_keyboard)
                )
                
                store["posted"][str(conf_id)]["channel_message_id"] = sent_message.message_id
                save_store()
                
                # 3. Success: Update the message in the admin group
                await query.edit_message_text(
                    success_text + f" and Posted as #{conf_id}", 
                    parse_mode="Markdown"
                )

            except Exception as e:
                logger.error(f"Failed to post confession #{conf_id} to channel {CHANNEL_ID}: {e}")
                
                # 3. Failure: Revert the store state
                store["pending"][data_id] = conf
                del store["posted"][str(conf_id)]
                store["next_id"] -= 1       
                save_store()

                # 4. Notify the admin with the error
                error_message = f"❌ *POSTING FAILED* Confession #{conf_id}.\n\nError: The bot could not post to the channel. **Check if the bot is an administrator in {CHANNEL_ID} with the 'Post Messages' permission.**"
                await query.edit_message_text(
                    success_text + f"\n\n{error_message}", 
                    parse_mode="Markdown"
                )
    
    # --- Handle Batch Approval Callback ---
    elif action_data[0] == "approve_batch":
        try:
            batch_size = int(action_data[1])
        except ValueError:
            await query.answer("Invalid batch size.")
            return

        if not store["pending"]:
            await query.edit_message_text(query.message.text + "\n\n⚠️ *No pending items left.*", parse_mode="Markdown")
            return

        approved_count = 0
        
        # Get pending keys sorted by ID (p1, p2, p3...)
        pending_keys = sorted(store["pending"].keys(), key=lambda k: int(k[1:]))

        for pending_id in pending_keys[:batch_size]:
            pending_item = store["pending"].pop(pending_id)
            conf_id = pending_item["id"]
            conf_alias = pending_item["user_alias"]
            
            temp_conf_data = {
                "text": pending_item["text"], 
                "user_alias": conf_alias,
                "post_time": datetime.now(utc).isoformat(), 
                "replies": [], 
                "channel_message_id": None
            }
            store["posted"][str(conf_id)] = temp_conf_data
            
            bot_info = await context.bot.get_me()
            bot_username = bot_info.username

            reply_keyboard = [[
                InlineKeyboardButton(
                    "💬 Add / View Comments", 
                    url=f"https://t.me/{bot_username}?start=comment_{conf_id}"
                )
            ]]
            post_text = f"*#{conf_id} Confession* - Posted by: {conf_alias}\n\n{pending_item['text']}"
            
            try:
                sent_message = await context.bot.send_message(
                    chat_id=CHANNEL_ID, 
                    text=post_text, 
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(reply_keyboard)
                )
                store["posted"][str(conf_id)]["channel_message_id"] = sent_message.message_id
                approved_count += 1
                # Add a short delay to avoid Telegram API rate limits during batch posting
                await asyncio.sleep(0.5) 
            except Exception as e:
                logger.error(f"Failed to post confession #{conf_id} during batch. Stopping batch processing: {e}")
                # Revert: If posting fails, put it back in pending
                del store["posted"][str(conf_id)]
                store["pending"][pending_id] = pending_item 
                break # Stop the batch on the first failure

        save_store()
        
        remaining = len(store["pending"])
        
        await query.edit_message_text(
            f"✅ *Batch Approval Complete!* Approved {approved_count} confessions.\n\n"
            f"There are {remaining} confessions remaining in the queue.",
            parse_mode="Markdown"
        )
        return


@is_admin_chat
async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to list all pending confessions with a batch approval button."""
    pending_items = sorted(store["pending"].values(), key=lambda x: x['id'])
    
    if not pending_items:
        await update.message.reply_text("🎉 No confessions are currently awaiting approval.")
        return

    response_text = f"*Pending Confessions Queue ({len(pending_items)} Total):*\n\n"
    items_to_process = []
    
    for i, item in enumerate(pending_items):
        if i >= 15: # Limit display for readability
            response_text += f"... and {len(pending_items) - i} more.\n"
            break

        pending_id = f"p{item['id']}"
        conf_id = item["id"]
        conf_alias = item["user_alias"]
        items_to_process.append(pending_id)

        response_text += (
            f"**{i+1}. Confession #{conf_id}** (Alias: {conf_alias} | ID: {pending_id})\n"
            f"Content Preview: _{item['text'][:100]}{'...' if len(item['text']) > 100 else ''}_\n\n"
        )
        
    batch_size = len(items_to_process)
    
    # Generate batch approval button if there are items
    if batch_size > 0:
        keyboard = [
            [
                # We send the batch size in callback_data, the handler will sort and pick the oldest N
                InlineKeyboardButton(f"✅ Approve Next {batch_size}", 
                                     callback_data=f"approve_batch|{batch_size}") 
            ]
        ]
        
        await update.message.reply_text(
            response_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

@is_admin_chat
async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to post an auto-approved anonymous comment using the ADMIN_ALIAS."""
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/reply <confession_id> <message>`. Example: `/reply 10 That's so funny!`")
        return

    try:
        conf_id = int(context.args[0])
        conf_key = str(conf_id)
        reply_text = " ".join(context.args[1:])
    except ValueError:
        await update.message.reply_text("The confession ID must be a number.")
        return

    if conf_key not in store["posted"]:
        await update.message.reply_text(f"Confession #{conf_id} not found in posted confessions.")
        return

    # Auto-Approve Logic (using admin alias)
    reply_id = store['next_id']
    store["next_id"] += 1

    posted_confession = store["posted"][conf_key]
    
    posted_confession["replies"].append({
        "reply_id": reply_id,
        "text": reply_text,
        "user_alias": ADMIN_ALIAS, 
        "approved_time": datetime.now(utc).isoformat(),
        "voters": {}
    })
    save_store()
    
    await update.message.reply_text(
        f"✅ Admin comment posted to Confession #{conf_id} as *{ADMIN_ALIAS}*.",
        parse_mode="Markdown"
    )

@is_admin_chat
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to show comment and vote statistics."""
    total_confessions = len(store["posted"])
    
    if total_confessions == 0:
        await update.message.reply_text("No confessions have been posted yet to calculate statistics.")
        return

    total_comments = 0
    total_likes = 0
    total_dislikes = 0
    
    # Calculate overall stats
    for conf_id, conf in store["posted"].items():
        for reply in conf.get("replies", []):
            total_comments += 1
            voters = reply.get('voters', {})
            total_likes += sum(1 for vote in voters.values() if vote == 'like')
            total_dislikes += sum(1 for vote in voters.values() if vote == 'dislike')
            
    stats_text = (
        "*Confession Bot Statistics*\n\n"
        f"**Confessions Posted:** {total_confessions}\n"
        f"**Total Comments:** {total_comments}\n"
        f"**Total Likes:** {total_likes}\n"
        f"**Total Dislikes:** {total_dislikes}\n\n"
    )

    # Top 5 most commented confessions
    commented = []
    for conf_id, conf in store["posted"].items():
        commented.append((conf_id, len(conf.get("replies", []))))
    
    commented.sort(key=lambda x: x[1], reverse=True)
    
    stats_text += "**🏆 Top 5 Most Commented Confessions**\n"
    if commented[0][1] > 0:
        for i, (conf_id, count) in enumerate(commented[:5], 1):
            stats_text += f"{i}. Confession #{conf_id}: {count} comments\n"
    else:
        stats_text += "_No comments yet._\n"
        
    await update.message.reply_text(stats_text, parse_mode="Markdown")

@is_admin_chat
async def delete_confession_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Permanently deletes a posted confession and its channel post."""
    if len(context.args) != 1:
        await update.message.reply_text("Usage: `/deleteconfession <id>`. Example: `/deleteconfession 10`")
        return

    try:
        conf_id = int(context.args[0])
        conf_key = str(conf_id)
    except ValueError:
        await update.message.reply_text("The confession ID must be a number.")
        return

    if conf_key not in store["posted"]:
        await update.message.reply_text(f"Confession #{conf_id} not found in posted confessions. It might have been deleted already.")
        return

    conf_data = store["posted"][conf_key]
    channel_message_id = conf_data.get("channel_message_id")

    # 1. Delete from channel
    if channel_message_id:
        try:
            await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=channel_message_id)
            channel_status = "✅ Channel post deleted."
        except Exception as e:
            logger.error(f"Failed to delete channel message {channel_message_id}: {e}")
            channel_status = "⚠️ Failed to delete channel post (it may have been manually deleted or the bot lacks permission)."
    else:
        channel_status = "ℹ️ No channel message ID found."
        
    # 2. Delete from data store
    del store["posted"][conf_key]
    save_store()

    await update.message.reply_text(
        f"🚨 *Confession #{conf_id} Deleted*\n"
        f"Data removed from store.\n{channel_status}",
        parse_mode="Markdown"
    )

@is_admin_chat
async def delete_comment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deletes a specific comment by confession ID and comment index (1-based)."""
    if len(context.args) != 2:
        await update.message.reply_text("Usage: `/deletecomment <conf_id> <comment_index>`. Example: `/deletecomment 10 3` (Deletes the 3rd comment on Confession #10)")
        return

    try:
        conf_id = int(context.args[0])
        comment_index = int(context.args[1])
        conf_key = str(conf_id)
    except ValueError:
        await update.message.reply_text("Both Confession ID and Comment Index must be numbers.")
        return
    
    if conf_key not in store["posted"]:
        await update.message.reply_text(f"Confession #{conf_id} not found.")
        return
        
    replies = store["posted"][conf_key].get("replies", [])
    
    if comment_index < 1 or comment_index > len(replies):
        await update.message.reply_text(f"Invalid comment index. Confession #{conf_id} only has {len(replies)} comments.")
        return

    # Comment index is 1-based, list index is 0-based
    deleted_comment_data = replies.pop(comment_index - 1)
    save_store()
    
    alias = deleted_comment_data.get('user_alias', 'Anonymous')
    text_preview = deleted_comment_data['text'][:50] + '...'

    await update.message.reply_text(
        f"✅ Deleted Comment {comment_index} from Confession #{conf_id}.\n"
        f"Preview (by {alias}): _{text_preview}_"
    )

@is_admin_chat
async def reset_counter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """DANGER ZONE: Clears ALL data and resets the counter."""
    keyboard = [
        [
            InlineKeyboardButton("I AM SURE, RESET ALL DATA", callback_data="confirm_reset|1"),
        ],
        [
            InlineKeyboardButton("Cancel", callback_data="confirm_reset|0"),
        ]
    ]

    await update.message.reply_text(
        "🛑 *DANGER ZONE* 🛑\n\n"
        "Are you absolutely sure you want to reset the bot data?\n"
        "This will permanently delete ALL pending and posted confessions, comments, votes, and user aliases.\n"
        "This action CANNOT be undone.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def confirm_reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the confirmation callback for the reset command."""
    query = update.callback_query
    await query.answer()

    if query.message.chat.id != ADMIN_GROUP_ID:
        await query.edit_message_text("🚫 This action is restricted to the Admin Group.")
        return

    action_data = query.data.split("|")
    confirmation = action_data[1]

    if confirmation == '1':
        global store
        # Reset the store to initial state
        store = {"next_id": 1, "pending": {}, "posted": {}, "user_profiles": {}}
        save_store()
        await query.edit_message_text(
            f"✅ *DATA RESET COMPLETE* by {update.effective_user.first_name}.\n"
            "All data has been cleared, and the next confession ID will be #1.",
            parse_mode="Markdown"
        )
    else:
        await query.edit_message_text("❌ Data reset cancelled.")


async def set_bot_commands(application: Application) -> None:
    """Sets the list of available commands in the Telegram client."""
    commands = [
        BotCommand("start", "Welcome message and rules."),
        BotCommand("confess", "Submit an anonymous confession."),
        BotCommand("setalias", "Set your anonymous nickname/alias."),
        BotCommand("feedback", "Send anonymous feedback to admins."),
        BotCommand("cancel", "Cancel pending comment or feedback submission."),
        BotCommand("help", "Show all commands."),
    ]
    await application.bot.set_my_commands(commands)

def main() -> None:
    """Start the bot."""
    load_store() # Load data on startup

    # Create the Application and pass it your bot's token.
    application = Application.builder().token(BOT_TOKEN).build()

    # --- Public Handlers (Private Chat) ---
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("confess", confess_command))
    application.add_handler(CommandHandler("feedback", feedback_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("setalias", set_alias_command))
    
    # Text message handler (for actual confession, comment, or feedback submission)
    # Ensure this only runs for private chats and ignores commands
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_confession)
    )

    # --- Admin Handlers (Admin Chat) ---
    application.add_handler(CommandHandler("pending", pending_command))
    # approve_batch logic is handled by handle_callbacks
    application.add_handler(CommandHandler("reply", reply_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("deleteconfession", delete_confession_command))
    application.add_handler(CommandHandler("deletecomment", delete_comment_command))
    application.add_handler(CommandHandler("reset_counter", reset_counter_command))
    
    # --- Callback Handlers (Admin/Public) ---
    application.add_handler(CallbackQueryHandler(handle_callbacks, pattern=r"^(approve|reject|add_comment|browse_comments|vote_comment|approve_batch)\|.*"))
    application.add_handler(CallbackQueryHandler(confirm_reset_callback, pattern=r"^confirm_reset\|.*"))
    
    # Run bot commands setup
    application.job_queue.run_once(set_bot_commands, 0)
    
    # Run the bot until the user presses Ctrl-C
    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()