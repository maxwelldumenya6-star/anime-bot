import json
import asyncio
import threading
import traceback
import logging
from collections import OrderedDict
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from init_system import initialize_system

from config import BOT_TOKEN, ADMIN_ID, EMOJI_COLORS, CLONE_BOT_FEE_GHS, CLONE_APP_CACHE_SIZE, MAIN_BOT_USERNAME
import os
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
from database import db
import flow_state
from keyboards import keyboard_gen
from handlers import admin_panel, clone_bot, admin_config, admin_remote
from handlers import superbot_handler, feature_handlers, external_handler, ai_handler, games_handler, bot_manager_handler
from handlers import moderation
from handlers import autopost_handler, broadcast_handler
from handlers import ads_marketplace_handler
from handlers import admin_tools
from handlers import utility_paywall
from handlers import welcome_pay
from handlers import link_buttons
from handlers import language_handler
from handlers import premium_group_handler
from i18n import t
from modules import superbot_adapter
from manual_payments import handle_admin_decision, handle_user_verification
from utils import escape_markdown_v1 as esc_md
from utils import safe_edit_message, safe_send_message

logger = logging.getLogger(__name__)

_app = None
_db_initialized = False
_loop = None
# Guards _loop: the loop is reused across every request in a warm container
# to avoid "Event loop is closed" errors between invocations, but asyncio
# loops aren't safe to enter from two threads at once. If Vercel's runtime
# handles two Telegram updates concurrently in the same warm container, the
# second thread's run_until_complete() call fails with "This event loop is
# already running". Serializing on this lock keeps requests correct without
# losing the loop-reuse benefit — they just queue briefly instead of racing.
_loop_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# Multi-tenant clone routing (Part 3.1 / 3.2 Step D)
#
# Each clone gets its own python-telegram-bot Application (a PTB Application is
# bound to one bot token at construction time — the main bot's _app can't be
# reused). Applications are built lazily on first request for a clone_id on a
# warm container and cached here, bounded to CLONE_APP_CACHE_SIZE (LRU) so a
# long-warm container serving many different clones' traffic over time doesn't
# grow memory unboundedly.
# ─────────────────────────────────────────────────────────────────────────────
_clone_apps: "OrderedDict[int, Application]" = OrderedDict()
_clone_apps_initialized: set = set()


def _get_event_loop():
    """Get or create a reusable event loop for the serverless container"""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop


def notify_admin_of_error(error_text: str):
    """Send the raw error straight to the admin's Telegram DM (bypasses the bot Application, uses plain HTTP)"""
    try:
        if ADMIN_ID:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": ADMIN_ID,
                    "text": f"⚠️ Bot error:\n\n{error_text[:3900]}"
                },
                timeout=5
            )
    except Exception:
        pass  # never let error reporting itself crash the handler


