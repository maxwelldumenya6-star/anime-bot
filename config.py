import os
from dotenv import load_dotenv

load_dotenv()

# Bot Configuration
BOT_TOKEN = os.getenv("SINOBANED2_BOT_TOKEN", "your_token_here")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Optional group chat to mirror admin-relevant events to (submissions, payments, etc.)
# Get a group's chat ID by adding the bot there and running /getchatid.
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID", "")

# Shared secret that protects the cron-triggered endpoints (api/cron_autopost.py,
# api/cron_broadcast.py) from being called by anyone but your scheduler. Sent by
# the caller either as header "Authorization: Bearer <CRON_SECRET>" (Vercel Cron
# does this automatically when this env var is set) or as query param ?secret=.
CRON_SECRET = os.getenv("CRON_SECRET", "")

# Temporary data directory (only for ephemeral cache, NOT production data)
# All persistent data MUST go to DATABASE_URL (Postgres)
DATA_DIR = os.getenv("DATA_DIR", "/tmp/data")

# Database — must be a Postgres connection string (Supabase, Neon, or Railway Postgres).
# SQLite is NOT usable here: this bot runs in a serverless/ephemeral container with
# no writable, persistent disk.
DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Set it to your Postgres connection string "
        "(e.g. postgresql://user:password@host:5432/dbname)."
    )

# API Configuration
ANILIST_ENDPOINT = "https://graphql.anilist.co"
JIKAN_ENDPOINT = "https://api.jikan.moe/v4"

# Payment Configuration
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")
CLONE_BOT_FEE_GHS = 50  # 50 GHS in pesewas = 5000

# --- Shared AI Chat / Download paywall ---------------------------------------
# Both "🤖 AI Chat" and "⬇️ Download" give every user UTILITY_FREE_USES free
# goes each (tracked separately per feature), then ONE 25 GHS / 2-month
# subscription unlocks BOTH features together (handlers/utility_paywall.py).
UTILITY_FREE_USES = 2
UTILITY_SUB_FEE_GHS = 25
UTILITY_SUB_DAYS = 60  # ~2 months

# --- Premium Group paywall (broadcast footer) --------------------------------
# Every admin broadcast (api/cron_broadcast.py) carries a fixed-price "Pay to
# Join Premium Group" button. This is a single, bot-wide premium group (not
# per-broadcast) — the price is fixed here; where paid users actually land is
# PREMIUM_GROUP_INVITE_LINK, which you set once to your premium group/channel's
# invite link (e.g. https://t.me/+xxxxxxxx). If left blank, the payment button
# still works but the post-payment confirmation won't include a join link —
# set this before relying on the feature.
PREMIUM_GROUP_FEE_GHS = 20
PREMIUM_GROUP_INVITE_LINK = os.getenv("PREMIUM_GROUP_INVITE_LINK", "")

# --- Clone monetization gate --------------------------------------------------
# A clone owner can (a) connect their own Selar/Stripe key instead of
# routing through the main bot's account, and (b) set their own price for
# every paywalled feature their clone runs — but both are gated behind this
# recurring activation fee (handlers/clone_bot.py's "💰 Monetization" menu).
# While inactive, a clone runs on registry-default pricing and the main
# bot's gateway account only. Auto-reverts on lapse (see
# database.py's expire_monetization_subscriptions(), run by
# api/cron_expire_monetization.py).
CLONE_MONETIZATION_FEE_GHS = 20
CLONE_MONETIZATION_DAYS = 30

# --- Yandex direct-search subscription --------------------------------------
# Reverse-search previews (thumbnails, source-link unlock) stay pay-per-use
# via IMAGE_SEARCH_FEE_GHS (handlers/image_search_handler.py). Jumping
# straight to Yandex's own reverse-image-search results page with the
# image pre-loaded is a separate, recurring perk gated behind its own
# monthly fee.
IMAGE_SEARCH_YANDEX_FEE_GHS = 20
IMAGE_SEARCH_YANDEX_DAYS = 30

