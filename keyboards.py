from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from config import EMOJI_COLORS, LOADING_ANIMATION, MAIN_BOT_USERNAME

class KeyboardGenerator:
    """Generate organized keyboard layouts with colored buttons"""

    @staticmethod
    def persistent_menu(clone_mode: bool = False) -> ReplyKeyboardMarkup:
        """Persistent bottom keyboard - always visible. Kept short and balanced
        across the bot's three areas (anime / group tools / utilities) instead
        of anime-only, plus a one-tap shortcut to the full command list."""
        if clone_mode:
            keyboard = [
                [KeyboardButton(f"{EMOJI_COLORS['search']} Search"), KeyboardButton("🧰 Tools")],
                [KeyboardButton("🛡️ Group Tools"), KeyboardButton("💎 Premium")],
                [KeyboardButton("☰ All Commands"), KeyboardButton("🏠 Menu")],
            ]
        else:
            keyboard = [
                [KeyboardButton(f"{EMOJI_COLORS['search']} Search"), KeyboardButton("🧰 Tools")],
                [KeyboardButton("🛡️ Group Tools"), KeyboardButton("💎 Premium")],
                [KeyboardButton("☰ All Commands"), KeyboardButton("🏠 Menu")],
            ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

    @staticmethod
    def main_menu(clone_mode: bool = False, clone_id: int = None) -> InlineKeyboardMarkup:
        """
        Main menu, grouped by area instead of one long flat list (previously
        13 buttons, almost all anime — didn't reflect that the bot is really
        ~1/3 anime, ~1/3 group management, ~1/3 utilities). Each button below
        opens a short submenu for that area. clone_mode hides "Clone Bot".
        """
        keyboard = [
            [
                InlineKeyboardButton("🤖 AI Chat", callback_data="tools_ai_info"),
                InlineKeyboardButton("🛡️ Group Tools", callback_data="m_grouptools")
            ],
            [
                InlineKeyboardButton("🧰 Tools", callback_data="m_tools")
            ],
        ]

        if clone_mode:
            keyboard.append([
                InlineKeyboardButton("⭐ Premium", callback_data="show_premium_tiers"),
                InlineKeyboardButton("🛠️ Admin Panel", callback_data="admin_panel")
            ])
            if clone_id is not None:
                keyboard.append([
                    InlineKeyboardButton("💰 Monetization", callback_data=f"clone_monetization_{clone_id}")
                ])
            keyboard.append([
                InlineKeyboardButton("ℹ️ About / How This Bot Works", callback_data="clone_about")
            ])
            if MAIN_BOT_USERNAME:
                keyboard.append([
                    InlineKeyboardButton("🔗 Main Bot", url=f"https://t.me/{MAIN_BOT_USERNAME}")
                ])
            if MAIN_BOT_USERNAME:
                # Growth loop: lets a clone's users jump to the main bot and
                # start their own clone. Also a visible trace of the main bot,
                # since clones otherwise show no sign of who powers them.
                start_payload = f"fromclone_{clone_id}" if clone_id is not None else "fromclone"
                keyboard.append([
                    InlineKeyboardButton(
                        "🤖 Get your own bot like this",
                        url=f"https://t.me/{MAIN_BOT_USERNAME}?start={start_payload}"
                    )
                ])
        else:
            keyboard.append([
                InlineKeyboardButton(f"{EMOJI_COLORS['clone']} Clone Bot", callback_data="clone_bot"),
                InlineKeyboardButton("⭐ Premium", callback_data="show_premium_tiers")
            ])
            keyboard.append([
                InlineKeyboardButton("🛠️ Admin Panel", callback_data="admin_panel")
            ])

        keyboard.append([
            InlineKeyboardButton("☰ All Commands", callback_data="all_commands")
        ])
        keyboard.append([
            InlineKeyboardButton("➕ Add Me to Your Group/Channel", callback_data="add_to_group_info")
        ])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def anime_menu() -> InlineKeyboardMarkup:
        """Anime area submenu (was the old flat main_menu's discovery half)."""
        keyboard = [
            [
                InlineKeyboardButton(f"{EMOJI_COLORS['trending']} Trending", callback_data="discover_trending"),
                InlineKeyboardButton(f"{EMOJI_COLORS['latest']} Latest", callback_data="discover_latest")
            ],
            [
                InlineKeyboardButton(f"{EMOJI_COLORS['ongoing']} Ongoing", callback_data="discover_ongoing"),
                InlineKeyboardButton(f"{EMOJI_COLORS['season']} Season", callback_data="discover_season")
            ],
            [
                InlineKeyboardButton(f"{EMOJI_COLORS['movies']} Movies", callback_data="discover_movies")
            ],
            [
                InlineKeyboardButton(f"{EMOJI_COLORS['search']} Search", callback_data="search_anime"),
                InlineKeyboardButton(f"{EMOJI_COLORS['categories']} Categories", callback_data="view_categories")
            ],
            [
                InlineKeyboardButton(f"{EMOJI_COLORS['submit']} Submit Anime", callback_data="submit_anime")
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data="main_menu")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def grouptools_menu(is_group: bool = False, extra_link_rows: list = None) -> InlineKeyboardMarkup:
        """
        Group-management area submenu. In a private DM this stays a
        reference card (most moderation commands need a reply-to-user
        target, so they can't be one-tap buttons there). Inside an actual
        group, adds Warn/Mute/Ban as tappable quick-action buttons — tap
        one, then reply to the target user's message to act on them.

        extra_link_rows: optional list of InlineKeyboardButton rows (feature
        #8's admin-configured custom label->URL buttons), inserted above Back.
        """
        keyboard = []
        if is_group:
            keyboard.append([
                InlineKeyboardButton("⚠️ Warn", callback_data="gt_quick_warn"),
                InlineKeyboardButton("🔇 Mute", callback_data="gt_quick_mute"),
                InlineKeyboardButton("🔨 Ban", callback_data="gt_quick_ban")
            ])
        keyboard += [
            [
                InlineKeyboardButton("⚙️ Mod Settings", callback_data="grouptools_settings"),
                InlineKeyboardButton("📜 Rules", callback_data="grouptools_rules")
            ],
            [
                InlineKeyboardButton("📋 Full Command List", callback_data="grouptools_commands")
            ],
        ]
        if extra_link_rows:
            keyboard += extra_link_rows
        keyboard.append([
            InlineKeyboardButton("⬅️ Back", callback_data="main_menu")
        ])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def tools_menu() -> InlineKeyboardMarkup:
        """Utilities area submenu — AI, market data, downloads, plus the
        already-built Games / Bot Manager / Marketplace mini-apps."""
        keyboard = [
            [
                InlineKeyboardButton("🤖 AI Chat", callback_data="tools_ai_info"),
                InlineKeyboardButton("💹 Crypto/Stocks", callback_data="tools_market_info")
            ],
            [
                InlineKeyboardButton("📰 News", callback_data="tools_news_info"),
                InlineKeyboardButton("🌐 Language", callback_data="tools_language_info")
            ],
            [
                InlineKeyboardButton("🌐 Language", callback_data="tools_language_info")
            ],
            [
                InlineKeyboardButton("🎮 Games", callback_data="m_games"),
                InlineKeyboardButton("🛠️ Bot Manager", callback_data="m_bots")
            ],
            [
                InlineKeyboardButton("🛍️ Marketplace", callback_data="m_market"),
                InlineKeyboardButton("📢 Advertise", callback_data="m_ads")
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data="main_menu")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def anime_list_keyboard(anime_list: list, page: int = 1, category: str = "trending") -> InlineKeyboardMarkup:
        """Generate keyboard for anime list with pagination"""
        keyboard = []
        
        # Each anime as a button
        for anime in anime_list:
            title = anime.get("title", "Unknown")[:30]  # Truncate long titles
            button_text = f"📺 {title}"
            keyboard.append([
                InlineKeyboardButton(button_text, callback_data=f"anime_details_{anime.get('id')}")
            ])
        
        # Navigation buttons
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton(f"{EMOJI_COLORS['back']} Back", callback_data=f"page_{category}_{page-1}"))
        nav_row.append(InlineKeyboardButton(f"Page {page}", callback_data="noop"))
        nav_row.append(InlineKeyboardButton(f"{EMOJI_COLORS['next']} Next", callback_data=f"page_{category}_{page+1}"))
        
        if nav_row:
            keyboard.append(nav_row)
        
        keyboard.append([
            InlineKeyboardButton(f"{EMOJI_COLORS['back']} Back to Menu", callback_data="main_menu")
        ])
        
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def anime_details_keyboard(anime_id: int) -> InlineKeyboardMarkup:
        """Keyboard for anime details view"""
        keyboard = [
            [
                InlineKeyboardButton("📁 Add to Category", callback_data=f"pick_category_{anime_id}")
            ],
            [
                InlineKeyboardButton(f"{EMOJI_COLORS['back']} Back", callback_data="main_menu")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def submission_keyboard() -> InlineKeyboardMarkup:
        """Keyboard for submission workflow"""
        keyboard = [
            [
                InlineKeyboardButton(f"{EMOJI_COLORS['success']} Anime", callback_data="submit_anime_type"),
                InlineKeyboardButton(f"{EMOJI_COLORS['success']} Movie", callback_data="submit_movie_type")
            ],
            [
                InlineKeyboardButton(f"{EMOJI_COLORS['back']} Cancel", callback_data="main_menu")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def admin_dashboard_keyboard() -> InlineKeyboardMarkup:
        """Admin dashboard keyboard with main actions"""
        keyboard = [
            [
                InlineKeyboardButton(f"{EMOJI_COLORS['submit']} Review Submissions", callback_data="review_submissions"),
                InlineKeyboardButton("💰 Revenue", callback_data="admin_revenue")
            ],
            [
                InlineKeyboardButton("👥 Subscribers", callback_data="admin_subscribers"),
                InlineKeyboardButton("🤝 Commissions", callback_data="admin_commissions")
            ],
            [
                InlineKeyboardButton("📊 Analytics", callback_data="admin_analytics"),
                InlineKeyboardButton("🤖 Manage Clones", callback_data="admin_manage_clones")
            ],
            [
                InlineKeyboardButton("📋 Manage Groups/Channels", callback_data="admin_grouplist_0")
            ],
            [
                InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast_start")
            ],
            [
                InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="main_menu")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def admin_group_list_keyboard(chats: list, page: int, page_size: int = 8) -> InlineKeyboardMarkup:
        """Paginated list of every group/channel the bot is in — tap one to
        remote-control it from handlers/admin_remote.py."""
        start = page * page_size
        page_chats = chats[start:start + page_size]

        keyboard = []
        for c in page_chats:
            title = c.get("chat_title") or f"Chat {c['chat_id']}"
            icon = "📢" if c.get("chat_type") == "channel" else "👥"
            keyboard.append([
                InlineKeyboardButton(f"{icon} {title[:40]}", callback_data=f"admin_target_{c['chat_id']}")
            ])

        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"admin_grouplist_{page-1}"))
        if start + page_size < len(chats):
            nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"admin_grouplist_{page+1}"))
        if nav_row:
            keyboard.append(nav_row)

        keyboard.append([InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def admin_remote_categories_keyboard(chat_id: int) -> InlineKeyboardMarkup:
        """Category menu shown after picking a target chat — mirrors the
        grouping used in handlers/admin_remote.py's REMOTE_COMMANDS."""
        keyboard = [
            [InlineKeyboardButton("👤 Member Actions", callback_data=f"admin_cat_member_{chat_id}")],
            [InlineKeyboardButton("🚫 Filters & Words", callback_data=f"admin_cat_filters_{chat_id}")],
            [InlineKeyboardButton("⚙️ Mod Settings", callback_data=f"admin_cat_settings_{chat_id}")],
            [InlineKeyboardButton("💬 Custom Commands", callback_data=f"admin_cat_custom_{chat_id}")],
            [InlineKeyboardButton("📋 Content & Info", callback_data=f"admin_cat_content_{chat_id}")],
            [
                InlineKeyboardButton("🔁 Switch Chat", callback_data="admin_grouplist_0"),
                InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def admin_remote_command_list_keyboard(chat_id: int, category: str, commands: list) -> InlineKeyboardMarkup:
        """List of commands within one category, each tappable to enter
        argument-waiting mode for that command against the chosen chat."""
        keyboard = []
        for cmd in commands:
            keyboard.append([
                InlineKeyboardButton(f"/{cmd['name']}", callback_data=f"admin_run_{cmd['name']}_{chat_id}")
            ])
        keyboard.append([
            InlineKeyboardButton("🔙 Back to Categories", callback_data=f"admin_target_{chat_id}")
        ])
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def admin_panel_keyboard() -> InlineKeyboardMarkup:
        """Admin review panel keyboard for submission review"""
        keyboard = [
            [
                InlineKeyboardButton(f"{EMOJI_COLORS['success']} Approve", callback_data="approve_submission"),
                InlineKeyboardButton(f"{EMOJI_COLORS['error']} Reject", callback_data="reject_submission")
            ],
            [
                InlineKeyboardButton("📝 Add Note", callback_data="add_admin_note"),
                InlineKeyboardButton("⬅️ Skip", callback_data="skip_submission")
            ],
            [
                InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def clone_payment_keyboard(amount_ghs: int) -> InlineKeyboardMarkup:
        """Keyboard for clone payment with Selar"""
        keyboard = [
            [
                InlineKeyboardButton(f"{EMOJI_COLORS['clone']} Pay {amount_ghs} GHS", callback_data="paystack_checkout")
            ],
            [
                InlineKeyboardButton("ℹ️ What's Included", callback_data="clone_info"),
                InlineKeyboardButton("❌ Cancel", callback_data="main_menu")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def clone_paid_keyboard() -> InlineKeyboardMarkup:
        """Button shown after checkout to notify the admin for manual review."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("I have paid — Notify admin", callback_data="clone_paid")],
            [InlineKeyboardButton("Cancel", callback_data="main_menu")],
        ])

    @staticmethod
    def clone_admin_review_keyboard(reference: str) -> InlineKeyboardMarkup:
        """Admin-only approval controls for a reported clone payment."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Approve", callback_data=f"clone_admin_approve:{reference}")],
            [InlineKeyboardButton("Reject", callback_data=f"clone_admin_reject:{reference}")],
        ])
    
    @staticmethod
    def clone_customization_keyboard() -> InlineKeyboardMarkup:
        """Keyboard for clone customization"""
        keyboard = [
            [
                InlineKeyboardButton("📝 Edit Bot Name", callback_data="customize_name"),
                InlineKeyboardButton("🔗 Set Webhook URL", callback_data="customize_webhook")
            ],
            [
                InlineKeyboardButton("🎨 Branding", callback_data="customize_branding"),
                InlineKeyboardButton("📂 Categories", callback_data="customize_categories")
            ],
            [
                InlineKeyboardButton(f"{EMOJI_COLORS['success']} Done", callback_data="finalize_clone"),
                InlineKeyboardButton("❌ Cancel", callback_data="main_menu")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    def clone_customize_back_keyboard() -> InlineKeyboardMarkup:
        """Shown under each 'awaiting_X' customization prompt (name/webhook/
        branding/categories) so the user isn't stuck typing or stranded —
        tapping it returns to the customization menu without re-checking
        payment (they're already past that gate to be in this flow)."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="clone_back_to_customize")]
        ])

    @staticmethod
    def my_clones_keyboard(clones: list) -> InlineKeyboardMarkup:
        """List of a user's own cloned bots, each opening its edit menu, plus
        a button to create another one."""
        keyboard = []
        for c in clones:
            label = c.get("display_name") or c.get("bot_username") or f"Clone {c['clone_id']}"
            keyboard.append([
                InlineKeyboardButton(f"🤖 {label}", callback_data=f"clone_detail_{c['clone_id']}")
            ])
        keyboard.append([InlineKeyboardButton("➕ Add Another Bot", callback_data="clone_add_another")])
        keyboard.append([InlineKeyboardButton(f"{EMOJI_COLORS.get('back', '⬅️')} Back", callback_data="main_menu")])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def clone_edit_keyboard(clone_id: int) -> InlineKeyboardMarkup:
        """Edit menu for a single existing clone — same fields as the initial
        customization step, but writing straight to the DB instead of staging
        in user_data for a not-yet-created bot."""
        keyboard = [
            [
                InlineKeyboardButton("📝 Edit Name", callback_data=f"clone_editfield_name_{clone_id}"),
                InlineKeyboardButton("🎨 Edit Branding", callback_data=f"clone_editfield_branding_{clone_id}")
            ],
            [
                InlineKeyboardButton("📂 Edit Categories", callback_data=f"clone_editfield_categories_{clone_id}")
            ],
            [
                InlineKeyboardButton("💰 Monetization", callback_data=f"clone_monetization_{clone_id}")
            ],
            [InlineKeyboardButton("⬅️ Back to My Clones", callback_data="my_clones")]
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def clone_monetization_menu_keyboard(clone_id: int, active: bool) -> InlineKeyboardMarkup:
        """Entry point for a clone owner's monetization controls. Locked
        (payment settings + custom pricing both hidden behind an Activate
        button) until CLONE_MONETIZATION_FEE_GHS/month is active."""
        if active:
            keyboard = [
                [InlineKeyboardButton("💳 Payment Settings", callback_data=f"clone_paysettings_{clone_id}")],
                [InlineKeyboardButton("🏷️ Set Your Prices", callback_data=f"clone_prices_{clone_id}")],
                [InlineKeyboardButton("🔄 Renew Subscription", callback_data=f"clone_monetize_activate_{clone_id}")],
            ]
        else:
            keyboard = [
                [InlineKeyboardButton("🔓 Activate Monetization", callback_data=f"clone_monetize_activate_{clone_id}")],
            ]
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"clone_detail_{clone_id}")])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def clone_monetize_payment_keyboard(clone_id: int, payment_link: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Pay Now", url=payment_link)],
            [InlineKeyboardButton("✅ I've Paid — Verify", callback_data=f"clone_monetize_verify_{clone_id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"clone_monetization_{clone_id}")],
        ])

    @staticmethod
    def clone_prices_menu_keyboard(clone_id: int, prices: dict) -> InlineKeyboardMarkup:
        """One editable row per PRICE_REGISTRY key, showing the current
        (possibly overridden) price."""
        from config import PRICE_REGISTRY
        keyboard = []
        for key, meta in PRICE_REGISTRY.items():
            amount = prices.get(key, meta["default"])
            keyboard.append([
                InlineKeyboardButton(
                    f"{meta['label']} — GHS {amount:g}",
                    callback_data=f"clone_editprice_{key}_{clone_id}"
                )
            ])
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"clone_monetization_{clone_id}")])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def clone_price_edit_back_keyboard(clone_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data=f"clone_prices_{clone_id}")]
        ])

    @staticmethod
    def clone_payment_settings_keyboard(clone_id: int, current_provider: str) -> InlineKeyboardMarkup:
        """Payment routing choice for a clone owner. 'main' (default) means
        payments go through the main bot's own Selar account until the
        owner connects their own gateway key. A checkmark marks whichever
        is currently active."""
        def label(text: str, provider: str) -> str:
            return f"✅ {text}" if current_provider == provider else text

        keyboard = [
            [InlineKeyboardButton(label("🏦 Use Main Bot (default)", "main"), callback_data=f"clone_paysetprovider_main_{clone_id}")],
            [InlineKeyboardButton(label("💳 Connect Paystack", "paystack"), callback_data=f"clone_paysetprovider_paystack_{clone_id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"clone_monetization_{clone_id}")]
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def clone_payment_key_prompt_keyboard(clone_id: int) -> InlineKeyboardMarkup:
        """Shown while waiting for the owner to paste their gateway key."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data=f"clone_paysettings_{clone_id}")]
        ])

    @staticmethod
    def clone_edit_back_keyboard(clone_id: int) -> InlineKeyboardMarkup:
        """Shown under an 'editing_X' prompt for an existing clone."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data=f"clone_detail_{clone_id}")]
        ])

    @staticmethod
    def category_management_keyboard() -> InlineKeyboardMarkup:
        """Keyboard for managing categories"""
        keyboard = [
            [
                InlineKeyboardButton(f"{EMOJI_COLORS['success']} Create New", callback_data="create_category"),
                InlineKeyboardButton("📋 View All", callback_data="view_all_categories")
            ],
            [
                InlineKeyboardButton(f"{EMOJI_COLORS['back']} Back", callback_data="main_menu")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def category_picker_keyboard(categories: list, anime_id: int) -> InlineKeyboardMarkup:
        """Keyboard for picking which category to add an anime to"""
        keyboard = []
        for cat in categories:
            keyboard.append([
                InlineKeyboardButton(
                    f"{cat.get('emoji', '📁')} {cat.get('category_name')}",
                    callback_data=f"add_to_category_{cat.get('category_id')}_{anime_id}"
                )
            ])
        keyboard.append([
            InlineKeyboardButton(f"{EMOJI_COLORS['success']} New Category", callback_data="create_category"),
            InlineKeyboardButton(f"{EMOJI_COLORS['back']} Cancel", callback_data="main_menu")
        ])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def category_list_keyboard(categories: list) -> InlineKeyboardMarkup:
        """Keyboard listing a user's categories to browse"""
        keyboard = []
        for cat in categories:
            count = len(cat.get("anime_ids", []))
            keyboard.append([
                InlineKeyboardButton(
                    f"{cat.get('emoji', '📁')} {cat.get('category_name')} ({count})",
                    callback_data=f"category_detail_{cat.get('category_id')}"
                )
            ])
        keyboard.append([
            InlineKeyboardButton(f"{EMOJI_COLORS['success']} Create New", callback_data="create_category")
        ])
        keyboard.append([
            InlineKeyboardButton(f"{EMOJI_COLORS['back']} Back", callback_data="view_categories")
        ])
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def search_keyboard() -> ReplyKeyboardMarkup:
        """Reply keyboard for search input"""
        keyboard = [
            [KeyboardButton("🔍 Search Anime")],
            [KeyboardButton("🔍 Search Movies")],
            ["❌ Cancel"]
        ]
        return ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    
    @staticmethod
    def subscription_verify_keyboard() -> InlineKeyboardMarkup:
        """Keyboard for verifying subscription payment"""
        keyboard = [
            [InlineKeyboardButton("✅ I've Paid — Verify", callback_data="verify_subscription")],
            [InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def clone_verify_keyboard() -> InlineKeyboardMarkup:
        """Keyboard for verifying clone payment"""
        keyboard = [
            [InlineKeyboardButton("✅ Verify & Create Bot", callback_data="finalize_clone")],
            [InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def clone_webhook_overwrite_keyboard() -> InlineKeyboardMarkup:
        """
        Confirmation gate before overwriting an existing webhook on a pasted bot
        token (Part 3.2 Step B) — never call setWebhook silently over someone
        else's live integration.
        """
        keyboard = [
            [InlineKeyboardButton("⚠️ Yes, disconnect it and continue", callback_data="clone_confirm_overwrite")],
            [InlineKeyboardButton("❌ Cancel, use a different bot", callback_data="clone_cancel_token")]
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def language_menu() -> InlineKeyboardMarkup:
        """
        Language picker for /language, two per row. Built from
        i18n.SUPPORTED_LANGUAGES so adding a language there is enough —
        no separate keyboard edit needed.
        """
        from i18n import SUPPORTED_LANGUAGES  # local import: avoid any import-order surprises at module load

        codes = list(SUPPORTED_LANGUAGES.items())
        keyboard = []
        for i in range(0, len(codes), 2):
            row = [
                InlineKeyboardButton(name, callback_data=f"lang_set_{code}")
                for code, name in codes[i:i + 2]
            ]
            keyboard.append(row)
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_loading_animation(frame: int) -> str:
        """Get loading animation frame"""
        return LOADING_ANIMATION[frame % len(LOADING_ANIMATION)]

# Global instance
keyboard_gen = KeyboardGenerator()
