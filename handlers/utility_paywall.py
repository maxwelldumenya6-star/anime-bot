"""
Shared paywall for the "🤖 AI Chat" and "⬇️ Download" tools.

Rule (per product decision): every user gets UTILITY_FREE_USES free goes on
EACH feature (tracked separately — using up your AI Chat free uses doesn't
touch your Download free uses, and vice versa). After that, ONE subscription
— UTILITY_SUB_FEE_GHS / UTILITY_SUB_DAYS days — unlocks BOTH features
together for the rest of the period. There is no separate "AI Chat plan" and
"Download plan" — paying once covers both.

Reuses the existing Selar initialize/verify pattern from
handlers/clone_bot.py, and logs to the generic payment_logs table rather than
building new payment plumbing.
"""

import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import db
from paystack import paystack
from config import EMOJI_COLORS, UTILITY_SUB_FEE_GHS, UTILITY_SUB_DAYS, UTILITY_FREE_USES
from manual_payments import request_review
from utils import safe_edit_message

def _clone_id(context) -> int:
    """0 for the main bot, else the running clone's id — tier, quota, and
    subscription lookups must be scoped to this so they never leak across
    the main bot and other clones."""
    clone_config = context.bot_data.get("clone_config")
    return clone_config.get("clone_id") if clone_config else 0

logger = logging.getLogger(__name__)

FEATURE_LABELS = {
    "ai_chat": "🤖 AI Chat",
    "download": "⬇️ Download",
}

_USES_COLUMN = {
    "ai_chat": "free_ai_chat_uses",
    "download": "free_download_uses",
}


def _is_subscription_active(usage: dict) -> bool:
    if not usage or usage.get("utility_sub_status") != "active":
        return False
    expiry = usage.get("utility_sub_expiry")
    if not expiry:
        return False
    if isinstance(expiry, str):
        try:
            expiry = datetime.fromisoformat(expiry)
        except ValueError:
            return False
    return datetime.now() <= expiry


async def _ensure_user_row(update: Update):
    """Best-effort upsert so free-use counters/subscription state persist
    even for a user whose first-ever interaction is tapping straight into
    AI Chat / Download (i.e. they never ran /start)."""
    user = update.effective_user
    if not user:
        return
    try:
        await db.add_user(user.id, user.username or "Anonymous", user.first_name or "User")
    except Exception as e:
        logger.error(f"[v0] utility_paywall: could not ensure user row for {user.id}: {e}")


async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE, feature: str) -> tuple[bool, dict]:
    """
    Returns (allowed, usage). Does NOT consume a free use — call
    consume_free_use() only once the feature actually runs, so e.g. entering
    waiting mode and then /cancel-ing out doesn't burn a free use.
    """
    await _ensure_user_row(update)
    user_id = update.effective_user.id
    usage = await db.get_utility_usage(user_id, clone_id=_clone_id(context))

    if _is_subscription_active(usage):
        return True, usage

    used = usage.get(_USES_COLUMN[feature], 0) or 0
    return used < UTILITY_FREE_USES, usage


async def consume_free_use(user_id: int, context: ContextTypes.DEFAULT_TYPE, feature: str):
    """Call this once, right when the feature actually runs (not just when
    entering waiting mode), unless the user has an active subscription."""
    usage = await db.get_utility_usage(user_id, clone_id=_clone_id(context))
    if _is_subscription_active(usage):
        return
    if feature == "ai_chat":
        await db.increment_free_ai_chat_uses(user_id, clone_id=_clone_id(context))
    else:
        await db.increment_free_download_uses(user_id, clone_id=_clone_id(context))


def _paywall_keyboard(price: float = UTILITY_SUB_FEE_GHS) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Subscribe – {price:g} GHS / 2 months", callback_data="pay_utility_sub")],
        [InlineKeyboardButton("⬅️ Back", callback_data="m_tools")],
    ])


async def send_paywall_message(update: Update, context: ContextTypes.DEFAULT_TYPE, feature: str):
    """Shown once a user is out of free uses on `feature` and has no active
    subscription. Works whether triggered by a button tap (callback_query)
    or a typed command (update.message)."""
    label = FEATURE_LABELS.get(feature, feature)
    price = await db.get_clone_price(_clone_id(context), "utility_sub_fee")
    text = (
        f"🔒 **{label} — free uses used up**\n\n"
        f"You've used your {UTILITY_FREE_USES} free uses for {label}.\n\n"
        f"Subscribe for {price:g} GHS / 2 months to unlock **both** "
        f"AI Chat and Download, unlimited, for the whole period.\n\n"
        f"Tap below to pay with Selar."
    )
    query = update.callback_query
    if query:
        await safe_edit_message(query, text, reply_markup=_paywall_keyboard(price), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=_paywall_keyboard(price), parse_mode="Markdown")


async def handle_payment_initiation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """callback_data == 'pay_utility_sub'"""
    query = update.callback_query
    user_id = update.effective_user.id
    email = f"user_{user_id}@animebot.com"
    clone_id = _clone_id(context)
    price = await db.get_clone_price(clone_id, "utility_sub_fee")

    payment_result = paystack.initialize_payment(
        email,
        int(price * 100),  # GHS -> pesewas
        user_id,
        f"UtilitySub_{user_id}",
        payment_type="utility_subscription",
        extra_metadata={"clone_id": clone_id},
    )

    if payment_result and payment_result.get("status") == "success":
        reference = payment_result.get("reference")
        payment_link = payment_result.get("authorization_url")

        await db.log_payment(user_id, price, reference, status="pending")
        # Persisted server-side (not context.user_data) so the 'Verify
        # Payment' tap - handled by manual_payments.handle_user_verification,
        # which may run on a different serverless instance - can still find it.
        await db.create_pending_payment_intent(reference, user_id, "utility_subscription")

        text = (
            f"{EMOJI_COLORS['success']} **Payment Ready**\n\n"
            f"Click the link below to pay GHS {price:g}.00 via Selar:\n\n"
            f"[Pay Now]({payment_link})\n\n"
            f"After payment, return here and tap \"Verify Payment\"."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Verify Payment", callback_data="verify_utility_sub")],
            [InlineKeyboardButton("❌ Cancel", callback_data="m_tools")],
        ])
        await safe_edit_message(query, text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        logger.error(f"[v0] Selar payment init failed for utility subscription, user {user_id}. Check PAYSTACK_SECRET_KEY.")
        await query.answer("Failed to initialize payment. Please try again.", show_alert=True)


async def handle_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """callback_data == 'verify_utility_sub'"""
    query = update.callback_query
    user_id = update.effective_user.id
    reference = context.user_data.get("utility_payment_reference")

    if not reference:
        await query.answer("Payment reference not found.", show_alert=True)
        return

    await request_review(
        context, user_id=user_id, reference=reference,
        payment_type="utility_subscription", clone_id=_clone_id(context),
    )
    context.user_data.pop("utility_payment_reference", None)
    await safe_edit_message(query, "Payment report sent to the admin. Access will be enabled after approval.")
