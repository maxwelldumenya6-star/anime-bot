from telegram import Update
from telegram.ext import ContextTypes
import secrets
import logging

from database import db
import flow_state
from keyboards import keyboard_gen
from formatter import AnimeFormatter
from paystack import paystack
from config import (
    EMOJI_COLORS, CLONE_BOT_FEE_GHS, CLONE_BOT_REAL_ENABLED, PUBLIC_BASE_URL,
    CLONE_MONETIZATION_FEE_GHS, CLONE_MONETIZATION_DAYS, PRICE_REGISTRY, ADMIN_ID,
)
from utils import is_owner, escape_markdown_v1 as esc_md
import clone_service
from utils import safe_edit_message

logger = logging.getLogger(__name__)


async def start_clone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for the Clone Bot button. If the user already has one or
    more active clones, show the My Clones list (view/edit/add another)
    instead of jumping straight into a fresh creation flow."""
    query = update.callback_query
    user_id = update.effective_user.id

    existing = await db.get_user_clones(user_id)
    if existing:
        await show_my_clones(update, context, existing=existing)
        return

    await _begin_new_clone_flow(update, context)


async def show_my_clones(update: Update, context: ContextTypes.DEFAULT_TYPE, existing: list = None):
    """List the user's active clones with an edit button each, plus Add Another."""
    query = update.callback_query
    user_id = update.effective_user.id
    clones = existing if existing is not None else await db.get_user_clones(user_id)

    display_clones = [
        {
            "clone_id": c["clone_id"],
            "display_name": esc_md((c.get("custom_data") or {}).get("name") or c.get("bot_name")),
            "bot_username": c.get("bot_username"),
        }
        for c in clones
    ]

    text = f"{EMOJI_COLORS.get('clone', '🤖')} **My Clones**\n\nTap a bot to edit its branding, or add another one."
    if query:
        await safe_edit_message(query, text, reply_markup=keyboard_gen.my_clones_keyboard(display_clones), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard_gen.my_clones_keyboard(display_clones), parse_mode="Markdown")


async def show_clone_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, clone_id: int):
    """Show one clone's current name/branding/categories with edit buttons."""
    query = update.callback_query
    user_id = update.effective_user.id

    clones = await db.get_user_clones(user_id)
    clone = next((c for c in clones if c["clone_id"] == clone_id), None)
    if clone is None:
        await query.answer("That clone wasn't found (maybe removed).", show_alert=True)
        return

    cd = clone.get("custom_data") or {}
    name = esc_md(cd.get("name") or clone.get("bot_name") or "—")
    branding = esc_md(cd.get("branding") or "—")
    categories = esc_md(cd.get("categories") or "—")
    username = esc_md(clone.get("bot_username") or "—")

    text = (
        f"🤖 **@{username}**\n\n"
        f"**Name:** {name}\n"
        f"**Branding:** {branding}\n"
        f"**Categories:** {categories}\n\n"
        f"What would you like to edit?"
    )
    await safe_edit_message(query, text, reply_markup=keyboard_gen.clone_edit_keyboard(clone_id), parse_mode="Markdown")


async def show_monetization_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, clone_id: int):
    """Entry point for '💰 Monetization' on the clone edit menu. Shows whether
    the CLONE_MONETIZATION_FEE_GHS/month activation is active; if not, the
    only option is to activate it — Payment Settings and Set Your Prices
    stay locked until then."""
    query = update.callback_query
    user_id = update.effective_user.id

    clones = await db.get_user_clones(user_id)
    clone = next((c for c in clones if c["clone_id"] == clone_id), None)
    if clone is None:
        await query.answer("That clone wasn't found (maybe removed).", show_alert=True)
        return

    active = await db.is_monetization_active(clone_id)
    sub = await db.get_monetization_subscription(clone_id)

    if active:
        expiry_line = f"Active until {sub['expires_at'].strftime('%Y-%m-%d')}." if sub and sub.get("expires_at") else "Active."
        text = (
            f"💰 **Monetization**\n\n"
            f"{expiry_line}\n\n"
            f"You can connect your own Selar/Stripe key and set your own "
            f"prices for this bot's paid features."
        )
    else:
        text = (
            f"💰 **Monetization**\n\n"
            f"Not active. Activating unlocks:\n"
            f"• Connecting your own Selar/Stripe key\n"
            f"• Setting your own prices for this bot's paid features\n\n"
            f"Fee: **GHS {CLONE_MONETIZATION_FEE_GHS}/month**. Until activated, "
            f"payments go through the main bot's account at default prices."
        )

    await safe_edit_message(query, 
        text,
        reply_markup=keyboard_gen.clone_monetization_menu_keyboard(clone_id, active),
        parse_mode="Markdown"
    )