def _clone_id(context) -> int:
    """0 for the main bot, else the running clone's id — tier, quota, and
    language lookups must be scoped to this so they never leak across the
    main bot and other clones."""
    clone_config = context.bot_data.get("clone_config")
    return clone_config.get("clone_id") if clone_config else 0


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command with Gen Z vibes"""
    user = update.effective_user

    await db.add_user(user.id, user.username or "Anonymous", user.first_name or "User")
    lang = await db.get_user_language(user.id, clone_id=_clone_id(context))

    # Feature 19: Handle deep links
    if context.args:
        arg = context.args[0]
        if arg.startswith("fromclone"):
            # Referral from a clone's "Get your own bot" button (keyboards.py).
            # Track which clone sent them, then fall through to the normal
            # welcome below instead of returning — the generic deep-link
            # handler has no matching row for this and would otherwise reply
            # with nothing at all.
            referrer_clone_id = None
            if "_" in arg:
                try:
                    referrer_clone_id = int(arg.split("_", 1)[1])
                except ValueError:
                    referrer_clone_id = None
            if referrer_clone_id is not None:
                await db.increment_clone_referral(referrer_clone_id)
        else:
            await feature_handlers.handle_deep_link_start(update, context)
            return

    clone_config = context.bot_data.get("clone_config")
    if clone_config:
        clone_name = clone_config.get("name") or clone_config.get("bot_name") or "This bot"
        branding = clone_config.get("branding")
        tagline = f" _{esc_md(branding)}_" if branding else ""
        welcome_text = t("welcome_clone", lang, name=esc_md(user.first_name), clone_name=f"**{esc_md(clone_name)}**", tagline=tagline)
        if MAIN_BOT_USERNAME:
            # Visible trace back to the main bot on every clone (growth loop +
            # branding), since clones otherwise carry no sign of who powers them.
            # IMPORTANT: plain text, NOT wrapped in _italics_. Bot usernames
            # almost always contain an underscore (Telegram requires most to
            # end in "_bot"), and that underscore inside an _..._ span closes
            # the italic entity early, leaving a dangling unmatched "_" after
            # it — Telegram's legacy Markdown parser then rejects the whole
            # message with "can't parse entities", which silently degrades to
            # safe_send_message's plain-text fallback (which strips "_" but
            # not "\", so an escaped clone name elsewhere in the same message
            # renders with a stray literal backslash instead of an underscore).
            # Also: markdown-escaping the username itself (to work around
            # that) breaks Telegram's automatic @mention link — plain text
            # avoids both problems and still auto-links the @mention.
            welcome_text += f"\n\n{t('powered_by', lang)} (t.me/{esc_md(MAIN_BOT_USERNAME)})"
    else:
        welcome_text = t("welcome_default", lang, name=esc_md(user.first_name))

    main_menu_keyboard = keyboard_gen.main_menu(clone_mode=bool(clone_config), clone_id=(clone_config or {}).get("clone_id"))
    main_menu_keyboard = InlineKeyboardMarkup(
        list(main_menu_keyboard.inline_keyboard) + [[premium_group_handler.premium_group_button()]]
    )

    await safe_send_message(
        update.message,
        welcome_text,
        reply_markup=main_menu_keyboard,
        parse_mode="Markdown"
    )


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu - short, grouped by area"""
    query = update.callback_query
    clone_config = context.bot_data.get("clone_config")

    menu_text = "🏠 **Main Menu**\n\nChoose an area:"

    await safe_edit_message(query, 
        menu_text,
        reply_markup=keyboard_gen.main_menu(clone_mode=bool(clone_config), clone_id=(clone_config or {}).get("clone_id")),
        parse_mode="Markdown"
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route callback queries to handlers"""
    query = update.callback_query
    callback_data = query.data
    user_id = update.effective_user.id

    try:
        # Cold-start recovery, same as handle_message (see flow_state.py) —
        # callback taps (e.g. confirming a webhook overwrite, picking a
        # BotStore category) can equally land on a fresh instance.
        await flow_state.hydrate(context, user_id, _clone_id(context))

        if callback_data.startswith("payment_approve:") or callback_data.startswith("payment_reject:"):
            await handle_admin_decision(update, context)
        elif (callback_data.startswith("welcome_pay_verify_") or callback_data in {
            "verify_utility_sub", "verify_tier_payment", "premium_pay_verify",
            "imgsearch_verify", "imgsearch_yandex_verify", "verify_botstore_premium",
            "verify_subscription",
        }):
            await handle_user_verification(update, context, callback_data)

        # Main navigation
        elif callback_data == "main_menu":
            await show_main_menu(update, context)

        elif callback_data == "m_grouptools":
            is_group = update.effective_chat.type in ("group", "supergroup")
            extra_rows = await link_buttons.get_link_button_rows(update.effective_chat.id) if is_group else None
            await safe_edit_message(query, 
                "🛡️ **Group Tools**\n\nModeration commands for group admins.",
                reply_markup=keyboard_gen.grouptools_menu(is_group=is_group, extra_link_rows=extra_rows),
                parse_mode="Markdown"
            )

        elif callback_data == "gt_quick_warn":
            await moderation.start_group_quick_action(update, context, "warn")

        elif callback_data == "gt_quick_mute":
            await moderation.start_group_quick_action(update, context, "mute")

        elif callback_data == "gt_quick_ban":
            await moderation.start_group_quick_action(update, context, "ban")

        elif callback_data == "grouptools_settings":
            text = (
                "⚙️ **Mod Settings**\n\n"
                "Run this in your group (admins only):\n"
                "`/modsettings` — view current settings\n"
                "`/modsettings <setting> on|off` — toggle one\n\n"
                "Toggles: captcha, wordfilter, antiraid, slowmode, nightmode"
            )
            await safe_edit_message(query, 
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="m_grouptools")]]),
                parse_mode="Markdown"
            )

        elif callback_data == "grouptools_rules":
            text = (
                "📜 **Rules**\n\n"
                "Run `/rules` in your group to post the rules.\n"
                "Admins: `/setwelcome <text>` sets the greeting new members see."
            )
            await safe_edit_message(query, 
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="m_grouptools")]]),
                parse_mode="Markdown"
            )

        elif callback_data == "grouptools_commands":
            text = (
                "📋 **Group Commands** (admins only, run in your group)\n\n"
                "**Moderation:** /warn /unwarn /warns /ban /unban /kick /mute /unmute\n"
                "**Filters:** /filter /unfilter /filters\n"
                "**Custom replies:** /setcmd /delcmd /listcmds\n"
                "**Join gate:** /setgate /gate\n"
                "**Settings:** /modsettings /setwelcome /rules /groupstats /logs\n"
                "**Chat:** /del /pin"
            )
            await safe_edit_message(query, 
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="m_grouptools")]]),
                parse_mode="Markdown"
            )

        elif callback_data == "add_to_group_info":
            bot_username = (await context.bot.get_me()).username
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add to a Group", url=f"https://t.me/{bot_username}?startgroup=true")],
                [InlineKeyboardButton("➕ Add to a Channel", url=f"https://t.me/{bot_username}?startchannel=true")],
                [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
            ])
            await safe_edit_message(query, 
                "➕ **Add Me to Your Group/Channel**\n\n"
                "Tap a button below to add the bot. Once added, open your group/channel's "
                "member list and make the bot an **admin** — moderation, auto-post, and other "
                "features won't work until it has admin rights.\n\n"
                "_Note: the channel deep link isn't supported on every Telegram client yet — "
                "if it doesn't open a channel picker, add the bot manually via Channel Info → "
                "Administrators → Add Admin._",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )

        elif callback_data == "m_tools":
            await safe_edit_message(query, 
                "🧰 **Tools**\n\nAI, market data, downloads, games, and more.",
                reply_markup=keyboard_gen.tools_menu(),
                parse_mode="Markdown"
            )

        elif callback_data == "tools_ai_info":
            await ai_handler.start_ai_chat_waiting(update, context)

        elif callback_data == "tools_market_info":
            await safe_edit_message(query, 
                "💹 **Crypto & Stocks**\n\n/crypto <coin> — price lookup\n/stock <ticker> — stock quote\n/convert <amount> <from> <to> — currency convert\n/alerts — manage price alerts",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="m_tools")]]),
                parse_mode="Markdown"
            )

        elif callback_data == "tools_news_info":
            await safe_edit_message(query, 
                "📰 **News**\n\n/news <topic> — latest headlines on a topic",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="m_tools")]]),
                parse_mode="Markdown"
            )

        elif callback_data.startswith("lang_set_"):
            await language_handler.language_callback(update, context)

        elif callback_data == "tools_language_info":
            await language_handler.show_language_menu(update, context)

        elif callback_data.startswith("rmlinkbtn_"):
            await link_buttons.handle_remove_callback(update, context)

        elif callback_data.startswith("welcome_pay_init_"):
            await welcome_pay.handle_payment_initiation(update, context)

        elif callback_data.startswith("welcome_pay_verify_"):
            await welcome_pay.handle_verify(update, context)

        elif callback_data == "pay_utility_sub":
            await utility_paywall.handle_payment_initiation(update, context)

        elif callback_data == "verify_utility_sub":
            await utility_paywall.handle_verify(update, context)

        elif callback_data == "cancel_waiting_mode":
            context.user_data.pop("awaiting_ai_message", None)
            context.user_data.pop("awaiting_download_link", None)
            context.user_data.pop("pending_download_url", None)
            await safe_edit_message(query, 
                "🧰 **Tools**\n\nAI, market data, downloads, games, and more.",
                reply_markup=keyboard_gen.tools_menu(),
                parse_mode="Markdown"
            )

        elif callback_data == "all_commands":
            await show_all_commands(update, context)

        elif callback_data == "clone_about":
            await show_clone_about(update, context)

        elif callback_data == "noop":
            # No-op for page number display buttons
            pass

        # Clone bot — not available from inside a clone's own Application (defense
        # in depth; the buttons that lead here are already hidden in clone_mode,
        # but a stale button or deep link could still fire this callback_data).
        elif callback_data == "clone_bot":
            if context.bot_data.get("clone_config"):
                await query.answer("Cloning isn't available from a cloned bot.", show_alert=True)
            else:
                await clone_bot.start_clone(update, context)

        elif callback_data == "my_clones":
            if context.bot_data.get("clone_config"):
                await query.answer("Not available from a cloned bot.", show_alert=True)
            else:
                await clone_bot.show_my_clones(update, context)

        elif callback_data == "clone_add_another":
            if context.bot_data.get("clone_config"):
                await query.answer("Cloning isn't available from a cloned bot.", show_alert=True)
            else:
                await clone_bot._begin_new_clone_flow(update, context)

        elif callback_data.startswith("clone_detail_"):
            await clone_bot.show_clone_detail(update, context, int(callback_data.split("_")[-1]))

        elif callback_data.startswith("clone_editfield_"):
            # clone_editfield_<field>_<clone_id>
            parts = callback_data.split("_")
            field, cid = parts[2], int(parts[-1])
            await clone_bot.start_edit_clone_field(update, context, field, cid)

        elif callback_data.startswith("clone_paysettings_"):
            await clone_bot.show_payment_settings(update, context, int(callback_data.split("_")[-1]))

        elif callback_data.startswith("clone_paysetprovider_"):
            # clone_paysetprovider_<main|selar|stripe>_<clone_id>
            parts = callback_data.split("_")
            provider, cid = parts[2], int(parts[-1])
            await clone_bot.handle_set_payment_provider(update, context, provider, cid)

        elif callback_data.startswith("clone_monetization_"):
            await clone_bot.show_monetization_menu(update, context, int(callback_data.split("_")[-1]))

        elif callback_data.startswith("clone_monetize_activate_"):
            await clone_bot.handle_monetization_activate(update, context, int(callback_data.split("_")[-1]))

        elif callback_data.startswith("clone_monetize_verify_"):
            await clone_bot.handle_monetization_verify(update, context, int(callback_data.split("_")[-1]))

        elif callback_data.startswith("clone_prices_"):
            await clone_bot.show_clone_prices(update, context, int(callback_data.split("_")[-1]))

        elif callback_data.startswith("clone_editprice_"):
            # clone_editprice_<key>_<clone_id> — key itself may contain underscores
            parts = callback_data.split("_")
            cid = int(parts[-1])
            price_key = "_".join(parts[2:-1])
            await clone_bot.start_edit_clone_price(update, context, price_key, cid)

        elif callback_data == "clone_info":
            if context.bot_data.get("clone_config"):
                await query.answer("Cloning isn't available from a cloned bot.", show_alert=True)
            else:
                clone_info = AnimeFormatter.format_clone_info()
                await safe_edit_message(query, 
                    clone_info,
                    reply_markup=keyboard_gen.clone_payment_keyboard(50),
                    parse_mode="Markdown"
                )

        elif callback_data == "paystack_checkout":
            await clone_bot.handle_payment_initiation(update, context)

        elif callback_data == "clone_paid":
            await clone_bot.handle_clone_paid(update, context)

        elif callback_data.startswith("clone_admin_approve:") or callback_data.startswith("clone_admin_reject:"):
            await clone_bot.handle_clone_admin_decision(update, context)

        elif callback_data in ["customize_name", "customize_webhook", "customize_branding", "customize_categories", "finalize_clone", "clone_back_to_customize"]:
            await clone_bot.handle_customization(update, context)

        elif callback_data in ["clone_confirm_overwrite", "clone_cancel_token"]:
            await clone_bot.handle_webhook_overwrite_confirmation(update, context)

        # Subscription/AI
        elif callback_data == "pay_paystack_ai":
            from handlers import subscription
            await subscription.handle_pay_paystack_ai(update, context)
        
        # Admin panel
        elif callback_data == "admin_panel":
            if user_id == ADMIN_ID:
                await admin_panel.show_admin_panel(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data == "admin_analytics":
            if user_id == ADMIN_ID:
                await admin_panel.show_bot_analytics(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data == "admin_revenue":
            if user_id == ADMIN_ID:
                await admin_panel.show_revenue_dashboard(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data == "admin_subscribers":
            if user_id == ADMIN_ID:
                await admin_panel.show_subscribers_list(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data == "admin_commissions":
            if user_id == ADMIN_ID:
                await query.answer("Commissions tracking coming soon", show_alert=True)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data == "admin_manage_clones":
            if user_id == ADMIN_ID:
                await admin_panel.show_clone_management(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data.startswith("clone_deactivate_"):
            if user_id == ADMIN_ID:
                await admin_panel.handle_deactivate_clone(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data.startswith("admin_grouplist_"):
            if user_id == ADMIN_ID:
                await admin_remote.handle_admin_grouplist_callback(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data.startswith("admin_target_"):
            if user_id == ADMIN_ID:
                await admin_remote.select_target_chat(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data.startswith("admin_cat_"):
            if user_id == ADMIN_ID:
                await admin_remote.show_category_commands(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data.startswith("admin_run_"):
            if user_id == ADMIN_ID:
                await admin_remote.handle_admin_run_callback(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data == "review_submissions":
            if user_id == ADMIN_ID:
                await admin_panel.review_submissions(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data == "approve_submission":
            if user_id == ADMIN_ID:
                await admin_panel.approve_submission_action(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data == "reject_submission":
            if user_id == ADMIN_ID:
                await admin_panel.reject_submission_action(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data == "skip_submission":
            if user_id == ADMIN_ID:
                await admin_panel.skip_submission_action(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data == "add_admin_note":
            await query.answer("Feature not yet implemented", show_alert=True)

        elif callback_data.startswith("remove_alert_"):
            coin = callback_data.split("_", 2)[2]
            user_id_val = update.effective_user.id
            superbot_adapter.remove_alert(user_id_val, coin)
            await query.answer(f"Alert for {coin} removed")
            await superbot_handler.show_crypto_alerts(update, context)

        # ════════════════════════════════════════════════════��══��═══
        # SUPERBOT ROUTING
        # ═══════════════════════════════════════════════════════════
        elif callback_data == "show_premium_tiers":
            await superbot_handler.show_premium_tiers(update, context)

        elif callback_data in ["tier_pro", "tier_elite"]:
            await superbot_handler.upgrade_tier(update, context)

        elif callback_data == "show_referrals":
            await superbot_handler.show_referral_stats(update, context)

        elif callback_data == "show_crypto_alerts":
            await superbot_handler.show_crypto_alerts(update, context)

        elif callback_data == "add_alert":
            await superbot_handler.start_add_alert(update, context)

        elif callback_data.startswith("select_coin_"):
            await superbot_handler.select_alert_coin(update, context)

        elif callback_data in ["alert_above", "alert_below"]:
            await superbot_handler.confirm_alert(update, context)

        elif callback_data == "show_leaderboard":
            await superbot_handler.show_leaderboard(update, context)

        # ══════════��════════════════════════════════════════════════
        # GAMES ROUTING
        # ══════════════════════════════════════════════���════════════
        elif callback_data == "m_games":
            await games_handler.show_games_menu(update, context)

        elif callback_data == "g_trivia":
            await games_handler.start_trivia(update, context)

        elif callback_data.startswith("g_ans_"):
            await games_handler.answer_trivia(update, context)

        elif callback_data == "g_riddle":
            await games_handler.start_riddle(update, context)

        elif callback_data == "g_reveal":
            await games_handler.reveal_riddle(update, context)

        elif callback_data == "g_guess":
            await games_handler.start_guess(update, context)

        elif callback_data == "g_spin":
            await games_handler.start_spin(update, context)

        # ═══════════════════════════════════════════════════════════
        # BOT MANAGER ROUTING
        # ═══════════════════════════════════════════════════════════
        elif callback_data == "m_bots":
            await bot_manager_handler.show_bot_manager(update, context)

        elif callback_data == "bot_add":
            await bot_manager_handler.start_add_bot(update, context)

        elif callback_data.startswith("bot_remove_confirm_"):
            await bot_manager_handler.confirm_remove_bot(update, context)

        elif callback_data.startswith("bot_remove_"):
            await bot_manager_handler.start_remove_bot(update, context)

        elif callback_data.startswith("bot_setname_"):
            await bot_manager_handler.start_set_name(update, context)

        elif callback_data.startswith("bot_setdesc_"):
            await bot_manager_handler.start_set_description(update, context)

        elif callback_data.startswith("bot_setcmds_"):
            await bot_manager_handler.start_set_commands(update, context)

        elif callback_data.startswith("bot_view_"):
            await bot_manager_handler.show_bot_detail(update, context)

        # ═══════════════════════════════════════════════════════════
        # MARKETPLACE ROUTING
        # ═══════════════════════════════════════════════════════════
        elif callback_data == "m_market":
            await ads_marketplace_handler.show_market_menu(update, context)

        elif callback_data == "market_add":
            await ads_marketplace_handler.start_add_listing(update, context)

        elif callback_data == "market_mine":
            await ads_marketplace_handler.show_my_listings(update, context)

        elif callback_data.startswith("market_browse_"):
            await ads_marketplace_handler.browse_market(update, context)

        elif callback_data.startswith("market_remove_"):
            await ads_marketplace_handler.remove_listing(update, context)

        elif callback_data.startswith("market_view_"):
            await ads_marketplace_handler.view_listing(update, context)

        # ═══════════════════════════════════════════════════════════
        # ADS ROUTING
        # ═══════════════════════════════════════════════════════════
        elif callback_data == "m_ads":
            await ads_marketplace_handler.show_ads_menu(update, context)

        elif callback_data == "ads_submit":
            await ads_marketplace_handler.start_submit_ad(update, context)

        elif callback_data.startswith("ad_approve_"):
            await ads_marketplace_handler.handle_approve_ad(update, context)

        elif callback_data.startswith("ad_reject_"):
            await ads_marketplace_handler.start_reject_ad(update, context)

        elif callback_data == "show_stats":
            await superbot_handler.show_user_stats(update, context)

        elif callback_data == "global_analytics":
            await superbot_handler.show_global_analytics(update, context)

        # ═══════════════════════════════════════════════════════════
        # ADMIN CONFIG ROUTING
        # ═══════════════════════════════════════════════════════════
        elif callback_data == "admin_config":
            if user_id == ADMIN_ID:
                await admin_config.show_config_panel(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data.startswith("cfg_") or callback_data.startswith("edit_"):
            if user_id == ADMIN_ID:
                await admin_config.handle_config_callback(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        # ═══════════════════════════════════════════════════════════
        # BROADCAST ROUTING
        # ═══════════════════════════════════════════════════════════
        elif callback_data == "admin_broadcast_start":
            if user_id == ADMIN_ID:
                await broadcast_handler.start_broadcast_button(update, context)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data.startswith("broadcast_scope_"):
            if user_id == ADMIN_ID:
                await broadcast_handler.handle_broadcast_scope_callback(
                    update, context, callback_data[len("broadcast_scope_"):]
                )
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data.startswith("broadcast_exempt_toggle_"):
            if user_id == ADMIN_ID:
                await broadcast_handler.handle_broadcast_exempt_callback(
                    update, context, callback_data[len("broadcast_exempt_toggle_"):]
                )
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data == "broadcast_exempt_done":
            if user_id == ADMIN_ID:
                await broadcast_handler.handle_broadcast_exempt_callback(update, context, "done")
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data == "broadcast_exempt_back":
            if user_id == ADMIN_ID:
                await broadcast_handler.handle_broadcast_exempt_callback(update, context, "back")
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data == "broadcast_confirm_yes":
            if user_id == ADMIN_ID:
                await broadcast_handler.handle_broadcast_confirm_callback(update, context, True)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data == "broadcast_confirm_no":
            if user_id == ADMIN_ID:
                await broadcast_handler.handle_broadcast_confirm_callback(update, context, False)
            else:
                await query.answer("Unauthorized", show_alert=True)

        elif callback_data.startswith("broadcast_continue_"):
            if user_id == ADMIN_ID:
                job_id = int(callback_data[len("broadcast_continue_"):])
                await broadcast_handler.handle_broadcast_continue_callback(update, context, job_id)
            else:
                await query.answer("Unauthorized", show_alert=True)

        # ═══════════════════════════════════════════════════════════
        # PREMIUM GROUP PAYWALL (attached to every broadcast — public,
        # any recipient can tap these, not admin-only)
        # ═══════════════════════════════════════════════════════════
        elif callback_data == "premium_pay_init":
            await premium_group_handler.handle_premium_pay_init(update, context)

        else:
            await safe_edit_message(query, "Option not yet implemented.")

    except BadRequest as e:
        # Telegram's harmless "nothing actually changed" error — thrown by any
        # of the ~150 raw query.edit_message_text/edit_message_caption call
        # sites across the handlers (not just the ones already routed through
        # safe_edit_message) whenever a button is tapped that would redraw an
        # identical message. It's a no-op, not a real failure, so it's
        # swallowed here at the single top-level callback dispatcher instead
        # of needing every call site individually wrapped.
        if "message is not modified" not in str(e).lower():
            print(f"[v0] Error in handle_callback: {e}")
            notify_admin_of_error(f"Callback error: {e}")
    except Exception as e:
        print(f"[v0] Error in handle_callback: {e}")
        notify_admin_of_error(f"Callback error: {e}")

    # Clear the loading spinner for any branch that didn't already answer.
    # Safe to call even if a branch above already answered with an alert -
    # Telegram just ignores/errors on the extra call and we swallow that here.
    try:
        await query.answer()
    except Exception:
        pass


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular messages with context-aware routing"""
    text = update.message.text
    user_id = update.effective_user.id

    try:
        # Cold-start recovery: repopulate any wizard step/draft data that was
        # set on a different serverless instance (see flow_state.py). No-op
        # if context.user_data already has flow state (warm-instance path).
        await flow_state.hydrate(context, user_id, _clone_id(context))

        # Group Tools quick action (Warn/Mute/Ban tap-then-reply flow)
        if context.user_data.get("group_quick_action"):
            await moderation.handle_group_quick_action_message(update, context)
            return

        # Admin panel: next DM message is the arguments for a remote group/channel command
        if context.user_data.get("admin_remote_cmd"):
            await admin_remote.handle_admin_remote_command_message(update, context)
            return

        # Admin is pasting the URL for a custom label->link button (feature #8)
        if context.user_data.get("awaiting_link_button_url"):
            await link_buttons.handle_link_button_url_message(update, context)
            return

        # Check for rejecting submission (admin typing rejection reason)
        if context.user_data.get("rejecting_submission"):
            submission_id = context.user_data.get("rejecting_submission")
            reason = text if text.lower() not in ("skip", "/skip") else "No reason given"
            
            await db.reject_submission(submission_id, reason)
            await update.message.reply_text(
                f"{EMOJI_COLORS.get('success', '✅')} Submission rejected.",
                reply_markup=keyboard_gen.admin_dashboard_keyboard()
            )
            context.user_data.pop("rejecting_submission", None)
            return

        # Autopost: awaiting the text content to repeat
        if context.user_data.get("mode") == "autopost_await_content":
            await autopost_handler.handle_autopost_content(update, context)
            return

        # Broadcast: awaiting the text content to send
        if context.user_data.get("mode") == "broadcast_await_content":
            await broadcast_handler.handle_broadcast_content(update, context)
            return

        # Broadcast: awaiting an optional per-broadcast "Join Group/Channel" link
        if context.user_data.get("mode") == "broadcast_await_joinlink":
            await broadcast_handler.handle_broadcast_joinlink_message(update, context)
            return

        # Check for clone customization steps
        if context.user_data.get("customize_step"):
            await clone_bot.handle_customization_message(update, context)
            return

        # Check for editing an existing clone's name/branding/categories
        if context.user_data.get("editing_clone_field"):
            await clone_bot.handle_clone_edit_message(update, context)
            return

        # Check for a clone owner pasting their Selar/Stripe key
        if context.user_data.get("awaiting_payment_key"):
            await clone_bot.handle_payment_key_message(update, context)
            return

        # Check for a clone owner typing a new price for one of their features
        if context.user_data.get("awaiting_price_edit"):
            await clone_bot.handle_price_message(update, context)
            return

        # Check for AI preference awaiting
        if context.user_data.get("awaiting_preference"):
            from groq_service import groq_service
            lang = await db.get_user_language(update.effective_user.id, clone_id=_clone_id(context))
            recommendation = await groq_service.get_anime_recommendation(text, [], language=lang)
            await update.message.reply_text(recommendation, reply_markup=keyboard_gen.main_menu())
            context.user_data.pop("awaiting_preference", None)
            await flow_state.clear(context, user_id, _clone_id(context))
            return

        # Check for crypto alert setup
        if context.user_data.get("alert_step") in [1, 2]:
            await superbot_handler.process_alert_message(update, context)
            return

        # Check for riddle game answer
        if context.user_data.get("mode") == "riddle":
            await games_handler.handle_riddle_message(update, context)
            return

        # Check for number-guess game answer
        if context.user_data.get("mode") == "guess":
            await games_handler.handle_guess_message(update, context)
            return

        # Check for Bot Manager: add a bot via token
        if context.user_data.get("mode") == "addbot":
            await bot_manager_handler.handle_add_bot_message(update, context)
            return

        # Check for Bot Manager: set name / description / commands
        _mode = context.user_data.get("mode") or ""
        if _mode.startswith("botmgr_setname_"):
            await bot_manager_handler.handle_setname_message(update, context, int(_mode.split("_")[-1]))
            return
        if _mode.startswith("botmgr_setdesc_"):
            await bot_manager_handler.handle_setdesc_message(update, context, int(_mode.split("_")[-1]))
            return
        if _mode.startswith("botmgr_setcmds_"):
            await bot_manager_handler.handle_setcmds_message(update, context, int(_mode.split("_")[-1]))
            return

        # Check for Marketplace: multi-step listing creation
        if _mode.startswith("market_add_"):
            await ads_marketplace_handler.handle_market_add_message(update, context)
            return

        # Check for Ads: multi-step submission
        if _mode.startswith("ads_submit_"):
            await ads_marketplace_handler.handle_ads_submit_message(update, context)
            return

        # Check for Ads: admin rejection reason
        if _mode.startswith("ads_reject_reason_"):
            await ads_marketplace_handler.handle_reject_reason_message(update, context, int(_mode.split("_")[-1]))
            return

        # Check for admin config input
        if context.user_data.get("editing_config_field"):
            if user_id == ADMIN_ID:
                await admin_config.handle_config_message(update, context)
            return

        # AI Chat waiting mode: lowest priority on purpose. This flag is
        # sticky (only cleared by /cancel or the Exit AI Chat button), so if
        # it were checked first it would hijack the next message of ANY
        # other flow the user starts afterward (submissions, broadcast
        # content, clone editing, admin rejection reasons, etc.). Every
        # more specific flow above gets first claim on the message; AI
        # chat only takes it if nothing else is currently in progress.
        if context.user_data.get("awaiting_ai_message"):
            await ai_handler.handle_ai_waiting_message(update, context)
            return

        # Fallback to emoji check (legacy)
        if "🔍" in text or text.lower().startswith("search"):
            context.user_data["mode"] = "search"
            await search.start_search(update, context)
        elif "📤" in text or text.lower().startswith("submit"):
            await submit.start_submission(update, context)
        elif "🤖" in text and "clone" in text.lower():
            if context.bot_data.get("clone_config"):
                await update.message.reply_text(
                    "This is already a cloned bot — cloning isn't available from here.",
                    reply_markup=keyboard_gen.main_menu(clone_mode=True, clone_id=context.bot_data.get("clone_config", {}).get("clone_id"))
                )
            else:
                await update.message.reply_text(
                    AnimeFormatter.format_clone_info(),
                    reply_markup=keyboard_gen.clone_payment_keyboard(CLONE_BOT_FEE_GHS),
                    parse_mode="Markdown"
                )
        elif "💎" in text or "premium" in text.lower():
            await superbot_handler.show_premium_tiers(update, context)
        elif "🏆" in text or "leaderboard" in text.lower():
            await superbot_handler.show_leaderboard(update, context)
        elif "🧰" in text or text.lower().startswith("tools"):
            await update.message.reply_text(
                "🧰 **Tools**\n\nAI, market data, downloads, games, and more.",
                reply_markup=keyboard_gen.tools_menu(),
                parse_mode="Markdown"
            )
        elif "🛡️" in text or text.lower().startswith("group tools"):
            is_group = update.effective_chat.type in ("group", "supergroup")
            await update.message.reply_text(
                "🛡️ **Group Tools**\n\nModeration commands for group admins.",
                reply_markup=keyboard_gen.grouptools_menu(is_group=is_group),
                parse_mode="Markdown"
            )
        elif "☰" in text or text.lower() in ("all commands", "commands"):
            await show_all_commands(update, context)
        elif "🏠" in text or text.lower().startswith("menu"):
            clone_config = context.bot_data.get("clone_config")
            await update.message.reply_text(
                "🏠 Main Menu",
                reply_markup=keyboard_gen.main_menu(clone_mode=bool(clone_config), clone_id=(clone_config or {}).get("clone_id"))
            )

    except Exception as e:
        print(f"[v0] Error in handle_message: {e}")
        notify_admin_of_error(f"Message handler error: {e}")


async def handle_media_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes photo/video/document/animation messages to whichever mode-based
    flow is waiting for media content (autopost setup, broadcast setup).
    A bare photo with no such mode active goes to Reverse Image Search
    instead of being silently ignored."""
    try:
        await flow_state.hydrate(context, update.effective_user.id, _clone_id(context))
        mode = context.user_data.get("mode")
        if mode == "autopost_await_content":
            await autopost_handler.handle_autopost_content(update, context)
        elif mode == "broadcast_await_content":
            await broadcast_handler.handle_broadcast_content(update, context)
        elif mode == "broadcast_await_joinlink":
            await update.message.reply_text("Send the invite link as text (or /skip) — not a photo/video/file.")
    except Exception as e:
        print(f"[v0] Error in handle_media_message: {e}")
        notify_admin_of_error(f"Media handler error: {e}")


_CANCELLABLE_MODE_KEYS = (
    "mode", "autopost_target_chat_id", "autopost_interval_minutes",
    "broadcast_draft", "broadcast_scope", "broadcast_exempt_groups",
    "awaiting_ai_message", "awaiting_download_link", "utility_payment_reference",
    "group_quick_action", "group_quick_action_chat_id", "awaiting_link_button_url",
    "admin_remote_cmd", "admin_remote_chat", "admin_target_chat", "admin_chat_cache",
    "awaiting_payment_key", "awaiting_price_edit",
)


async def cancel_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generic /cancel — clears any in-progress conversational mode (autopost
    setup, broadcast setup, search, etc.) so the user isn't stuck mid-flow.
    Also clears the DB-backed wizard snapshot (flow_state.py) - otherwise a
    cancelled flow could get resurrected on the next cold start."""
    for key in _CANCELLABLE_MODE_KEYS:
        context.user_data.pop(key, None)
    await flow_state.clear(context, update.effective_user.id, _clone_id(context))
    await update.message.reply_text("Cancelled.")


async def show_clone_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    'ℹ️ About / How This Bot Works' — clone-only. A full explainer, not a
    one-line summary, because clone owners hand this bot to their own
    community and get asked "what even is this" constantly. Split into two
    messages since the full explanation runs past Telegram's 4096-char cap.
    """
    clone_config = context.bot_data.get("clone_config")
    clone_name = (clone_config.get("name") or clone_config.get("bot_name") or "This bot") if clone_config else "This bot"

    part1 = (
        f"ℹ️ **About {esc_md(clone_name)} — How This Bot Works**\n\n"
        f"**What this is**\n"
        f"{esc_md(clone_name)} is a *clone* — an independent, fully working copy of a larger "
        f"anime, group-management, and utility bot. It isn't a stripped-down demo or a bot that "
        f"just forwards your messages somewhere else: it runs its own Telegram bot account, has "
        f"its own webhook, its own users, and (if the owner has activated it) its own billing, "
        f"while sharing the same underlying anime database, search index, and feature code as "
        f"every other bot in this network. Think of it the way a franchise location works — same "
        f"menu and same kitchen equipment as headquarters, but a different storefront, a different "
        f"owner, and its own customers.\n\n"
        f"**Why clones exist**\n"
        f"Building and hosting a Telegram bot with anime discovery, moderation tools, AI chat, "
        f"downloaders, and a store from scratch is slow and expensive. Instead, one person creates "
        f"a bot with @BotFather, pastes the token into the main bot's \"Clone Bot\" flow, pays a "
        f"one-time setup fee, and instantly has a bot with all of that functionality live under "
        f"their own name and branding — no server to rent, no code to write, no database to manage.\n\n"
        f"**What you can do here**\n"
        f"• **🎬 Anime** — search titles, browse trending/latest/ongoing/seasonal releases, and "
        f"submit anime that isn't in the database yet for review.\n"
        f"• **🛡️ Group Tools** — add this bot to a group or channel you admin to get warnings, "
        f"bans, mutes, kicks, word filters, configurable moderation settings, a rules command, "
        f"and basic group activity stats.\n"
        f"• **🧰 Tools** — an AI chat assistant, AI image generation, crypto and stock price "
        f"lookups, currency conversion, news lookups by topic, and a link-based video/audio "
        f"downloader.\n"
              f"• **⭐ Premium** — paid tiers that unlock extra usage limits and features for you as an "
        f"individual user of this bot.\n"
    )

    part2 = (
        f"**Who runs it, and who gets your money**\n"
        f"This bot has an *owner* — the person who created this clone — who is not the same as "
        f"whoever runs the main network. By default, any payments made inside this clone (premium "
        f"upgrades, store listings, etc.) are processed through the main bot's shared payment "
        f"account at standard prices. If the owner activates **💰 Monetization** (a recurring fee "
        f"paid to the network), they unlock the ability to connect their *own* Selar or Stripe "
        f"account and set their *own* prices, so revenue from this specific bot goes directly to "
        f"them instead of through the shared account.\n\n"
        f"**Data and privacy**\n"
        f"Your searches, group settings, and submissions are stored against this clone's own "
        f"ID, kept separate from every other clone's data, even though the underlying anime "
        f"catalog and code are shared infrastructure. The clone owner does not get access to "
        f"other clones' users, and other clones' owners do not get access to this bot's users.\n\n"
        f"**Reliability**\n"
        f"Even though many clones run on shared code, each one has its own Telegram webhook "
        f"registration, so one clone having issues does not take down another. If this specific "
        f"bot ever stops responding, it's almost always either the owner's bot token being revoked, "
        f"the owner's account lapsing, or a temporary hosting issue — not something wrong with your "
        f"account.\n\n"
        f"**In short**\n"
        f"You're talking to a real, independently-owned bot that happens to share its engine with "
        f"a wider network. Everything in the menus below — search, group tools, AI tools, the "
        f"store, premium — works the same way it would on any other bot in this network, just "
        f"under this bot's name and (if the owner set one) this bot's own branding."
    )

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]])
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await safe_edit_message(query, part1, parse_mode="Markdown")
        await safe_send_message(query.message, part2, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await safe_send_message(update.message, part1, parse_mode="Markdown")
        await safe_send_message(update.message, part2, reply_markup=keyboard, parse_mode="Markdown")


async def show_all_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    ☰ All Commands — the in-chat version of Telegram's native chat-menu button
    (set up via set_my_commands/set_chat_menu_button in get_application() and
    get_clone_application()). Works whether triggered by a button tap or by
    typing "menu"/"commands".
    """
    text = (
  "☰ **All Commands**\n\n"

        "**🛡️ Group** (admin, in-group)\n/warn /ban /mute /kick /filter /modsettings /rules /groupstats\n\n"
        "**🧰 Tools**\n/ai /aiimage /crypto /stock /convert /news /download\n\n"
        "**⚙️ Account**\n/subscribe /stats /alerts /referrals /cancel"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]])
    if update.callback_query:
        await safe_edit_message(update.callback_query, text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


# Command catalog used for Telegram's native chat-menu button (the small ☰
# icon next to the message box — this is the actual "floating commands
# button" Telegram provides; set_my_commands populates it).
PRIVATE_CHAT_COMMANDS = [
    ("start", "Open the main menu"),
      ("premium", "View premium tiers"),
    ("ai", "Chat with AI"),
    ("crypto", "Crypto price lookup"),
    ("stock", "Stock price lookup"),
    ("convert", "Currency converter"),
    ("news", "Latest news on a topic"),
    ("download", "Download video/audio from a link"),
    ("subscribe", "Manage your subscription"),
    ("referrals", "Your referral stats"),
    ("leaderboard", "Top users leaderboard"),
    ("cancel", "Cancel the current action"),
]

GROUP_CHAT_COMMANDS = [
    ("warn", "Warn a user (reply to them)"),
    ("ban", "Ban a user (reply to them)"),
    ("mute", "Mute a user (reply to them)"),
    ("kick", "Kick a user (reply to them)"),
    ("filter", "Add a word filter"),
    ("modsettings", "View/change moderation settings"),
    ("rules", "Show group rules"),
    ("groupstats", "Group activity stats"),
]


async def _set_native_command_menu(application: Application):
    """
    Registers Telegram's native chat-menu button (the ☰ icon by the message
    box that opens a scrollable command list) — scoped separately for private
    chats vs groups so group-only admin commands don't clutter a DM menu.
    Safe to call on every cold start; failures are logged, never fatal.
    """
    try:
        from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, MenuButtonCommands
        await application.bot.set_my_commands(
            [BotCommand(c, d) for c, d in PRIVATE_CHAT_COMMANDS],
            scope=BotCommandScopeAllPrivateChats(),
        )
        await application.bot.set_my_commands(
            [BotCommand(c, d) for c, d in GROUP_CHAT_COMMANDS],
            scope=BotCommandScopeAllGroupChats(),
        )
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:
        print(f"[v0] Could not set native command menu: {e}")


def _register_moderation_handlers(app: Application):
    """Group management commands + the message/join hooks they depend on.
    Shared between the main bot and every clone Application."""
    app.add_handler(CommandHandler("warn", moderation.warn_command))
    app.add_handler(CommandHandler("unwarn", moderation.unwarn_command))
    app.add_handler(CommandHandler("warns", moderation.warns_command))
    app.add_handler(CommandHandler("ban", moderation.ban_command))
    app.add_handler(CommandHandler("unban", moderation.unban_command))
    app.add_handler(CommandHandler("kick", moderation.kick_command))
    app.add_handler(CommandHandler("mute", moderation.mute_command))
    app.add_handler(CommandHandler("unmute", moderation.unmute_command))
    app.add_handler(CommandHandler("filter", moderation.filter_add_command))
    app.add_handler(CommandHandler("unfilter", moderation.filter_remove_command))
    app.add_handler(CommandHandler("filters", moderation.filter_list_command))
    app.add_handler(CommandHandler("setcmd", moderation.setcmd_command))
    app.add_handler(CommandHandler("delcmd", moderation.delcmd_command))
    app.add_handler(CommandHandler("listcmds", moderation.listcmds_command))
    app.add_handler(CommandHandler("setgate", moderation.setgate_command))
    app.add_handler(CommandHandler("gate", moderation.gate_toggle_command))
    app.add_handler(CommandHandler("modsettings", moderation.modsettings_command))
    app.add_handler(CommandHandler("del", moderation.del_command))
    app.add_handler(CommandHandler("pin", moderation.pin_command))
    app.add_handler(CommandHandler("groupstats", moderation.stats_command))
    app.add_handler(CommandHandler("rules", moderation.rules_command))
    app.add_handler(CommandHandler("setwelcome", moderation.setwelcome_command))
    app.add_handler(CommandHandler("setpaybutton", moderation.setpaybutton_command))
    app.add_handler(CommandHandler("removepaybutton", moderation.removepaybutton_command))
    app.add_handler(CommandHandler("addlinkbutton", link_buttons.addlinkbutton_command))
    app.add_handler(CommandHandler("listlinkbuttons", link_buttons.listlinkbuttons_command))
    app.add_handler(CommandHandler("removelinkbutton", link_buttons.removelinkbutton_command))
    app.add_handler(CommandHandler("whitelistdomain", moderation.whitelistdomain_command))
    app.add_handler(CommandHandler("unwhitelistdomain", moderation.unwhitelistdomain_command))
    app.add_handler(CommandHandler("listwhitelist", moderation.listwhitelist_command))
    app.add_handler(CommandHandler("setspamrules", moderation.setspamrules_command))
    app.add_handler(CommandHandler("setautomute", moderation.setautomute_command))
    app.add_handler(CommandHandler("setwarnlimit", moderation.setwarnlimit_command))
    app.add_handler(CommandHandler("setpintag", moderation.setpintag_command))
    app.add_handler(CommandHandler("setjoinlink", moderation.setjoinlink_command))
    app.add_handler(CommandHandler("logs", moderation.logs_command))
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, moderation.greet_new_member),
        group=-1,
    )
    app.add_handler(
        MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, moderation.handle_member_left),
        group=-1,
    )
    # Verification buttons (captcha / join gate) — registered with a pattern
    # and in an earlier group than the catch-all CallbackQueryHandler below,
    # since PTB stops at the first handler in a group whose filter matches.
    app.add_handler(
        CallbackQueryHandler(moderation.handle_verify_callback, pattern=r"^(captcha_verify|gate_verify):"),
        group=-1,
    )
    # Word filter + "!command" custom commands — must run before the general
    # catch-all text handler, and only in groups (DMs have no moderation).
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, moderation.handle_group_text),
        group=-1,
    )


