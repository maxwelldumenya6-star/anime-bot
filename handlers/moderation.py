"""
Group Moderation Handlers.

Wires up the moderation tables database.py already creates (warns, blocked
words, custom commands, join gate, per-chat settings) to actual bot
behavior — none of this existed before even though the tables did.

Known limitation: this bot runs as a Vercel serverless webhook, not a
long-running process, so there's no persistent JobQueue. Captcha verification
restricts new members on join and lifts the restriction when they tap
"I'm human", but there is no automatic timeout-kick if they never tap it —
that would need a scheduled/cron job hitting a separate endpoint, which is
outside what this change adds. An admin can always /kick someone manually.
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from modules import moderation_adapter as mod
from utils import escape_markdown_v1 as esc_md
from utils import safe_edit_message

logger = logging.getLogger(__name__)

MUTED_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)

FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)

WARN_LIMIT_BEFORE_MUTE = 3

GROUP_QUICK_ACTION_LABELS = {"warn": "warn", "mute": "mute", "ban": "ban"}


async def start_group_quick_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    """
    Entry point for the group-only Group Tools quick-action buttons
    (⚠️ Warn / 🔇 Mute / 🔨 Ban). Since Telegram gives bots no user-picker UI,
    "select a user" here means: tap the button, then reply to that user's
    message next — the reply IS the selection. Puts the admin in a waiting
    state (context.user_data["group_quick_action"]) consumed by
    handle_group_quick_action_message() in api/bot.py's handle_message().
    """
    query = update.callback_query
    if update.effective_chat.type not in ("group", "supergroup"):
        await query.answer("This only works inside a group.", show_alert=True)
        return
    if not await _is_group_admin(update, context):
        await query.answer("Only group admins can use this.", show_alert=True)
        return

    context.user_data["group_quick_action"] = action
    context.user_data["group_quick_action_chat_id"] = update.effective_chat.id
    label = GROUP_QUICK_ACTION_LABELS[action]
    await safe_edit_message(query, 
        f"👉 Reply to the message of the user you want to {label}. Your reply text "
        f"(if any) is used as the reason.{' Add a number of minutes to mute for (e.g. `60`).' if action == 'mute' else ''}\n\n"
        f"Send /cancel to back out.",
        parse_mode="Markdown"
    )


async def handle_group_quick_action_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Routed from handle_message() in api/bot.py when 'group_quick_action' is
    set. The admin's message is expected to be a reply to the target user's
    message — that reply is how the target gets "selected". Mirrors the
    logic in warn_command/mute_command/ban_command but triggered from a
    button tap instead of a typed command.
    """
    action = context.user_data.pop("group_quick_action", None)
    chat_id = context.user_data.pop("group_quick_action_chat_id", None)

    if update.effective_chat.id != chat_id:
        return

    target_id, target_name = _get_reply_target(update)
    if not target_id:
        await update.message.reply_text(
            "That wasn't a reply to a user's message, so I don't know who to act on — "
            "cancelled. Tap the button again and reply to their message this time."
        )
        return

    text = (update.message.text or "").strip()

    if action == "warn":
        reason = text or "No reason given"
        count = await mod.add_warn(target_id, chat_id, update.effective_user.id, reason)
        await _finalize_warn(update, context, chat_id, target_id, target_name, reason, count)

    elif action == "mute":
        until_date = None
        if text.isdigit():
            import time
            until_date = int(time.time()) + int(text) * 60
        try:
            kwargs = {"permissions": MUTED_PERMISSIONS}
            if until_date:
                kwargs["until_date"] = until_date
            await context.bot.restrict_chat_member(chat_id, target_id, **kwargs)
            duration = f" for {text} min" if until_date else " indefinitely"
            await update.message.reply_text(f"🔇 {esc_md(target_name)} muted{duration}.", parse_mode="Markdown")
        except TelegramError as e:
            await update.message.reply_text(f"Couldn't mute {esc_md(target_name)}: {e}", parse_mode="Markdown")

    elif action == "ban":
        reason = text or "No reason given"
        try:
            await context.bot.ban_chat_member(chat_id, target_id)
            await update.message.reply_text(f"🔨 {esc_md(target_name)} banned.\nReason: {esc_md(reason)}", parse_mode="Markdown")
        except TelegramError as e:
            await update.message.reply_text(f"Couldn't ban {esc_md(target_name)}: {e}", parse_mode="Markdown")


async def _require_group(update: Update) -> bool:
    if update.effective_chat.type not in ("group", "supergroup", "channel"):
        await update.message.reply_text("This command only works in groups or channels.")
        return False
    return True


async def _is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int = None) -> bool:
    user_id = user_id or update.effective_user.id
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
        return member.status in ("administrator", "creator")
    except TelegramError as e:
        logger.error(f"[v0] get_chat_member failed: {e}")
        return False


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not await _is_group_admin(update, context):
        await update.message.reply_text("Only group admins can use this command.")
        return False
    return True


def _get_reply_target(update: Update):
    """Return the user_id and name being replied to, or (None, None).

    Remote-panel escape hatch: handlers/admin_remote.py runs these same
    command functions from a DM with the admin, where there is no message
    inside the target group to reply to. When it builds its proxy Update it
    stashes the explicitly-typed target as update._remote_target =
    (user_id, name); that takes priority here over reply_to_message, which
    won't exist in that path anyway.
    """
    remote_target = getattr(update, "_remote_target", None)
    if remote_target:
        return remote_target
    msg = update.message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return u.id, (u.first_name or u.username or str(u.id))
    return None, None


# ═══════════════════════════════════════════════════════════════════════════
# WARN / UNWARN
# ═══════════════════════════════════════════════════════════════════════════