async def handle_monetization_activate(update: Update, context: ContextTypes.DEFAULT_TYPE, clone_id: int):
    """callback_data == clone_monetize_activate_<clone_id> — starts (or
    renews) the Selar payment for the monetization activation fee."""
    query = update.callback_query
    user_id = update.effective_user.id

    clones = await db.get_user_clones(user_id)
    clone = next((c for c in clones if c["clone_id"] == clone_id), None)
    if clone is None:
        await query.answer("That clone wasn't found (maybe removed).", show_alert=True)
        return

    email = f"user_{user_id}@animebot.com"
    payment_result = paystack.initialize_payment(
        email,
        CLONE_MONETIZATION_FEE_GHS * 100,  # GHS -> pesewas
        user_id,
        f"CloneMonetize_{clone_id}",
        payment_type="clone_monetization",
        extra_metadata={"clone_id": clone_id},
    )

    if not payment_result or payment_result.get("status") != "success":
        await query.answer("Failed to start payment. Please try again.", show_alert=True)
        return

    reference = payment_result.get("reference")
    await db.start_monetization_payment(clone_id, user_id, reference)

    await safe_edit_message(query, 
        f"{EMOJI_COLORS['success']} **Activate Monetization — GHS {CLONE_MONETIZATION_FEE_GHS}/month**\n\n"
        f"Pay via the link below, then tap Verify.",
        reply_markup=keyboard_gen.clone_monetize_payment_keyboard(clone_id, payment_result.get("authorization_url")),
        parse_mode="Markdown"
    )


async def handle_monetization_verify(update: Update, context: ContextTypes.DEFAULT_TYPE, clone_id: int):
    """callback_data == clone_monetize_verify_<clone_id>"""
    query = update.callback_query
    user_id = update.effective_user.id

    sub = await db.get_monetization_subscription(clone_id)
    reference = sub.get("payment_reference") if sub else None
    if not reference:
        await query.answer("No pending payment found. Tap Activate again.", show_alert=True)
        return

    from manual_payments import request_review
    await request_review(
        context, user_id=user_id, reference=reference,
        payment_type="clone_monetization", clone_id=clone_id,
    )
    await safe_edit_message(query, 
        f"{EMOJI_COLORS['success']} Payment reported to the admin.\n\n"
        "Monetization will activate once the admin approves it.",
        reply_markup=keyboard_gen.main_menu(),
    )


async def show_clone_prices(update: Update, context: ContextTypes.DEFAULT_TYPE, clone_id: int):
    """callback_data == clone_prices_<clone_id> — lists every editable price
    for this clone. Gated: only reachable via the monetization menu once
    active, but double-check here too since callback_data can be replayed."""
    query = update.callback_query
    user_id = update.effective_user.id

    if not await db.is_monetization_active(clone_id):
        await query.answer("Activate monetization first.", show_alert=True)
        await show_monetization_menu(update, context, clone_id)
        return

    prices = await db.get_clone_prices(clone_id)
    await safe_edit_message(query, 
        f"🏷️ **Set Your Prices**\n\nTap a feature to change its price. Users of this bot will always pay whatever's shown here.",
        reply_markup=keyboard_gen.clone_prices_menu_keyboard(clone_id, prices),
        parse_mode="Markdown"
    )


async def start_edit_clone_price(update: Update, context: ContextTypes.DEFAULT_TYPE, price_key: str, clone_id: int):
    """callback_data == clone_editprice_<key>_<clone_id> — prompts for a new
    numeric GHS amount, caught by handle_price_message."""
    query = update.callback_query

    if not await db.is_monetization_active(clone_id):
        await query.answer("Activate monetization first.", show_alert=True)
        await show_monetization_menu(update, context, clone_id)
        return

    if price_key not in PRICE_REGISTRY:
        await query.answer("Unknown price key.", show_alert=True)
        return

    context.user_data["awaiting_price_edit"] = {"clone_id": clone_id, "key": price_key}
    await flow_state.sync(context, update.effective_user.id, 0, flow="clone_price_edit")
    label = PRICE_REGISTRY[price_key]["label"]
    await safe_edit_message(query, 
        f"{EMOJI_COLORS['submit']} Send the new price in GHS for **{esc_md(label)}** (numbers only, e.g. `15` or `15.50`):",
        reply_markup=keyboard_gen.clone_price_edit_back_keyboard(clone_id),
        parse_mode="Markdown"
    )