def get_application() -> Application:
    """Build (once) and reuse the Application across warm invocations"""
    global _app
    if _app is None:
        _app = Application.builder().token(BOT_TOKEN).build()
        # Run initialization on first startup
        async def startup(app):
            await initialize_system()
            await _set_native_command_menu(app)
        _app.post_init = startup
        _app.add_handler(CommandHandler("start", start))
        _app.add_handler(CommandHandler("admin", admin_panel.admin_command))
        from handlers import subscription
        _app.add_handler(CommandHandler("subscribe", subscription.handle_subscribe_ai))
        _app.add_handler(CommandHandler("ai_recommend", subscription.handle_ai_recommendation))
        _app.add_handler(CommandHandler("ai_summary", subscription.handle_ai_summary))

        # SuperBot commands
        _app.add_handler(CommandHandler("premium", superbot_handler.show_premium_tiers))
        _app.add_handler(CommandHandler("referrals", superbot_handler.show_referral_stats))
        _app.add_handler(CommandHandler("leaderboard", superbot_handler.show_leaderboard))
        _app.add_handler(CommandHandler("stats", superbot_handler.show_user_stats))
        _app.add_handler(CommandHandler("alerts", superbot_handler.show_crypto_alerts))
        # Admin config
        _app.add_handler(CommandHandler("config", admin_config.show_config_panel))
        _app.add_handler(CommandHandler("envcheck", admin_config.cmd_envcheck))
        _app.add_handler(CommandHandler("getchatid", admin_config.cmd_getchatid))
        _app.add_handler(CommandHandler("testlog", admin_config.cmd_testlog))
        _app.add_handler(CommandHandler("setpremium", admin_config.cmd_setpremium))
        _app.add_handler(CommandHandler("confirmpay", admin_config.cmd_confirmpay))
        
        # External Integrations (Items 1-2 from backlog)
        _app.add_handler(CommandHandler("news", external_handler.news_command))
        _app.add_handler(CommandHandler("convert", external_handler.convert_command))
        _app.add_handler(CommandHandler("stock", external_handler.stock_command))
        _app.add_handler(CommandHandler("crypto", external_handler.crypto_command))
        
        # AI Features (Items 1-2 from backlog - AI Chat & Image Generation)
        _app.add_handler(CommandHandler("ai", ai_handler.ai_chat_handler))
        _app.add_handler(CommandHandler("aichat", ai_handler.ai_chat_handler))
        _app.add_handler(CommandHandler("aiimage", ai_handler.ai_image_handler))
        _app.add_handler(CommandHandler("aistatus", ai_handler.ai_status_handler))

        # Autopost (recurring posts) & one-off broadcast
        _app.add_handler(CommandHandler("setrecurring", autopost_handler.cmd_setrecurring))
        _app.add_handler(CommandHandler("stoprecurring", autopost_handler.cmd_stoprecurring))
        _app.add_handler(CommandHandler("listrecurring", autopost_handler.cmd_listrecurring))
        _app.add_handler(CommandHandler("broadcast", broadcast_handler.cmd_broadcast))
        _app.add_handler(CommandHandler("cancel", cancel_mode_command))
        # /skip is only meaningful mid-wizard (e.g. broadcast_await_joinlink,
        # rejecting_submission). It's typed as a command, so the generic
        # `filters.TEXT & ~filters.COMMAND` handler below never sees it -
        # route it into the same mode-dispatch logic as handle_message.
        _app.add_handler(CommandHandler("skip", handle_message))

        # Admin tooling — these existed for clones (_register_shared_handlers)
        # but were never registered on the main bot, so /botmanager etc. never
        # worked here even though the matching inline buttons did (buttons
        # route by callback_data, not by command registration).
        _app.add_handler(CommandHandler("botmanager", bot_manager_handler.show_bot_manager))
        _app.add_handler(CommandHandler("pendingads", ads_marketplace_handler.cmd_pending_ads))
        _app.add_handler(CommandHandler("analytics", admin_tools.cmd_analytics))
        _app.add_handler(CommandHandler("exportusers", admin_tools.cmd_exportusers))
        _app.add_handler(CommandHandler("registerme", admin_tools.cmd_registerme))
        _app.add_handler(CommandHandler("addsponsor", admin_tools.cmd_addsponsor))
        
        # ═══════════════════════════════════════════════════════════════════════════
        # 20 ADVANCED FEATURES - Handler Registration
        # ═══════════════════════════════════════════════════════════════════════════
        from telegram.ext import InlineQueryHandler, ChosenInlineResultHandler, ChatMemberHandler
        from telegram.ext import PreCheckoutQueryHandler, ShippingQueryHandler
        from telegram.ext import PollAnswerHandler, MessageReactionHandler, ChatJoinRequestHandler
        
        # Feature 1-2: Inline Query & Chosen Results
        _app.add_handler(InlineQueryHandler(feature_handlers.handle_inline_query))
        _app.add_handler(ChosenInlineResultHandler(feature_handlers.handle_chosen_inline_result))
        
        # Feature 3-4: Chat Member Tracking
        _app.add_handler(ChatMemberHandler(feature_handlers.handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
        _app.add_handler(ChatMemberHandler(feature_handlers.handle_chat_member, ChatMemberHandler.CHAT_MEMBER))

        # Auto-DM on Join Request (feature: autodmjoin) — fires when someone
        # taps a "request to join" invite link, before they're actually a
        # group member yet.
        _app.add_handler(ChatJoinRequestHandler(moderation.handle_join_request))
        
        # Feature 5-6: Payment Handlers
        _app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, feature_handlers.handle_successful_payment))
        _app.add_handler(PreCheckoutQueryHandler(feature_handlers.handle_pre_checkout_query))
        
        # Feature 7: Shipping
        _app.add_handler(ShippingQueryHandler(feature_handlers.handle_shipping_query))
        
        # Feature 9: Edited Messages
        _app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.TEXT, feature_handlers.handle_edited_message))
        
        # Feature 10: Message Reactions
        _app.add_handler(MessageReactionHandler(feature_handlers.handle_message_reaction))
        
        # Feature 11: Poll Answer
        _app.add_handler(PollAnswerHandler(feature_handlers.handle_poll_answer))
        
        # Feature 12: Dice
        _app.add_handler(MessageHandler(filters.Dice.ALL, feature_handlers.handle_dice))
        
        # Feature 14: Web App Data
        _app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, feature_handlers.handle_web_app_data))
        
        # Feature 15: Passport
        _app.add_handler(MessageHandler(filters.PASSPORT_DATA, feature_handlers.handle_passport_data))
        
        # Feature 16: Location
        _app.add_handler(MessageHandler(filters.LOCATION, feature_handlers.handle_location))
        
        # Feature 18: User Shared
        _app.add_handler(MessageHandler(filters.StatusUpdate.USERS_SHARED, feature_handlers.handle_user_shared))
        
        # Feature 20: Write Access Allowed
        _app.add_handler(MessageHandler(filters.StatusUpdate.WRITE_ACCESS_ALLOWED, feature_handlers.handle_write_access_allowed))
        
        # Feature 19: Deep Links (handled in /start command handler)
        
        # Media content capture for autopost/broadcast setup (a group admin or
        # the bot owner sending a photo/video/document/animation as the thing
        # to repeat/broadcast, after /setrecurring or /broadcast)
        _app.add_handler(MessageHandler(
            (filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION) & ~filters.COMMAND,
            handle_media_message
        ))

        # Default handlers
        _app.add_handler(CallbackQueryHandler(handle_callback))
        _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        # Group moderation (warn/ban/mute/filter/captcha/join-gate/custom commands)
        _register_moderation_handlers(_app)

        async def error_handler(update, context):
            print(f"[v0] Exception while handling update: {context.error}")
            traceback.print_exception(type(context.error), context.error, context.error.__traceback__)
            try:
                notify_admin_of_error(f"Bot error: {context.error}")
            except Exception:
                pass

        _app.add_error_handler(error_handler)
    return _app