async def _finalize_warn(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                          target_id: int, target_name: str, reason: str, count: int):
    """Shared by warn_command and the group-tools quick-action warn flow.
    Auto Warn -> Auto Ban Escalation (feature #6): if a group has turned on
    `autobanwarns`, a user who reaches warn_ban_threshold total warns is
    banned instead of muted. Below that threshold, the existing
    WARN_LIMIT_BEFORE_MUTE mute behavior is unchanged."""
    from modules import moderation_extra as modx  # imported lazily to avoid a
    # module-load-order dependency (modx is normally imported further down
    # this file); safe since it's only used at call time here.

    settings = await mod.get_settings(chat_id)
    ban_threshold = settings.get("warn_ban_threshold") or 5

    if settings.get("auto_ban_on_warns_enabled") and count >= ban_threshold:
        try:
            await context.bot.ban_chat_member(chat_id, target_id)
            await modx.log_action(
                chat_id, "auto_ban_warns", context.bot.id, target_user_id=target_id,
                reason=f"Reached {count}/{ban_threshold} warns"
            )
            await update.message.reply_text(
                f"🔨 {esc_md(target_name)} has been auto-banned for reaching {count}/{ban_threshold} warns.\n"
                f"Reason: {esc_md(reason)}",
                parse_mode="Markdown",
            )
        except TelegramError as e:
            await update.message.reply_text(
                f"⚠️ {esc_md(target_name)} reached {count}/{ban_threshold} warns but I couldn't ban them "
                f"— make sure I'm an admin with permission to ban members. ({e})",
                parse_mode="Markdown",
            )
        return

    if count >= WARN_LIMIT_BEFORE_MUTE:
        try:
            await context.bot.restrict_chat_member(chat_id, target_id, permissions=MUTED_PERMISSIONS)
            await update.message.reply_text(
                f"⚠️ {esc_md(target_name)} has been warned ({count}/{WARN_LIMIT_BEFORE_MUTE}) "
                f"and muted for reaching the warn limit.\nReason: {esc_md(reason)}",
                parse_mode="Markdown",
            )
        except TelegramError as e:
            await update.message.reply_text(
                f"⚠️ {esc_md(target_name)} warned ({count}/{WARN_LIMIT_BEFORE_MUTE}) but I couldn't mute them "
                f"— make sure I'm an admin with permission to restrict members. ({e})",
                parse_mode="Markdown",
            )
        return

    await update.message.reply_text(
        f"⚠️ {esc_md(target_name)} warned ({count}/{WARN_LIMIT_BEFORE_MUTE}).\nReason: {esc_md(reason)}",
        parse_mode="Markdown",
    )


async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return

    target_id, target_name = _get_reply_target(update)
    if not target_id:
        await update.message.reply_text("Reply to a user's message with /warn [reason] to warn them.")
        return

    reason = " ".join(context.args) if context.args else "No reason given"
    chat_id = update.effective_chat.id
    count = await mod.add_warn(target_id, chat_id, update.effective_user.id, reason)
    await _finalize_warn(update, context, chat_id, target_id, target_name, reason, count)


async def unwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return

    target_id, target_name = _get_reply_target(update)
    if not target_id:
        await update.message.reply_text("Reply to a user's message with /unwarn to clear their warns.")
        return

    await mod.clear_warns(target_id, update.effective_chat.id)
    await update.message.reply_text(f"✅ Cleared warns for {esc_md(target_name)}.", parse_mode="Markdown")


async def warns_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return

    target_id, target_name = _get_reply_target(update)
    if not target_id:
        target_id = update.effective_user.id
        target_name = update.effective_user.first_name or "You"

    count = await mod.get_warn_count(target_id, update.effective_chat.id)
    await update.message.reply_text(f"{esc_md(target_name)} has {count}/{WARN_LIMIT_BEFORE_MUTE} warns.", parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════
# BAN / KICK / MUTE / UNMUTE
# ═══════════════════════════════════════════════════════════════════════════

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return

    target_id, target_name = _get_reply_target(update)
    if not target_id:
        await update.message.reply_text("Reply to a user's message with /ban [reason] to ban them.")
        return

    reason = " ".join(context.args) if context.args else "No reason given"
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target_id)
        await update.message.reply_text(f"🔨 {esc_md(target_name)} banned.\nReason: {esc_md(reason)}", parse_mode="Markdown")
    except TelegramError as e:
        await update.message.reply_text(f"Couldn't ban {esc_md(target_name)}: {e}", parse_mode="Markdown")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /unban <user_id>")
        return

    target_id = int(context.args[0])
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, target_id, only_if_banned=True)
        await update.message.reply_text(f"✅ Unbanned user {target_id}.")
    except TelegramError as e:
        await update.message.reply_text(f"Couldn't unban: {e}")


