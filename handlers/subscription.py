import logging
from datetime import datetime, timedelta
from typing import Dict, List
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import db, get_pool
import flow_state
from keyboards import keyboard_gen
from config import EMOJI_COLORS
from paystack import paystack
from utils import is_owner
from utils import safe_edit_message

def _clone_id(context) -> int:
    """0 for the main bot, else the running clone's id — tier, quota, and
    subscription lookups must be scoped to this so they never leak across
    the main bot and other clones."""
    clone_config = context.bot_data.get("clone_config")
    return clone_config.get("clone_id") if clone_config else 0

logger = logging.getLogger(__name__)

SUBSCRIPTION_PRICE = 10  # GHS per month
SUBSCRIPTION_PLAN_ID = "ai_features_monthly"

async def handle_subscribe_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle subscription to AI features"""
    user_id = update.effective_user.id
    
    # Check if user already has subscription
    user = await db.get_user(user_id, clone_id=_clone_id(context))
    
    if user and user.get("subscription_status") == "active":
        expiry = user.get("subscription_expiry", "Unknown")
        await update.message.reply_text(
            f"Yo, you already got AI unlocked! 🎮\n\n"
            f"Your subscription expires on: {expiry}\n\n"
            f"Need AI stuff? Hit /ai_recommend or /ai_summary",
            reply_markup=keyboard_gen.main_menu()
        )
        return

    price = await db.get_clone_price(_clone_id(context), "ai_subscription")

    keyboard = [
        [InlineKeyboardButton(f"📲 Pay with Paystack ({price:g} GHS/month)", callback_data="pay_paystack_ai")],
        [InlineKeyboardButton("❌ Nah, I'm good", callback_data="main_menu")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "AI Features Subscription 🤖\n\n"
        "Unlock:\n"
        "• Anime recommendations personalized for you\n"
        "• Gen Z AI-powered anime summaries\n"
        "• Priority AI processing\n\n"
        f"Only {price:g} GHS/month! Cancel anytime.\n\n"
        "How you tryna pay?",
        reply_markup=reply_markup
    )


async def handle_ai_recommendation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle AI anime recommendation request"""
    user_id = update.effective_user.id
    
    # Check subscription (founder / clone owner bypass everything below)
    user = await db.get_user(user_id, clone_id=_clone_id(context))
    owner = is_owner(user_id, context)

    if not owner and (not user or user.get("subscription_status") != "active"):
        await update.message.reply_text(
            "Nah fam, this feature requires AI subscription! 🔒\n\n"
            "Subscribe for just 10 GHS/month!\n\n"
            "Hit /subscribe to unlock this",
            reply_markup=keyboard_gen.main_menu()
        )
        return

    # Check expiry
    if not owner and user.get("subscription_expiry") and datetime.now() > datetime.fromisoformat(str(user.get("subscription_expiry"))):
        await update.message.reply_text(
            "Your subscription expired bruh! 😅\n"
            "Renew it to keep using AI features\n\n"
            "Hit /subscribe to renew!",
            reply_markup=keyboard_gen.main_menu()
        )
        return
    
    await update.message.reply_text(
        "Drop your anime preferences! What kind of anime hits different for you?\n\n"
        "E.g.: Action, romance, comedy vibes idk"
    )
    context.user_data["awaiting_preference"] = True
    await flow_state.sync(context, update.effective_user.id, _clone_id(context), flow="ai_preference")


async def handle_ai_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle AI anime summary request"""
    user_id = update.effective_user.id
    
    # Check subscription (founder / clone owner bypass)
    user = await db.get_user(user_id, clone_id=_clone_id(context))

    if not is_owner(user_id, context) and (not user or user.get("subscription_status") != "active"):
        await update.message.reply_text(
            "Yo, need subscription for summaries! 🔐\n"
            "Only 10 GHS/month though!\n\n"
            "Hit /subscribe to unlock",
            reply_markup=keyboard_gen.main_menu()
        )
        return
    
    await update.message.reply_text(
        "Search for an anime and I'll give you the gen z summary! 💯"
    )
    context.user_data["awaiting_summary_title"] = True
    await flow_state.sync(context, update.effective_user.id, _clone_id(context), flow="ai_summary")


async def activate_subscription(user_id: int, months: int = 1, clone_id: int = 0) -> bool:
    """Activate subscription for user, scoped to clone_id (0 = main bot) —
    a subscription bought on one bot must not unlock another bot."""
    try:
        pool = await get_pool()
        expiry = datetime.now() + timedelta(days=30 * months)

        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_clone_status (user_id, clone_id)
                VALUES ($1, $2)
                ON CONFLICT (user_id, clone_id) DO NOTHING
            """, user_id, clone_id)
            await conn.execute(
                "UPDATE user_clone_status SET subscription_status = $1, subscription_expiry = $2 "
                "WHERE user_id = $3 AND clone_id = $4",
                "active", expiry, user_id, clone_id
            )
        return True
    except Exception as e:
        logger.error(f"[v0] Error activating subscription: {e}")
        return False