async def handle_price_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the text reply to start_edit_clone_price's prompt."""
    pending = context.user_data.get("awaiting_price_edit")
    if not pending:
        return

    clone_id = pending["clone_id"]
    key = pending["key"]
    user_id = update.effective_user.id
    raw = (update.message.text or "").strip().replace(",", "")

    context.user_data.pop("awaiting_price_edit", None)
    await flow_state.clear(context, update.effective_user.id, 0)

    try:
        amount = float(raw)
        if amount <= 0 or amount > 100000:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            f"{EMOJI_COLORS['error']} That doesn't look like a valid price. Try again from Set Your Prices."
        )
        return

    if not await db.is_monetization_active(clone_id):
        await update.message.reply_text(
            f"{EMOJI_COLORS['error']} Monetization isn't active on this bot anymore."
        )
        return

    ok = await db.set_clone_price(clone_id, user_id, key, amount)
    if not ok:
        await update.message.reply_text(f"{EMOJI_COLORS['error']} Couldn't update that price — try again.")
        return

    label = PRICE_REGISTRY.get(key, {}).get("label", key)
    await update.message.reply_text(
        f"{EMOJI_COLORS['success']} {label} is now GHS {amount:g}.",
        reply_markup=keyboard_gen.clone_price_edit_back_keyboard(clone_id)
    )


async def show_payment_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, clone_id: int):
    """Show where this clone's payments currently go, with the option to
    switch to the owner's own Selar/Stripe key. Gated behind an active
    monetization subscription — replayed callback_data or a stale button
    could reach this even if it lapsed since the menu was last shown."""
    query = update.callback_query
    user_id = update.effective_user.id

    if not await db.is_monetization_active(clone_id):
        await query.answer("Activate monetization first.", show_alert=True)
        await show_monetization_menu(update, context, clone_id)
        return

    clones = await db.get_user_clones(user_id)
    clone = next((c for c in clones if c["clone_id"] == clone_id), None)
    if clone is None:
        await query.answer("That clone wasn't found (maybe removed).", show_alert=True)
        return

    cd = clone.get("custom_data") or {}
    provider = cd.get("payment_provider", "main")
    provider_label = {
        "main": "the main bot's account (default)",
        "selar": "your own connected Selar key",
        "stripe": "your own connected Stripe key",
    }.get(provider, "the main bot's account (default)")

    text = (
        f"💳 **Payment Settings**\n\n"
        f"Payments for this bot currently go to **{esc_md(provider_label)}**.\n\n"
        f"By default, all clone payments are collected by the main bot until "
        f"you connect your own gateway key below. This is optional."
    )
    await safe_edit_message(query, 
        text,
        reply_markup=keyboard_gen.clone_payment_settings_keyboard(clone_id, provider),
        parse_mode="Markdown"
    )


async def handle_set_payment_provider(update: Update, context: ContextTypes.DEFAULT_TYPE, provider: str, clone_id: int):
    """callback_data == clone_paysetprovider_<main|selar|stripe>_<clone_id>.
    'main' switches back to the default immediately (and wipes any stored
    key). 'selar'/'stripe' prompts for the key, caught by
    handle_payment_key_message."""
    query = update.callback_query
    user_id = update.effective_user.id

    if not await db.is_monetization_active(clone_id):
        await query.answer("Activate monetization first.", show_alert=True)
        await show_monetization_menu(update, context, clone_id)
        return

    clones = await db.get_user_clones(user_id)
    clone = next((c for c in clones if c["clone_id"] == clone_id), None)
    if clone is None:
        await query.answer("That clone wasn't found (maybe removed).", show_alert=True)
        return

    if provider == "main":
        await db.set_clone_payment_provider(clone_id, user_id, "main")
        await query.answer("Switched back to the main bot's account.")
        await show_payment_settings(update, context, clone_id)
        return

    context.user_data["awaiting_payment_key"] = {"clone_id": clone_id, "provider": provider}
    await flow_state.sync(context, update.effective_user.id, 0, flow="clone_payment_key")
    gateway_name = "Selar" if provider == "selar" else "Stripe"
    await safe_edit_message(query, 
        f"{EMOJI_COLORS['submit']} Send your {gateway_name} **secret key**.\n\n"
        f"It's encrypted before storage and used only to route this bot's own payments.",
        reply_markup=keyboard_gen.clone_payment_key_prompt_keyboard(clone_id),
        parse_mode="Markdown"
    )