async def kick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return

    target_id, target_name = _get_reply_target(update)
    if not target_id:
        await update.message.reply_text("Reply to a user's message with /kick to remove them.")
        return

    chat_id = update.effective_chat.id
    try:
        # ban then immediately unban = kick (removes without a permanent ban)
        await context.bot.ban_chat_member(chat_id, target_id)
        await context.bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
        await update.message.reply_text(f"👢 {esc_md(target_name)} kicked.", parse_mode="Markdown")
    except TelegramError as e:
        await update.message.reply_text(f"Couldn't kick {esc_md(target_name)}: {e}", parse_mode="Markdown")


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return

    target_id, target_name = _get_reply_target(update)
    if not target_id:
        await update.message.reply_text("Reply to a user's message with /mute [minutes] to mute them.")
        return

    until_date = None
    if context.args and context.args[0].isdigit():
        import time
        until_date = int(time.time()) + int(context.args[0]) * 60

    try:
        kwargs = {"permissions": MUTED_PERMISSIONS}
        if until_date:
            kwargs["until_date"] = until_date
        await context.bot.restrict_chat_member(update.effective_chat.id, target_id, **kwargs)
        duration = f" for {context.args[0]} min" if until_date else " indefinitely"
        await update.message.reply_text(f"🔇 {esc_md(target_name)} muted{duration}.", parse_mode="Markdown")
    except TelegramError as e:
        await update.message.reply_text(f"Couldn't mute {esc_md(target_name)}: {e}", parse_mode="Markdown")


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return

    target_id, target_name = _get_reply_target(update)
    if not target_id:
        await update.message.reply_text("Reply to a user's message with /unmute to restore their permissions.")
        return

    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, target_id, permissions=FULL_PERMISSIONS)
        await update.message.reply_text(f"🔊 {esc_md(target_name)} unmuted.", parse_mode="Markdown")
    except TelegramError as e:
        await update.message.reply_text(f"Couldn't unmute {esc_md(target_name)}: {e}", parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════
# WORD FILTER
# ═══════════════════════════════════════════════════════════════════════════

async def filter_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return

    if not context.args:
        await update.message.reply_text("Usage: /filter <word or phrase>")
        return

    word = " ".join(context.args)
    chat_id = update.effective_chat.id
    ok = await mod.add_blocked_word(chat_id, word, update.effective_user.id)
    if ok:
        await mod.set_setting(chat_id, update.effective_user.id, "word_filter_enabled", True)
        await update.message.reply_text(f"🚫 Added \"{word}\" to the filter list (word filter is now on).")
    else:
        await update.message.reply_text("Couldn't add that filter — try again.")


async def filter_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return

    if not context.args:
        await update.message.reply_text("Usage: /unfilter <word or phrase>")
        return

    word = " ".join(context.args)
    removed = await mod.remove_blocked_word(update.effective_chat.id, word)
    if removed:
        await update.message.reply_text(f"✅ Removed \"{word}\" from the filter list.")
    else:
        await update.message.reply_text(f"\"{word}\" wasn't in the filter list.")


async def filter_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return

    words = await mod.list_blocked_words(update.effective_chat.id)
    if not words:
        await update.message.reply_text("No filtered words set. Add one with /filter <word>.")
        return
    await update.message.reply_text("🚫 Filtered words:\n" + "\n".join(f"• {w}" for w in words))


# ═══════════════════════════════════════════════════════════════════════════
# CUSTOM COMMANDS
# ═══════════════════════════════════════════════════════════════════════════

async def setcmd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /setcmd <name> <response text>")
        return

    name = context.args[0]
    response = " ".join(context.args[1:])
    await mod.set_custom_command(update.effective_chat.id, name, response, update.effective_user.id)
    await update.message.reply_text(f"✅ Custom command \"!{name.lower().lstrip('!/')}\" saved.")


async def delcmd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return

    if not context.args:
        await update.message.reply_text("Usage: /delcmd <name>")
        return

    removed = await mod.delete_custom_command(update.effective_chat.id, context.args[0])
    if removed:
        await update.message.reply_text("✅ Custom command deleted.")
    else:
        await update.message.reply_text("No such custom command.")


async def listcmds_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return

    names = await mod.list_custom_commands(update.effective_chat.id)
    if not names:
        await update.message.reply_text("No custom commands set. Add one with /setcmd <name> <response>.")
        return
    await update.message.reply_text(
        "📋 Custom commands (trigger with !name):\n" + "\n".join(f"• !{n}" for n in names)
    )


# ═══════════════════════════════════════════════════════════════════════════
# JOIN GATE
# ═══════════════════════════════════════════════════════════════════════════

async def setgate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return

    if not context.args:
        await update.message.reply_text("Usage: /setgate <link> [label text]")
        return

    link = context.args[0]
    label = " ".join(context.args[1:]) if len(context.args) > 1 else "Join Required"
    await mod.set_join_gate(update.effective_chat.id, link, label)
    await update.message.reply_text(
        "✅ Join gate set. New members will be restricted until they tap the join link and confirm.\n\n"
        "Note: this is honor-system — I can't automatically verify someone joined an external "
        "channel, only prompt them to and let them self-confirm. Use /gate off to disable."
    )


async def gate_toggle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /gate on | /gate off")
        return

    enabled = context.args[0].lower() == "on"
    await mod.set_join_gate_enabled(update.effective_chat.id, enabled)
    await update.message.reply_text(f"✅ Join gate {'enabled' if enabled else 'disabled'}.")


# ══════════════════════��════════════════════════════════════════════════════
# MOD SETTINGS (captcha / anti-raid / etc.)
# ═══════════════════════════════════════════════════════════════════════════

MODSETTINGS_HELP = (
    "Usage: /modsettings <setting> <on|off>\n\n"
    "Settings: captcha, wordfilter, antiraid, slowmode, nightmode, linkfilter, autobanspam, "
    "deleteservice, automute, autobanwarns, autopin, autodmjoin\n"
    "Example: /modsettings linkfilter on\n\n"
    "Also: /whitelistdomain <domain>, /unwhitelistdomain <domain>, /listwhitelist\n"
    "      /setspamrules <dup_count> <flood_count> <flood_window_seconds>\n"
    "      /setautomute <minutes>\n"
    "      /setwarnlimit <N> — ban after N total warns (needs autobanwarns on)\n"
    "      /setpintag <tag> — e.g. `#pin` (needs autopin on)\n"
    "      /setjoinlink — generate a \"request to join\" invite link (needs autodmjoin on)"
)

_SETTING_ALIASES = {
    "captcha": "captcha_enabled",
    "wordfilter": "word_filter_enabled",
    "antiraid": "anti_raid_enabled",
    "slowmode": "slow_mode_enabled",
    "nightmode": "night_mode_enabled",
    "linkfilter": "auto_delete_links_enabled",
    "autobanspam": "auto_ban_spam_enabled",
    "deleteservice": "auto_delete_service_messages",
    "automute": "auto_mute_new_members_enabled",
    "autobanwarns": "auto_ban_on_warns_enabled",
    "autopin": "auto_pin_announcements_enabled",
    "autodmjoin": "auto_dm_on_join_enabled",
}


async def modsettings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return

    chat_id = update.effective_chat.id

    if not context.args:
        settings = await mod.get_settings(chat_id)
        lines = [f"• {alias}: {'on' if settings.get(field) else 'off'}" for alias, field in _SETTING_ALIASES.items()]
        await update.message.reply_text("⚙️ Moderation settings:\n" + "\n".join(lines) + f"\n\n{MODSETTINGS_HELP}")
        return

    if len(context.args) < 2 or context.args[0].lower() not in _SETTING_ALIASES or context.args[1].lower() not in ("on", "off"):
        await update.message.reply_text(MODSETTINGS_HELP)
        return

    alias = context.args[0].lower()
    field = _SETTING_ALIASES[alias]
    value = context.args[1].lower() == "on"
    await mod.set_setting(chat_id, update.effective_user.id, field, value)
    await update.message.reply_text(f"✅ {alias} turned {'on' if value else 'off'}.")


async def whitelistdomain_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/whitelistdomain <domain> — Auto Delete Links won't touch links to
    this domain (e.g. youtube.com). Only takes effect if linkfilter is on."""
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return
    domain = update.message.text.partition(" ")[2].strip().lower()
    if not domain:
        await update.message.reply_text("Usage: `/whitelistdomain youtube.com`", parse_mode="Markdown")
        return
    ok = await mod.add_whitelist_domain(update.effective_chat.id, domain, update.effective_user.id)
    await update.message.reply_text(f"✅ Whitelisted `{domain}`." if ok else "⚠️ Failed to save.", parse_mode="Markdown")


async def unwhitelistdomain_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unwhitelistdomain <domain>"""
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return
    domain = update.message.text.partition(" ")[2].strip().lower()
    if not domain:
        await update.message.reply_text("Usage: `/unwhitelistdomain youtube.com`", parse_mode="Markdown")
        return
    ok = await mod.remove_whitelist_domain(update.effective_chat.id, domain)
    await update.message.reply_text(f"✅ Removed `{domain}` from the whitelist." if ok else f"⚠️ `{domain}` wasn't whitelisted.", parse_mode="Markdown")


async def listwhitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/listwhitelist"""
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return
    domains = await mod.list_whitelist_domains(update.effective_chat.id)
    if not domains:
        await update.message.reply_text(
            "No whitelisted domains — with linkfilter on, ALL links are removed.\n"
            "Add one with `/whitelistdomain youtube.com`.",
            parse_mode="Markdown"
        )
        return
    await update.message.reply_text("✅ Whitelisted domains:\n" + "\n".join(f"• {d}" for d in domains))


async def setspamrules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setspamrules <dup_count> <flood_count> <flood_window_seconds>
    Configures Auto Ban on Spam's two triggers. Doesn't turn the feature on
    by itself — use `/modsettings autobanspam on` for that."""
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return
    args = context.args
    if len(args) != 3 or not all(a.isdigit() for a in args):
        await update.message.reply_text(
            "Usage: `/setspamrules <dup_count> <flood_count> <flood_window_seconds>`\n"
            "Example: `/setspamrules 3 10 10` — ban after 3 identical messages in a row, "
            "OR 10 messages within 10 seconds.",
            parse_mode="Markdown"
        )
        return
    dup_count, flood_count, flood_window = (int(a) for a in args)
    if dup_count < 2 or flood_count < 2 or flood_window < 1:
        await update.message.reply_text("⚠️ Values too low — dup_count/flood_count must be ≥2, window ≥1.")
        return
    chat_id, admin_id = update.effective_chat.id, update.effective_user.id
    await mod.set_setting(chat_id, admin_id, "spam_duplicate_threshold", dup_count)
    await mod.set_setting(chat_id, admin_id, "spam_flood_threshold", flood_count)
    await mod.set_setting(chat_id, admin_id, "spam_flood_window_seconds", flood_window)
    await update.message.reply_text(
        f"✅ Spam rules saved: ban after {dup_count} identical messages in a row, "
        f"or {flood_count} messages within {flood_window}s."
    )


async def setautomute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setautomute <minutes> — how long new joiners stay muted when
    `automute` is on. Doesn't turn the feature on by itself — use
    `/modsettings automute on` for that."""
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return
    args = context.args
    if len(args) != 1 or not args[0].isdigit() or int(args[0]) < 1:
        await update.message.reply_text("Usage: `/setautomute 10` (minutes)", parse_mode="Markdown")
        return
    minutes = int(args[0])
    await mod.set_setting(update.effective_chat.id, update.effective_user.id, "auto_mute_new_members_minutes", minutes)
    await update.message.reply_text(f"✅ New members will be auto-muted for {minutes} minutes (when `automute` is on).", parse_mode="Markdown")


async def setwarnlimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setwarnlimit <N> — total accumulated warns at which a user is
    auto-banned instead of just muted. Doesn't turn the feature on by
    itself — use `/modsettings autobanwarns on` for that."""
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return
    args = context.args
    if len(args) != 1 or not args[0].isdigit() or int(args[0]) < 2:
        await update.message.reply_text("Usage: `/setwarnlimit 5` (must be ≥2)", parse_mode="Markdown")
        return
    threshold = int(args[0])
    await mod.set_setting(update.effective_chat.id, update.effective_user.id, "warn_ban_threshold", threshold)
    await update.message.reply_text(
        f"✅ Users will be auto-banned after {threshold} total warns (when `autobanwarns` is on).",
        parse_mode="Markdown",
    )


async def setpintag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setpintag <tag> — the tag an admin's message must start with to be
    auto-pinned. Doesn't turn the feature on by itself — use
    `/modsettings autopin on` for that."""
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return
    tag = update.message.text.partition(" ")[2].strip()
    if not tag:
        await update.message.reply_text("Usage: `/setpintag #pin`", parse_mode="Markdown")
        return
    await mod.set_setting(update.effective_chat.id, update.effective_user.id, "auto_pin_tag", tag)
    await update.message.reply_text(
        f"✅ Admin messages starting with `{esc_md(tag)}` will be auto-pinned (when `autopin` is on).",
        parse_mode="Markdown",
    )


# ═══════════════════════════════════════════════════════════════════════════
# AUTO-DM ON JOIN REQUEST
# ═══════════════════════════════════════════════════════════════════════════
#
# Uses Telegram's "request to join" invite links (creates_join_request=True)
# instead of instant-join links. Tapping one doesn't add the user to the
# group directly — it fires a ChatJoinRequest update to the bot first. That
# request itself counts as the user initiating contact with the bot, so
# (unlike a normal group join) the bot IS allowed to DM them directly even if
# they've never messaged it before. handle_join_request DMs them the main
# menu, then approves the request either way — a failed DM never blocks
# someone from getting into the group.

async def setjoinlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setjoinlink — generates a "request to join" invite link for this
    group (creates_join_request=True). Existing/manually-created invite
    links keep working as normal instant-join links; this just adds one
    that routes through handle_join_request so autodmjoin can act on it.
    Doesn't turn autodmjoin on by itself — use `/modsettings autodmjoin on`."""
    if not await _require_group(update):
        return
    if not await _require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    try:
        link = await context.bot.create_chat_invite_link(
            chat_id, name="Auto-DM Join Link", creates_join_request=True
        )
    except TelegramError as e:
        await update.message.reply_text(
            f"⚠️ Couldn't create the link — make sure I'm an admin here with permission to invite users. ({e})"
        )
        return
    await update.message.reply_text(
        f"✅ Request-to-join link created:\n{link.invite_link}\n\n"
        f"Share this instead of the regular invite link. With `autodmjoin` on "
        f"(`/modsettings autodmjoin on`), anyone who taps it gets DMed the main "
        f"menu the moment they request to join, then is let in automatically.\n\n"
        f"Note: a manually-created \"request to join\" link from Telegram's own "
        f"UI works the same way — this command is just a shortcut."
    )


async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ChatJoinRequestHandler — fires when someone taps a "request to join"
    invite link. Only acts if the group has `autodmjoin` on; otherwise leaves
    the request untouched for a human admin to approve/deny manually (e.g. a
    request-link created for reasons unrelated to this feature)."""
    request = update.chat_join_request
    chat_id = request.chat.id
    user = request.from_user

    settings = await mod.get_settings(chat_id)
    if not settings.get("auto_dm_on_join_enabled"):
        return

    clone_config = context.bot_data.get("clone_config")
    dm_sent = False
    try:
        clone_name = (clone_config or {}).get("name") or (clone_config or {}).get("bot_name")
        greeting = f"👋 Hi {esc_md(user.first_name or 'there')}! Thanks for requesting to join {esc_md(request.chat.title or 'the group')}."
        if clone_name:
            greeting += f" You're now in — here's the {esc_md(clone_name)} menu:"
        else:
            greeting += " You're now in — here's the main menu:"
        from keyboards import KeyboardGenerator
        await context.bot.send_message(
            user.id, greeting,
            reply_markup=KeyboardGenerator.main_menu(clone_mode=bool(clone_config), clone_id=(clone_config or {}).get("clone_id")),
            parse_mode="Markdown",
        )
        dm_sent = True
    except TelegramError as e:
        logger.warning(f"[v0] Auto-DM on join failed for user {user.id} in chat {chat_id}: {e}")

    try:
        await context.bot.approve_chat_join_request(chat_id, user.id)
    except TelegramError as e:
        logger.warning(f"[v0] Couldn't approve join request for user {user.id} in chat {chat_id}: {e}")

    from modules import moderation_extra as modx
    await modx.log_action(
        chat_id, "auto_dm_join", context.bot.id, target_user_id=user.id,
        reason="DM sent" if dm_sent else "DM failed (user may have DMs closed) — approved anyway"
    )


# ═══════════════════════════════════════════════════════════════════════════
# GROUP TEXT: word filter + custom commands
# (registered as a MessageHandler running before the general catch-all)
# ═══════════════════════════════════════════════════════════════════════════

async def handle_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs on every non-command group text message: anti-flood/link auto-mod,
    enforce word filter, respond to custom commands."""
    message = update.effective_message
    if not message or not message.text:
        return
    chat_id = update.effective_chat.id
    text = message.text

    # Anti-flood + link/invite auto-moderation. If it took action (flood-mute
    # or delete+mute), the message is gone — stop here rather than running the
    # word filter against a message that no longer exists.
    if await auto_moderate(update, context):
        return

    # Custom commands, triggered with a "!" prefix (Telegram slash commands are
    # registered statically at handler-setup time, so dynamic per-group
    # commands can't use "/" without colliding with real commands).
    if text.startswith("!"):
        name = text[1:].split()[0] if len(text) > 1 else ""
        if name:
            response = await mod.get_custom_command(chat_id, name)
            if response:
                await message.reply_text(response)
                return

    # Word filter
    settings = await mod.get_settings(chat_id)

    # Auto Pin Announcements (feature #6): an admin message starting with
    # the configured tag (default "#pin") gets pinned automatically — no
    # need to reply-and-/pin. Checked for every admin message, independent
    # of the word filter below.
    if settings.get("auto_pin_announcements_enabled"):
        tag = (settings.get("auto_pin_tag") or "#pin").strip()
        if tag and text.lower().startswith(tag.lower()):
            if await _is_group_admin(update, context):
                try:
                    await context.bot.pin_chat_message(chat_id, message.message_id)
                    await modx.log_action(
                        chat_id, "auto_pin", update.effective_user.id,
                        target_user_id=update.effective_user.id, reason=f"Tagged with {tag}"
                    )
                except TelegramError as e:
                    logger.warning(f"[v0] Auto-pin failed: {e}")

    if settings.get("word_filter_enabled"):
        matched = await mod.message_contains_blocked_word(chat_id, text)
        if matched:
            try:
                await message.delete()
                logger.info(f"[v0] Deleted message in {chat_id} for blocked word match")
            except TelegramError as e:
                logger.warning(f"[v0] Couldn't delete filtered message (missing delete permission?): {e}")


# ═══════════════════════════════════════════════════════════════════════════
# NEW MEMBER: captcha + join gate + anti-raid
# (called from feature_handlers.handle_chat_member after its own logic)
# ═══════════════════════════════════════════════════════════════════════════

async def handle_member_join_moderation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if cm is None:
        return
    if cm.new_chat_member.status != "member" or cm.old_chat_member.status == "member":
        return  # only care about brand-new joins, not status churn

    chat_id = cm.chat.id
    user = cm.new_chat_member.user
    if user.is_bot:
        return

    settings = await mod.get_settings(chat_id)

    # Anti-raid: alert if joins are spiking, don't auto-lock anything down
    # (a false positive auto-lockdown would be worse than a missed alert).
    if settings.get("anti_raid_enabled"):
        window = settings.get("anti_raid_window_minutes") or 5
        threshold = settings.get("anti_raid_threshold") or 5
        recent = await mod.count_recent_joins(chat_id, window)
        if recent >= threshold:
            log_channel = settings.get("logging_channel_id")
            alert_text = (
                f"🚨 Anti-raid alert: {recent} joins in the last {window} min "
                f"(threshold {threshold}) in chat {chat_id}."
            )
            try:
                await context.bot.send_message(log_channel or chat_id, alert_text)
            except TelegramError as e:
                logger.warning(f"[v0] Couldn't send anti-raid alert: {e}")

    captcha_on = settings.get("captcha_enabled")
    gate = await mod.get_join_gate(chat_id) if not captcha_on else None
    gate_on = gate and gate.get("enabled")

    if not captcha_on and not gate_on:
        return

    try:
        await context.bot.restrict_chat_member(chat_id, user.id, permissions=MUTED_PERMISSIONS)
    except TelegramError as e:
        logger.warning(f"[v0] Couldn't restrict new member for verification: {e}")
        return

    if captcha_on:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ I'm human", callback_data=f"captcha_verify:{chat_id}:{user.id}")
        ]])
        try:
            await context.bot.send_message(
                chat_id,
                f"👋 Welcome {esc_md(user.first_name or 'there')}! Please tap the button below to verify "
                f"you're human before you can chat.",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        except TelegramError as e:
            logger.warning(f"[v0] Couldn't send captcha message: {e}")
    elif gate_on:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(gate.get("gate_label") or "Join Required", url=gate.get("gate_link"))],
            [InlineKeyboardButton("✅ I've joined", callback_data=f"gate_verify:{chat_id}:{user.id}")],
        ])
        try:
            await context.bot.send_message(
                chat_id,
                f"👋 Welcome {esc_md(user.first_name or 'there')}! Join the link below, then tap "
                f"\"I've joined\" to start chatting here.",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        except TelegramError as e:
            logger.warning(f"[v0] Couldn't send join gate message: {e}")


async def handle_verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles both captcha_verify: and gate_verify: callback buttons."""
    query = update.callback_query
    data = query.data
    try:
        _, chat_id_str, user_id_str = data.split(":")
        chat_id, target_user_id = int(chat_id_str), int(user_id_str)
    except (ValueError, IndexError):
        await query.answer("Invalid verification data.")
        return

    if query.from_user.id != target_user_id:
        await query.answer("This verification isn't for you.", show_alert=True)
        return

    try:
        await context.bot.restrict_chat_member(chat_id, target_user_id, permissions=FULL_PERMISSIONS)
        await query.answer("Verified! You can chat now.")
        await safe_edit_message(query, "✅ Verified — welcome!")
    except TelegramError as e:
        await query.answer("Couldn't verify — ask an admin for help.", show_alert=True)
        logger.warning(f"[v0] Verification restrict failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# DEL / PIN / STATS / RULES / SETWELCOME / GREETING — ported from SUPER-BOT
# ═══════════════════════════════════════════════════════════════════════════

import re
from datetime import datetime, timedelta
from modules import moderation_extra as modx

LINK_PATTERN = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/|telegram\.dog/|discord\.gg/|discord\.com/invite/|"
    r"bit\.ly/|tinyurl\.com/|wa\.me/|chat\.whatsapp\.com/|\b\S+\.(?:com|net|org|io|gg|me|co|ly|to|xyz|info)\b)",
    re.IGNORECASE)


async def del_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/del — reply to a message to delete it (and the /del command itself)"""
    if not await _require_admin(update, context):
        return
    if update.message.reply_to_message:
        try:
            await context.bot.delete_message(update.effective_chat.id, update.message.reply_to_message.message_id)
            await update.message.delete()
        except TelegramError as e:
            logger.warning(f"[v0] /del failed: {e}")
    else:
        await update.message.reply_text("Reply to a message to delete it.")


async def pin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pin — reply to a message to pin it"""
    if not await _require_admin(update, context):
        return
    if update.message.reply_to_message:
        try:
            await context.bot.pin_chat_message(update.effective_chat.id, update.message.reply_to_message.message_id)
        except TelegramError as e:
            await update.message.reply_text(f"⚠️ {e}")
    else:
        await update.message.reply_text("Reply to a message to pin it.")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats — basic group info"""
    chat = update.effective_chat
    try:
        count = await context.bot.get_chat_member_count(chat.id)
    except TelegramError:
        count = "?"
    await update.message.reply_text(
        f"📊 *GROUP STATS*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Members: *{count}*\n🆔 Chat ID: `{chat.id}`\n📛 Name: *{esc_md(chat.title or 'Unknown')}*",
        parse_mode="Markdown"
    )


async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rules — static group rules"""
    await update.message.reply_text(
        "📋 *GROUP RULES*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "1. Be respectful\n2. No spam\n3. No hate speech\n4. Stay on topic\n5. Have fun! ⚡",
        parse_mode="Markdown"
    )


async def setwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setwelcome <message> — admin sets a custom greeting; use {name} as a placeholder"""
    if not await _require_admin(update, context):
        return
    msg = update.message.text.partition(" ")[2].strip()
    if not msg:
        await update.message.reply_text(
            "Usage: `/setwelcome Welcome {name} to the group!`\n"
            "Use `{name}` where the new member's name should go.",
            parse_mode="Markdown"
        )
        return
    ok = await modx.set_welcome_message(update.effective_chat.id, msg)
    if ok:
        await update.message.reply_text("✅ Welcome message saved!")
    else:
        await update.message.reply_text("⚠️ Failed to save. Try again.")


async def setpaybutton_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setpaybutton <Label> | <amount in GHS> — admin attaches a generic
    Pay Now-style button to this group's welcome message. What the payment
    is FOR is entirely up to the admin's label (paid access, membership,
    a fundraiser, anything) — the bot just collects it via Paystack."""
    if not await _require_admin(update, context):
        return
    raw = update.message.text.partition(" ")[2].strip()
    if not raw or "|" not in raw:
        await update.message.reply_text(
            "Usage: `/setpaybutton Pay Now | 10`\n"
            "Left of `|` is the button label, right of `|` is the amount in GHS.\n"
            "Use `/removepaybutton` to remove it.",
            parse_mode="Markdown"
        )
        return
    label, _, amount_str = raw.partition("|")
    label = label.strip()
    amount_str = amount_str.strip()
    if not label or not amount_str.isdigit() or int(amount_str) <= 0:
        await update.message.reply_text(
            "⚠️ Couldn't parse that. Example: `/setpaybutton Pay Now | 10`",
            parse_mode="Markdown"
        )
        return
    ok = await modx.set_pay_button(update.effective_chat.id, label, int(amount_str))
    if ok:
        await update.message.reply_text(
            f"✅ \"{label}\" button (GHS {amount_str}) will now show on the welcome message."
        )
    else:
        await update.message.reply_text("⚠️ Failed to save. Try again.")


async def removepaybutton_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/removepaybutton — admin removes the welcome-message Pay Now button."""
    if not await _require_admin(update, context):
        return
    ok = await modx.clear_pay_button(update.effective_chat.id)
    if ok:
        await update.message.reply_text("✅ Pay button removed from the welcome message.")
    else:
        await update.message.reply_text("⚠️ Failed to remove. Try again.")


