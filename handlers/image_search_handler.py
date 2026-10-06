"""
Reverse Image Search (via a free Yandex Images scrape, no API key) with a
Selar paywall.

User sends any photo -> bot runs a reverse image search -> matched result
thumbnails are shown immediately, unblurred, in full. Only the *source
links* are locked: every user gets exactly 1 free reveal, then it's GHS 10
per unlock via the existing Selar flow (same initialize/verify pattern
as handlers/clone_bot.py).
"""

import logging
from urllib.parse import quote

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import db
from paystack import paystack
from modules.image_search import reverse_image_search
from config import ADMIN_ID, IMAGE_SEARCH_YANDEX_FEE_GHS, IMAGE_SEARCH_YANDEX_DAYS
from utils import is_owner, safe_edit_message

def _clone_id(context) -> int:
    """0 for the main bot, else the running clone's id — tier, quota, and
    subscription lookups must be scoped to this so they never leak across
    the main bot and other clones."""
    clone_config = context.bot_data.get("clone_config")
    return clone_config.get("clone_id") if clone_config else 0

logger = logging.getLogger(__name__)

IMAGE_SEARCH_FEE_GHS = 10


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for any photo sent with no other mode (autopost/broadcast
    content capture, etc.) currently active — see handle_media_message."""
    try:
        user_id = update.effective_user.id
        photo = update.message.photo[-1]  # highest resolution variant
        tg_file = await context.bot.get_file(photo.file_id)

        file_path = tg_file.file_path or ""
        if file_path.startswith("http"):
            image_url = file_path
        else:
            image_url = f"https://api.telegram.org/file/bot{context.bot.token}/{file_path}"

        await update.message.reply_text("🔍 Searching for matches...")

        results = await reverse_image_search(image_url)

        if results is None:
            detail = getattr(reverse_image_search, "last_error", None)
            if user_id == ADMIN_ID and detail:
                await update.message.reply_text(f"⚠️ Image search error (admin detail):\n{detail[:500]}")
            elif detail and "didn't respond" in detail:
                await update.message.reply_text(f"⚠️ {detail}")
            else:
                await update.message.reply_text(
                    "⚠️ Reverse image search is temporarily unavailable. Try again later."
                )
            return

        if not results:
            # Persisted server-side (not context.user_data) so a later tap
            # (e.g. the Yandex subscribe button) - handled by a possibly
            # different serverless instance - can still find the image_url.
            await db.save_image_search_session(user_id, _clone_id(context), image_url, [])
            await update.message.reply_text("No matches found for this image.")
            await _send_yandex_option(update.message, user_id, context)
            return

        # Persisted server-side (not context.user_data) so the "Reveal Source
        # Links (Free)" tap - handled by a possibly different serverless
        # instance - can still find the results, and so can the Yandex button.
        await db.save_image_search_session(user_id, _clone_id(context), image_url, results)

        # Preview thumbnails are always shown in full, unblurred — only the
        # source link behind each result is what gets locked below.
        for i, r in enumerate(results, 1):
            thumb = r.get("thumbnail")
            caption = f"Match {i}" + (f" — {r['title'][:80]}" if r.get("title") else "")
            if not thumb:
                continue
            try:
                await update.message.reply_photo(photo=thumb, caption=caption)
            except Exception as e:
                logger.warning(f"[v0] Failed to send image-search preview {i}: {e}")

        user = await db.get_user(user_id, clone_id=_clone_id(context))
        free_used = bool(user and user.get("free_image_search_used"))

        if is_owner(user_id, context):
            await update.message.reply_text(f"Found {len(results)} match(es). Owner bypass — revealing links now.")
            await _reveal_links_message(update.message, results)
        elif not free_used:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🆓 Reveal Source Links (Free)", callback_data="imgsearch_free_unlock")
            ]])
            await update.message.reply_text(
                f"Found {len(results)} match(es). You get 1 free source-link reveal — this one's on the house.",
                reply_markup=keyboard
            )
        else:
            price = await db.get_clone_price(_clone_id(context), "image_search_unlock")
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"🔓 Unlock Source Links — GHS {price:g}", callback_data="imgsearch_pay")
            ]])
            await update.message.reply_text(
                f"Found {len(results)} match(es). Unlock the source links for GHS {price:g}.",
                reply_markup=keyboard
            )

        await _send_yandex_option(update.message, user_id, context)

    except Exception as e:
        logger.error(f"[v0] Error in handle_photo_message (image search): {e}")
        await update.message.reply_text(f"Error running image search: {str(e)[:80]}")


def _yandex_url(image_url: str) -> str:
    return f"https://yandex.com/images/search?url={quote(image_url, safe='')}&rpt=imageview"


async def _send_yandex_option(message, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Separate perk from the free/paid source-link reveal above: jumping
    straight to Yandex's own reverse-search results page with this image
    pre-loaded, gated behind its own recurring subscription (owner bypasses
    it same as everything else paywalled)."""
    clone_id = _clone_id(context)
    session = await db.get_image_search_session(user_id, clone_id)
    image_url = session.get("image_url") if session else None
    if not image_url:
        return

    if is_owner(user_id, context) or await db.is_image_search_yandex_active(user_id, clone_id):
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔎 Open in Yandex", url=_yandex_url(image_url))
        ]])
        await message.reply_text(
            "Want more matches? Open this image directly on Yandex's reverse search.",
            reply_markup=keyboard
        )
    else:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🔎 Open in Yandex — GHS {IMAGE_SEARCH_YANDEX_FEE_GHS}/month", callback_data="imgsearch_yandex_subscribe")
        ]])
        await message.reply_text(
            f"Want to jump straight to Yandex's own reverse-search results for this image? "
            f"Subscribe for GHS {IMAGE_SEARCH_YANDEX_FEE_GHS}/month to unlock direct Yandex search on every image you send.",
            reply_markup=keyboard
        )