async def handle_payment_key_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the text reply to handle_set_payment_provider's key prompt."""
    pending = context.user_data.get("awaiting_payment_key")
    if not pending:
        return

    clone_id = pending["clone_id"]
    provider = pending["provider"]
    user_id = update.effective_user.id
    key_text = (update.message.text or "").strip()

    context.user_data.pop("awaiting_payment_key", None)
    await flow_state.clear(context, user_id, 0)

    if not key_text or len(key_text) < 10:
        await update.message.reply_text(
            f"{EMOJI_COLORS['error']} That doesn't look like a valid key. Try again from Payment Settings.",
            reply_markup=keyboard_gen.clone_payment_key_prompt_keyboard(clone_id)
        )
        return

    await db.set_clone_payment_provider(clone_id, user_id, provider, api_key=key_text)

    # Best-effort: delete the message containing the raw key so it doesn't
    # linger in chat history once it's safely stored encrypted.
    try:
        await update.message.delete()
    except Exception:
        pass

    gateway_name = "Selar" if provider == "selar" else "Stripe"
    await update.message.reply_text(
        f"{EMOJI_COLORS['success']} {gateway_name} connected. This bot's payments will now go to your own account.",
        reply_markup=keyboard_gen.clone_payment_key_prompt_keyboard(clone_id)
    )


async def start_edit_clone_field(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, clone_id: int):
    """Prompt for a new value for one field (name/branding/categories) of an
    existing clone. The reply is caught by handle_clone_edit_message."""
    query = update.callback_query
    context.user_data["editing_clone_id"] = clone_id
    context.user_data["editing_clone_field"] = field
    await flow_state.sync(context, update.effective_user.id, 0, flow="clone_edit_field")

    prompts = {
        "name": "Send the new name for this bot:",
        "branding": "Send the new branding/theme (e.g. 'Anime only', 'Manga focused'):",
        "categories": "Send the new categories, comma separated (e.g. 'Reviews, News'):",
    }
    await safe_edit_message(query, 
        f"{EMOJI_COLORS['submit']} {prompts.get(field, 'Send the new value:')}",
        reply_markup=keyboard_gen.clone_edit_back_keyboard(clone_id)
    )


async def handle_clone_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the text reply to start_edit_clone_field — writes straight to
    the DB (unlike the pre-creation flow, which stages in user_data) and
    invalidates the cached Application so the change takes effect immediately."""
    text = update.message.text
    clone_id = context.user_data.get("editing_clone_id")
    field = context.user_data.get("editing_clone_field")
    user_id = update.effective_user.id

    if clone_id is None or field is None:
        return

    ok = await db.update_clone_custom_data(clone_id, user_id, {field: text})
    context.user_data.pop("editing_clone_id", None)
    context.user_data.pop("editing_clone_field", None)
    await flow_state.clear(context, user_id, 0)

    if not ok:
        await update.message.reply_text(
            f"{EMOJI_COLORS['error']} Couldn't update that — the bot may have been removed."
        )
        return

    # Drop the cached Application so the new branding is live on the next
    # message to that clone, not just after it eventually falls out of the LRU cache.
    from api.bot import invalidate_clone_cache
    invalidate_clone_cache(clone_id)

    await update.message.reply_text(
        f"{EMOJI_COLORS['success']} {field.capitalize()} updated!",
        reply_markup=keyboard_gen.clone_edit_keyboard(clone_id)
    )


async def _begin_new_clone_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The original clone-creation entry point (payment or owner bypass ->
    customization). Used both for a first-time clone and for 'Add Another Bot'."""
    query = update.callback_query
    user_id = update.effective_user.id

    if is_owner(user_id, context):
        context.user_data["clone_step"] = "customizing"
        context.user_data["payment_status"] = "owner_bypass"
        await safe_edit_message(query, 
            f"{EMOJI_COLORS.get('success', '✅')} Owner bypass — no payment needed. Let's customize your bot!",
            reply_markup=keyboard_gen.clone_customization_keyboard(),
            parse_mode="Markdown"
        )
        return

    clone_info = AnimeFormatter.format_clone_info()

    await safe_edit_message(query, 
        clone_info,
        reply_markup=keyboard_gen.clone_payment_keyboard(CLONE_BOT_FEE_GHS),
        parse_mode="Markdown"
    )

    context.user_data["clone_step"] = "awaiting_payment"