async def greet_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """MessageHandler for filters.StatusUpdate.NEW_CHAT_MEMBERS"""
    chat_id = update.effective_chat.id
    custom = await modx.get_welcome_message(chat_id)
    pay_button = await modx.get_pay_button(chat_id)
    settings = await mod.get_settings(chat_id)
    reply_markup = None
    if pay_button:
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"💳 {pay_button['label']} ({pay_button['amount_ghs']} GHS)",
                callback_data=f"welcome_pay_init_{chat_id}"
            )
        ]])
    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            continue
        name = member.first_name or member.username or "there"
        text = custom.replace("{name}", name) if custom else \
            f"👋 Welcome, *{esc_md(name)}*! Glad to have you here. Check /rules to get started."
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

        # Auto Mute New Members — skip if captcha/join-gate already restricts
        # them (handle_member_join_moderation runs separately off the
        # ChatMemberHandler update, not this message update), to avoid
        # double-gating the same person.
        if settings.get("auto_mute_new_members_enabled") and not settings.get("captcha_enabled"):
            minutes = settings.get("auto_mute_new_members_minutes") or 10
            until = datetime.now() + timedelta(minutes=minutes)
            try:
                await context.bot.restrict_chat_member(chat_id, member.id, permissions=MUTED_PERMISSIONS, until_date=until)
            except TelegramError as e:
                logger.warning(f"[v0] Auto-mute new member failed: {e}")

    # Auto Delete Service Messages — remove the "X joined the group" notice
    # itself. Runs last so it doesn't interfere with reading new_chat_members
    # above.
    if settings.get("auto_delete_service_messages"):
        try:
            await update.message.delete()
        except TelegramError as e:
            logger.warning(f"[v0] Couldn't delete join service message: {e}")


