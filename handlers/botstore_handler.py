"""
BotStore Handlers
Directory for discovering and listing bots, groups, channels
"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from config import EMOJI_COLORS
from keyboards import keyboard_gen
from modules import botstore_adapter
from database import db
import flow_state
from paystack import paystack
from utils import is_owner
from utils import safe_edit_message

def _clone_id(context) -> int:
    """0 for the main bot, else the running clone's id — tier, quota, and
    subscription lookups must be scoped to this so they never leak across
    the main bot and other clones."""
    clone_config = context.bot_data.get("clone_config")
    return clone_config.get("clone_id") if clone_config else 0

async def show_botstore_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show BotStore main menu"""
    query = update.callback_query if update.callback_query else None
    
    text = """
🏪 **BotStore Directory**

Discover and list:
• Bots — Automation & tools
• Groups — Communities  
• Channels — Content feeds

Browse sections or list yours!
"""
    
    keyboard = [
        [InlineKeyboardButton("🤖 Browse Bots", callback_data="botstore_bots")],
        [InlineKeyboardButton("👥 Browse Groups", callback_data="botstore_groups")],
        [InlineKeyboardButton("📢 Browse Channels", callback_data="botstore_channels")],
        [InlineKeyboardButton("📤 List Mine", callback_data="botstore_submit")],
        [InlineKeyboardButton("🔍 Search", callback_data="botstore_search")],
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

async def show_category_listings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show listings for a category (bots/groups/channels)"""
    query = update.callback_query
    callback_type = query.data.split("_")[1]  # bots, groups, or channels
    
    listings = await botstore_adapter.list_by_type(callback_type)
    featured = await botstore_adapter.featured(callback_type)
    trending = await botstore_adapter.trending(callback_type, limit=5)
    
    if not listings:
        await safe_edit_message(query, 
            f"No {callback_type} yet! Submit yours to get listed.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Submit", callback_data="botstore_submit")],
                [InlineKeyboardButton("⬅️ Back", callback_data="botstore_home")]
            ])
        )
        return
    
    # Show featured first
    display_items = featured[:3] + trending[:5]
    if not display_items:
        display_items = listings[:5]
    
    text = f"🏪 **{callback_type.title()} Directory**\n\n"
    
    if featured:
        text += "⭐ **Featured**\n"
        for item in featured[:3]:
            rating = await botstore_adapter.get_avg_rating(item["id"]) or "N/A"
            text += f"• {item['title']} — {rating}⭐\n"
        text += "\n"
    
    text += "🔥 **Trending**\n"
    for item in trending[:5]:
        clicks = await botstore_adapter.get_clicks(item["id"])
        text += f"• {item['title']} ({clicks} clicks)\n"
    
    keyboard = []
    for item in display_items[:3]:
        keyboard.append([InlineKeyboardButton(f"👁️ {item['title'][:20]}", 
                                             callback_data=f"botstore_view_{item['id']}")])
    
    keyboard.extend([
        [InlineKeyboardButton("📤 Submit", callback_data="botstore_submit")],
        [InlineKeyboardButton("🔙 Back", callback_data="botstore_home")]
    ])
    
    await safe_edit_message(query, 
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def show_listing_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed listing view"""
    query = update.callback_query
    lid = query.data.split("_", 2)[2]
    
    listing = await botstore_adapter.get_listing(lid)
    if not listing:
        await query.answer("Listing not found", show_alert=True)
        return
    
    # Record click
    await botstore_adapter.record_click(lid)
    
    rating = await botstore_adapter.get_avg_rating(lid) or "No ratings"
    clicks = await botstore_adapter.get_clicks(lid)
    url = botstore_adapter.to_url(listing["identifier"])
    
    text = f"""
📋 **{listing['title']}**

{listing['description']}

**Info:**
• Type: {listing['type']}
• Category: {listing['category']}
• Rating: {rating}⭐
• Views: {clicks}

**Identifier:** `{listing['identifier']}`
"""
    
    keyboard = [
        [InlineKeyboardButton("🔗 Open", url=url)],
        [InlineKeyboardButton("⭐ Rate This", callback_data=f"botstore_rate_{lid}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"botstore_{listing['type']}s")]
    ]
    
    await safe_edit_message(query, 
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def handle_submit_listing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start listing submission flow"""
    query = update.callback_query
    user_id = update.effective_user.id
    user = await db.get_user(user_id, clone_id=_clone_id(context))
    
    # Check if user is premium or hasn't hit free limit (founder/clone owner bypass)
    if not is_owner(user_id, context) and await botstore_adapter.bot_limit_reached(user_id, user.get("tier") == "premium" if user else False):
        await safe_edit_message(query, 
            "🚫 You've hit the free listing limit.\n\n"
            "Upgrade to Premium for unlimited listings!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Go Premium", callback_data="go_premium")],
                [InlineKeyboardButton("⬅️ Back", callback_data="botstore_home")]
            ])
        )
        return
    
    context.user_data["botstore_mode"] = "submit_type"
    await flow_state.sync(context, user_id, _clone_id(context), flow="botstore")

    if not (user and user.get("tos_accepted")):
        await safe_edit_message(query, 
            "⚠️ **Rules**: No nudity, no illegal content, must follow Telegram's "
            "Terms of Service. Listings violating this will be removed and reported.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ I Agree to the Terms", callback_data="botstore_tos_accept")],
                [InlineKeyboardButton("⬅️ Back", callback_data="botstore_home")]
            ]),
            parse_mode="Markdown"
        )
        return

    await safe_edit_message(query, 
        "What would you like to list?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🤖 Bot", callback_data="list_bot")],
            [InlineKeyboardButton("👥 Group", callback_data="list_group")],
            [InlineKeyboardButton("📢 Channel", callback_data="list_channel")],
            [InlineKeyboardButton("⬅️ Back", callback_data="botstore_home")]
        ])
    )

async def handle_search_botstore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start BotStore search"""
    query = update.callback_query
    context.user_data["botstore_mode"] = "search"
    await flow_state.sync(context, update.effective_user.id, _clone_id(context), flow="botstore")
    
    await safe_edit_message(query, 
        "🔍 What are you looking for?\n\nReply with search term (bot name, group, etc):"
    )

async def handle_botstore_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process BotStore messages (submit or search)"""
    text = update.message.text
    mode = context.user_data.get("botstore_mode")
    
    if mode == "search":
        results = await botstore_adapter.search_listings(text)
        if not results:
            await update.message.reply_text("No results found for that search.")
            return
        
        msg = "🔍 **Search Results**\n\n"
        for r in results[:5]:
            msg += f"• {r['title']} ({r['type']})\n"
        
        await update.message.reply_text(msg, reply_markup=keyboard_gen.main_menu())
        context.user_data.pop("botstore_mode", None)
        await flow_state.clear(context, update.effective_user.id, _clone_id(context))
    
    elif mode == "submit_type":
        # User chose type, now collect details
        step = context.user_data.get("submit_step", 0)
        
        if step == 0:
            context.user_data["listing_title"] = text
            context.user_data["submit_step"] = 1
            await flow_state.sync(context, update.effective_user.id, _clone_id(context), flow="botstore")
            await update.message.reply_text("Got it! Now describe it (what does it do?):")
        elif step == 1:
            context.user_data["listing_desc"] = text
            context.user_data["submit_step"] = 2
            await flow_state.sync(context, update.effective_user.id, _clone_id(context), flow="botstore")
            await update.message.reply_text("Nice! What's the link/username? (@username or t.me/...):")
        elif step == 2:
            context.user_data["listing_identifier"] = text
            context.user_data["submit_step"] = 3
            await flow_state.sync(context, update.effective_user.id, _clone_id(context), flow="botstore")
            await update.message.reply_text(
                "Pick a category:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(cat, callback_data=f"cat_{cat}")]
                    for cat in botstore_adapter.CATEGORIES[:5]
                ])
            )

async def finish_listing_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Complete the listing submission"""
    user_id = update.effective_user.id
    query = update.callback_query
    
    category = query.data.split("_", 1)[1]
    listing_type = context.user_data.get("listing_type")
    title = context.user_data.get("listing_title")
    desc = context.user_data.get("listing_desc")
    identifier = context.user_data.get("listing_identifier")
    
    if not all([listing_type, title, desc, identifier, category]):
        await query.answer("Missing submission details", show_alert=True)
        return
    
    await botstore_adapter.add_listing(user_id, listing_type, identifier, title, desc, category)
    
    await safe_edit_message(query, 
        f"{EMOJI_COLORS.get('success', '✅')} **Listing Created!**\n\n"
        f"Your {listing_type} **{title}** is now live in BotStore!\n\n"
        f"Share it with others to get clicks and ratings.",
        reply_markup=keyboard_gen.main_menu()
    )
    
    context.user_data.pop("botstore_mode", None)
    context.user_data.pop("submit_step", None)
    context.user_data.pop("listing_type", None)
    context.user_data.pop("listing_title", None)
    context.user_data.pop("listing_desc", None)
    context.user_data.pop("listing_identifier", None)
    await flow_state.clear(context, user_id, _clone_id(context))

async def handle_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show rating prompt"""
    query = update.callback_query
    lid = query.data.split("_", 2)[2]
    
    context.user_data["rating_lid"] = lid
    
    keyboard = [[InlineKeyboardButton(f"{'⭐' * i}", callback_data=f"rate_{lid}_{i}")] 
                for i in range(1, 6)]
    keyboard.append([InlineKeyboardButton("⬅️ Cancel", callback_data=f"botstore_view_{lid}")])
    
    await safe_edit_message(query, 
        "Rate this listing:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def submit_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record the rating"""
    query = update.callback_query
    parts = query.data.split("_")
    lid = parts[1]
    stars = int(parts[2])
    user_id = update.effective_user.id
    
    await botstore_adapter.add_rating(lid, user_id, stars)
    
    await safe_edit_message(query, 
        f"{EMOJI_COLORS.get('success', '✅')} Thanks for the {stars}⭐ rating!"
    )

async def handle_go_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initialize Selar payment for BotStore premium (unlimited listings).
    Owner / clone owner get premium activated directly, no payment."""
    query = update.callback_query
    user_id = update.effective_user.id

    if is_owner(user_id, context):
        success = await db.set_premium_tier(user_id, clone_id=_clone_id(context))
        if success:
            await safe_edit_message(query, 
                f"{EMOJI_COLORS.get('success', '✅')} **Owner bypass — Premium Activated!**\n\n"
                f"You can now submit unlimited listings. 🎉",
                reply_markup=keyboard_gen.main_menu()
            )
        else:
            await query.answer("Owner bypass, but premium activation failed — check logs.", show_alert=True)
        return

    email = f"user_{user_id}@animebot.com"
    price_ghs = botstore_adapter.ConfigCache.PREMIUM_PRICE_GHS

    payment_result = paystack.initialize_payment(
        email,
        price_ghs * 100,  # Convert GHS to pesewas
        user_id,
        f"BotStorePremium_{user_id}",
        payment_type="botstore_premium",
        extra_metadata={"clone_id": _clone_id(context)}
    )

    if payment_result and payment_result.get("status") == "success":
        payment_reference = payment_result.get("reference")
        payment_link = payment_result.get("authorization_url")

        # Persisted server-side (not context.user_data) so the 'I've Paid —
        # Verify' tap - handled by manual_payments.handle_user_verification,
        # which may run on a different serverless instance - can still find it.
        await db.create_pending_payment_intent(payment_reference, user_id, "botstore_premium")

        payment_text = f"""
{EMOJI_COLORS.get('success', '✅')} **BotStore Premium**

Click the link below to pay GHS {price_ghs}.00 via Selar:

[Pay Now]({payment_link})

After payment, click "I've Paid — Verify" below. The admin will confirm your payment and unlock unlimited listings.
"""
        await safe_edit_message(query, 
            payment_text,
            reply_markup=keyboard_gen.botstore_premium_verify_keyboard(),
            parse_mode="Markdown"
        )
    else:
        await query.answer("Failed to initialize payment. Please try again.", show_alert=True)


async def handle_tos_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record ToS acceptance, then resume the submission flow"""
    query = update.callback_query
    if query:
        await query.answer()
    user_id = update.effective_user.id
    await db.set_tos_accepted(user_id, clone_id=_clone_id(context))
    await handle_submit_listing(update, context)