async def _resolve_clone_payment_reference(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """The clone payment reference is written to the clone_payments table
    (via db.store_pending_clone_payment) the moment 'Pay Now' succeeds, so it
    survives a serverless cold start; context.user_data is only checked first
    as a same-instance fast path. Falls back to the latest pending/awaiting-
    review row, then the latest paid one, for whichever step is asking."""
    reference = context.user_data.get("payment_reference")
    if reference:
        return reference
    row = await db.get_latest_pending_clone_payment(user_id) or await db.get_latest_paid_clone_payment(user_id)
    if row:
        context.user_data["payment_reference"] = row["reference"]
        return row["reference"]
    return None


async def handle_payment_initiation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initialize Selar payment"""
    query = update.callback_query
    user_id = update.effective_user.id
    email = f"user_{user_id}@animebot.com"  # Fallback email

    if query.data == "paystack_checkout":
        payment_result = paystack.initialize_payment(
            email,
            CLONE_BOT_FEE_GHS * 100,  # Convert GHS to pesewas
            user_id,
            f"AnimeBotClone_{user_id}"
        )

        if payment_result and payment_result.get("status") == "success":
            payment_reference = payment_result.get("reference")
            payment_link = payment_result.get("authorization_url")

            await db.store_pending_clone_payment(user_id, payment_reference)

            context.user_data["payment_reference"] = payment_reference
            context.user_data["clone_payment_pending"] = True

            payment_text = f"""
{EMOJI_COLORS['success']} **Payment Ready**

Click the link below to pay GHS {CLONE_BOT_FEE_GHS}.00 via Paystack:

[Pay Now]({payment_link})

After you finish checkout, return here and tap **I have paid — Notify admin**.
The admin will confirm or reject the payment manually.
"""

            await safe_edit_message(query, 
                payment_text,
                reply_markup=keyboard_gen.clone_paid_keyboard(),
                parse_mode="Markdown"
            )
        else:
            logger.error(f"[v0] Selar payment init failed for user {user_id}.")
            await query.answer("Failed to initialize payment. Please try again.", show_alert=True)


async def handle_clone_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Notify the admin after the buyer taps that they paid."""
    query = update.callback_query
    user_id = update.effective_user.id
    reference = await _resolve_clone_payment_reference(context, user_id)
    if not reference:
        await query.answer("Payment session not found. Start again.", show_alert=True)
        return
    if not await db.request_clone_payment_review(reference):
        status = await db.get_clone_payment_status(reference)
        await query.answer(f"Payment is already {status}.", show_alert=True)
        return
    payment = await db.get_clone_payment(reference)
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(f"Clone payment review requested.\n\nUser ID: {payment['user_id']}\n"
              f"Reference: {reference}\nAmount: GHS {CLONE_BOT_FEE_GHS}.00\n\n"
              "Approve only after checking Selar."),
        reply_markup=keyboard_gen.clone_admin_review_keyboard(reference),
    )
    await safe_edit_message(query, 
        f"{EMOJI_COLORS['success']} Payment reported to the admin.\n\n"
        "Your clone will unlock after the admin approves it.",
        reply_markup=keyboard_gen.main_menu(),
    )


async def handle_clone_admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve or reject a clone payment from the admin's Telegram DM."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer("Unauthorized", show_alert=True)
        return
    action, reference = query.data.split(":", 1)
    payment = await db.get_clone_payment(reference)
    if not payment:
        await query.answer("Payment record not found.", show_alert=True)
        return
    if action == "clone_admin_approve":
        if not await db.mark_clone_payment_paid(reference):
            await query.answer("Payment was already decided.", show_alert=True)
            return
        await context.bot.send_message(
            chat_id=payment["user_id"],
            text="Payment approved. You can now customize your clone.",
            reply_markup=keyboard_gen.clone_customization_keyboard(),
        )
        await safe_edit_message(query, f"Approved clone payment {reference}.")
    else:
        if not await db.reject_clone_payment(reference):
            await query.answer("Payment was already decided.", show_alert=True)
            return
        await context.bot.send_message(
            chat_id=payment["user_id"],
            text="Payment rejected by the admin. Please contact the admin if this is a mistake.",
            reply_markup=keyboard_gen.main_menu(),
        )
        await safe_edit_message(query, f"Rejected clone payment {reference}.")