def _register_shared_handlers(app: Application):
    """
    Register the same handler set the main bot uses. Called for both the main
    Application and every per-clone Application, so clones get the exact same
    feature surface (discovery/search/submit/etc.) — only bot_data["clone_config"]
    differs, which handlers read to swap branding text (Part 3.1).
    """
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("language", language_handler.language_command))
    app.add_handler(CommandHandler("admin", admin_panel.admin_command))
    from handlers import subscription
    app.add_handler(CommandHandler("subscribe", subscription.handle_subscribe_ai))
    app.add_handler(CommandHandler("ai_recommend", subscription.handle_ai_recommendation))
    app.add_handler(CommandHandler("ai_summary", subscription.handle_ai_summary))
    app.add_handler(CommandHandler("premium", superbot_handler.show_premium_tiers))
    app.add_handler(CommandHandler("referrals", superbot_handler.show_referral_stats))
    app.add_handler(CommandHandler("leaderboard", superbot_handler.show_leaderboard))
    app.add_handler(CommandHandler("stats", superbot_handler.show_user_stats))
    app.add_handler(CommandHandler("alerts", superbot_handler.show_crypto_alerts))
    app.add_handler(CommandHandler("news", external_handler.news_command))
    app.add_handler(CommandHandler("convert", external_handler.convert_command))
    app.add_handler(CommandHandler("stock", external_handler.stock_command))
    app.add_handler(CommandHandler("crypto", external_handler.crypto_command))
    app.add_handler(CommandHandler("ai", ai_handler.ai_chat_handler))
    app.add_handler(CommandHandler("aichat", ai_handler.ai_chat_handler))
    app.add_handler(CommandHandler("aiimage", ai_handler.ai_image_handler))
    app.add_handler(CommandHandler("aistatus", ai_handler.ai_status_handler))
    app.add_handler(CommandHandler("botmanager", bot_manager_handler.show_bot_manager))
    app.add_handler(CommandHandler("setrecurring", autopost_handler.cmd_setrecurring))
    app.add_handler(CommandHandler("stoprecurring", autopost_handler.cmd_stoprecurring))
    app.add_handler(CommandHandler("listrecurring", autopost_handler.cmd_listrecurring))
    app.add_handler(CommandHandler("broadcast", broadcast_handler.cmd_broadcast))
    app.add_handler(CommandHandler("pendingads", ads_marketplace_handler.cmd_pending_ads))
    app.add_handler(CommandHandler("analytics", admin_tools.cmd_analytics))
    app.add_handler(CommandHandler("exportusers", admin_tools.cmd_exportusers))
    app.add_handler(CommandHandler("registerme", admin_tools.cmd_registerme))
    app.add_handler(CommandHandler("addsponsor", admin_tools.cmd_addsponsor))
    app.add_handler(CommandHandler("cancel", cancel_mode_command))
    # See comment on the clone-app registration above: /skip needs its own
    # CommandHandler because filters.TEXT & ~filters.COMMAND drops it.
    app.add_handler(CommandHandler("skip", handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Auto-DM on Join Request (feature: autodmjoin) — same as the main bot,
    # so clone-bot groups get this too.
    from telegram.ext import ChatJoinRequestHandler
    app.add_handler(ChatJoinRequestHandler(moderation.handle_join_request))

    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION) & ~filters.COMMAND,
        handle_media_message
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Group moderation (warn/ban/mute/filter/captcha/join-gate/custom commands)
    _register_moderation_handlers(app)

    async def error_handler(update, context):
        print(f"[v0] Exception while handling update (clone_id={context.bot_data.get('clone_config', {}).get('clone_id')}): {context.error}")
        traceback.print_exception(type(context.error), context.error, context.error.__traceback__)
        try:
            notify_admin_of_error(f"Bot error: {context.error}")
        except Exception:
            pass

    app.add_error_handler(error_handler)


def invalidate_clone_cache(clone_id: int):
    """Drop a clone's cached Application so the next update rebuilds it from
    the DB — used after editing a clone's name/branding/categories so the
    change is visible immediately instead of waiting for LRU eviction."""
    _clone_apps.pop(clone_id, None)
    _clone_apps_initialized.discard(clone_id)


async def get_clone_application(clone_id: int):
    """
    Lazily build (or return cached) an Application for a specific clone.

    Returns (application, clone_row) or (None, None) if the clone doesn't
    exist / isn't active / fails to decrypt — caller treats that as "drop the
    update, log it, still return 200 to Telegram" per Part 3.2 Step D.
    """
    if clone_id in _clone_apps:
        _clone_apps.move_to_end(clone_id)  # LRU touch
        return _clone_apps[clone_id], None

    clone = await db.get_clone_for_routing(clone_id)
    if clone is None:
        return None, None

    application = Application.builder().token(clone["bot_token"]).build()
    _register_shared_handlers(application)
    application.bot_data["clone_config"] = {
        "clone_id": clone["clone_id"],
        "owner_id": clone["owner_id"],
        "bot_name": clone["bot_name"],
        "bot_username": clone["bot_username"],
        "webhook_secret": clone["webhook_secret"],
        **(clone.get("custom_data") or {}),
    }

    _clone_apps[clone_id] = application
    _clone_apps.move_to_end(clone_id)
    while len(_clone_apps) > CLONE_APP_CACHE_SIZE:
        evicted_id, evicted_app = _clone_apps.popitem(last=False)
        _clone_apps_initialized.discard(evicted_id)
        logger.info(f"[v0] Evicted clone_id={evicted_id} Application from warm cache (LRU cap {CLONE_APP_CACHE_SIZE})")

    return application, clone


async def process_update(update_data: dict, clone_id: int = None):
    """
    Initialize (once per cold start, or once per clone per cold start) and
    process a single Telegram update. If clone_id is given, route it through
    that clone's own Application with its own bot_data["clone_config"]
    (Part 3.2 Step D) instead of the main bot.
    """
    global _db_initialized

    if not _db_initialized:
        # Shared DB init happens once regardless of which bot/clone triggers it.
        await db.init()
        _db_initialized = True

    if clone_id is not None:
        application, _ = await get_clone_application(clone_id)
        if application is None:
            logger.warning(f"[v0] Dropping update for unknown/inactive clone_id={clone_id}")
            return
        if clone_id not in _clone_apps_initialized:
            await application.initialize()
            await _set_native_command_menu(application)
            _clone_apps_initialized.add(clone_id)
        update = Update.de_json(update_data, application.bot)
        await application.process_update(update)
        return

    application = get_application()
    if "main" not in _clone_apps_initialized:
        await application.initialize()
        await _set_native_command_menu(application)
        _clone_apps_initialized.add("main")
    update = Update.de_json(update_data, application.bot)
    await application.process_update(update)


class handler(BaseHTTPRequestHandler):
    """Vercel serverless handler for Telegram webhooks"""

    def do_POST(self):
        """
        Handle POST request from Telegram.

        Routes on the `clone_id` query param (Part 3.1/3.2 Step D):
        - absent -> main bot, unchanged behavior, global WEBHOOK_SECRET applies.
        - present -> that clone's own Application, verified against THAT
          clone's own webhook_secret (never the global one), so one compromised
          clone secret can't forge updates for a different clone or the main bot.

        On any lookup/secret failure we still return 200 (so Telegram doesn't
        retry-storm us) but never process the update.
        """
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        clone_id_raw = query.get("clone_id", [None])[0]
        clone_id = None
        if clone_id_raw is not None:
            try:
                clone_id = int(clone_id_raw)
            except ValueError:
                logger.warning(f"[v0] Malformed clone_id in webhook path: {clone_id_raw!r}")
                self._respond_ok()
                return

        secret_header = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")

        content_length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        update_data = json.loads(body)

        # Reuse event loop for the lifetime of the warm container (fixes "Event loop is closed" on Vercel).
        # _loop_lock serializes concurrent requests hitting this warm container so two threads
        # never call run_until_complete() on the same loop at once (see comment on _loop_lock above).
        with _loop_lock:
            loop = _get_event_loop()
            asyncio.set_event_loop(loop)
            try:
                if clone_id is not None:
                    loop.run_until_complete(self._handle_clone_update(clone_id, secret_header, update_data))
                else:
                    if WEBHOOK_SECRET and secret_header != WEBHOOK_SECRET:
                        self.send_response(403)
                        self.end_headers()
                        return
                    loop.run_until_complete(process_update(update_data))
            except Exception:
                error_text = traceback.format_exc()
                print(f"[v0] Error processing update: {error_text}")
                notify_admin_of_error(error_text)
            # DO NOT close the loop — keep it for the next warm invocation

        self._respond_ok()

    async def _handle_clone_update(self, clone_id: int, secret_header: str, update_data: dict):
        """Verify the clone-specific secret, then process the update through that clone's Application."""
        clone = await db.get_clone_for_routing(clone_id)
        if clone is None:
            logger.warning(f"[v0] Rejected update for unknown/inactive clone_id={clone_id}")
            return
        expected_secret = clone.get("webhook_secret")
        if not expected_secret or secret_header != expected_secret:
            logger.warning(f"[v0] Rejected update for clone_id={clone_id}: secret_token mismatch")
            return
        await process_update(update_data, clone_id=clone_id)

    def _respond_ok(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))

    def do_GET(self):
        """Health check so you can confirm the endpoint is live in a browser"""
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "bot webhook is live"}).encode("utf-8"))

    def log_message(self, format, *args):
        """Suppress default logging"""