async def handle_member_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """MessageHandler for filters.StatusUpdate.LEFT_CHAT_MEMBER — only acts
    when Auto Delete Service Messages is on; otherwise Telegram's own "X
    left" notice is left alone."""
    settings = await mod.get_settings(update.effective_chat.id)
    if settings.get("auto_delete_service_messages"):
        try:
            await update.message.delete()
        except TelegramError as e:
            logger.warning(f"[v0] Couldn't delete leave service message: {e}")


def message_has_link(msg) -> bool:
    """Detects links via plain-text regex, hidden text_link/url entities, and
    inline keyboard button URLs."""
    text = msg.text or msg.caption or ""
    if text and LINK_PATTERN.search(text):
        return True
    entities = (msg.entities or []) + (msg.caption_entities or [])
    for ent in entities:
        if ent.type in ("url", "text_link"):
            return True
    if msg.reply_markup and msg.reply_markup.inline_keyboard:
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                if getattr(btn, "url", None):
                    return True
    return False


async def check_flood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Postgres-backed anti-flood (NOT an in-memory dict — this is a stateless
    webhook, an in-process dict resets unpredictably between invocations).
    Assumes the current message's flood event was already recorded by the
    caller (auto_moderate) — this only counts and acts.
    Returns True if the sender was just muted for flooding."""
    msg, user = update.message, update.effective_user
    if not msg or not user:
        return False
    chat_id = update.effective_chat.id

    recent_count = await modx.count_recent_flood_events(chat_id, user.id)
    if recent_count < modx.FLOOD_MAX_MESSAGES:
        return False

    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        if member.status in ("administrator", "creator"):
            return False
    except TelegramError:
        pass

    await modx.clear_flood_events(chat_id, user.id)
    until = datetime.now() + timedelta(minutes=modx.FLOOD_MUTE_MINUTES)
    try:
        await context.bot.restrict_chat_member(
            chat_id, user.id, permissions=ChatPermissions(can_send_messages=False), until_date=until
        )
        await modx.log_action(chat_id, "antiflood_mute", context.bot.id, target_user_id=user.id,
                               reason=f"Sent {modx.FLOOD_MAX_MESSAGES}+ messages within {modx.FLOOD_WINDOW_SECONDS}s")
        await context.bot.send_message(
            chat_id,
            f"🌊 *ANTI-FLOOD*\n{esc_md(user.first_name or 'User')} was muted for {modx.FLOOD_MUTE_MINUTES} minutes "
            f"for sending messages too fast.",
            parse_mode="Markdown"
        )
    except TelegramError as e:
        logger.warning(f"[v0] Anti-flood mute failed: {e}")
    return True


def _extract_domains(text: str) -> list:
    """Best-effort bare-domain extraction from a message for whitelist
    comparison (e.g. 'youtube.com' from 'check https://youtube.com/watch?v=x')."""
    if not text:
        return []
    domains = []
    for match in LINK_PATTERN.finditer(text):
        chunk = match.group(0)
        chunk = re.sub(r"^https?://", "", chunk, flags=re.IGNORECASE)
        chunk = chunk.split("/")[0].lower()
        if chunk:
            domains.append(chunk)
    return domains


def _link_is_whitelisted(text: str, whitelist: list) -> bool:
    """True only if EVERY domain found in the message matches a whitelisted
    entry (as a suffix, so 'm.youtube.com' matches a 'youtube.com' entry)."""
    if not whitelist:
        return False
    domains = _extract_domains(text)
    if not domains:
        return False
    return all(any(d == w or d.endswith("." + w) for w in whitelist) for d in domains)


async def check_spam_ban(update: Update, context: ContextTypes.DEFAULT_TYPE, settings: dict) -> bool:
    """Auto Ban on Spam (opt-in, feature #6). Two independent triggers:
      - same message text posted back-to-back `spam_duplicate_threshold` times
      - `spam_flood_threshold` messages within `spam_flood_window_seconds`
    Returns True if the sender was just banned."""
    if not settings.get("auto_ban_spam_enabled"):
        return False
    msg, user = update.message, update.effective_user
    if not msg or not user:
        return False
    chat_id = update.effective_chat.id
    text = msg.text or msg.caption or ""

    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        if member.status in ("administrator", "creator"):
            return False
    except TelegramError:
        pass

    dup_threshold = settings.get("spam_duplicate_threshold") or 3
    flood_threshold = settings.get("spam_flood_threshold") or 10
    flood_window = settings.get("spam_flood_window_seconds") or 10

    triggered, reason = False, ""

    if text:
        recent = await modx.get_recent_message_texts(chat_id, user.id, limit=dup_threshold)
        if len(recent) >= dup_threshold and all(t == text for t in recent):
            triggered = True
            reason = f"Posted the same message {dup_threshold}+ times in a row"

    if not triggered:
        recent_count = await modx.count_recent_flood_events(chat_id, user.id, window_seconds=flood_window)
        if recent_count >= flood_threshold:
            triggered = True
            reason = f"Sent {flood_threshold}+ messages within {flood_window}s"

    if not triggered:
        return False

    try:
        await context.bot.ban_chat_member(chat_id, user.id)
        await modx.clear_flood_events(chat_id, user.id)
        await modx.log_action(chat_id, "auto_ban_spam", context.bot.id, target_user_id=user.id, reason=reason)
        name = esc_md(user.first_name or user.username or "User")
        await context.bot.send_message(
            chat_id,
            f"🔨 *AUTO-BAN*\n{name} was banned for spam.\n_{reason}._",
            parse_mode="Markdown"
        )
    except TelegramError as e:
        logger.warning(f"[v0] Auto-ban spam failed: {e}")
    return True


async def auto_moderate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Deletes link/invite messages and temp-mutes the sender for 1 hour
    (MUTED_PERMISSIONS, not a ban — they stay in the group), admins exempt.
    Checks spam-ban, then flood, then links, in that order. Call this from
    the group message pipeline.
    Returns True if it took action (banned for spam, flood-muted, or
    deleted+muted for a link), so the caller can skip further processing
    (e.g. word filter) on a message that's already gone."""
    msg = update.message
    if not msg or update.effective_chat.type not in ("group", "supergroup"):
        return False
    user = update.effective_user
    if not user or user.id == context.bot.id:
        return False

    chat_id = update.effective_chat.id
    settings = await mod.get_settings(chat_id)

    # Recorded once here (not inside check_spam_ban/check_flood) so both
    # checks see the CURRENT message counted, not just prior ones.
    await modx.record_flood_event(chat_id, user.id, msg.text or msg.caption or "")

    if await check_spam_ban(update, context, settings):
        return True
    if await check_flood(update, context):
        return True
    if not settings.get("auto_delete_links_enabled", True):
        return False
    if not message_has_link(msg):
        return False

    text = msg.text or msg.caption or "(link hidden in button/entity)"

    whitelist = await mod.list_whitelist_domains(chat_id)
    if whitelist and _link_is_whitelisted(text, whitelist):
        return False

    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        if member.status in ("administrator", "creator"):
            return False
    except TelegramError:
        pass

    deleted = False
    try:
        await msg.delete()
        deleted = True
    except TelegramError as e:
        logger.warning(f"[v0] Auto-mod delete failed: {e}")

    until = datetime.now() + timedelta(minutes=modx.AUTOBAN_MINUTES)
    muted = False
    try:
        await context.bot.restrict_chat_member(chat_id, user.id, permissions=MUTED_PERMISSIONS, until_date=until)
        muted = True
    except TelegramError as e:
        logger.warning(f"[v0] Auto-mod mute failed: {e}")

    await modx.log_action(
        chat_id, "auto_mute_link", context.bot.id, target_user_id=user.id,
        reason=f"Posted a link/invite (msg {'deleted' if deleted else 'NOT deleted'}): {text[:200]}"
    )
    name = esc_md(user.first_name or user.username or "User")
    status_line = (
        f"🔇 *{name}* has been muted for *{modx.AUTOBAN_MINUTES} minutes* for posting a link/invite."
        if muted else
        f"⚠️ Detected a link/invite from *{name}*, but couldn't mute them — check my admin permissions."
    )
    try:
        await context.bot.send_message(
            chat_id,
            f"🚫 *AUTO-MODERATION*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n{status_line}\n"
            f"🗑️ Their message was {'deleted' if deleted else 'flagged (could not delete)'}.\n"
            f"_Only admins can post links in this group._",
            parse_mode="Markdown"
        )
    except TelegramError as e:
        logger.warning(f"[v0] Auto-mod notice failed: {e}")
    return True


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/logs — admin-only, last 20 moderation records for this group"""
    if not await _require_admin(update, context):
        return
    records = await modx.get_logs(update.effective_chat.id, limit=20)
    if not records:
        await update.message.reply_text("📭 No moderation records yet for this group.")
        return
    lines = ["🗂️ *MODERATION LOGS*", "━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for r in records:
        lines.append(
            f"🕒 {r['created_at']}\n   *{r['action_type'].upper()}* — user `{r['target_user_id']}`\n   _{r['reason']}_"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