async def deactivate_subscription(user_id: int, clone_id: int = 0) -> bool:
    """Deactivate subscription for user, scoped to clone_id (0 = main bot)."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE user_clone_status SET subscription_status = $1 WHERE user_id = $2 AND clone_id = $3",
                "inactive", user_id, clone_id
            )
        return True
    except Exception as e:
        logger.error(f"[v0] Error deactivating subscription: {e}")
        return False


async def get_active_subscribers(clone_id: int = 0) -> List[Dict]:
    """Get all active subscribers for admin, scoped to clone_id (0 = main
    bot) — a clone owner's subscriber list must not include the main bot's
    or another clone's subscribers."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT u.user_id, u.username, s.subscription_expiry FROM user_clone_status s "
                "JOIN users u ON u.user_id = s.user_id "
                "WHERE s.subscription_status = 'active' AND s.clone_id = $1 "
                "ORDER BY s.subscription_expiry DESC",
                clone_id
            )
        
        subscribers = []
        for row in rows:
            subscribers.append({
                "user_id": row["user_id"],
                "username": row["username"],
                "expiry": row["subscription_expiry"]
            })
        
        return subscribers
    except Exception as e:
        logger.error(f"[v0] Error fetching subscribers: {e}")
        return []


async def get_subscription_revenue() -> Dict:
    """Get subscription revenue stats for admin"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            active_result = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE subscription_status = 'active'"
            )
            expired_result = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE subscription_status = 'expired'"
            )
        
        active_count = active_result or 0
        expired_count = expired_result or 0
        monthly_revenue = active_count * SUBSCRIPTION_PRICE
        
        return {
            "active": active_count,
            "expired": expired_count,
            "monthly_revenue_ghs": monthly_revenue,
            "price_per_month": SUBSCRIPTION_PRICE
        }
    except Exception as e:
        logger.error(f"[v0] Error calculating revenue: {e}")
        return {}


async def handle_pay_paystack_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Selar payment for AI subscription"""
    query = update.callback_query
    user_id = update.effective_user.id
    email = f"user_{user_id}@animebot.com"
    clone_id = _clone_id(context)
    price = await db.get_clone_price(clone_id, "ai_subscription")

    # Initialize Selar payment
    payment_result = paystack.initialize_payment(
        email,
        int(price * 100),  # Convert GHS to pesewas
        user_id,
        f"AI_Sub_{user_id}",
        payment_type="ai_subscription",
        extra_metadata={"clone_id": clone_id}
    )
    
    if payment_result and payment_result.get("status") == "success":
        payment_reference = payment_result.get("reference")
        payment_link = payment_result.get("authorization_url")
        
        # Persisted server-side (not context.user_data) so the 'I've Paid —
        # Verify' tap - handled by manual_payments.handle_user_verification,
        # which may run on a different serverless instance - can still find it.
        await db.create_pending_payment_intent(payment_reference, user_id, "ai_subscription")
        
        payment_text = f"""
{EMOJI_COLORS.get('success', '✅')} **AI Subscription Payment**

Click the link below to pay GHS {price:g}.00 via Selar:

[Pay Now]({payment_link})

After payment, click "I've Paid — Verify" below. The admin will confirm your payment and activate your subscription.
"""
        
        await safe_edit_message(query, 
            payment_text,
            reply_markup=keyboard_gen.subscription_verify_keyboard(),
            parse_mode="Markdown"
        )
    else:
        await query.answer("Failed to initialize payment. Please try again.", show_alert=True)



