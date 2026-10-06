"""
Admin Configuration Handler
Time-based settings, pricing, and feature toggles
"""

from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from config import ADMIN_ID, EMOJI_COLORS, BOT_TOKEN, PAYSTACK_SECRET_KEY, DATABASE_URL, PUBLIC_BASE_URL, LOG_GROUP_ID
from modules import superbot_adapter, botstore_adapter
from database import db
from paystack import paystack
from utils import safe_edit_message

def _clone_id(context) -> int:
    """0 for the main bot, else the running clone's id — tier, quota, and
    subscription lookups must be scoped to this so they never leak across
    the main bot and other clones."""
    clone_config = context.bot_data.get("clone_config")
    return clone_config.get("clone_id") if clone_config else 0

async def show_config_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin configuration panel"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await query.answer("Unauthorized", show_alert=True)
        return
    
    config_text = f"""
⚙️ **ADMIN CONFIGURATION**

**BotStore Settings**
├ Featured Price: GHS {botstore_adapter.ConfigCache.FEATURED_PRICE_GHS}
├ Featured Days: {botstore_adapter.ConfigCache.FEATURED_DAYS}
├ Premium Price: GHS {botstore_adapter.ConfigCache.PREMIUM_PRICE_GHS}
└ Free Bot Limit: {botstore_adapter.ConfigCache.FREE_BOT_LIMIT}

**SuperBot Settings**
├ Pro Tier Price: GHS {superbot_adapter.ConfigCache.TIER_PRO['price']}
├ Elite Tier Price: GHS {superbot_adapter.ConfigCache.TIER_ELITE['price']}
├ Referral Reward: {superbot_adapter.ConfigCache.REFERRAL_REWARD_COINS} coins
├ Crypto Alert Check: Every {superbot_adapter.ConfigCache.CRYPTO_ALERT_CHECK_INTERVAL_MINUTES}m
└ Leaderboard Update: Every {superbot_adapter.ConfigCache.LEADERBOARD_UPDATE_INTERVAL_HOURS}h

Pick a setting to edit:
"""
    
    keyboard = [
        [
            InlineKeyboardButton("📦 BotStore", callback_data="cfg_botstore"),
            InlineKeyboardButton("⭐ SuperBot", callback_data="cfg_superbot")
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
    ]
    
    await safe_edit_message(query, 
        config_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def handle_config_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route config edit callbacks"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await query.answer("Unauthorized", show_alert=True)
        return
    
    if query.data == "cfg_botstore":
        await show_botstore_config(update, context)
    elif query.data == "cfg_superbot":
        await show_superbot_config(update, context)
    elif query.data.startswith("edit_"):
        await handle_edit_field(update, context)

async def show_botstore_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """BotStore configuration editor"""
    query = update.callback_query
    
    text = f"""
📦 **BotStore Configuration**

**Current Settings:**
├ Featured Price: GHS {botstore_adapter.ConfigCache.FEATURED_PRICE_GHS}
├ Featured Duration: {botstore_adapter.ConfigCache.FEATURED_DAYS} days
├ Premium Price: GHS {botstore_adapter.ConfigCache.PREMIUM_PRICE_GHS}
└ Free Bot Limit: {botstore_adapter.ConfigCache.FREE_BOT_LIMIT} listings

Tap a field to edit:
"""
    
    keyboard = [
        [InlineKeyboardButton("💰 Featured Price", callback_data="edit_featured_price")],
        [InlineKeyboardButton("📅 Featured Days", callback_data="edit_featured_days")],
        [InlineKeyboardButton("💎 Premium Price", callback_data="edit_premium_price")],
        [InlineKeyboardButton("📊 Free Bot Limit", callback_data="edit_free_bot_limit")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin_config")]
    ]
    
    await safe_edit_message(query, 
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def show_superbot_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """SuperBot configuration editor"""
    query = update.callback_query
    
    text = f"""
⭐ **SuperBot Configuration**

**Pricing:**
├ Pro Tier: GHS {superbot_adapter.ConfigCache.TIER_PRO['price']}/month
└ Elite Tier: GHS {superbot_adapter.ConfigCache.TIER_ELITE['price']}/month

**Rewards & Intervals:**
├ Referral Reward: {superbot_adapter.ConfigCache.REFERRAL_REWARD_COINS} coins
├ Crypto Alert Check: Every {superbot_adapter.ConfigCache.CRYPTO_ALERT_CHECK_INTERVAL_MINUTES} min
└ Leaderboard Update: Every {superbot_adapter.ConfigCache.LEADERBOARD_UPDATE_INTERVAL_HOURS} hour

Tap a field to edit:
"""
    
    keyboard = [
        [InlineKeyboardButton("💚 Pro Price", callback_data="edit_pro_price")],
        [InlineKeyboardButton("💎 Elite Price", callback_data="edit_elite_price")],
        [InlineKeyboardButton("🎁 Referral Reward", callback_data="edit_referral_reward")],
        [InlineKeyboardButton("⏰ Crypto Alert Interval", callback_data="edit_crypto_interval")],
        [InlineKeyboardButton("📊 Leaderboard Interval", callback_data="edit_leaderboard_interval")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin_config")]
    ]
    
    await safe_edit_message(query, 
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def handle_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle edit field - prompt for new value"""
    query = update.callback_query
    field = query.data.replace("edit_", "")
    
    context.user_data["editing_config_field"] = field
    
    prompts = {
        "featured_price": "Enter new featured listing price (GHS):",
        "featured_days": "Enter featured listing duration (days):",
        "premium_price": "Enter premium tier price (GHS):",
        "free_bot_limit": "Enter max free bot listings:",
        "pro_price": "Enter Pro tier price (GHS/month):",
        "elite_price": "Enter Elite tier price (GHS/month):",
        "referral_reward": "Enter referral reward (coins):",
        "crypto_interval": "Enter crypto alert check interval (minutes):",
        "leaderboard_interval": "Enter leaderboard update interval (hours):",
    }
    
    await safe_edit_message(query, 
        f"⏳ {prompts.get(field, 'Enter value:')}\n\nReply with a number:"
    )

async def handle_config_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process config value input and save to database"""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    
    field = context.user_data.get("editing_config_field")
    if not field:
        return
    
    try:
        value = int(update.message.text.strip())
        
        # Save to database
        saved = False
        
        # BotStore settings
        if field == "featured_price":
            saved = await db.update_config("botstore_featured_price", value)
            botstore_adapter.ConfigCache.FEATURED_PRICE_GHS = value
        elif field == "featured_days":
            saved = await db.update_config("botstore_featured_days", value)
            botstore_adapter.ConfigCache.FEATURED_DAYS = value
        elif field == "premium_price":
            saved = await db.update_config("botstore_premium_price", value)
            botstore_adapter.ConfigCache.PREMIUM_PRICE_GHS = value
        elif field == "free_bot_limit":
            saved = await db.update_config("botstore_free_bot_limit", value)
            botstore_adapter.ConfigCache.FREE_BOT_LIMIT = value
        
        # SuperBot settings
        elif field == "pro_price":
            saved = await db.update_config("superbot_pro_price", value)
            superbot_adapter.ConfigCache.TIER_PRO["price"] = value
        elif field == "elite_price":
            saved = await db.update_config("superbot_elite_price", value)
            superbot_adapter.ConfigCache.TIER_ELITE["price"] = value
        elif field == "referral_reward":
            saved = await db.update_config("superbot_referral_reward", value)
            superbot_adapter.ConfigCache.REFERRAL_REWARD_COINS = value
        elif field == "crypto_interval":
            saved = await db.update_config("superbot_crypto_interval", value)
            superbot_adapter.ConfigCache.CRYPTO_ALERT_CHECK_INTERVAL_MINUTES = value
        elif field == "leaderboard_interval":
            saved = await db.update_config("superbot_leaderboard_interval", value)
            superbot_adapter.ConfigCache.LEADERBOARD_UPDATE_INTERVAL_HOURS = value
        
        context.user_data.pop("editing_config_field", None)
        
        status = "✅ SAVED" if saved else "⚠️ Updated (cache only)"
        await update.message.reply_text(
            f"{status}\n"
            f"Field: {field}\n"
            f"New value: {value}\n\n"
            f"Changes are persistent in the database."
        )
    
    except ValueError:
        await update.message.reply_text(
            f"{EMOJI_COLORS.get('error', '❌')} Invalid input. Please enter a number."
        )

async def cmd_envcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: shows which required env vars this RUNNING process actually
    sees (masked), so you can tell a Vercel variable typo/missing-redeploy
    apart from a real Selar rejection without digging through logs."""
    if update.effective_user.id != ADMIN_ID:
        return

    def mask(v):
        if not v:
            return "❌ NOT SET"
        return f"✅ set ({v[:6]}…{v[-4:]}, {len(v)} chars)" if len(v) > 12 else "✅ set (short value)"

    lines = [
        "🔧 *Environment check* (this running deployment):",
        f"BOT_TOKEN: {mask(BOT_TOKEN if BOT_TOKEN != 'your_token_here' else '')}",
        f"ADMIN_ID: {'✅ set (' + str(ADMIN_ID) + ')' if ADMIN_ID else '❌ NOT SET'}",
        f"DATABASE_URL: {mask(DATABASE_URL)}",
        f"PAYSTACK_SECRET_KEY: {mask(PAYSTACK_SECRET_KEY)}",
        f"PAYSTACK_SECRET_KEY: {mask(PAYSTACK_SECRET_KEY)}",
        f"PUBLIC_BASE_URL: {'✅ ' + PUBLIC_BASE_URL if PUBLIC_BASE_URL else '❌ NOT SET'}",
        f"LOG_GROUP_ID: {'✅ set (' + str(LOG_GROUP_ID) + ')' if LOG_GROUP_ID else '❌ NOT SET (admin event logging disabled)'}",
    ]
    lines.append(
        "\nIf PAYSTACK_SECRET_KEY shows NOT SET here but you *do* see it in "
        "Vercel → Settings → Environment Variables, the running deployment "
        "just hasn't picked it up yet — trigger a fresh redeploy (Vercel only "
        "injects vars set *before* the deploy starts)."
    )
    lines.append(
        "\nTip: use /testlog to check LOG_GROUP_ID actually works right now, "
        "instead of waiting for a real event."
    )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_getchatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: run this inside the group you want to use as LOG_GROUP_ID
    to get its chat ID."""
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        f"This chat's ID: `{update.effective_chat.id}`", parse_mode="Markdown"
    )


async def cmd_testlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: fires one real log message right now and reports back
    success/failure directly, instead of waiting for a real event and hoping
    it shows up in the group."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not LOG_GROUP_ID:
        await update.message.reply_text(
            "❌ LOG_GROUP_ID is not set on this running deployment. "
            "Set it in Vercel and redeploy, then try again."
        )
        return
    try:
        await context.bot.send_message(
            LOG_GROUP_ID,
            f"🧪 *Test log entry* triggered by admin at {datetime.now().strftime('%H:%M:%S UTC')}",
            parse_mode="Markdown"
        )
        await update.message.reply_text(
            f"✅ Sent a test message to LOG_GROUP_ID ({LOG_GROUP_ID}). "
            f"Go check that group now — if it's not there, the bot isn't actually "
            f"able to post there even though it looked configured."
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Failed to send to LOG_GROUP_ID ({LOG_GROUP_ID}).\n"
            f"Error: {e}\n\n"
            f"Most common causes: the bot isn't an admin in that group, the bot "
            f"was removed/left the group, or LOG_GROUP_ID doesn't match this group's "
            f"actual chat ID (group chat IDs are negative numbers, e.g. -100123456789)."
        )


async def log_event(bot, message: str):
    """Fire-and-forget: mirror an admin-relevant event (new submission, payment,
    etc.) to LOG_GROUP_ID if configured. Safe to call even when it isn't."""
    if not LOG_GROUP_ID:
        return
    try:
        await bot.send_message(LOG_GROUP_ID, message, parse_mode="Markdown")
    except Exception as e:
        print(f"[v0] log_event failed: {e}")


async def cmd_setpremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: directly grants BotStore Premium to a user ID, with no
    payment record required. Use this for the account owner, manual/offline
    arrangements, or anyone the Selar webhook couldn't reach.
    Usage: /setpremium <user_id>
    """
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/setpremium <user_id>`\n"
            "Don't know a user's numeric ID? Have them run /start on the bot, "
            "or check /getchatid in a DM with them.",
            parse_mode="Markdown"
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(f"{EMOJI_COLORS.get('error', '❌')} That doesn't look like a numeric user ID.")
        return

    success = await db.set_premium_tier(target_id, clone_id=_clone_id(context))
    if success:
        await update.message.reply_text(f"{EMOJI_COLORS.get('success', '✅')} User `{target_id}` upgraded to Premium.", parse_mode="Markdown")
        try:
            await context.bot.send_message(
                target_id,
                "🚀 *You're Premium now!*\nYour BotStore listing limit has been removed — add as many as you like.",
                parse_mode="Markdown"
            )
        except Exception:
            pass  # user may have blocked the bot or never started a DM — not fatal
    else:
        await update.message.reply_text(f"{EMOJI_COLORS.get('error', '❌')} Failed to set premium tier. Check logs.")


async def cmd_confirmpay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: manually verify a Selar reference and activate whatever
    it was for (bot_clone, ai_subscription, botstore_premium), for cases where
    the webhook didn't fire or you want to double-check before it does.
    Usage: /confirmpay <reference>
    """
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: `/confirmpay <reference>`", parse_mode="Markdown")
        return

    reference = context.args[0]
    result = paystack.verify_payment(reference)

    if result.get("status") != "success":
        await update.message.reply_text(
            f"⚠️ Selar does not confirm this reference as a successful payment.\n"
            f"Status: {result.get('status')}\n\n"
            f"If you're certain it was paid (manual/offline), use /setpremium directly instead."
        )
        return

    metadata = result.get("metadata", {}) or {}
    payment_type = metadata.get("type")
    user_id = metadata.get("user_id")

    if not user_id:
        await update.message.reply_text(f"{EMOJI_COLORS.get('error', '❌')} No user_id found in this payment's metadata.")
        return

    user_id = int(user_id)

    if payment_type == "bot_clone":
        await db.mark_clone_payment_paid(reference)
        await update.message.reply_text(f"{EMOJI_COLORS.get('success', '✅')} Confirmed — clone payment `{reference}` marked paid.", parse_mode="Markdown")

    elif payment_type == "ai_subscription":
        from handlers.subscription import activate_subscription
        await activate_subscription(user_id, months=1, clone_id=_clone_id(context))
        await update.message.reply_text(f"{EMOJI_COLORS.get('success', '✅')} Confirmed — AI subscription activated for `{user_id}`.", parse_mode="Markdown")

    elif payment_type == "botstore_premium":
        await db.set_premium_tier(user_id, clone_id=_clone_id(context))
        await update.message.reply_text(f"{EMOJI_COLORS.get('success', '✅')} Confirmed — BotStore Premium activated for `{user_id}`.", parse_mode="Markdown")

    else:
        await update.message.reply_text(f"⚠️ Payment verified but has an unrecognized type: `{payment_type}`. No action taken.", parse_mode="Markdown")
