"""
Generic "Pay Now" button attached to a group's welcome message.

The admin sets an arbitrary label + amount via /setpaybutton (see
handlers/moderation.py + modules/moderation_extra.py) — this bot doesn't
care what the payment is FOR (paid access, membership, a fundraiser,
whatever); it just runs the existing Selar initialize/verify flow and
logs it via the generic payment_logs table (database.log_payment /
mark_payment_paid), the same pattern used by clone_bot.py and
utility_paywall.py.

callback_data conventions:
  welcome_pay_init_<chat_id>   -> initialize a Selar transaction
  welcome_pay_verify_<chat_id> -> verify + confirm in the group
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import db
from paystack import paystack
from modules import moderation_extra as modx
from config import EMOJI_COLORS
from manual_payments import request_review
from utils import safe_edit_message

logger = logging.getLogger(__name__)


async def handle_payment_initiation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """callback_data == 'welcome_pay_init_<chat_id>'"""
    query = update.callback_query
    user = update.effective_user
    chat_id = int(query.data.split("_")[-1])

    pay_button = await modx.get_pay_button(chat_id)
    if not pay_button:
        await query.answer("This payment option is no longer available.", show_alert=True)
        return

    label = pay_button["label"]
    amount_ghs = pay_button["amount_ghs"]
    email = f"user_{user.id}@animebot.com"

    payment_result = paystack.initialize_payment(
        email,
        amount_ghs * 100,  # GHS -> pesewas
        user.id,
        f"GroupPay_{chat_id}_{user.id}",
        payment_type="group_pay_now",
        extra_metadata={"chat_id": chat_id, "label": label},
    )

    if payment_result and payment_result.get("status") == "success":
        reference = payment_result.get("reference")
        payment_link = payment_result.get("authorization_url")

        await db.log_payment(user.id, amount_ghs, reference, status="pending")
        # Persisted server-side (not context.user_data) so the 'I've Paid' tap
        # - handled by manual_payments.handle_user_verification, which may run
        # on a different serverless instance - can still find it.
        await db.create_pending_payment_intent(reference, user.id, "group_pay_now", chat_id=chat_id)

        # Sent privately where possible via a URL button rather than a
        # markdown link, so tapping it works the same on every client.
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💳 Pay {amount_ghs} GHS", url=payment_link)],
            [InlineKeyboardButton("I've Paid — Notify admin", callback_data=f"welcome_pay_verify_{chat_id}")],
        ])
        await query.answer()
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"{EMOJI_COLORS['success']} **{label} — Payment Ready**\n\n"
                f"Amount: GHS {amount_ghs}.00\n\n"
                f"Tap below to pay via Selar, then come back and tap "
                f"\"I've Paid — Verify\"."
            ),
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    else:
        logger.error(f"[v0] Selar init failed for group_pay_now, chat {chat_id}, user {user.id}.")
        await query.answer("Failed to initialize payment. Please try again.", show_alert=True)


async def handle_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """callback_data == 'welcome_pay_verify_<chat_id>'"""
    query = update.callback_query
    user = update.effective_user
    chat_id = int(query.data.split("_")[-1])
    reference = context.user_data.get(f"welcome_pay_ref_{chat_id}")

    if not reference:
        await query.answer("Payment reference not found. Tap Pay Now again.", show_alert=True)
        return

    await request_review(
        context, user_id=user.id, reference=reference, payment_type="group_pay_now",
        details={"chat_id": chat_id},
    )
    context.user_data.pop(f"welcome_pay_ref_{chat_id}", None)
    await safe_edit_message(query, "Payment report sent to the admin. You will be notified after approval.")
