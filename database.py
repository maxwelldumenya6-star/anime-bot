import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import asyncpg

from config import DATABASE_URL
from utils.crypto import secret_manager

logger = logging.getLogger(__name__)

_pool = None


async def get_pool():
    """
    Get or create the asyncpg connection pool for serverless (Issue 1.4).
    
    On serverless deployments (Vercel), each cold start and concurrent instance
    creates its own pool. If not using PgBouncer, this leads to connection exhaustion.
    
    Fix: Use PgBouncer if available (Supabase built-in, Neon, etc.) and reduce
    pool size to 1 connection per instance (sufficient for single async request flow).
    """
    global _pool
    if _pool is None:
        # Auto-detect PgBouncer endpoint (Supabase pooler: port 6543, transaction mode)
        # If using pooler, reduce pool size since PgBouncer handles connection multiplexing
        url = DATABASE_URL
        is_using_pooler = "pooler" in url or ":6543" in url
        
        # For serverless: 1 connection per instance is sufficient
        # (a single serverless invocation doesn't run concurrent queries)
        # If using a pooler, it's even safer (pooler handles connection fan-in)
        min_pool_size = 1
        max_pool_size = 1 if is_using_pooler else 2
        
        logger.info(
            "[v0] Creating connection pool: min=%s, max=%s, pooler=%s",
            min_pool_size, max_pool_size,
            "yes" if is_using_pooler else "no (consider using PgBouncer)"
        )
        
        _pool = await asyncpg.create_pool(
            url,
            min_size=min_pool_size,
            max_size=max_pool_size,
            statement_cache_size=0,  # Required for PgBouncer compatibility
            command_timeout=10
        )
    return _pool


