"""
Premium Group paywall — the "Pay to Join Premium Group" button attached to
every admin broadcast (see api/cron_broadcast.py's _send_broadcast).

Unlike handlers/welcome_pay.py (a per-group, admin-configured amount), this
is a single fixed-price offer for the whole bot: config.PREMIUM_GROUP_FEE_GHS
GHS to join config.PREMIUM_GROUP_INVITE_LINK. Same generic Selar
initialize/verify flow and payment_logs table as the rest of the bot
(clone_bot.py, utility_paywall.py, welcome_pay.py) — just a different
payment_type label ("premium_group_join") to tell them apart in the logs.

callback_data conventions:
  premium_pay_init    -> initialize a Selar transaction (sent privately)
  premium_pay_verify  -> verify + hand over the invite link
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import db
from paystack import paystack
from config import EMOJI_COLORS, PREMIUM_GROUP_FEE_GHS, PREMIUM_GROUP_INVITE_LINK
from utils import is_owner, safe_edit_message

logger = logging.getLogger(__name__)


def _clone_id(context) -> int:
    """0 for the main bot, else the running clone's id — pricing lookups must
    be scoped to this so a clone owner's custom price never leaks onto the
    main bot or another clone."""
    clone_config = context.bot_data.get("clone_config")
    return clone_config.get("clone_id") if clone_config else 0


def premium_group_button(price_ghs: float = PREMIUM_GROUP_FEE_GHS) -> InlineKeyboardButton:
    """The single button other modules (broadcast_runner.py) attach to their
    own keyboards — kept here so the label/price/callback stay in one place.
    Callers running inside a clone should pass that clone's own price (via
    db.get_clone_price(clone_id, "premium_group_fee")); this default only
    applies for the main bot / callers that haven't been updated yet."""
    return InlineKeyboardButton(
        f"💎 AZIGI GROUP JOIN — GHS {price_ghs:g}",
        callback_data="premium_pay_init"
    )


async def handle_premium_pay_init(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """callback_data == 'premium_pay_init' — works from any chat (DM, group,
    or channel comment), always follows up in the user's DM so payment
    details never sit in a group feed."""
    query = update.callback_query
    user = update.effective_user

    if is_owner(user.id, context):
        if PREMIUM_GROUP_INVITE_LINK:
            await safe_edit_message(query, 
                f"{EMOJI_COLORS.get('success', '✅')} Owner bypass — no payment needed.\n\n"
                f"Tap below to join the Premium Group:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Join Premium Group", url=PREMIUM_GROUP_INVITE_LINK)]
                ])
            )
        else:
            await query.answer("Owner bypass — but PREMIUM_GROUP_INVITE_LINK isn't set yet.", show_alert=True)
        return

    clone_id = _clone_id(context)
    price = await db.get_clone_price(clone_id, "premium_group_fee")
    email = f"user_{user.id}@animebot.com"
    payment_result = paystack.initialize_payment(
        email,
        int(price * 100),  # GHS -> pesewas
        user.id,
        f"PremiumGroup_{user.id}",
        payment_type="premium_group_join",
        extra_metadata={"clone_id": clone_id},
    )

    if payment_result and payment_result.get("status") == "success":
        reference = payment_result.get("reference")
        payment_link = payment_result.get("authorization_url")

        await db.log_payment(user.id, price, reference, status="pending")
        # Persisted server-side (not context.user_data) so the 'I've Paid' tap
        # - handled by manual_payments.handle_user_verification, which may run
        # on a different serverless instance - can still find it.
        await db.create_pending_payment_intent(reference, user.id, "premium_group")

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💳 Pay GHS {price:g}", url=payment_link)],
            [InlineKeyboardButton("✅ I've Paid — Verify", callback_data="premium_pay_verify")],
        ])

        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    f"{EMOJI_COLORS.get('success', '✅')} **Premium Group — Payment Ready**\n\n"
                    f"Amount: GHS {price:g}.00\n\n"
                    f"Tap below to pay via Selar, then come back and tap "
                    f"\"I've Paid — Verify\" to get your invite link."
                ),
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            await query.answer("Check your DMs to complete payment 💬")
        except Exception as e:
            logger.warning(f"[v0] Could not DM premium-group payment link to {user.id}: {e}")
            await query.answer(
                "Please start a DM with the bot first, then tap this button again.",
                show_alert=True
            )
    else:
        logger.error(f"[v0] Selar init failed for premium_group_join, user {user.id}.")
        await query.answer("Failed to initialize payment. Please try again.", show_alert=True)


async def handle_premium_pay_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """callback_data == 'premium_pay_verify'"""
    query = update.callback_query
    reference = context.user_data.get("premium_group_pay_ref")

    if not reference:
        await query.answer("Payment reference not found. Tap Pay Now again.", show_alert=True)
        return

    result = paystack.verify_payment(reference)

    if result.get("status") == "success":
        await db.mark_payment_paid(reference)
        context.user_data.pop("premium_group_pay_ref", None)

        if PREMIUM_GROUP_INVITE_LINK:
            await safe_edit_message(query, 
                f"{EMOJI_COLORS.get('success', '✅')} **Payment confirmed!**\n\n"
                f"Tap below to join the Premium Group:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Join Premium Group", url=PREMIUM_GROUP_INVITE_LINK)]
                ])
            )
        else:
            # Configured amount but no invite link set yet — don't leave the
            # user with nothing after paying.
            await safe_edit_message(query, 
                f"{EMOJI_COLORS.get('success', '✅')} **Payment confirmed!** "
                f"An admin will add you to the Premium Group shortly."
            )
            logger.warning("[v0] PREMIUM_GROUP_INVITE_LINK is not set — paid user has no invite link to tap.")
    else:
        await query.answer("Payment not confirmed yet. Complete checkout, then tap Verify again.", show_alert=True)
