"""
SuperBot Handlers
Premium tiers, referrals, crypto alerts, analytics, leaderboard
"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from config import EMOJI_COLORS
from database import db
import flow_state
from modules import superbot_adapter
from paystack import paystack
from utils import is_owner
from utils import safe_edit_message

# ═══════════════════════════════════════════════════════════════════════════
# PREMIUM TIER SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

async def show_premium_tiers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display premium tier options"""
    query = update.callback_query
    user_id = update.effective_user.id
    current_tier = await superbot_adapter.get_user_tier(user_id)
    
    text = f"""
💎 **Premium Tiers**

Your Current: **{current_tier.upper()}**

**{superbot_adapter.ConfigCache.TIER_BASIC['name']}** (Free)
• Basic access
• Limited features

**{superbot_adapter.ConfigCache.TIER_PRO['name']}** — GHS {superbot_adapter.ConfigCache.TIER_PRO['price']}/month
✨ Everything in Basic
✨ Price alerts
✨ Advanced analytics

**{superbot_adapter.ConfigCache.TIER_ELITE['name']}** — GHS {superbot_adapter.ConfigCache.TIER_ELITE['price']}/month
✨ Everything in Pro
✨ Priority support
✨ Unlimited alerts
✨ Custom watchlists
"""
    
    keyboard = [
        [InlineKeyboardButton("Upgrade to Pro", callback_data="tier_pro" if current_tier != "pro" else "noop")],
        [InlineKeyboardButton("Upgrade to Elite", callback_data="tier_elite" if current_tier != "elite" else "noop")],
        [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
    ]
    
    if query:
        await safe_edit_message(query, 
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )


async def upgrade_tier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kick off a real Selar payment for a premium tier. Nothing is granted
    until verify_tier_payment confirms the payment succeeded - this used to
    grant the tier immediately with a '# integrate with payment later' comment,
    and separately was unreachable anyway (this function didn't exist as a
    real callable - a missing `async def` line left this code as dead tail
    code of show_premium_tiers, so tapping Upgrade actually just crashed)."""
    query = update.callback_query
    user_id = update.effective_user.id
    tier = query.data.split("_")[1]  # pro or elite

    tier_config = {"pro": superbot_adapter.ConfigCache.TIER_PRO, "elite": superbot_adapter.ConfigCache.TIER_ELITE}
    config = tier_config.get(tier)

    if not config:
        return

    if is_owner(user_id, context):
        # Owner immunity (main bot admin, or this clone's owner): grant instantly,
        # no payment — matches is_owner() gating everywhere else (clone_bot.py,
        # ai_handler.py, botstore_handler.py). It's their own bot.
        await superbot_adapter.set_user_tier(user_id, tier)
        await safe_edit_message(query, 
            f"{EMOJI_COLORS.get('success', '✅')} **Upgraded to {config['name']}!** (owner — no charge)\n\n"
            f"Your new features are active now. Enjoy!"
        )
        return

    email = f"user_{user_id}@animebot.com"
    payment_result = paystack.initialize_payment(
        email,
        config["price"] * 100,  # GHS to pesewas
        user_id,
        f"SuperbotTier_{tier}_{user_id}",
        payment_type="superbot_tier",
        extra_metadata={"tier": tier}
    )

    if not payment_result or payment_result.get("status") != "success":
        await query.answer("Failed to start payment. Please try again.", show_alert=True)
        return

    reference = payment_result.get("reference")
    # Persisted server-side (not context.user_data) so 'I've Paid' - handled by
    # manual_payments.handle_user_verification, which may run on a different
    # serverless instance - can still find the reference and which tier this
    # payment was for.
    await db.create_pending_payment_intent(reference, user_id, "superbot_tier", context={"tier": tier})

    keyboard = [
        [InlineKeyboardButton("✅ I've Paid", callback_data="verify_tier_payment")],
        [InlineKeyboardButton("⬅️ Back", callback_data="show_premium_tiers")]
    ]

    await safe_edit_message(query, 
        f"{EMOJI_COLORS.get('success', '✅')} **Payment Ready**\n\n"
        f"Click below to pay GHS {config['price']}.00 for **{config['name']}** via Selar:\n\n"
        f"[Pay Now]({payment_result.get('authorization_url')})\n\n"
        f"Once you've paid, tap \"I've Paid\" to activate your tier!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def verify_tier_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify the tier payment and only then grant the tier"""
    query = update.callback_query
    user_id = update.effective_user.id

    reference = context.user_data.get("tier_payment_reference")
    tier = context.user_data.get("tier_payment_tier")
    if not reference or not tier:
        await query.answer("No pending payment found", show_alert=True)
        return

    result = paystack.verify_payment(reference)

    if result.get("status") == "success":
        await superbot_adapter.set_user_tier(user_id, tier)
        context.user_data.pop("tier_payment_reference", None)
        context.user_data.pop("tier_payment_tier", None)

        config = {"pro": superbot_adapter.ConfigCache.TIER_PRO, "elite": superbot_adapter.ConfigCache.TIER_ELITE}[tier]
        await safe_edit_message(query, 
            f"{EMOJI_COLORS.get('success', '✅')} **Upgraded to {config['name']}!**\n\n"
            f"Your new features are active now. Enjoy!"
        )
    else:
        await safe_edit_message(query, 
            f"{EMOJI_COLORS.get('error', '❌')} Payment not confirmed yet. If you just paid, wait a few seconds and tap \"I've Paid\" again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ I've Paid", callback_data="verify_tier_payment")]])
        )