async def handle_free_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback for the 'Reveal Source Links (Free)' button."""
    query = update.callback_query
    user_id = update.effective_user.id
    session = await db.get_image_search_session(user_id, _clone_id(context))
    results = session.get("results") if session else None

    if not results:
        await query.answer("Search results expired — send the image again.", show_alert=True)
        return

    user = await db.get_user(user_id, clone_id=_clone_id(context))
    if user and user.get("free_image_search_used"):
        await query.answer("Your free search is already used — this one needs payment.", show_alert=True)
        return

    await db.mark_free_image_search_used(user_id, clone_id=_clone_id(context))
    await _reveal_links(query, results)


async def handle_pay_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback for the 'Unlock Source Links — GHS 10' button. Initializes Selar payment."""
    query = update.callback_query
    user_id = update.effective_user.id
    email = f"user_{user_id}@animebot.com"
    clone_id = _clone_id(context)
    price = await db.get_clone_price(clone_id, "image_search_unlock")

    payment_result = paystack.initialize_payment(
        email,
        int(price * 100),
        user_id,
        f"ImageSearchUnlock_{user_id}",
        payment_type="image_search_unlock",
        extra_metadata={"clone_id": clone_id}
    )

    if payment_result and payment_result.get("status") == "success":
        reference = payment_result.get("reference")
        payment_link = payment_result.get("authorization_url")

        # Persisted server-side (not context.user_data) so the 'Verify
        # Payment' tap - handled by manual_payments.handle_user_verification,
        # which may run on a different serverless instance - can still find it.
        await db.create_pending_payment_intent(reference, user_id, "image_search_unlock")

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Pay Now", url=payment_link)],
            [InlineKeyboardButton("✅ Verify Payment", callback_data="imgsearch_verify")]
        ])
        await safe_edit_message(query, 
            f"💳 **Unlock Source Links**\n\nPay GHS {price:g}.00 via Selar, "
            f"then tap **Verify Payment**.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    else:
        logger.error(f"[v0] Selar init failed for image-search unlock, user {user_id}")
        await query.answer("Failed to start payment. Please try again.", show_alert=True)


async def _reveal_links(query, results):
    lines = ["🔗 **Source Links**\n"]
    for i, r in enumerate(results, 1):
        url = r.get("url", "#")
        title = (r.get("title") or f"Result {i}")[:60]
        lines.append(f"{i}. [{title}]({url})")
    await safe_edit_message(query, 
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def _reveal_links_message(message, results):
    lines = ["🔗 **Source Links**\n"]
    for i, r in enumerate(results, 1):
        url = r.get("url", "#")
        title = (r.get("title") or f"Result {i}")[:60]
        lines.append(f"{i}. [{title}]({url})")
    await message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def handle_yandex_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback for 'Open in Yandex — GHS 20/month'. Initializes Selar payment
    for the recurring Yandex direct-search subscription."""
    query = update.callback_query
    user_id = update.effective_user.id
    clone_id = _clone_id(context)
    email = f"user_{user_id}@animebot.com"

    payment_result = paystack.initialize_payment(
        email,
        int(IMAGE_SEARCH_YANDEX_FEE_GHS * 100),
        user_id,
        f"YandexSearchSub_{user_id}",
        payment_type="image_search_yandex",
        extra_metadata={"clone_id": clone_id}
    )

    if payment_result and payment_result.get("status") == "success":
        reference = payment_result.get("reference")
        payment_link = payment_result.get("authorization_url")

        await db.start_image_search_yandex_payment(user_id, clone_id, reference)
        # Persisted server-side (not context.user_data) so the 'Verify
        # Payment' tap - handled by manual_payments.handle_user_verification,
        # which may run on a different serverless instance - can still find it.
        await db.create_pending_payment_intent(reference, user_id, "yandex_subscription")

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Pay Now", url=payment_link)],
            [InlineKeyboardButton("✅ Verify Payment", callback_data="imgsearch_yandex_verify")]
        ])
        await safe_edit_message(query,
            f"💳 **Yandex Direct Search — GHS {IMAGE_SEARCH_YANDEX_FEE_GHS}/month**\n\n"
            f"Pay via Selar, then tap **Verify Payment**. This unlocks a direct "
            f"'Open in Yandex' link on every image you send for {IMAGE_SEARCH_YANDEX_DAYS} days.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    else:
        logger.error(f"[v0] Selar init failed for Yandex search subscription, user {user_id}")
        await query.answer("Failed to start payment. Please try again.", show_alert=True)


async def handle_yandex_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback for 'Cancel Auto-Renewal'. Stops future charges but leaves
    the current subscription period active until it naturally expires."""
    query = update.callback_query
    user_id = update.effective_user.id
    clone_id = _clone_id(context)

    await db.cancel_image_search_yandex_autorenew(user_id, clone_id)
    await query.answer("Auto-renewal cancelled. Your access stays active until it expires.", show_alert=True)