async def handle_customization(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle clone customization steps"""
    query = update.callback_query
    callback_data = query.data

    if callback_data == "customize_name":
        context.user_data["customize_step"] = "awaiting_name"
        await safe_edit_message(query, 
            f"{EMOJI_COLORS['submit']} What name would you like for your bot?\n\n"
            f"(e.g., 'MyAnimeBot', 'NarutoFan_Bot')",
            reply_markup=keyboard_gen.clone_customize_back_keyboard()
        )

    elif callback_data == "customize_webhook":
        context.user_data["customize_step"] = "awaiting_webhook"
        await safe_edit_message(query, 
            f"{EMOJI_COLORS['submit']} What's your webhook URL?\n\n"
            f"(Optional: only needed if you want submission notifications forwarded "
            f"somewhere other than this bot. Leave blank by typing 'skip'.)",
            reply_markup=keyboard_gen.clone_customize_back_keyboard()
        )

    elif callback_data == "customize_branding":
        context.user_data["customize_step"] = "awaiting_branding"
        await safe_edit_message(query, 
            f"{EMOJI_COLORS['submit']} Describe your bot's branding/theme:\n\n"
            f"(e.g., 'Anime only', 'Manga focused', 'All genres')",
            reply_markup=keyboard_gen.clone_customize_back_keyboard()
        )

    elif callback_data == "customize_categories":
        context.user_data["customize_step"] = "awaiting_categories"
        await safe_edit_message(query, 
            f"{EMOJI_COLORS['submit']} What service categories?\n\n"
            f"(comma separated, e.g., 'Reviews, Recommendations, News')",
            reply_markup=keyboard_gen.clone_customize_back_keyboard()
        )

    elif callback_data == "finalize_clone":
        await finalize_clone(update, context)

    elif callback_data == "clone_back_to_customize":
        context.user_data["customize_step"] = None
        await safe_edit_message(query, 
            f"{EMOJI_COLORS.get('success', '✅')} Let's customize your bot!",
            reply_markup=keyboard_gen.clone_customization_keyboard(),
            parse_mode="Markdown"
        )

    await flow_state.sync(context, update.effective_user.id, 0, flow="clone_customize")


async def handle_customization_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle customization input from messages, including the real bot-token paste step."""
    text = update.message.text
    customize_step = context.user_data.get("customize_step")

    if customize_step == "awaiting_name":
        context.user_data["clone_name"] = text
        context.user_data["customize_step"] = None
        await update.message.reply_text(
            f"{EMOJI_COLORS['success']} Bot name set to '{text}'\n\n"
            f"What's next?",
            reply_markup=keyboard_gen.clone_customization_keyboard()
        )

    elif customize_step == "awaiting_webhook":
        context.user_data["clone_webhook"] = "" if text.strip().lower() == "skip" else text
        context.user_data["customize_step"] = None
        await update.message.reply_text(
            f"{EMOJI_COLORS['success']} Webhook set to '{text}'\n\n"
            f"What's next?",
            reply_markup=keyboard_gen.clone_customization_keyboard()
        )

    elif customize_step == "awaiting_branding":
        context.user_data["clone_branding"] = text
        context.user_data["customize_step"] = None
        await update.message.reply_text(
            f"{EMOJI_COLORS['success']} Branding set to '{text}'\n\n"
            f"What's next?",
            reply_markup=keyboard_gen.clone_customization_keyboard()
        )

    elif customize_step == "awaiting_categories":
        context.user_data["clone_categories"] = text
        context.user_data["customize_step"] = None
        await update.message.reply_text(
            f"{EMOJI_COLORS['success']} Categories set to '{text}'\n\n"
            f"What's next?",
            reply_markup=keyboard_gen.clone_customization_keyboard()
        )

    elif customize_step == "awaiting_bot_token":
        await _handle_pasted_token(update, context, text.strip())

    await flow_state.sync(context, update.effective_user.id, 0, flow="clone_customize")


# ─────────────────────────────────────────────────────────────────────────────
# Real clone flow (Part 3): token collection, validation, webhook registration
# ─────────────────────────────────────────────────────────────────────────────

async def finalize_clone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Entry point for "Done" in the customization menu.

    If CLONE_BOT_REAL_ENABLED is off (rollback flag, 3.5), falls back to the
    old placeholder-token behavior so the feature never disappears mid-rollout
    — but that old path is now clearly labeled as non-functional, not silently
    presented as a working bot.
    """
    query = update.callback_query
    user_id = update.effective_user.id

    if is_owner(user_id, context) or context.user_data.get("payment_status") == "owner_bypass":
        pass  # owner: no payment reference to verify, skip straight through
    else:
        payment_reference = await _resolve_clone_payment_reference(context, user_id)
        if not payment_reference:
            await query.answer("Payment reference not found. Please complete payment first.", show_alert=True)
            return

        payment_status = await db.get_clone_payment_status(payment_reference)
        if payment_status != "paid":
            await safe_edit_message(query, 
                f"{EMOJI_COLORS['error']} **Payment Not Confirmed**\n\n"
                f"Status: {payment_status}\n\n"
                f"Your payment must be confirmed before creating a bot.\n"
                f"If you've paid, wait 30 seconds for confirmation.",
                reply_markup=keyboard_gen.main_menu()
            )
            return

    if not CLONE_BOT_REAL_ENABLED:
        await safe_edit_message(query, 
            f"{EMOJI_COLORS['error']} Bot cloning is temporarily paused for maintenance.\n\n"
            f"Your payment is safe and on file — message the admin and we'll get your bot "
            f"set up manually in the meantime.",
            reply_markup=keyboard_gen.main_menu()
        )
        return

    if not PUBLIC_BASE_URL:
        logger.error("[v0] PUBLIC_BASE_URL is not set; cannot register a real clone webhook.")
        await safe_edit_message(query, 
            f"{EMOJI_COLORS['error']} Clone setup isn't fully configured on our end yet "
            f"(missing deployment URL). Your payment is safe — please message the admin.",
            reply_markup=keyboard_gen.main_menu()
        )
        return

    context.user_data["customize_step"] = "awaiting_bot_token"
    await flow_state.sync(context, user_id, 0, flow="clone_customize")
    await safe_edit_message(query, 
        f"{EMOJI_COLORS['success']} **One real step left!**\n\n"
        f"1. Open @BotFather on Telegram\n"
        f"2. Send /newbot and follow the prompts\n"
        f"3. Paste the token BotFather gives you right here in this chat\n\n"
        f"_(Telegram doesn't let anyone create bots via API — this manual step is the "
        f"same for every bot-cloning product, not just this one.)_",
        parse_mode="Markdown"
    )


async def _handle_pasted_token(update: Update, context: ContextTypes.DEFAULT_TYPE, token_text: str):
    """Step A: validate the pasted token via getMe."""
    validation = clone_service.validate_bot_token(token_text)

    if not validation.get("ok"):
        await update.message.reply_text(
            f"{EMOJI_COLORS['error']} That token didn't work: {validation.get('error')}\n\n"
            f"Please paste your BotFather token again, or type /cancel to stop."
        )
        return  # stay in awaiting_bot_token, let them retry

    # Stash validated info; don't act on Telegram yet until any overwrite is confirmed.
    context.user_data["pending_clone_token"] = token_text
    context.user_data["pending_clone_username"] = validation.get("username")
    context.user_data["pending_clone_first_name"] = validation.get("first_name")
    await flow_state.sync(context, update.effective_user.id, 0, flow="clone_customize")

    webhook_info = clone_service.get_webhook_info(token_text)
    if not webhook_info.get("ok"):
        await update.message.reply_text(
            f"{EMOJI_COLORS['error']} Validated the token, but couldn't check its current "
            f"webhook status: {webhook_info.get('error')}\n\nPlease try again in a moment."
        )
        return

    existing_url = webhook_info.get("url", "")
    if existing_url:
        context.user_data["customize_step"] = None  # gate on button tap, not free text, while confirming
        await flow_state.sync(context, update.effective_user.id, 0, flow="clone_customize")
        await update.message.reply_text(
            f"⚠️ **Heads up** — @{esc_md(validation.get('username'))} already has a webhook configured at:\n"
            f"`{existing_url}`\n\n"
            f"Continuing will disconnect it from whatever is currently using it "
            f"(your own project, another service, or a earlier attempt).\n\n"
            f"Only continue if this bot is dedicated to this clone.",
            reply_markup=keyboard_gen.clone_webhook_overwrite_keyboard(),
            parse_mode="Markdown"
        )
        return

    await _register_clone(update, context, token_text, validation.get("username"), bot_first_name=validation.get("first_name"))


async def handle_webhook_overwrite_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the confirm/cancel buttons shown when a pasted token already has a webhook."""
    query = update.callback_query
    token = context.user_data.get("pending_clone_token")
    username = context.user_data.get("pending_clone_username")
    first_name = context.user_data.get("pending_clone_first_name")

    if query.data == "clone_cancel_token":
        context.user_data.pop("pending_clone_token", None)
        context.user_data.pop("pending_clone_username", None)
        context.user_data.pop("pending_clone_first_name", None)
        context.user_data["customize_step"] = "awaiting_bot_token"
        await flow_state.sync(context, update.effective_user.id, 0, flow="clone_customize")
        await safe_edit_message(query, 
            f"{EMOJI_COLORS['submit']} No problem — paste a different bot's token from @BotFather."
        )
        return

    if not token:
        await query.answer("That token confirmation expired, please paste your token again.", show_alert=True)
        context.user_data["customize_step"] = "awaiting_bot_token"
        await flow_state.sync(context, update.effective_user.id, 0, flow="clone_customize")
        return

    await safe_edit_message(query, f"{EMOJI_COLORS['loading']} Setting up your bot...")
    await _register_clone(update, context, token, username, edit_query=query, bot_first_name=first_name)


async def _register_clone(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str,
                           bot_username: str, edit_query=None, bot_first_name: str = None):
    """
    Step B/C: generate a per-clone webhook secret, store the clone row (to get
    a clone_id), register the webhook pointed at that clone_id, and roll back
    the DB row if Telegram registration fails so we never have an orphaned
    "active" clone with no real webhook.
    """
    user_id = update.effective_user.id
    payment_reference = await _resolve_clone_payment_reference(context, user_id)

    custom_data = {
        # Prefer a name the user explicitly typed via "Edit Bot Name"; otherwise
        # auto-fill from the bot's own Telegram display name (getMe first_name)
        # instead of asking the user to type anything — removes a step and
        # avoids the old underscore-heavy "AnimeBotClone_<id>" fallback that
        # was a recurring source of Markdown-escaping bugs.
        "name": context.user_data.get("clone_name") or bot_first_name or f"AnimeBotClone_{user_id}",
        "webhook_url": context.user_data.get("clone_webhook", ""),
        "branding": context.user_data.get("clone_branding", ""),
        "categories": context.user_data.get("clone_categories", "")
    }

    webhook_secret = secrets.token_urlsafe(32)

    clone_id = await db.add_cloned_bot(
        user_id,
        custom_data.get("name"),
        token,
        custom_data.get("webhook_url"),
        custom_data,
        payment_id=payment_reference,
        payment_status="verified",
        bot_username=bot_username,
        webhook_secret=webhook_secret,
    )

    base_url = PUBLIC_BASE_URL
    if not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"
    webhook_url = f"{base_url}/api/bot?clone_id={clone_id}"
    set_result = clone_service.set_webhook(token, webhook_url, webhook_secret)

    if not set_result.get("ok"):
        # Roll back: don't leave a DB row claiming "active" with no real webhook behind it.
        await db.deactivate_clone(clone_id)
        error_text = (
            f"{EMOJI_COLORS['error']} Telegram rejected the webhook registration: "
            f"{set_result.get('error')}\n\nYour payment is untouched — paste your token again to retry."
        )
        context.user_data["customize_step"] = "awaiting_bot_token"
        await flow_state.sync(context, user_id, 0, flow="clone_customize")
        if edit_query:
            await safe_edit_message(edit_query, error_text)
        else:
            await update.message.reply_text(error_text)
        return

    safe_username = esc_md(bot_username)
    safe_name = esc_md(custom_data.get('name'))

    success_text = f"""
{EMOJI_COLORS['success']} **Your Bot is Live!**

**Bot:** @{safe_username}
**Name:** {safe_name}
**Status:** Active

Messages sent to @{safe_username} now get real anime-discovery, search, and \
submission responses with your branding — served by our shared infrastructure.

Come back any time to update your branding or check on your clone.
"""

    for key in ("pending_clone_token", "pending_clone_username", "pending_clone_first_name", "clone_name",
                "clone_webhook", "clone_branding", "clone_categories",
                "clone_step", "payment_status", "customize_step"):
        context.user_data.pop(key, None)
    await flow_state.clear(context, update.effective_user.id, 0)

    if edit_query:
        await safe_edit_message(edit_query, success_text, reply_markup=keyboard_gen.main_menu(), parse_mode="Markdown")
    else:
        await update.message.reply_text(success_text, reply_markup=keyboard_gen.main_menu(), parse_mode="Markdown")