class Database:
    """Database handler backed by Postgres (Supabase / Neon / Railway Postgres)."""

    def __init__(self):
        # asyncpg wants postgres:// or postgresql://, both work
        self.dsn = DATABASE_URL

    async def init(self):
        """Create tables if they don't exist yet (safe to run on every cold start)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await self._create_tables(conn)

    # ─────────────────────────────────────────────────────────────────────
    # Generic query passthroughs. Several handlers (feature_handlers.py,
    # moderation.py) run one-off queries against tables that don't warrant
    # a dedicated method — these mirror asyncpg's Connection API so `db.`
    # can be used as a drop-in pool everywhere else already assumes it is.
    # ─────────────────────────────────────────────────────────────────────
    async def execute(self, query: str, *args):
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetchrow(self, query: str, *args):
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetch(self, query: str, *args):
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchval(self, query: str, *args):
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    # --- Custom categories (table existed in schema but had zero CRUD around it) ---

    async def create_category(self, owner_id: int, category_name: str, emoji: str = "📁") -> int:
        """Create a new custom category for a user"""
        row = await self.fetchrow("""
            INSERT INTO categories (owner_id, category_name, emoji, anime_ids)
            VALUES ($1, $2, $3, '')
            RETURNING category_id
        """, owner_id, category_name, emoji)
        return row["category_id"]

    async def get_user_categories(self, owner_id: int) -> List[Dict]:
        """Get all categories a user has created"""
        rows = await self.fetch("""
            SELECT * FROM categories WHERE owner_id = $1 ORDER BY created_date DESC
        """, owner_id)
        return [{
            "category_id": row["category_id"],
            "category_name": row["category_name"],
            "emoji": row["emoji"],
            "anime_ids": [int(a) for a in row["anime_ids"].split(",") if a] if row["anime_ids"] else []
        } for row in rows]

    async def get_category(self, category_id: int) -> Optional[Dict]:
        """Get a single category by id"""
        row = await self.fetchrow("SELECT * FROM categories WHERE category_id = $1", category_id)
        if not row:
            return None
        return {
            "category_id": row["category_id"],
            "owner_id": row["owner_id"],
            "category_name": row["category_name"],
            "emoji": row["emoji"],
            "anime_ids": [int(a) for a in row["anime_ids"].split(",") if a] if row["anime_ids"] else []
        }

    async def add_anime_to_category(self, category_id: int, anime_id: int):
        """Add an anime id to a category's list (comma-separated string column)"""
        row = await self.fetchrow("SELECT anime_ids FROM categories WHERE category_id = $1", category_id)
        existing = [a for a in (row["anime_ids"] or "").split(",") if a]
        if str(anime_id) not in existing:
            existing.append(str(anime_id))
        await self.execute(
            "UPDATE categories SET anime_ids = $1 WHERE category_id = $2",
            ",".join(existing), category_id
        )

    async def _create_tables(self, conn):
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tier TEXT DEFAULT 'free',
                submissions_count INTEGER DEFAULT 0,
                is_admin BOOLEAN DEFAULT FALSE,
                subscription_status TEXT DEFAULT 'inactive',
                subscription_expiry TIMESTAMP,
                stripe_key TEXT
            )
        """)

        # Per-clone tier/quota/subscription status. `users` above stays a
        # single global identity row per Telegram user_id (needed because
        # `submissions.user_id` has a foreign key into it) — but tier,
        # premium status, free-use counters, and subscriptions must NOT be
        # global, since the same Telegram user_id talks to the main bot and
        # every clone. clone_id = 0 is the main bot; each clone gets its own
        # row per user, so a premium purchase on one bot never grants
        # premium on another.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_clone_status (
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                clone_id BIGINT NOT NULL DEFAULT 0,
                tier TEXT DEFAULT 'free',
                tos_accepted BOOLEAN DEFAULT FALSE,
                subscription_status TEXT DEFAULT 'inactive',
                subscription_expiry TIMESTAMP,
                free_ai_chat_uses INTEGER DEFAULT 0,
                free_download_uses INTEGER DEFAULT 0,
                utility_sub_status TEXT DEFAULT 'inactive',
                utility_sub_expiry TIMESTAMP,
                free_image_search_used BOOLEAN DEFAULT FALSE,
                language TEXT DEFAULT 'en',
                PRIMARY KEY (user_id, clone_id)
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                submission_id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                anime_name TEXT NOT NULL,
                episodes INTEGER,
                genres TEXT,
                synopsis TEXT,
                image_url TEXT,
                status TEXT DEFAULT 'pending',
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_date TIMESTAMP,
                rejection_reason TEXT
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cloned_bots (
                clone_id SERIAL PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES users(user_id),
                bot_name TEXT NOT NULL,
                bot_token TEXT UNIQUE NOT NULL,
                webhook_url TEXT,
                custom_data TEXT,
                status TEXT DEFAULT 'active',
                payment_id TEXT,
                payment_status TEXT,
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # --- Migration: real clone-bot columns (Part 3.2 Step C) -------------
        # cloned_bots already existed before this change (with a fake bot_token
        # and a `status` column). These ALTERs are additive and safe to run on
        # every cold start against a table that may or may not have them yet.
        await conn.execute("""
            ALTER TABLE cloned_bots ADD COLUMN IF NOT EXISTS webhook_secret TEXT
        """)
        await conn.execute("""
            ALTER TABLE cloned_bots ADD COLUMN IF NOT EXISTS bot_username TEXT
        """)
        # --- Migration: referral tracking for the "Get your own bot" growth-loop
        # button (keyboards.py main_menu). Counts how many new signups a given
        # clone has sent to the main bot's /start=fromclone_<id> deep link.
        await conn.execute("""
            ALTER TABLE cloned_bots ADD COLUMN IF NOT EXISTS referral_count INTEGER NOT NULL DEFAULT 0
        """)
        # --- Migration: BotStore ToS acceptance flag ---------------------------
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS tos_accepted BOOLEAN DEFAULT FALSE
        """)

        # --- Migration: shared AI Chat / Download paywall -----------------------
        # 2 free uses each (tracked separately), then ONE subscription (25 GHS /
        # 2 months) unlocks both features together. See handlers/utility_paywall.py.
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS free_ai_chat_uses INTEGER DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS free_download_uses INTEGER DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS utility_sub_status TEXT DEFAULT 'inactive'
        """)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS utility_sub_expiry TIMESTAMP
        """)

        # --- Migration: Reverse Image Search paywall -----------------------------
        # 1 free source-link reveal per user, then GHS 10 via Selar. See
        # handlers/image_search_handler.py.
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS free_image_search_used BOOLEAN DEFAULT FALSE
        """)

        # --- Migration: user language preference (i18n) -------------------------
        # Two-letter code, must match a key in i18n.SUPPORTED_LANGUAGES. Static UI
        # strings are looked up via i18n.t(key, lang); AI-generated responses are
        # steered via i18n.language_instruction(lang). See i18n.py.
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS language TEXT DEFAULT 'en'
        """)

        # --- Migration: backfill user_clone_status from the old shared
        # columns on `users`, into clone_id = 0 (the main bot) — so
        # existing tiers/subscriptions/quotas aren't lost when this
        # isolation fix ships. The `users` columns themselves stay in
        # place for now (harmless legacy) but are no longer read/written
        # by the app; user_clone_status is the source of truth going
        # forward.
        await conn.execute("""
            INSERT INTO user_clone_status (
                user_id, clone_id, tier, tos_accepted, subscription_status, subscription_expiry,
                free_ai_chat_uses, free_download_uses, utility_sub_status, utility_sub_expiry,
                free_image_search_used, language
            )
            SELECT user_id, 0, COALESCE(tier, 'free'), COALESCE(tos_accepted, FALSE),
                   COALESCE(subscription_status, 'inactive'), subscription_expiry,
                   COALESCE(free_ai_chat_uses, 0), COALESCE(free_download_uses, 0),
                   COALESCE(utility_sub_status, 'inactive'), utility_sub_expiry,
                   COALESCE(free_image_search_used, FALSE), COALESCE(language, 'en')
            FROM users
            ON CONFLICT (user_id, clone_id) DO NOTHING
        """)

        # --- Migration: BotFather-style Bot Manager -----------------------------
        # Lets a user register bots they already own (by token) so they can edit
        # name/description/commands via the Bot API without leaving this bot.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS managed_bots (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                token TEXT NOT NULL,
                username TEXT,
                name TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, token)
            )
        """)
        # `status` already exists in this inline schema (unlike what the original
        # audit found in sql/schema.sql's drifted version) — nothing to add there,
        # just documenting that the reconciliation was checked, not skipped.
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_cloned_bots_bot_token ON cloned_bots(bot_token)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS anime_entries (
                anime_id SERIAL PRIMARY KEY,
                anilist_id INTEGER UNIQUE,
                mal_id INTEGER UNIQUE,
                title TEXT NOT NULL,
                episodes INTEGER,
                genres TEXT,
                rating REAL,
                status TEXT,
                synopsis TEXT,
                image_url TEXT,
                source_api TEXT,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                category_id SERIAL PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES users(user_id),
                category_name TEXT NOT NULL,
                emoji TEXT,
                anime_ids TEXT,
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payment_logs (
                payment_id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                amount REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                selar_sale_id TEXT UNIQUE,
                paystack_reference TEXT UNIQUE,
                payment_method TEXT DEFAULT 'paystack',
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS selar_sale_id TEXT")
        await conn.execute("ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS paystack_reference TEXT")
        await conn.execute("ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS payment_method TEXT DEFAULT 'paystack'")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_logs_selar_sale_id ON payment_logs(selar_sale_id) WHERE selar_sale_id IS NOT NULL")


        await conn.execute("""
            CREATE TABLE IF NOT EXISTS commission_tracking (
                commission_id SERIAL PRIMARY KEY,
                cloned_bot_id INTEGER NOT NULL REFERENCES cloned_bots(clone_id),
                payment_amount INTEGER NOT NULL,
                main_commission INTEGER NOT NULL,
                owner_amount INTEGER NOT NULL,
                stripe_key_id TEXT,
                payment_intent_id TEXT UNIQUE,
                status TEXT DEFAULT 'completed',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscription_payments (
                subscription_id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                payment_amount INTEGER NOT NULL,
                subscription_month TEXT NOT NULL,
                payment_method TEXT,
                payment_reference TEXT UNIQUE,
                status TEXT DEFAULT 'completed',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS botstore_listings (
                id TEXT PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES users(user_id),
                type TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                link TEXT,
                status TEXT DEFAULT 'pending',
                rating FLOAT DEFAULT 0,
                clicks INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS botstore_ratings (
                id SERIAL PRIMARY KEY,
                listing_id TEXT NOT NULL REFERENCES botstore_listings(id),
                user_id BIGINT NOT NULL,
                stars INTEGER NOT NULL,
                review TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS superbot_user_tiers (
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
                tier TEXT DEFAULT 'basic',
                tier_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS superbot_referrals (
                id SERIAL PRIMARY KEY,
                referrer_id BIGINT NOT NULL,
                referee_id BIGINT NOT NULL,
                status TEXT DEFAULT 'active',
                reward_given BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS superbot_crypto_alerts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                coin TEXT NOT NULL,
                price_threshold FLOAT NOT NULL,
                alert_type TEXT,
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ═══════════════════════════════════════════════════════════════════════════
        # 20 FEATURES TABLES
        # ════���������══════════════════════════════════════════════════════════════════════

        # Feature 1-2: Inline Query & Chosen Results
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS inline_searches (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                query TEXT NOT NULL,
                result_id TEXT NOT NULL,
                result_type TEXT,
                was_chosen BOOLEAN DEFAULT FALSE,
                search_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 3: My Chat Member (bot added/removed)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_group_membership (
                id SERIAL PRIMARY KEY,
                group_id BIGINT NOT NULL,
                clone_id BIGINT NOT NULL DEFAULT 0,
                bot_status TEXT DEFAULT 'member',
                status_changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (group_id, clone_id)
            )
        """)

        # Feature 4: Chat Member (track user joins/leaves)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_group_events (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                group_id BIGINT NOT NULL,
                event_type TEXT,
                event_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Features 5-6: Payment & Pre-Checkout
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount_usd FLOAT NOT NULL,
                currency TEXT DEFAULT 'USD',
                payment_method TEXT,
                status TEXT DEFAULT 'pending',
                tier_unlocked TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)

        # Feature 7: Shipping
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shipping_orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                item_id TEXT,
                shipping_address TEXT,
                shipping_option TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 8: User Profile Photos
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profile_photos (
                user_id BIGINT PRIMARY KEY,
                file_id TEXT NOT NULL,
                photo_url TEXT,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 9: Edit Message Tracking
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS edited_messages (
                id SERIAL PRIMARY KEY,
                message_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                original_text TEXT,
                edited_text TEXT,
                edit_count INTEGER DEFAULT 1,
                first_edited TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_edited TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 10: Message Reactions
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS message_reactions (
                id SERIAL PRIMARY KEY,
                message_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                emoji TEXT NOT NULL,
                reaction_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(message_id, user_id, emoji)
            )
        """)

        # Feature 11: Polls
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS polls (
                id SERIAL PRIMARY KEY,
                poll_id TEXT NOT NULL UNIQUE,
                creator_id BIGINT NOT NULL,
                question TEXT NOT NULL,
                options TEXT ARRAY,
                vote_counts INTEGER ARRAY,
                is_closed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 12: Dice/Lottery Rolls
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS dice_rolls (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                dice_type TEXT DEFAULT 'cube',
                result INTEGER,
                reward INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 13: Games
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_games (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                game_name TEXT NOT NULL,
                high_score INTEGER DEFAULT 0,
                play_count INTEGER DEFAULT 0,
                last_played TIMESTAMP
            )
        """)

        # Feature 14: Web App Data
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS web_app_sessions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                session_token TEXT UNIQUE,
                web_app_data TEXT,
                write_access_allowed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP
            )
        """)

        # Feature 15: Passport (Identity Verification)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS passport_verifications (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                document_type TEXT,
                verification_status TEXT DEFAULT 'pending',
                age_verified BOOLEAN DEFAULT FALSE,
                verified_age INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 16: Location & Geofencing
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_locations (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                latitude FLOAT NOT NULL,
                longitude FLOAT NOT NULL,
                location_name TEXT,
                location_type TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS proximity_alerts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                event_name TEXT NOT NULL,
                event_latitude FLOAT NOT NULL,
                event_longitude FLOAT NOT NULL,
                alert_radius_km FLOAT DEFAULT 5,
                alert_sent BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 17: Video Chat Members
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS group_video_calls (
                id SERIAL PRIMARY KEY,
                group_id BIGINT NOT NULL,
                call_started TIMESTAMP,
                call_ended TIMESTAMP,
                participant_count INTEGER,
                status TEXT DEFAULT 'active'
            )
        """)

        # Feature 18: User Shared (Referrals)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_shares (
                id SERIAL PRIMARY KEY,
                sharer_id BIGINT NOT NULL,
                shared_user_id BIGINT NOT NULL,
                share_type TEXT,
                bonus_points INTEGER DEFAULT 100,
                bonus_claimed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 19: Deep Links Tracking
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS deep_links (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                link_code TEXT UNIQUE,
                target_type TEXT,
                target_id TEXT,
                clicked_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 20: Write Access Allowed
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_write_access (
                user_id BIGINT PRIMARY KEY,
                write_access_allowed BOOLEAN DEFAULT FALSE,
                permission_granted_at TIMESTAMP,
                last_message_sent TIMESTAMP
            )
        """)

        # Clone Bot Payments - tracks pending/verified payments for bot cloning (Task 1)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS clone_payments (
                reference TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS manual_payment_reviews (
                reference TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                payment_type TEXT NOT NULL,
                clone_id BIGINT NOT NULL DEFAULT 0,
                context JSONB NOT NULL DEFAULT '{}'::jsonb,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                decided_at TIMESTAMP,
                decided_by BIGINT
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_manual_payment_reviews_status ON manual_payment_reviews(status)")

        # Bridges a payment reference (and any small bit of context it needs,
        # e.g. clone_id, chat_id, tier) between the "Pay Now" tap and the
        # "I've Paid" tap. On Vercel serverless each webhook update can land
        # on a fresh instance with no shared memory, so context.user_data set
        # during "Pay Now" is not reliably there anymore by "I've Paid" -
        # this table is the durable handoff instead. Rows are deleted once
        # the review has been requested (see manual_payments.py), so this
        # table only ever holds short-lived, in-flight intents.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_payment_intents (
                reference TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                payment_type TEXT NOT NULL,
                chat_id BIGINT,
                context JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_payment_intents_lookup ON pending_payment_intents(user_id, payment_type, chat_id)")

        # Generic durable backing store for multi-step "wizard" flows
        # (broadcast, autopost, clone customization, bot manager, ads
        # marketplace, botstore, games, crypto alerts, AI prompts, etc).
        # Same problem as pending_payment_intents above: these flows track
        # "what step is the user on" + draft data in context.user_data,
        # which is not reliably there anymore on the next webhook update
        # once a serverless instance has been recycled. context.user_data
        # is kept as a same-instance fast path (see flow_state.py), but
        # this table is the source of truth. A user can only be in one
        # wizard at a time, so this is one row per (user_id, clone_id).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_flow_state (
                user_id BIGINT NOT NULL,
                clone_id BIGINT NOT NULL DEFAULT 0,
                flow TEXT NOT NULL,
                data JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, clone_id)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_flow_state_updated_at ON user_flow_state(updated_at)")

        # Reverse-image-search results cached between "send a photo" and the
        # "Reveal Source Links (Free)" tap - same cold-serverless-instance
        # problem as payments, so this can't live in context.user_data either.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS image_search_sessions (
                user_id BIGINT NOT NULL,
                clone_id BIGINT NOT NULL DEFAULT 0,
                image_url TEXT,
                results JSONB NOT NULL DEFAULT '[]'::jsonb,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, clone_id)
            )
        """)

        # Clone monetization subscription (20 GHS/month gate on connecting a
        # clone owner's own Selar/Stripe key AND on setting custom prices
        # for their clone's paywalled features — see PRICE_REGISTRY in
        # config.py and get_clone_price()/set_clone_price() below).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS clone_monetization_subscriptions (
                clone_id INTEGER PRIMARY KEY REFERENCES cloned_bots(clone_id) ON DELETE CASCADE,
                owner_id BIGINT NOT NULL,
                status VARCHAR(20) DEFAULT 'inactive',
                payment_reference VARCHAR(255),
                activated_at TIMESTAMP NULL,
                expires_at TIMESTAMP NULL
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_clone_monetization_status
            ON clone_monetization_subscriptions(status)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_clone_monetization_expires
            ON clone_monetization_subscriptions(expires_at)
        """)

        # Per-user Yandex direct-search subscription (config.IMAGE_SEARCH_YANDEX_FEE_GHS
        # /month). Scoped by (user_id, clone_id) since it's a user-level perk,
        # not a clone-level one — a user's subscription on one clone doesn't
        # carry over to another clone or the main bot.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS image_search_yandex_subscriptions (
                user_id BIGINT NOT NULL,
                clone_id INTEGER NOT NULL DEFAULT 0,
                status VARCHAR(20) DEFAULT 'inactive',
                payment_reference VARCHAR(255),
                authorization_code VARCHAR(255),
                activated_at TIMESTAMP NULL,
                expires_at TIMESTAMP NULL,
                PRIMARY KEY (user_id, clone_id)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_image_search_yandex_expires
            ON image_search_yandex_subscriptions(expires_at)
        """)
        await conn.execute("""
            ALTER TABLE image_search_yandex_subscriptions ADD COLUMN IF NOT EXISTS authorization_code VARCHAR(255)
        """)

        # ═══════════════════════════════════════════════════════════════════════════
        # NEW FEATURES TABLES (Task 1-14)
        # ═══════════════════════════════════════════════════════════════════════════

        # AI Chat Usage & History
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_chat_usage (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                prompt TEXT,
                response TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Backfill for installs that already had ai_chat_usage without this column.
        await conn.execute("""
            ALTER TABLE ai_chat_usage ADD COLUMN IF NOT EXISTS response TEXT
        """)

        # AI Image Generation Usage
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_image_usage (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                prompt TEXT,
                image_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Sponsored Posts
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sponsored_posts (
                id SERIAL PRIMARY KEY,
                admin_id BIGINT NOT NULL REFERENCES users(user_id),
                content TEXT NOT NULL,
                button_label TEXT,
                button_url TEXT,
                runs_remaining INTEGER NOT NULL,
                runs_total INTEGER NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Ad Submissions
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ad_submissions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                company_name TEXT NOT NULL,
                ad_title TEXT NOT NULL,
                ad_description TEXT NOT NULL,
                target_url TEXT NOT NULL,
                budget_usd FLOAT NOT NULL,
                status TEXT DEFAULT 'pending',
                rejection_reason TEXT,
                submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_at TIMESTAMP
            )
        """)

        # Services Marketplace
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS services_listings (
                id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                service_name TEXT NOT NULL,
                service_title TEXT NOT NULL,
                description TEXT,
                price_usd FLOAT NOT NULL,
                category TEXT,
                rating FLOAT DEFAULT 0,
                reviews_count INTEGER DEFAULT 0,
                clicks INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Managed Bot Tokens (user-registered bots)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS managed_bot_tokens (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                bot_name TEXT NOT NULL,
                bot_token TEXT UNIQUE NOT NULL,
                bot_username TEXT,
                is_valid BOOLEAN DEFAULT TRUE,
                last_verified TIMESTAMP,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Chat Lifecycle & Membership
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_memberships (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                bot_is_member BOOLEAN DEFAULT TRUE,
                autopost_link TEXT,
                autopost_label TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                left_at TIMESTAMP
            )
        """)

        # Recurring Posts (scheduled messages in chats)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS recurring_posts (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                admin_id BIGINT NOT NULL REFERENCES users(user_id),
                content TEXT NOT NULL,
                interval_hours INTEGER NOT NULL,
                last_posted TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Group Moderation Settings & Filters
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS group_moderation_settings (
                chat_id BIGINT PRIMARY KEY,
                admin_id BIGINT NOT NULL REFERENCES users(user_id),
                captcha_enabled BOOLEAN DEFAULT FALSE,
                captcha_timeout_seconds INTEGER DEFAULT 300,
                slow_mode_enabled BOOLEAN DEFAULT FALSE,
                slow_mode_interval_seconds INTEGER DEFAULT 5,
                night_mode_enabled BOOLEAN DEFAULT FALSE,
                night_mode_start_hour INTEGER,
                night_mode_end_hour INTEGER,
                word_filter_enabled BOOLEAN DEFAULT FALSE,
                anti_raid_enabled BOOLEAN DEFAULT FALSE,
                anti_raid_threshold INTEGER DEFAULT 5,
                anti_raid_window_minutes INTEGER DEFAULT 5,
                report_enabled BOOLEAN DEFAULT TRUE,
                logging_channel_id BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Blocked Words/Phrases
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS blocked_words (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                word_phrase TEXT NOT NULL,
                added_by BIGINT NOT NULL REFERENCES users(user_id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, word_phrase)
            )
        """)

        # User Warns (for moderation)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_warns (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                reason TEXT,
                warned_by BIGINT NOT NULL REFERENCES users(user_id),
                warn_count INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Generic labeled-link buttons (feature #8) — reusable for "Pay Now",
        # "Join Our Channel", "Rules", or anything else an admin wants as a
        # one-tap button without needing new code per label.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS custom_link_buttons (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                label TEXT NOT NULL,
                url TEXT NOT NULL,
                position INTEGER DEFAULT 0,
                created_by BIGINT NOT NULL REFERENCES users(user_id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, label)
            )
        """)

        # Custom Group Commands
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS custom_group_commands (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                command_name TEXT NOT NULL,
                response_text TEXT NOT NULL,
                created_by BIGINT NOT NULL REFERENCES users(user_id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, command_name)
            )
        """)

        # Join Gate (mandatory subscribe link)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS join_gate_settings (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                gate_link TEXT NOT NULL,
                gate_label TEXT DEFAULT 'Join Required',
                enabled BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Mandatory Join Verifications
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS join_gate_verifications (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                verified BOOLEAN DEFAULT FALSE,
                verified_at TIMESTAMP,
                check_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Ad Analytics (click tracking for sponsored posts)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ad_analytics (
                id SERIAL PRIMARY KEY,
                ad_id INTEGER REFERENCES ad_submissions(id),
                impression_count INTEGER DEFAULT 0,
                click_count INTEGER DEFAULT 0,
                tracked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Moderation Logs
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS moderation_logs (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                action_type TEXT NOT NULL,
                target_user_id BIGINT,
                performed_by BIGINT NOT NULL REFERENCES users(user_id),
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # --- Autopost: bring recurring_posts up to date for minute-granularity
        # intervals and media support (originally text/hours-only). Uses
        # ALTER ... ADD COLUMN IF NOT EXISTS so this is safe to re-run against
        # a database that already has the older, narrower table.
        await conn.execute("ALTER TABLE recurring_posts ALTER COLUMN interval_hours DROP NOT NULL")
        await conn.execute("ALTER TABLE recurring_posts ADD COLUMN IF NOT EXISTS interval_minutes INTEGER")
        await conn.execute("ALTER TABLE recurring_posts ADD COLUMN IF NOT EXISTS media_file_id TEXT")
        await conn.execute("ALTER TABLE recurring_posts ADD COLUMN IF NOT EXISTS media_type TEXT")
        await conn.execute("ALTER TABLE recurring_posts ADD COLUMN IF NOT EXISTS failure_count INTEGER DEFAULT 0")

        # --- Migration: per-chat welcome message (Bot Manager / moderation extras) ---
        await conn.execute("ALTER TABLE chat_memberships ADD COLUMN IF NOT EXISTS welcome_message TEXT")

        # --- Migration: optional "Pay Now" button attached to the welcome message.
        # Generic — admin picks the label (e.g. "Pay Now", "Unlock VIP") and an
        # amount in GHS; not tied to any specific payment purpose.
        await conn.execute("ALTER TABLE chat_memberships ADD COLUMN IF NOT EXISTS pay_button_label TEXT")
        await conn.execute("ALTER TABLE chat_memberships ADD COLUMN IF NOT EXISTS pay_button_amount_ghs INTEGER")

        # --- Migration: bot_group_membership.group_id needs a UNIQUE
        # constraint — feature_handlers.handle_my_chat_member already used
        # "ON CONFLICT (group_id) DO UPDATE" without one, which Postgres
        # rejects at runtime with no matching unique/exclusion constraint.
        # This was a live bug before this migration existed. De-duplicate
        # first (keep the most recent row per group_id) since ON CONFLICT
        # having silently failed until now may have let duplicates accumulate.
        await conn.execute("""
            DELETE FROM bot_group_membership a USING bot_group_membership b
            WHERE a.group_id = b.group_id AND a.id < b.id
        """)
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'bot_group_membership_group_id_key'
                ) THEN
                    ALTER TABLE bot_group_membership ADD CONSTRAINT bot_group_membership_group_id_key UNIQUE (group_id);
                END IF;
            END $$;
        """)

        # --- Migration: chat metadata for the admin panel's remote
        # group/channel picker (handlers/admin_remote.py) — previously this
        # table only tracked group_id + bot_status with no title/type/
        # username, so there was no way to show admins a readable list of
        # which chats the bot is actually in.
        await conn.execute("ALTER TABLE bot_group_membership ADD COLUMN IF NOT EXISTS chat_title TEXT")
        await conn.execute("ALTER TABLE bot_group_membership ADD COLUMN IF NOT EXISTS chat_type TEXT")
        await conn.execute("ALTER TABLE bot_group_membership ADD COLUMN IF NOT EXISTS chat_username TEXT")

        # --- Migration: tenant isolation. The main bot and every clone share
        # this one Postgres database, and this table previously had no way to
        # tell which bot a chat membership belonged to — so a clone's admin
        # panel / broadcast list could show groups that only the main bot (or
        # another clone entirely) was actually a member of. clone_id = 0 means
        # "the main bot"; a real clone_id means that specific clone.
        await conn.execute("ALTER TABLE bot_group_membership ADD COLUMN IF NOT EXISTS clone_id BIGINT NOT NULL DEFAULT 0")
        await conn.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'bot_group_membership_group_id_key'
                ) THEN
                    ALTER TABLE bot_group_membership DROP CONSTRAINT bot_group_membership_group_id_key;
                END IF;
            END $$;
        """)
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'bot_group_membership_group_clone_key'
                ) THEN
                    ALTER TABLE bot_group_membership ADD CONSTRAINT bot_group_membership_group_clone_key UNIQUE (group_id, clone_id);
                END IF;
            END $$;
        """)

        # --- Migration: anti-flood tracking (Postgres-backed, not in-memory —
        # this bot is a stateless webhook, an in-process dict would reset
        # unpredictably between invocations) --------------------------------
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS flood_events (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                occurred_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_flood_events_lookup ON flood_events (chat_id, user_id, occurred_at)")

        # --- Migration: batch 1 of the auto-mod suite (feature #6) ---
        # message_text lets us detect "same message posted repeatedly", not
        # just "messages posted too fast" (those are different spam shapes).
        await conn.execute("ALTER TABLE flood_events ADD COLUMN IF NOT EXISTS message_text TEXT")

        # Auto Delete Links: now toggleable (was previously unconditional)
        # plus a per-group domain whitelist table below.
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_delete_links_enabled BOOLEAN DEFAULT TRUE")

        # Auto Ban on Spam: opt-in (default off, since banning is destructive
        # and this is a new behavior, unlike the pre-existing flood-mute).
        # Two independent triggers, both configurable:
        #   - spam_duplicate_threshold: same message text posted back-to-back
        #     this many times in a row
        #   - spam_flood_threshold / spam_flood_window_seconds: this many
        #     messages (any content) within this many seconds
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_ban_spam_enabled BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS spam_duplicate_threshold INTEGER DEFAULT 3")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS spam_flood_threshold INTEGER DEFAULT 10")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS spam_flood_window_seconds INTEGER DEFAULT 10")

        # --- Migration: batch 2 of the auto-mod suite (feature #6) ---
        # Auto Delete Service Messages: remove Telegram's own "X joined"/"X
        # left" notices. Opt-in since it changes visible chat behavior.
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_delete_service_messages BOOLEAN DEFAULT FALSE")

        # Auto Mute New Members: temporarily restrict posting for new joiners
        # to blunt join-and-spam bots. Skipped for a member if captcha is
        # already handling restriction for them (avoid double-gating).
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_mute_new_members_enabled BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_mute_new_members_minutes INTEGER DEFAULT 10")

        # --- Migration: batch 3 of the auto-mod suite (feature #6) ---
        # Auto Warn -> Auto Ban Escalation: opt-in. When on, a user who
        # accumulates warn_ban_threshold total warns in a chat is banned
        # instead of just muted (the existing WARN_LIMIT_BEFORE_MUTE mute
        # still applies below that threshold).
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_ban_on_warns_enabled BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS warn_ban_threshold INTEGER DEFAULT 5")

        # Auto Pin Announcements: opt-in. When on, any admin message that
        # starts with auto_pin_tag (default "#pin") gets pinned automatically
        # — no need to reply-and-/pin.
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_pin_announcements_enabled BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_pin_tag TEXT DEFAULT '#pin'")

        # Auto-DM on Join Request: opt-in. Requires the group's invite link to
        # be a "request to join" link (creates_join_request=True) — either
        # generated by /setjoinlink or created manually in Telegram's UI.
        # When on, the bot DMs the requester the main menu the instant their
        # join request comes in (this is allowed even for users who've never
        # messaged the bot before, since the join request itself counts as
        # the user initiating contact), then approves the request either way.
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_dm_on_join_enabled BOOLEAN DEFAULT FALSE")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS link_whitelist_domains (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                domain TEXT NOT NULL,
                added_by BIGINT NOT NULL REFERENCES users(user_id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, domain)
            )
        """)

        # --- Migration: dedicated analytics table (was incorrectly piggy-
        # backing on superbot_user_points, which is the points/leaderboard
        # table — writing arbitrary "action" strings there would have
        # corrupted point totals) -----------------------------------------
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_analytics (
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
                ai_uses INTEGER DEFAULT 0,
                downloads INTEGER DEFAULT 0,
                purchases INTEGER DEFAULT 0,
                referrals INTEGER DEFAULT 0,
                total_spent NUMERIC DEFAULT 0,
                total_earned NUMERIC DEFAULT 0,
                last_action DATE
            )
        """)

        # Broadcast Jobs (one-off admin broadcasts, processed in batches by
        # a cron-triggered endpoint since this deployment has no long-running
        # process to loop over recipients in a single request)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_jobs (
                id SERIAL PRIMARY KEY,
                admin_id BIGINT NOT NULL REFERENCES users(user_id),
                content TEXT,
                media_file_id TEXT,
                media_type TEXT,
                target_scope TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                total_recipients INTEGER DEFAULT 0,
                sent_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)

        # --- Migration: per-broadcast "join our group/channel" link -------------
        # Admin-assigned per broadcast (not a fixed bot-wide setting) — set via
        # the /broadcast flow, see handlers/broadcast_handler.py. Rendered as a
        # URL button alongside the fixed-price Premium Group paywall button in
        # api/cron_broadcast.py's _send_broadcast.
        await conn.execute("""
            ALTER TABLE broadcast_jobs ADD COLUMN IF NOT EXISTS join_link TEXT
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_recipients (
                id SERIAL PRIMARY KEY,
                job_id INTEGER NOT NULL REFERENCES broadcast_jobs(id),
                chat_id BIGINT NOT NULL,
                kind TEXT NOT NULL,
                sent BOOLEAN DEFAULT FALSE,
                sent_at TIMESTAMP,
                error TEXT
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_job_pending
            ON broadcast_recipients (job_id, sent)
        """)

    # ─────────────────────────────────────────────────────────────────
    # Autopost (recurring posts)
    # ─────────────────────────────────────────────────────────────────

    async def create_recurring_post(self, chat_id: int, admin_id: int, interval_minutes: int,
                                     content: Optional[str] = None, media_file_id: Optional[str] = None,
                                     media_type: Optional[str] = None) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO recurring_posts (chat_id, admin_id, content, interval_minutes, media_file_id, media_type, is_active)
                VALUES ($1, $2, $3, $4, $5, $6, TRUE)
                RETURNING id
            """, chat_id, admin_id, content, interval_minutes, media_file_id, media_type)
            return row["id"]

    async def get_due_recurring_posts(self, limit: int = 20) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM recurring_posts
                WHERE is_active = TRUE
                AND (
                    last_posted IS NULL
                    OR NOW() - last_posted >= (COALESCE(interval_minutes, interval_hours * 60) || ' minutes')::interval
                )
                ORDER BY last_posted NULLS FIRST
                LIMIT $1
            """, limit)
            return [dict(r) for r in rows]

    async def mark_recurring_posted(self, post_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE recurring_posts SET last_posted = NOW() WHERE id = $1", post_id)

    async def bump_recurring_failure(self, post_id: int, max_failures: int = 5) -> int:
        """Increment failure_count; auto-deactivate after max_failures (e.g. bot was removed/kicked)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE recurring_posts SET failure_count = failure_count + 1
                WHERE id = $1
                RETURNING failure_count
            """, post_id)
            new_count = row["failure_count"] if row else 0
            if new_count >= max_failures:
                await conn.execute("UPDATE recurring_posts SET is_active = FALSE WHERE id = $1", post_id)
            return new_count

    async def add_sponsored_post(self, admin_id: int, content: str, button_label: str,
                                  button_url: str, runs_total: int) -> Optional[int]:
        """Queue a sponsored post to be injected into the autopost cron cycle."""
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO sponsored_posts (admin_id, content, button_label, button_url, runs_remaining, runs_total)
                    VALUES ($1, $2, $3, $4, $5, $5)
                    RETURNING id
                """, admin_id, content, button_label, button_url, runs_total)
            return row["id"] if row else None
        except Exception as e:
            print(f"[v0] Error adding sponsored post: {e}")
            return None

    async def get_next_sponsored(self) -> Optional[Dict]:
        """The oldest active sponsored post that still has runs remaining, or None."""
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT * FROM sponsored_posts
                    WHERE is_active = TRUE AND runs_remaining > 0
                    ORDER BY created_at ASC LIMIT 1
                """)
            return dict(row) if row else None
        except Exception as e:
            print(f"[v0] Error fetching next sponsored post: {e}")
            return None

    async def mark_sponsored_sent(self, sponsored_id: int):
        """Decrement runs_remaining once per cron cycle (not once per chat sent to).
        Auto-deactivates once it hits zero."""
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE sponsored_posts SET runs_remaining = runs_remaining - 1
                    WHERE id = $1
                """, sponsored_id)
                await conn.execute("""
                    UPDATE sponsored_posts SET is_active = FALSE
                    WHERE id = $1 AND runs_remaining <= 0
                """, sponsored_id)
        except Exception as e:
            print(f"[v0] Error marking sponsored post sent: {e}")

    async def get_autopost_chat_ids(self) -> List[int]:
        """Every chat currently known to have the bot as a member — reuses the
        same authoritative table get_known_group_ids()/broadcast use, NOT
        chat_memberships (a much smaller table only touched by manual
        /registerme + welcome-message settings — using it here would have
        silently limited sponsored posts to only manually-registered chats)."""
        return await self.get_known_group_ids()

    async def deactivate_recurring_post(self, post_id: int, requester_id: int, is_owner: bool = False) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if is_owner:
                result = await conn.execute("UPDATE recurring_posts SET is_active = FALSE WHERE id = $1", post_id)
            else:
                result = await conn.execute(
                    "UPDATE recurring_posts SET is_active = FALSE WHERE id = $1 AND admin_id = $2",
                    post_id, requester_id
                )
            return bool(result) and result.endswith("1")

    async def list_recurring_for_chat(self, chat_id: int) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM recurring_posts WHERE chat_id = $1 AND is_active = TRUE ORDER BY id", chat_id
            )
            return [dict(r) for r in rows]

    async def list_recurring_for_admin(self, admin_id: int) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM recurring_posts WHERE admin_id = $1 AND is_active = TRUE ORDER BY id", admin_id
            )
            return [dict(r) for r in rows]

    async def get_all_active_recurring(self) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM recurring_posts WHERE is_active = TRUE ORDER BY id")
            return [dict(r) for r in rows]

    # ─────────────────────────────────────────────────────────────────
    # Broadcast
    # ─────────────────────────────────────────────────────────────────

    async def get_known_group_ids(self, clone_id: int = 0) -> List[int]:
        """clone_id=0 is the main bot; each clone only ever sees its own
        chats, never the main bot's or another clone's."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT group_id FROM bot_group_membership WHERE bot_status IN ('member', 'administrator') "
                "AND clone_id = $1",
                clone_id
            )
            return [r["group_id"] for r in rows]

    async def get_known_groups_with_titles(self, clone_id: int = 0) -> List[Dict]:
        """Same set as get_known_group_ids, but with chat_title so the
        broadcast exempt-groups picker can show names instead of raw IDs.
        Scoped to clone_id (0 = main bot) so clones can't see each other's
        or the main bot's groups."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT group_id, chat_title FROM bot_group_membership "
                "WHERE bot_status IN ('member', 'administrator') AND clone_id = $1 "
                "ORDER BY chat_title NULLS LAST",
                clone_id
            )
            return [{"group_id": r["group_id"], "chat_title": r["chat_title"] or f"Group {r['group_id']}"} for r in rows]

    async def get_all_user_ids(self) -> List[int]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM users")
            return [r["user_id"] for r in rows]

    async def create_broadcast_job(self, admin_id: int, target_scope: str, content: Optional[str] = None,
                                    media_file_id: Optional[str] = None, media_type: Optional[str] = None,
                                    excluded_group_ids: Optional[List[int]] = None,
                                    join_link: Optional[str] = None, clone_id: int = 0) -> Dict:
        """Creates the job and pre-populates its recipient list. Returns the job dict.
        excluded_group_ids: group_ids to leave out even though target_scope includes groups —
        the tap-to-exempt picker in the broadcast flow, no group ID typing needed.
        join_link: optional admin-provided invite link for this specific broadcast
        (see handlers/broadcast_handler.py) — rendered as a "Join" button alongside
        the fixed Premium Group paywall button when the job is sent.
        clone_id: 0 for the main bot, else the clone's own id — a clone's broadcast
        must only ever reach groups that clone itself is a member of."""
        excluded = set(excluded_group_ids or [])
        recipients: List[tuple] = []
        if target_scope in ("users", "both"):
            for uid in await self.get_all_user_ids():
                recipients.append((uid, "user"))
        if target_scope in ("groups", "both"):
            for gid in await self.get_known_group_ids(clone_id=clone_id):
                if gid not in excluded:
                    recipients.append((gid, "group"))

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO broadcast_jobs (admin_id, content, media_file_id, media_type, target_scope, total_recipients, status, join_link)
                VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7)
                RETURNING *
            """, admin_id, content, media_file_id, media_type, target_scope, len(recipients), join_link)
            job = dict(row)
            if recipients:
                await conn.executemany(
                    "INSERT INTO broadcast_recipients (job_id, chat_id, kind) VALUES ($1, $2, $3)",
                    [(job["id"], chat_id, kind) for chat_id, kind in recipients]
                )
            return job

    async def preview_broadcast_counts(self, target_scope: str, excluded_group_ids: Optional[List[int]] = None,
                                        clone_id: int = 0) -> Dict[str, int]:
        excluded = set(excluded_group_ids or [])
        users = len(await self.get_all_user_ids()) if target_scope in ("users", "both") else 0
        groups = 0
        if target_scope in ("groups", "both"):
            groups = len([gid for gid in await self.get_known_group_ids(clone_id=clone_id) if gid not in excluded])
        return {"users": users, "groups": groups, "total": users + groups}

    async def get_next_broadcast_job(self) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM broadcast_jobs WHERE status IN ('pending', 'in_progress') ORDER BY id LIMIT 1"
            )
            return dict(row) if row else None

    async def get_broadcast_batch(self, job_id: int, batch_size: int = 20) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM broadcast_recipients WHERE job_id = $1 AND sent = FALSE ORDER BY id LIMIT $2",
                job_id, batch_size
            )
            return [dict(r) for r in rows]

    async def mark_broadcast_recipient(self, recipient_id: int, error: Optional[str] = None):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE broadcast_recipients SET sent = TRUE, sent_at = NOW(), error = $2 WHERE id = $1",
                recipient_id, error
            )

    async def finalize_broadcast_job_progress(self, job_id: int) -> str:
        """Recompute sent/failed counts from broadcast_recipients and close out the job if fully processed."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            counts = await conn.fetchrow("""
                SELECT
                    COUNT(*) FILTER (WHERE sent AND error IS NULL) AS sent_count,
                    COUNT(*) FILTER (WHERE sent AND error IS NOT NULL) AS failed_count,
                    COUNT(*) FILTER (WHERE NOT sent) AS remaining
                FROM broadcast_recipients WHERE job_id = $1
            """, job_id)
            status = "done" if counts["remaining"] == 0 else "in_progress"
            await conn.execute("""
                UPDATE broadcast_jobs
                SET sent_count = $2, failed_count = $3, status = $4,
                    completed_at = CASE WHEN $4 = 'done' THEN NOW() ELSE completed_at END
                WHERE id = $1
            """, job_id, counts["sent_count"], counts["failed_count"], status)
            return status

    async def get_broadcast_job(self, job_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM broadcast_jobs WHERE id = $1", job_id)
            return dict(row) if row else None

    async def add_user(self, user_id: int, username: str, first_name: str, is_admin: bool = False):
        """Add or update a user"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, username, first_name, is_admin)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id) DO UPDATE
                SET username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    is_admin = EXCLUDED.is_admin
            """, user_id, username, first_name, is_admin)

    async def _ensure_clone_status_row(self, conn, user_id: int, clone_id: int) -> None:
        """Make sure a user_clone_status row exists for this (user_id, clone_id)
        before an UPDATE — a brand-new user on a brand-new clone won't have one
        yet, and UPDATE against a missing row silently does nothing."""
        await conn.execute("""
            INSERT INTO user_clone_status (user_id, clone_id)
            VALUES ($1, $2)
            ON CONFLICT (user_id, clone_id) DO NOTHING
        """, user_id, clone_id)

    async def set_tos_accepted(self, user_id: int, clone_id: int = 0) -> bool:
        """Mark a user as having accepted the BotStore listing terms — scoped
        to clone_id (0 = main bot) since ToS acceptance, like tier and
        subscriptions, must not carry across bots."""
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await self._ensure_clone_status_row(conn, user_id, clone_id)
                await conn.execute(
                    "UPDATE user_clone_status SET tos_accepted = TRUE WHERE user_id = $1 AND clone_id = $2",
                    user_id, clone_id
                )
            return True
        except Exception as e:
            print(f"[v0] Error setting tos_accepted: {e}")
            return False

    async def set_premium_tier(self, user_id: int, clone_id: int = 0) -> bool:
        """Mark a user's tier as premium (BotStore unlimited listings) —
        scoped to clone_id (0 = main bot). A premium purchase on one bot
        must never grant premium on another bot."""
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await self._ensure_clone_status_row(conn, user_id, clone_id)
                await conn.execute(
                    "UPDATE user_clone_status SET tier = $1 WHERE user_id = $2 AND clone_id = $3",
                    "premium", user_id, clone_id
                )
            return True
        except Exception as e:
            print(f"[v0] Error setting premium tier: {e}")
            return False

    async def get_user(self, user_id: int, clone_id: int = 0) -> Optional[Dict]:
        """Get user info: identity fields (username/first_name/joined_date/
        is_admin/submissions_count) come from the global `users` row, but
        tier/subscription/quota fields come from user_clone_status scoped to
        clone_id (0 = main bot) — those must never leak across bots."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
            if not row:
                return None
            status = await conn.fetchrow(
                "SELECT * FROM user_clone_status WHERE user_id = $1 AND clone_id = $2",
                user_id, clone_id
            )
            return {
                "user_id": row["user_id"],
                "username": row["username"],
                "first_name": row["first_name"],
                "joined_date": row["joined_date"],
                "tier": status["tier"] if status else "free",
                "submissions_count": row["submissions_count"],
                "is_admin": bool(row["is_admin"]),
                "subscription_status": status["subscription_status"] if status else "inactive",
                "subscription_expiry": status["subscription_expiry"] if status else None,
                "tos_accepted": bool(status["tos_accepted"]) if status else False,
                "free_ai_chat_uses": status["free_ai_chat_uses"] if status else 0,
                "free_download_uses": status["free_download_uses"] if status else 0,
                "utility_sub_status": status["utility_sub_status"] if status else "inactive",
                "utility_sub_expiry": status["utility_sub_expiry"] if status else None,
                "free_image_search_used": bool(status["free_image_search_used"]) if status else False,
                "language": (status["language"] if status and status["language"] else "en"),
            }

    async def get_user_language(self, user_id: int, clone_id: int = 0) -> str:
        """Get the user's saved language code for this bot, defaulting to 'en'
        if unset/missing. Language is per (user, clone) — a user may want a
        different language on a different clone."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT language FROM user_clone_status WHERE user_id = $1 AND clone_id = $2",
                user_id, clone_id
            )
            if row and row["language"]:
                return row["language"]
            return "en"

    async def set_user_language(self, user_id: int, lang: str, clone_id: int = 0) -> bool:
        """Persist the user's chosen language (2-letter code, see i18n.SUPPORTED_LANGUAGES),
        scoped to clone_id (0 = main bot)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await self._ensure_clone_status_row(conn, user_id, clone_id)
            await conn.execute(
                "UPDATE user_clone_status SET language = $1 WHERE user_id = $2 AND clone_id = $3",
                lang, user_id, clone_id
            )
            return True

    async def mark_free_image_search_used(self, user_id: int, clone_id: int = 0) -> bool:
        """Marks a user's one free reverse-image-search reveal as used on this
        bot (0 = main bot) — free-use allowances are per-clone, not shared."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await self._ensure_clone_status_row(conn, user_id, clone_id)
            await conn.execute(
                "UPDATE user_clone_status SET free_image_search_used = TRUE WHERE user_id = $1 AND clone_id = $2",
                user_id, clone_id
            )
            return True

    async def get_utility_usage(self, user_id: int, clone_id: int = 0) -> Dict:
        """Free-use counters + subscription status for the AI Chat / Download
        paywall (handlers/utility_paywall.py), scoped to clone_id (0 = main
        bot). Ensures the user's clone-status row exists so brand-new users
        read as 0 uses / no subscription instead of erroring out."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT free_ai_chat_uses, free_download_uses, utility_sub_status, utility_sub_expiry "
                "FROM user_clone_status WHERE user_id = $1 AND clone_id = $2",
                user_id, clone_id
            )
            if not row:
                return {
                    "free_ai_chat_uses": 0,
                    "free_download_uses": 0,
                    "utility_sub_status": "inactive",
                    "utility_sub_expiry": None,
                }
            return dict(row)

    async def increment_free_ai_chat_uses(self, user_id: int, clone_id: int = 0) -> int:
        """Consume one free AI Chat use on this bot (0 = main bot). Returns
        the new count."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await self._ensure_clone_status_row(conn, user_id, clone_id)
            row = await conn.fetchrow(
                "UPDATE user_clone_status SET free_ai_chat_uses = COALESCE(free_ai_chat_uses, 0) + 1 "
                "WHERE user_id = $1 AND clone_id = $2 RETURNING free_ai_chat_uses",
                user_id, clone_id
            )
            return row["free_ai_chat_uses"] if row else 0

    async def increment_free_download_uses(self, user_id: int, clone_id: int = 0) -> int:
        """Consume one free Download use on this bot (0 = main bot). Returns
        the new count."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await self._ensure_clone_status_row(conn, user_id, clone_id)
            row = await conn.fetchrow(
                "UPDATE user_clone_status SET free_download_uses = COALESCE(free_download_uses, 0) + 1 "
                "WHERE user_id = $1 AND clone_id = $2 RETURNING free_download_uses",
                user_id, clone_id
            )
            return row["free_download_uses"] if row else 0

    async def activate_utility_subscription(self, user_id: int, days: int = 60, clone_id: int = 0) -> bool:
        """Activate the AI Chat + Download subscription for `days` (default
        60, i.e. ~2 months) from now, scoped to clone_id (0 = main bot) — a
        subscription bought on one bot must not unlock another bot."""
        try:
            pool = await get_pool()
            expiry = datetime.now() + timedelta(days=days)
            async with pool.acquire() as conn:
                await self._ensure_clone_status_row(conn, user_id, clone_id)
                await conn.execute(
                    "UPDATE user_clone_status SET utility_sub_status = 'active', utility_sub_expiry = $1 "
                    "WHERE user_id = $2 AND clone_id = $3",
                    expiry, user_id, clone_id
                )
            return True
        except Exception as e:
            print(f"[v0] Error activating utility subscription: {e}")
            return False

    async def add_link_button(self, chat_id: int, label: str, url: str, created_by: int) -> bool:
        """Upsert a labeled-link button (feature #8) — same label in the
        same chat just updates the URL rather than erroring."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            try:
                existing = await conn.fetchval(
                    "SELECT COALESCE(MAX(position), -1) FROM custom_link_buttons WHERE chat_id = $1", chat_id
                )
                await conn.execute(
                    """
                    INSERT INTO custom_link_buttons (chat_id, label, url, position, created_by)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (chat_id, label) DO UPDATE SET url = EXCLUDED.url
                    """,
                    chat_id, label, url, existing + 1, created_by,
                )
                return True
            except Exception as e:
                print(f"[v0] Error adding link button: {e}")
                return False

    async def remove_link_button(self, chat_id: int, label: str) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM custom_link_buttons WHERE chat_id = $1 AND label = $2",
                chat_id, label,
            )
            return result is not None and "DELETE 0" not in result

    async def list_link_buttons(self, chat_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT label, url FROM custom_link_buttons WHERE chat_id = $1 ORDER BY position",
                chat_id,
            )
            return [dict(r) for r in rows]

    async def claim_paystack_transaction(self, transaction_id: str, user_id: int, transaction: dict, entitlement: str) -> str:
        """Atomically record a verified Paystack transaction before fulfillment."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO payment_logs (user_id, amount, status, paystack_reference, payment_method)
                   VALUES ($1, $2, 'completed', $3, 'paystack')
                   ON CONFLICT (paystack_reference) DO NOTHING
                   RETURNING payment_id""",
                user_id, float(transaction.get("amount") or 0) / 100, transaction_id,
            )
            return "processed" if row else "duplicate"

    async def mark_image_search_paid(self, user_id: int, clone_id: int = 0):
        await self.execute(
            "UPDATE user_clone_status SET free_image_search_used = TRUE WHERE user_id = $1 AND clone_id = $2",
            user_id, clone_id,
        )

    async def log_payment(self, user_id: int, amount: float, reference: str, status: str = "pending"):
        """Log a payment to the generic payment_logs table (reused here for
        utility-subscription payments rather than a new table)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO payment_logs (user_id, amount, status, paystack_reference, payment_method) "
                "VALUES ($1, $2, $3, $4, 'paystack') ON CONFLICT (paystack_reference) DO NOTHING",
                user_id, amount, status, reference
            )

    async def mark_payment_paid(self, reference: str):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE payment_logs SET status = 'completed' WHERE paystack_reference = $1",
                reference
            )

    async def add_submission(self, user_id: int, anime_name: str, episodes: int, genres: str, synopsis: str, image_url: str):
        """Add a new submission"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO submissions (user_id, anime_name, episodes, genres, synopsis, image_url, status)
                VALUES ($1, $2, $3, $4, $5, $6, 'pending')
                RETURNING submission_id
            """, user_id, anime_name, episodes, genres, synopsis, image_url)
            return row["submission_id"]

    async def get_pending_submissions(self) -> List[Dict]:
        """Get all pending submissions"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM submissions WHERE status = 'pending' ORDER BY created_date DESC
            """)
            return [{
                "submission_id": row["submission_id"],
                "user_id": row["user_id"],
                "anime_name": row["anime_name"],
                "episodes": row["episodes"],
                "genres": row["genres"],
                "synopsis": row["synopsis"],
                "image_url": row["image_url"],
                "status": row["status"],
                "created_date": row["created_date"]
            } for row in rows]

    async def approve_submission(self, submission_id: int):
        """Approve a submission"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE submissions SET status = 'approved', approved_date = CURRENT_TIMESTAMP
                WHERE submission_id = $1
            """, submission_id)

    async def reject_submission(self, submission_id: int, reason: str):
        """Reject a submission"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE submissions SET status = 'rejected', rejection_reason = $1
                WHERE submission_id = $2
            """, reason, submission_id)

    async def increment_clone_referral(self, clone_id: int) -> None:
        """
        Bump referral_count for a clone whose 'Get your own bot' button
        (keyboards.py main_menu) sent someone to /start=fromclone_<clone_id>.
        No-ops quietly (just logs) if clone_id doesn't exist — a stale or
        tampered deep link shouldn't ever raise into the /start handler.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE cloned_bots SET referral_count = referral_count + 1
                WHERE clone_id = $1
            """, clone_id)
            if result == "UPDATE 0":
                logger.warning(f"[v0] Referral deep link pointed at unknown clone_id={clone_id}")

    async def add_cloned_bot(self, owner_id: int, bot_name: str, bot_token: str, webhook_url: str,
                              custom_data: Dict, payment_id: str = None, payment_status: str = "verified",
                              bot_username: str = None, webhook_secret: str = None):
        """
        Add a cloned bot with encrypted token AND encrypted webhook secret (Part 3.2 Step C).

        bot_token here must be the REAL, validated (getMe-confirmed) BotFather token.
        webhook_secret is the unique per-clone secret used for setWebhook's secret_token,
        stored encrypted so a DB leak doesn't hand out live webhook secrets either.
        """
        encrypted_token = secret_manager.encrypt(bot_token)
        encrypted_secret = secret_manager.encrypt(webhook_secret) if webhook_secret else None

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO cloned_bots
                    (owner_id, bot_name, bot_token, webhook_url, custom_data,
                     payment_id, payment_status, bot_username, webhook_secret, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'active')
                RETURNING clone_id
            """, owner_id, bot_name, encrypted_token, webhook_url, json.dumps(custom_data),
                 payment_id, payment_status, bot_username, encrypted_secret)
            return row["clone_id"]

    async def get_user_clones(self, user_id: int) -> List[Dict]:
        """Get all active cloned bots for a user (with decrypted tokens - Issue 1.2)"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM cloned_bots WHERE owner_id = $1 AND status = 'active'
            """, user_id)
            result = []
            for row in rows:
                decrypted_token = secret_manager.decrypt(row["bot_token"])
                if decrypted_token is None:
                    logger.error(
                        f"[v0] Failed to decrypt bot_token for clone_id={row.get('clone_id')}, "
                        f"owner_id={user_id}. Excluding from results rather than returning ciphertext."
                    )
                    continue  # skip this clone rather than return an unusable/garbage token
                result.append({
                    "clone_id": row["clone_id"],
                    "owner_id": row["owner_id"],
                    "bot_name": row["bot_name"],
                    "bot_token": decrypted_token,
                    "bot_username": row["bot_username"],
                    "webhook_url": row["webhook_url"],
                    "custom_data": json.loads(row["custom_data"]) if row["custom_data"] else {},
                    "created_date": row["created_date"]
                })
            return result

    async def get_clone_for_routing(self, clone_id: int) -> Optional[Dict]:
        """
        Look up a single clone by clone_id for the api/bot.py webhook router
        (Part 3.2 Step D). Returns decrypted bot_token + webhook_secret so the
        caller can build an Application and verify the per-clone secret header.

        Returns None if the clone doesn't exist, isn't active, or either secret
        fails to decrypt (fail closed — never hand back ciphertext as if it
        were usable, same principle as the get_user_clones fix in round 2/3).
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM cloned_bots WHERE clone_id = $1 AND status = 'active'
            """, clone_id)
            if row is None:
                return None

            decrypted_token = secret_manager.decrypt(row["bot_token"])
            if decrypted_token is None:
                logger.error(f"[v0] Failed to decrypt bot_token for clone_id={clone_id}; refusing to route.")
                return None

            decrypted_secret = None
            if row["webhook_secret"]:
                decrypted_secret = secret_manager.decrypt(row["webhook_secret"])
                if decrypted_secret is None:
                    logger.error(f"[v0] Failed to decrypt webhook_secret for clone_id={clone_id}; refusing to route.")
                    return None

            return {
                "clone_id": row["clone_id"],
                "owner_id": row["owner_id"],
                "bot_name": row["bot_name"],
                "bot_token": decrypted_token,
                "bot_username": row["bot_username"],
                "webhook_secret": decrypted_secret,
                "custom_data": json.loads(row["custom_data"]) if row["custom_data"] else {},
                "status": row["status"],
            }

    async def list_active_clones(self) -> List[Dict]:
        """Admin tooling (Part 3.2 Step E): list all active clones, no secrets included."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT clone_id, owner_id, bot_name, bot_username, status, created_date
                FROM cloned_bots
                WHERE status = 'active'
                ORDER BY created_date DESC
            """)
            return [dict(row) for row in rows]

    async def upsert_bot_chat(self, chat_id: int, bot_status: str, chat_title: str = None,
                               chat_type: str = None, chat_username: str = None, clone_id: int = 0) -> None:
        """Record/refresh a chat the bot is (or was) a member of, with enough
        metadata (title/type/username) to show it in the admin panel's
        remote group/channel picker. Called from
        handlers/feature_handlers.handle_my_chat_member on every join/leave/
        promote/demote event. clone_id (0 = main bot) keeps every bot's
        membership rows separate, since the main bot and every clone share
        this same table/database — without it a clone would upsert into (and
        read back) rows that belong to the main bot or another clone."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bot_group_membership (group_id, clone_id, bot_status, chat_title, chat_type, chat_username)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (group_id, clone_id) DO UPDATE SET
                    bot_status = EXCLUDED.bot_status,
                    chat_title = COALESCE(EXCLUDED.chat_title, bot_group_membership.chat_title),
                    chat_type = COALESCE(EXCLUDED.chat_type, bot_group_membership.chat_type),
                    chat_username = EXCLUDED.chat_username,
                    status_changed_at = CURRENT_TIMESTAMP
            """, chat_id, clone_id, bot_status, chat_title, chat_type, chat_username)

    async def list_bot_chats(self, clone_id: int = 0) -> List[Dict]:
        """All groups/channels THIS bot (main bot when clone_id=0, otherwise
        that specific clone) currently believes itself to be a member of, for
        the admin panel's remote group/channel picker (handlers/admin_remote.py).
        Scoped by clone_id so a clone never sees the main bot's or another
        clone's chats. Ordered by title so the picker is actually browsable
        once there are more than a handful."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT group_id AS chat_id, chat_title, chat_type, chat_username
                FROM bot_group_membership
                WHERE bot_status IN ('member', 'administrator', 'creator') AND clone_id = $1
                ORDER BY COALESCE(chat_title, '') ASC, group_id ASC
            """, clone_id)
            return [dict(row) for row in rows]

    async def list_all_clones(self) -> List[Dict]:
        """Used by the data-audit step (Part 3.3): every clone row, active or not, no secrets."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT clone_id, owner_id, bot_name, bot_username, status, payment_id, payment_status, created_date
                FROM cloned_bots
                ORDER BY created_date DESC
            """)
            return [dict(row) for row in rows]

    async def update_clone_custom_data(self, clone_id: int, owner_id: int, updates: Dict) -> bool:
        """
        Merge `updates` into a clone's custom_data JSON (e.g. {"name": "New Name"}).
        Only succeeds if owner_id actually owns this active clone — callers don't
        need to re-check ownership themselves. Also keeps the bot_name column in
        sync when "name" is one of the updated fields, since some queries read
        bot_name directly rather than through custom_data.
        Returns True if a row was updated, False if not found / not owned.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT custom_data FROM cloned_bots
                WHERE clone_id = $1 AND owner_id = $2 AND status = 'active'
            """, clone_id, owner_id)
            if row is None:
                return False

            current = json.loads(row["custom_data"]) if row["custom_data"] else {}
            current.update(updates)

            if "name" in updates:
                await conn.execute("""
                    UPDATE cloned_bots SET custom_data = $1, bot_name = $2 WHERE clone_id = $3
                """, json.dumps(current), updates["name"], clone_id)
            else:
                await conn.execute("""
                    UPDATE cloned_bots SET custom_data = $1 WHERE clone_id = $2
                """, json.dumps(current), clone_id)
            return True

    async def set_clone_payment_provider(self, clone_id: int, owner_id: int, provider: str, api_key: Optional[str] = None) -> bool:
        """Set how a clone routes payments: 'main' (default — payments go
        through the main bot's own Selar account) or 'selar'/'stripe'
        with the owner's own key, encrypted at rest with the same cipher
        used for clone bot tokens. Passing provider='main' also wipes any
        previously stored key."""
        updates = {"payment_provider": provider}
        if provider == "main":
            updates["payment_key_encrypted"] = None
        elif api_key:
            updates["payment_key_encrypted"] = secret_manager.encrypt(api_key)
        return await self.update_clone_custom_data(clone_id, owner_id, updates)

    async def get_clone_payment_config(self, clone_id: int) -> Dict:
        """Returns {'provider': 'main'|'selar'|'stripe', 'api_key': str|None}
        (api_key decrypted, or None if not connected / on 'main'). Used at
        payment-initiation time to decide whose gateway credentials to use —
        clone owners' own keys are never sent anywhere except back to their
        own gateway call."""
        clone = await self.get_clone_for_routing(clone_id)
        cd = (clone or {}).get("custom_data") or {}
        provider = cd.get("payment_provider", "main")
        encrypted = cd.get("payment_key_encrypted")
        api_key = secret_manager.decrypt(encrypted) if (provider != "main" and encrypted) else None
        return {"provider": provider if api_key or provider == "main" else "main", "api_key": api_key}

    # ── Per-clone custom pricing ────────────────────────────────────────────
    # Clone owners can override the price of any feature listed in
    # config.PRICE_REGISTRY, but only while their monetization subscription
    # (see below) is active. Overrides live in custom_data->'pricing' (same
    # JSON-blob pattern as payment_provider), so no schema migration is
    # needed to add a new priceable feature — just add it to PRICE_REGISTRY.
    async def get_clone_prices(self, clone_id: int) -> Dict[str, float]:
        """Every PRICE_REGISTRY key resolved for this clone: the owner's own
        override where set, else the registry default. clone_id=0 (the main
        bot) always gets registry defaults — the main bot doesn't monetize
        itself the way a clone does."""
        from config import PRICE_REGISTRY
        prices = {k: v["default"] for k, v in PRICE_REGISTRY.items()}
        if not clone_id:
            return prices
        clone = await self.get_clone_for_routing(clone_id)
        cd = (clone or {}).get("custom_data") or {}
        overrides = cd.get("pricing") or {}
        for k, v in overrides.items():
            if k in prices:
                try:
                    prices[k] = float(v)
                except (TypeError, ValueError):
                    pass
        return prices

    async def get_clone_price(self, clone_id: int, key: str) -> float:
        """Single-key convenience wrapper around get_clone_prices(), used at
        the point of charging (e.g. handlers/subscription.py)."""
        from config import PRICE_REGISTRY
        prices = await self.get_clone_prices(clone_id)
        return prices.get(key, PRICE_REGISTRY.get(key, {}).get("default", 0))

    async def set_clone_price(self, clone_id: int, owner_id: int, key: str, amount: float) -> bool:
        """Persist one custom price override. Callers (handlers/clone_bot.py)
        must confirm is_monetization_active(clone_id) before calling this —
        it does not check the subscription itself, so it stays reusable for
        the expiry-sweep's revert-to-defaults path too."""
        from config import PRICE_REGISTRY
        if key not in PRICE_REGISTRY:
            return False
        clone = await self.get_clone_for_routing(clone_id)
        cd = (clone or {}).get("custom_data") or {}
        pricing = dict(cd.get("pricing") or {})
        pricing[key] = amount
        return await self.update_clone_custom_data(clone_id, owner_id, {"pricing": pricing})

    async def clear_clone_prices(self, clone_id: int, owner_id: int) -> bool:
        """Wipe all custom price overrides back to registry defaults — called
        when a clone's monetization subscription lapses, same principle as
        set_clone_payment_provider(..., 'main') reverting the gateway."""
        return await self.update_clone_custom_data(clone_id, owner_id, {"pricing": {}})

    # ── Clone monetization subscription ─────────────────────────────────────
    # Gates BOTH connecting a clone owner's own Selar/Stripe key
    # (set_clone_payment_provider above) AND setting custom prices
    # (set_clone_price above) behind one recurring fee
    # (config.CLONE_MONETIZATION_FEE_GHS / month).
    async def get_monetization_subscription(self, clone_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM clone_monetization_subscriptions WHERE clone_id = $1",
                clone_id
            )
            return dict(row) if row else None

    async def is_monetization_active(self, clone_id: int) -> bool:
        if not clone_id:
            return False
        sub = await self.get_monetization_subscription(clone_id)
        if not sub or sub.get("status") != "active" or not sub.get("expires_at"):
            return False
        return sub["expires_at"] > datetime.now()

    async def start_monetization_payment(self, clone_id: int, owner_id: int, reference: str) -> None:
        """Called right after initializing the Selar transaction for the
        activation fee, so a stale/duplicate webhook can be matched back to
        the right clone_id even if metadata is ever incomplete."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO clone_monetization_subscriptions (clone_id, owner_id, status, payment_reference)
                VALUES ($1, $2, 'pending', $3)
                ON CONFLICT (clone_id) DO UPDATE SET
                    owner_id = EXCLUDED.owner_id,
                    status = 'pending',
                    payment_reference = EXCLUDED.payment_reference
            """, clone_id, owner_id, reference)

    async def activate_monetization_subscription(self, clone_id: int, days: int = 30) -> None:
        """Called from the Selar webhook once charge.success confirms the
        activation-fee payment. Renewing before expiry simply extends from
        now (not from the old expiry) — matches the simpler "renew = 30 more
        days from today" model used elsewhere in this codebase."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE clone_monetization_subscriptions
                SET status = 'active', activated_at = NOW(),
                    expires_at = NOW() + ($2 || ' days')::INTERVAL
                WHERE clone_id = $1
            """, clone_id, str(int(days)))

    async def expire_monetization_subscriptions(self) -> List[int]:
        """Cron sweep (api/cron_expire_monetization.py): any clone whose
        activation fee lapsed gets auto-reverted — payment provider back to
        'main', custom prices wiped back to registry defaults. This is the
        safe default: payments keep flowing (through the main account,
        which still nets the owner their commission split via
        commission_tracking) instead of silently breaking if a key or price
        is left in a stale state. Returns the clone_ids that were reverted."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT clone_id, owner_id FROM clone_monetization_subscriptions
                WHERE status = 'active' AND expires_at < NOW()
            """)
            reverted = []
            for r in rows:
                await conn.execute(
                    "UPDATE clone_monetization_subscriptions SET status = 'expired' WHERE clone_id = $1",
                    r["clone_id"]
                )
                await self.set_clone_payment_provider(r["clone_id"], r["owner_id"], "main")
                await self.clear_clone_prices(r["clone_id"], r["owner_id"])
                reverted.append(r["clone_id"])
            return reverted

    # ── Yandex direct-search subscription (per-user, per-clone) ─────────────
    async def get_image_search_yandex_subscription(self, user_id: int, clone_id: int = 0) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM image_search_yandex_subscriptions WHERE user_id = $1 AND clone_id = $2",
                user_id, clone_id
            )
            return dict(row) if row else None

    async def is_image_search_yandex_active(self, user_id: int, clone_id: int = 0) -> bool:
        sub = await self.get_image_search_yandex_subscription(user_id, clone_id)
        if not sub or sub.get("status") != "active" or not sub.get("expires_at"):
            return False
        return sub["expires_at"] > datetime.now()

    async def start_image_search_yandex_payment(self, user_id: int, clone_id: int, reference: str) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO image_search_yandex_subscriptions (user_id, clone_id, status, payment_reference)
                VALUES ($1, $2, 'pending', $3)
                ON CONFLICT (user_id, clone_id) DO UPDATE SET
                    status = 'pending',
                    payment_reference = EXCLUDED.payment_reference
            """, user_id, clone_id, reference)

    async def activate_image_search_yandex_subscription(self, user_id: int, clone_id: int = 0, days: int = 30) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE image_search_yandex_subscriptions
                SET status = 'active', activated_at = NOW(),
                    expires_at = NOW() + ($3 || ' days')::INTERVAL
                WHERE user_id = $1 AND clone_id = $2
            """, user_id, clone_id, str(int(days)))

    async def save_image_search_yandex_authorization(self, user_id: int, clone_id: int, authorization_code: str) -> None:
        """Stores the reusable-card token from the first successful payment so
        auto_renew_image_search_yandex_subscriptions() can charge it again at
        the next billing cycle without the user re-entering card details."""
        if not authorization_code:
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE image_search_yandex_subscriptions
                SET authorization_code = $3
                WHERE user_id = $1 AND clone_id = $2
            """, user_id, clone_id, authorization_code)

    async def expire_image_search_yandex_subscriptions(self) -> None:
        """Cron sweep companion to expire_monetization_subscriptions — just
        flips lapsed rows to 'expired', nothing else to revert since this
        subscription doesn't grant anything besides the direct-link button."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE image_search_yandex_subscriptions
                SET status = 'expired'
                WHERE status = 'active' AND expires_at < NOW()
            """)

    async def get_image_search_yandex_due_for_renewal(self) -> List[Dict]:
        """Active subscriptions with a saved card that expire within the next
        24 hours — the renewal window for the daily cron sweep."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id, clone_id, authorization_code
                FROM image_search_yandex_subscriptions
                WHERE status = 'active'
                  AND authorization_code IS NOT NULL
                  AND expires_at < NOW() + INTERVAL '1 day'
            """)
            return [dict(r) for r in rows]
    async def mark_image_search_yandex_renewal_failed(self, user_id: int, clone_id: int) -> None:
        """Auto-charge failed (card declined/expired) — drop to 'expired' and
        clear the dead authorization_code so we stop retrying a card that
        won't work; the user has to subscribe again manually."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE image_search_yandex_subscriptions
                SET status = 'expired', authorization_code = NULL
                WHERE user_id = $1 AND clone_id = $2
            """, user_id, clone_id)

    async def cancel_image_search_yandex_autorenew(self, user_id: int, clone_id: int) -> None:
        """User-initiated cancel: stop future auto-charges but leave the
        subscription active until its current expires_at (standard 'cancel =
        no renewal, access continues until period end' behavior)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE image_search_yandex_subscriptions
                SET authorization_code = NULL
                WHERE user_id = $1 AND clone_id = $2
            """, user_id, clone_id)

    async def deactivate_clone(self, clone_id: int) -> Optional[Dict]:
        """
        Mark a clone inactive in the DB. Returns the pre-deactivation row (with
        decrypted token) so the caller can also call deleteWebhook on Telegram's
        side — this method only touches the database, it does not call Telegram.
        """
        clone = await self.get_clone_for_routing(clone_id)
        if clone is None:
            return None
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE cloned_bots SET status = 'inactive' WHERE clone_id = $1
            """, clone_id)
        return clone

    async def store_pending_clone_payment(self, user_id: int, payment_reference: str):
        """Store pending clone payment in clone_payments table."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO clone_payments (reference, user_id, status)
                VALUES ($1, $2, 'pending')
                ON CONFLICT (reference) DO NOTHING
            """, payment_reference, user_id)

    async def request_clone_payment_review(self, payment_reference: str) -> bool:
        """Move a pending payment to admin review exactly once."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE clone_payments
                SET status = 'awaiting_review'
                WHERE reference = $1 AND status = 'pending'
            """, payment_reference)
            return result == "UPDATE 1"

    async def get_clone_payment(self, payment_reference: str):
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT reference, user_id, status, created_at
                FROM clone_payments WHERE reference = $1
            """, payment_reference)
            return dict(row) if row else None

    async def get_latest_paid_clone_payment(self, user_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT reference, user_id, status, created_at
                FROM clone_payments
                WHERE user_id = $1 AND status = 'paid'
                ORDER BY created_at DESC LIMIT 1
            """, user_id)
            return dict(row) if row else None

    async def reject_clone_payment(self, payment_reference: str) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE clone_payments SET status = 'rejected'
                WHERE reference = $1 AND status IN ('awaiting_review', 'pending')
            """, payment_reference)
            return result == "UPDATE 1"

    async def claim_selar_sale(self, sale_id: str, user_id: int, sale: dict, entitlement: str) -> str:
        """Atomically claim a Selar sale; duplicate deliveries are harmless."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS selar_sales (
                    sale_id TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    entitlement TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            inserted = await conn.fetchval("""
                INSERT INTO selar_sales (sale_id, user_id, entitlement, payload)
                VALUES ($1, $2, $3, $4::jsonb)
                ON CONFLICT (sale_id) DO NOTHING
                RETURNING sale_id
            """, sale_id, user_id, entitlement, json.dumps(sale.get("raw", {}), default=str))
            return "claimed" if inserted else "duplicate"

    async def mark_clone_payment_paid(self, payment_reference: str) -> bool:
        """Approve a manually reviewed clone payment exactly once."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE clone_payments
                SET status = 'paid'
                WHERE reference = $1 AND status = 'awaiting_review'
            """, payment_reference)
            return result == "UPDATE 1"


    async def create_manual_payment_review(self, reference: str, user_id: int, payment_type: str, clone_id: int, context: dict) -> str:
        """Create one pending manual payment review, preserving server-side context.

        Returns "created" the first time (caller should notify the admin),
        "duplicate" if it's already awaiting review (caller should not
        re-notify the admin), or "already_decided" if it was already
        approved/rejected.

        Previously this always inserted with status='awaiting_review' and
        then checked `row["status"] == 'awaiting_review'` to decide between
        "duplicate" and "already_decided" — which was true on the very
        first insert too, so every genuine payment request came back as
        "duplicate" and the admin was NEVER notified. INSERT ... ON
        CONFLICT DO NOTHING lets us tell a fresh insert (row returned) apart
        from an existing row (nothing returned) instead.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO manual_payment_reviews (reference, user_id, payment_type, clone_id, context, status)
                VALUES ($1, $2, $3, $4, $5::jsonb, 'awaiting_review')
                ON CONFLICT (reference) DO NOTHING
                RETURNING status
            """, reference, user_id, payment_type, clone_id, json.dumps(context, default=str))
            if row:
                return "created"
            existing = await conn.fetchrow(
                "SELECT status FROM manual_payment_reviews WHERE reference = $1", reference
            )
            if not existing:
                return "duplicate"
            return "duplicate" if existing["status"] == "awaiting_review" else "already_decided"

    async def decide_manual_payment_review(self, reference: str, admin_id: int, approved: bool):
        """Atomically decide a review exactly once and return its stored context."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE manual_payment_reviews
                SET status = $2, decided_at = CURRENT_TIMESTAMP, decided_by = $3
                WHERE reference = $1 AND status = 'awaiting_review'
                RETURNING reference, user_id, payment_type, clone_id, context
            """, reference, "approved" if approved else "rejected", admin_id)
            return dict(row) if row else None

    async def create_pending_payment_intent(self, reference: str, user_id: int, payment_type: str,
                                             chat_id: Optional[int] = None, context: Optional[dict] = None):
        """Persist a payment reference (+ small context) right when 'Pay Now'
        succeeds, so 'I've Paid' can find it regardless of which serverless
        instance handles that later request. Idempotent - a retried Pay Now
        tap with the same reference is a no-op."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pending_payment_intents (reference, user_id, payment_type, chat_id, context)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (reference) DO NOTHING
            """, reference, user_id, payment_type, chat_id, json.dumps(context or {}, default=str))

    async def get_latest_pending_payment_intent(self, user_id: int, payment_type: str,
                                                 chat_id: Optional[int] = None) -> Optional[Dict]:
        """Look up the reference stashed at 'Pay Now' time for this user/type
        (and chat_id, for the per-group welcome_pay flow)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            if chat_id is None:
                row = await conn.fetchrow("""
                    SELECT reference, context FROM pending_payment_intents
                    WHERE user_id = $1 AND payment_type = $2
                    ORDER BY created_at DESC LIMIT 1
                """, user_id, payment_type)
            else:
                row = await conn.fetchrow("""
                    SELECT reference, context FROM pending_payment_intents
                    WHERE user_id = $1 AND payment_type = $2 AND chat_id = $3
                    ORDER BY created_at DESC LIMIT 1
                """, user_id, payment_type, chat_id)
            return dict(row) if row else None

    async def delete_pending_payment_intent(self, reference: str):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM pending_payment_intents WHERE reference = $1", reference)

    # Wizard-flow TTL: an in-progress flow older than this is considered
    # abandoned and is silently dropped on next read rather than mis-served.
    USER_FLOW_STATE_TTL = timedelta(hours=6)

    async def save_user_flow_state(self, user_id: int, clone_id: int, flow: str, data: dict):
        """Upsert the durable snapshot for a user's in-progress wizard.
        `data` is the full dict of whatever context.user_data keys that
        flow needs (mode/step string included), so it can be replayed
        back into context.user_data verbatim on a cold instance."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_flow_state (user_id, clone_id, flow, data, updated_at)
                VALUES ($1, $2, $3, $4::jsonb, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id, clone_id)
                DO UPDATE SET flow = $3, data = $4::jsonb, updated_at = CURRENT_TIMESTAMP
            """, user_id, clone_id, flow, json.dumps(data, default=str))

    async def get_user_flow_state(self, user_id: int, clone_id: int) -> Optional[Dict]:
        """Fetch the durable wizard snapshot, if any and not expired.
        Expired rows are deleted on read (lazy cleanup - no cron needed)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT flow, data, updated_at FROM user_flow_state WHERE user_id = $1 AND clone_id = $2",
                user_id, clone_id
            )
            if not row:
                return None
            if datetime.utcnow() - row["updated_at"] > self.USER_FLOW_STATE_TTL:
                await conn.execute(
                    "DELETE FROM user_flow_state WHERE user_id = $1 AND clone_id = $2",
                    user_id, clone_id
                )
                return None
            return {"flow": row["flow"], "data": json.loads(row["data"]) if row["data"] else {}}

    async def clear_user_flow_state(self, user_id: int, clone_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_flow_state WHERE user_id = $1 AND clone_id = $2",
                user_id, clone_id
            )

    async def cleanup_expired_flow_state(self):
        """Optional periodic sweep for abandoned wizards, mirroring this
        codebase's other cleanup jobs. Lazy per-read expiry above already
        prevents stale state from being served; this just reclaims rows
        for users who never come back."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            cutoff = datetime.utcnow() - self.USER_FLOW_STATE_TTL
            result = await conn.execute(
                "DELETE FROM user_flow_state WHERE updated_at < $1", cutoff
            )
            return result

    async def get_latest_pending_clone_payment(self, user_id: int):
        """Mirrors get_latest_paid_clone_payment but for a payment that's
        still pending/awaiting review - used as the durable fallback when
        context.user_data['payment_reference'] didn't survive a cold start."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT reference, user_id, status, created_at
                FROM clone_payments
                WHERE user_id = $1 AND status IN ('pending', 'awaiting_review')
                ORDER BY created_at DESC LIMIT 1
            """, user_id)
            return dict(row) if row else None

    async def save_image_search_session(self, user_id: int, clone_id: int, image_url: str, results: list):
        """Cache a reverse-image-search result set between the initial photo
        message and a later 'Reveal Source Links (Free)' tap, which can land
        on a different serverless instance."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO image_search_sessions (user_id, clone_id, image_url, results, created_at)
                VALUES ($1, $2, $3, $4::jsonb, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id, clone_id) DO UPDATE
                SET image_url = EXCLUDED.image_url, results = EXCLUDED.results, created_at = EXCLUDED.created_at
            """, user_id, clone_id, image_url, json.dumps(results, default=str))

    async def get_image_search_session(self, user_id: int, clone_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT image_url, results FROM image_search_sessions
                WHERE user_id = $1 AND clone_id = $2
            """, user_id, clone_id)
            return dict(row) if row else None

    async def delete_image_search_session(self, user_id: int, clone_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM image_search_sessions WHERE user_id = $1 AND clone_id = $2",
                user_id, clone_id
            )

    async def get_clone_payment_status(self, payment_reference: str) -> str:
        """Get clone payment status from database"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            status = await conn.fetchval("""
                SELECT status FROM clone_payments WHERE reference = $1
            """, payment_reference)
            return status or "not_found"

    async def add_anime(self, title: str, episodes: int, genres: str, rating: float, status: str, synopsis: str, image_url: str, anilist_id: Optional[int] = None, mal_id: Optional[int] = None):
        """Add anime entry"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO anime_entries (anilist_id, mal_id, title, episodes, genres, rating, status, synopsis, image_url, source_api)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'mixed')
                RETURNING anime_id
            """, anilist_id, mal_id, title, episodes, genres, rating, status, synopsis, image_url)
            return row["anime_id"]

    async def search_anime(self, query: str, limit: int = 5) -> List[Dict]:
        """Search anime by title"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM anime_entries WHERE title ILIKE $1 LIMIT $2
            """, f"%{query}%", limit)
            return [{
                "anime_id": row["anime_id"],
                "title": row["title"],
                "episodes": row["episodes"],
                "genres": row["genres"],
                "rating": row["rating"],
                "status": row["status"],
                "synopsis": row["synopsis"],
                "image_url": row["image_url"]
            } for row in rows]

    async def update_config(self, key: str, value: Any) -> bool:
        """Update admin configuration - persists to database"""
        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO admin_config (key, value, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (key) DO UPDATE SET
                        value = EXCLUDED.value,
                        updated_at = NOW()
                """, key, str(value))
            return True
        except Exception as e:
            print(f"[v0] Error updating config: {e}")
            return False

    async def get_config(self, key: str) -> Optional[str]:
        """Get admin configuration value from database"""
        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                value = await conn.fetchval(
                    "SELECT value FROM admin_config WHERE key = $1", key
                )
            return value
        except Exception as e:
            print(f"[v0] Error reading config: {e}")
            return None


# Global database instance
db = Database()