# ═══════════════════════════════════════════════════════════════════════════
# REFERRAL SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

async def show_referral_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's referral stats and link"""
    query = update.callback_query if update.callback_query else None
    user_id = update.effective_user.id
    
    ref_count = await superbot_adapter.get_referral_count(user_id)
    ref_reward = await superbot_adapter.get_referral_reward(user_id)
    
    text = f"""
🤝 **Referral Program**

Refer friends and earn rewards!

**Your Stats:**
├ Referrals: {ref_count}
├ Coins Earned: {ref_reward}
├ Current Tier: {(await superbot_adapter.get_user_tier(user_id)).upper()}
└ Rank: {await superbot_adapter.get_user_rank(user_id) or 'Unranked'}

**Rewards:**
• 1 referral = {superbot_adapter.ConfigCache.REFERRAL_REWARD_COINS} coins
• Coins boost your leaderboard rank

**Your Referral Link:**
`/start ref_{user_id}`

Share this to earn!
"""
    
    keyboard = [
        [InlineKeyboardButton("👥 View Referrals", callback_data="ref_list")],
        [InlineKeyboardButton("📊 Leaderboard", callback_data="show_leaderboard")],
        [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
    ]
    
    if query:
        await safe_edit_message(query, 
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

# ═══════════════════════════════════════════════════════════════════════════
# CRYPTO PRICE ALERTS
# ═══════════════════════════════════════════════════════════════════════════

async def show_crypto_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's crypto alerts"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    # Check tier
    tier = await superbot_adapter.get_user_tier(user_id)
    if tier == "basic":
        await query.answer(
            "Price alerts require Pro tier or higher!",
            show_alert=True
        )
        return
    
    alerts = await superbot_adapter.get_user_alerts(user_id)
    
    if not alerts:
        text = "📊 **No Price Alerts Yet**\n\nSet up price alerts to track crypto movements!"
        keyboard = [
            [InlineKeyboardButton("➕ Add Alert", callback_data="add_alert")],
            [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
    else:
        text = "📊 **Your Price Alerts**\n\n"
        for i, alert in enumerate(alerts, 1):
            direction_emoji = "📈" if alert["direction"] == "above" else "📉"
            text += f"{i}. {alert['coin']} {direction_emoji} ${alert['target']}\n"
        
        keyboard = []
        for i, alert in enumerate(alerts):
            keyboard.append([InlineKeyboardButton(
                f"❌ Remove {alert['coin']}", 
                callback_data=f"remove_alert_{alert['coin']}"
            )])
        keyboard.extend([
            [InlineKeyboardButton("➕ Add Alert", callback_data="add_alert")],
            [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ])
    
    await safe_edit_message(query, 
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def start_add_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start crypto alert setup"""
    query = update.callback_query
    context.user_data["alert_step"] = 0
    await flow_state.sync(context, update.effective_user.id, 0, flow="crypto_alert")
    
    coins = superbot_adapter.CRYPTO_WATCHLIST[:8]  # Show top coins
    keyboard = [[InlineKeyboardButton(coin, callback_data=f"select_coin_{coin}")] 
                for coin in coins]
    
    await safe_edit_message(query, 
        "Select a coin to track:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def select_alert_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process coin selection"""
    query = update.callback_query
    coin = query.data.split("_", 2)[2]
    context.user_data["alert_coin"] = coin
    context.user_data["alert_step"] = 1
    await flow_state.sync(context, update.effective_user.id, 0, flow="crypto_alert")
    
    await safe_edit_message(query, 
        f"📍 {coin}\n\nSet target price (USD):"
    )

async def process_alert_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process crypto alert message input"""
    step = context.user_data.get("alert_step", 0)
    
    if step == 1:
        try:
            target = float(update.message.text.strip())
            context.user_data["alert_target"] = target
            context.user_data["alert_step"] = 2
            await flow_state.sync(context, update.effective_user.id, 0, flow="crypto_alert")
            
            await update.message.reply_text(
                "Alert when price goes:\n\nTap one:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📈 Above $" + str(target), callback_data="alert_above")],
                    [InlineKeyboardButton("📉 Below $" + str(target), callback_data="alert_below")]
                ])
            )
        except ValueError:
            await update.message.reply_text(
                f"{EMOJI_COLORS.get('error', '❌')} Invalid price. Enter a number:"
            )

async def confirm_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm and save crypto alert"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    coin = context.user_data.get("alert_coin")
    target = context.user_data.get("alert_target")
    direction = "above" if query.data == "alert_above" else "below"
    
    await superbot_adapter.set_alert(user_id, coin, target, direction)
    
    await safe_edit_message(query, 
        f"{EMOJI_COLORS.get('success', '✅')} Alert set!\n\n"
        f"You'll be notified when {coin} goes {direction} ${target}."
    )
    
    context.user_data.pop("alert_step", None)
    context.user_data.pop("alert_coin", None)
    context.user_data.pop("alert_target", None)
    await flow_state.clear(context, user_id, 0)

# ═══════════════════════════════════════════════════════════════════════════
# LEADERBOARD & ANALYTICS
# ═══════════════════════════════════════════════════════════════════════════

async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display leaderboard"""
    query = update.callback_query if update.callback_query else None
    user_id = update.effective_user.id
    
    top = await superbot_adapter.get_top_users(10)
    user_rank = await superbot_adapter.get_user_rank(user_id)
    user_points = await superbot_adapter.get_user_points(user_id)
    
    text = "🏆 **Leaderboard Top 10**\n\n"
    for i, user in enumerate(top, 1):
        medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
        text += f"{medal} {user['name']}: {user['points']} pts\n"
    
    text += "\n**Your Position:**\n"
    if user_rank:
        text += f"Rank: #{user_rank}\nPoints: {user_points}"
    else:
        text += "Not on leaderboard yet. Keep participating!"
    
    keyboard = [
        [InlineKeyboardButton("👤 My Stats", callback_data="show_stats")],
        [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
    ]
    
    if query:
        await safe_edit_message(query, 
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

async def show_user_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display user analytics"""
    query = update.callback_query if update.callback_query else None
    user_id = update.effective_user.id
    
    stats = await superbot_adapter.get_user_stats(user_id)
    
    text = f"""
📊 **Your Analytics**

**Account:**
├ Tier: {stats['tier'].upper()}
├ Rank: #{stats['rank'] or 'N/A'}
├ Points: {stats['points']}
└ Referrals: {stats['referrals']}

**Activity:**
├ Total Interactions: {stats['total_interactions']}
└ Recent Actions: {len(stats['actions'])} logged

**Features Unlocked:**
✨ {', '.join([f.replace('_', ' ').title() for f in {'basic': ['basic_access'], 'pro': ['alerts', 'analytics'], 'elite': ['priority', 'custom_alerts']}.get(stats['tier'], [])])}
"""
    
    keyboard = [
        [InlineKeyboardButton("💎 Upgrade", callback_data="show_premium_tiers")],
        [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
    ]
    
    if query:
        await safe_edit_message(query, 
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

# ═══════════════════════════════════════════════════════════════════════════
# ADMIN ONLY: Global Analytics
# ═══════════════════════════════════════════════════════════════════════════

async def show_global_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: Show global bot analytics"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    from config import ADMIN_ID
    if user_id != ADMIN_ID:
        await query.answer("Unauthorized", show_alert=True)
        return
    
    stats = await superbot_adapter.get_global_stats()
    
    text = f"""
📈 **Global Analytics**

**User Base:**
├ Total Users: {stats['total_users']}
├ Total Interactions: {stats['total_interactions']}

**Tier Distribution:**
├ Basic: {stats['tier_distribution']['basic']} users
├ Pro: {stats['tier_distribution']['pro']} users
└ Elite: {stats['tier_distribution']['elite']} users

**Top Performers:**
"""
    
    for i, user in enumerate(stats['top_users'][:5], 1):
        text += f"{i}. {user['name']}: {user['points']} pts\n"
    
    await safe_edit_message(query, 
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="admin_panel")]
        ]),
        parse_mode="Markdown"
    )