# Registry of every price a clone owner can override once their
# monetization subscription is active. key -> {label, default GHS}.
# database.py's get_clone_price()/get_clone_prices() resolve against this,
# and handlers/clone_bot.py's price-editing menu is generated from it — add
# a new paywalled feature here and it's automatically editable, no other
# wiring needed beyond having that feature's handler call get_clone_price().
#
# NOTE: superbot tier pricing (Pro/Elite) and botstore listing pricing
# (Featured/Premium) are intentionally NOT in this registry yet. Their
# current grants (modules/superbot_adapter.set_user_tier,
# db.set_premium_tier) aren't clone-scoped at all — a user's Pro tier on the
# main bot currently also shows as Pro on every clone. Giving clone owners
# their own price for those without first fixing that scoping bug would let
# a clone owner sell a tier whose access the main bot (or another clone)
# secretly shares. That's a separate fix — flag before extending this
# registry to cover them.
PRICE_REGISTRY = {
    "ai_subscription":     {"label": "AI Chat/Image subscription (per month)",     "default": 10},
    "image_search_unlock": {"label": "Image search source-link unlock",           "default": 10},
    "premium_group_fee":   {"label": "Premium group join fee",                    "default": 20},
    "utility_sub_fee":     {"label": "AI Chat + Download subscription (2 months)", "default": 25},
}

# --- Real clone-bot system (Part 3 of the master brief) ---------------------
# Rollback flag (3.5): keep the OLD fake-token behavior available behind this
# flag during rollout. Default is OFF (real system) once this ships — flip to
# "false" instantly if the multi-tenant router ever misroutes a message.
CLONE_BOT_REAL_ENABLED = os.getenv("CLONE_BOT_REAL_ENABLED", "true").lower() == "true"

# The public HTTPS base URL this app is deployed at (e.g. your Vercel domain,
# no trailing slash). Required to register per-clone webhooks
# (https://<PUBLIC_BASE_URL>/api/bot?clone_id=N). Clone creation fails loudly
# if this isn't set, rather than silently registering a broken webhook URL.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Max number of per-clone Application instances kept warm in memory at once
# (Part 3.1 "Per-clone Application instances" — bounded LRU, not unbounded).
CLONE_APP_CACHE_SIZE = int(os.getenv("CLONE_APP_CACHE_SIZE", "20"))

# Username (no @) of the MAIN bot, e.g. "AnimeCrunchBot". Used so every clone
# carries a visible trace back to the main bot — a "Powered by" line plus a
# deep-link button that lets clone users jump to the main bot and start their
# own clone (growth loop). Leave unset to hide this entirely.
MAIN_BOT_USERNAME = os.getenv("MAIN_BOT_USERNAME", "")

# Features
MAX_BUTTONS_PER_ROW = 2
PAGINATION_SIZE = 5
RATE_LIMIT_SEARCHES = 10
RATE_LIMIT_SUBMISSIONS = 5

# Emoji Color Codes for UI
EMOJI_COLORS = {
    "trending": "🔥",      # Red/Hot
    "latest": "✨",        # Sparkle/New
    "ongoing": "🔄",       # Cycle/Ongoing
    "season": "📅",        # Calendar
    "movies": "🎬",        # Movie
    "search": "🔍",        # Search
    "submit": "📤",        # Upload
    "admin": "⚙️",         # Settings
    "clone": "🤖",         # Robot
    "categories": "📚",    # Books/Library
    "success": "✅",       # Check
    "error": "❌",         # Cross
    "loading": "⏳",       # Hourglass
    "back": "⬅️",          # Back
    "next": "➡️",          # Next
}

# Animation frames
LOADING_ANIMATION = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# Messages
MESSAGES = {
    "welcome": "Welcome to the Anime & Movies Bot! Choose what you want to explore.",
    "trending_title": "Trending Anime Right Now",
    "latest_title": "Latest Anime Releases",
    "ongoing_title": "Ongoing Series",
    "season_title": "This Season's Anime",
    "movies_title": "Anime Movies",
    "no_results": "No results found. Try another search.",
    "submission_received": "Thank you! Your submission has been received and is under review.",
    "clone_prompt": "Clone this bot for 50 GHS and customize it for yourself!",
    "payment_success": "Payment successful! Setting up your new bot...",
}
