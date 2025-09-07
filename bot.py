# bot.py
import os
from dotenv import load_dotenv
load_dotenv()  # loads variables from .env into process env

import time
import discord
import random
import datetime
import json
import logging
import logging.handlers
from logging.handlers import RotatingFileHandler
from typing import Tuple, Dict

from discord import app_commands
from discord.ext import commands, tasks

from currency import Money
from bank import (
    get_balance, set_balance, add_balance, subtract_if_enough,
    top_users, top_characters,
)
from shop import list_items, get_price
from links import (
    link_character, unlink_character, resolve_character,
    all_links, normalize_display_name,
)
from vaults import set_vault_thread, get_vault_thread, unlink_vault_thread, post_receipt

# ---------------- DATA DIR / FILES ----------------
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)

DB_FILE = os.path.join(DATA_DIR, "balances.json")
PENDING_FILE = os.path.join(DATA_DIR, "pending_receipts.json")

# ---------------- LOGGING ----------------
import logging, logging.handlers, os, re

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE  = os.getenv("LOG_FILE", "bot.log")

# Optional: silence a noisy webhook by ID or name (Wizarding Cards example)
SILENCE_WEBHOOK_IDS = {1130656856990289961}  # add/remove as needed
SILENCE_WEBHOOK_NAMES = {"Wizarding Cards"}  # exact webhook author display name

_WEBHOOK_ID_RE = re.compile(r"\bwebhook_id=(\d{5,})\b")
_AUTHOR_RE     = re.compile(r"author='([^']+)'")  # matches our earn_trace author='...'

class WebhookNoiseFilter(logging.Filter):
    """
    Drop only *our* records that explicitly mention a silenced webhook id or author.
    We look for 'webhook_id=<id>' and "author='Name'" tokens to avoid substring accidents.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        # Only filter our app logger; never touch discord or root logs.
        if not record.name.startswith("gringotts"):
            return True

        msg = record.getMessage()

        # Match an exact webhook id token
        m = _WEBHOOK_ID_RE.search(msg)
        if m:
            try:
                wid = int(m.group(1))
                if wid in SILENCE_WEBHOOK_IDS:
                    return False
            except ValueError:
                pass

        # Match explicit author token
        m2 = _AUTHOR_RE.search(msg)
        if m2 and m2.group(1) in SILENCE_WEBHOOK_NAMES:
            return False

        return True

def setup_logging():
    # Root logger: keep minimal setup so third-party libs aren't affected.
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.INFO)
    root.addHandler(logging.StreamHandler())  # simple console for non-app logs

    # Our app logger + handlers
    app_logger = logging.getLogger("gringotts")
    app_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    ch = logging.StreamHandler()
    ch.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    ch.setFormatter(fmt)
    ch.addFilter(WebhookNoiseFilter())

    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    fh.setFormatter(fmt)
    fh.addFilter(WebhookNoiseFilter())

    # Clear existing handlers on the app logger to avoid duplicates
    for h in list(app_logger.handlers):
        app_logger.removeHandler(h)

    app_logger.addHandler(ch)
    app_logger.addHandler(fh)

setup_logging()
logger = logging.getLogger("gringotts")

# ---------------- CONFIG ----------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN env var not set.")

# Your server (guild-only sync = instant command availability)
TEST_GUILD_ID = 1393623189241991168
TEST_GUILD = discord.Object(id=TEST_GUILD_ID)

# Channels where messages earn money (include forum PARENT channel IDs)
ALLOWED_CHANNEL_IDS: set[int] = {
    1398762545430401155, # Events
    1411901315185119252, # Witch's Market (Forum parent or specific thread)
    1393688264531247277, # Hogwarts Grounds
    1393683936009257141, # Hogwarts Castle
    1393687041212153886, # Slytherin Common Room
    1393685488338210917, # Gryffindor Common Room
    1393687315162857493, # Ravenclaw Common Room
    1393687608684580874, # Hufflepuff Common Room
    1393689121666502666, # Hogsmeade Village
    1411841740654248046, # Diagon Alley
    1411841810371711037, # Knockturn Alley
    1407084037843189882, # The Highlands
    1393690088835125330, # Wizarding London
    1409802777126768640, # Elsewhere
    1406803659202887862, # Chaos Testing Center
}

# OPTIONAL IGNORE LISTS FOR WEBHOOKS (utility bots etc.)
IGNORED_WEBHOOK_IDS: set[int] = {
    1130656856990289961,  # Wizarding Cards
}
IGNORED_WEBHOOK_NAMES: set[str] = {
    "Wizarding Cards",
}

# Bypass per-character cooldown in channels where debug is enabled (handy for testing)
DEBUG_BYPASS_COOLDOWN = os.getenv("DEBUG_BYPASS_COOLDOWN", "0") == "1"

# ---------------- DEBUG (targeted, low-noise) ----------------
# Set DEBUG_EARN_ALL=1 to trace all webhook messages everywhere (very noisy).
DEBUG_EARN_ALL = os.getenv("DEBUG_EARN_ALL", "0") == "1"

# Channels/threads where we want deep diagnostics: toggle with /debug_toggle
DEBUG_EARNING_CHANNEL_IDS: set[int] = set()  # MUST be set(), not {}

def _debug_enabled_for_channel(ch: discord.abc.GuildChannel | discord.Thread) -> bool:
    if DEBUG_EARN_ALL:
        return True
    parent = getattr(ch, "parent", None)
    candidates = {
        getattr(ch, "id", None),
        getattr(ch, "parent_id", None),
        getattr(parent, "id", None) if parent else None,
    }
    return any(cid and cid in DEBUG_EARNING_CHANNEL_IDS for cid in candidates)


# Channels for Gringotts Bank
GRINGOTTS_FORUM_ID = 1393690306410450975  # test forum ID

EARN_PER_MESSAGE = Money.from_str("7k")     # payout per qualifying message
EARN_COOLDOWN_SECONDS = 15                  # cooldown PER CHARACTER
MIN_MESSAGE_LENGTH = 500                    # minimum characters to count

# Weekly pay (currently user-level, not per-character)
ADULT_ROLE_NAME = "Adult"
BASE_WEEKLY_PAY = Money.from_str("1g")
JOB_BONUSES: dict[str, Money] = {
    # "Professor": Money.from_str("2g"),
    # "Shopkeeper": Money.from_str("1g 10s"),
}

STARTER_FUNDS = Money.from_str("50g")   # starting balance for a newly linked character

# ---------------- DEBUG (targeted, low-noise) ----------------
# Set DEBUG_EARN_ALL=1 in env to trace decisions in ALL channels/threads temporarily.
DEBUG_EARN_ALL = os.getenv("DEBUG_EARN_ALL", "0") == "1"

# Track channels/threads where we want deep diagnostics (toggle with /debug_toggle).
# Track channels/threads where we want deep diagnostics:
DEBUG_EARNING_CHANNEL_IDS: set[int] = set()  # must be set(), not {}

def _debug_enabled_for_channel(ch: discord.abc.GuildChannel | discord.Thread) -> bool:
    if os.getenv("DEBUG_EARN_ALL", "0") == "1":
        return True
    parent = getattr(ch, "parent", None)
    candidates = {
        getattr(ch, "id", None),
        getattr(ch, "parent_id", None),
        getattr(parent, "id", None) if parent else None,
    }
    return any(cid and cid in DEBUG_EARNING_CHANNEL_IDS for cid in candidates)


# ---------------- BOT SETUP ----------------
intents = discord.Intents.default()
intents.message_content = True   # to read message content length
intents.members = True           # for payday role checks
bot = commands.Bot(command_prefix="!", intents=intents)

# Per-character cooldown: (user_id, normalized_char_name) -> last_time
last_earn_at: dict[tuple[int, str], float] = {}

# ---------------- HELPERS ----------------

# ---- logging helpers ----
def _msg_ctx(message: discord.Message) -> dict:
    ch = message.channel
    parent = getattr(ch, "parent", None)
    return {
        "guild_id": getattr(message.guild, "id", None),
        "message_id": getattr(message, "id", None),
        "author_id": getattr(message.author, "id", None),
        "author_name": getattr(message.author, "name", None),
        "author_is_bot": getattr(message.author, "bot", None),
        "webhook_id": getattr(message, "webhook_id", None),
        "channel_id": getattr(ch, "id", None),
        "channel_name": getattr(ch, "name", None),
        "parent_id": getattr(ch, "parent_id", None),
        "category_id": getattr(ch, "category_id", None),
        "parent_category_id": getattr(parent, "category_id", None) if parent else None,
        "is_thread": isinstance(ch, discord.Thread),
        "content_len": len((message.content or "").strip()),
    }

def _pending_load() -> dict:
    if not os.path.exists(PENDING_FILE):
        return {}
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}

def _pending_save_atomic(data: dict) -> None:
    d = os.path.dirname(PENDING_FILE) or "."
    tmp = os.path.join(d, f".tmp_{os.path.basename(PENDING_FILE)}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, PENDING_FILE)

def _utc_datestr(dt: datetime.datetime | None = None) -> str:
    dt = dt or datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d")

def queue_rp_earning(guild_id: int, user_id: int, char_key: str, delta_knuts: int) -> None:
    """
    Accumulate today's total (UTC) RP earnings per (guild, user, character).
    """
    data = _pending_load()
    day = _utc_datestr()
    gkey = str(guild_id)
    uck = f"{user_id}:{char_key}"

    day_bucket = data.setdefault(day, {})
    guild_bucket = day_bucket.setdefault(gkey, {})
    rec = guild_bucket.setdefault(uck, {"knuts": 0, "count": 0})
    rec["knuts"] += int(delta_knuts)
    rec["count"] += 1

    _pending_save_atomic(data)

def is_earning_channel_with_details(
    ch: discord.abc.GuildChannel | discord.Thread
) -> tuple[bool, dict]:
    ids_to_check: list[int] = []
    ids_to_check.append(getattr(ch, "id", None))
    ids_to_check.append(getattr(ch, "parent_id", None))
    ids_to_check.append(getattr(ch, "category_id", None))

    parent = getattr(ch, "parent", None)
    if parent is not None:
        ids_to_check.append(getattr(parent, "id", None))
        ids_to_check.append(getattr(parent, "parent_id", None))
        ids_to_check.append(getattr(parent, "category_id", None))

    ids_to_check = [cid for cid in ids_to_check if cid]
    matched = [cid for cid in ids_to_check if cid in ALLOWED_CHANNEL_IDS]
    allowed = bool(matched)
    details = {
        "channel_type": type(ch).__name__,
        "channel_id": getattr(ch, "id", None),
        "parent_id": getattr(ch, "parent_id", None),
        "category_id": getattr(ch, "category_id", None),
        "parent_type": type(parent).__name__ if parent else None,
        "parent_category_id": getattr(parent, "category_id", None) if parent else None,
        "ids_checked": ids_to_check,
        "ids_matched": matched,
        "allowed": allowed,
    }
    return allowed, details

def is_earning_channel(message: discord.Message) -> bool:
    """Allow by channel ID, its parent (e.g., forum or text channel), or their category."""
    ch = message.channel
    ids_to_check: set[int] = set()
    ids_to_check.add(getattr(ch, "id", None))
    ids_to_check.add(getattr(ch, "parent_id", None))
    ids_to_check.add(getattr(ch, "category_id", None))
    parent = getattr(ch, "parent", None)
    if parent is not None:
        ids_to_check.add(getattr(parent, "id", None))
        ids_to_check.add(getattr(parent, "parent_id", None))
        ids_to_check.add(getattr(parent, "category_id", None))
    return any(cid in ALLOWED_CHANNEL_IDS for cid in ids_to_check if cid)

def can_payout(owner_user_id: int, char_key: str | None) -> bool:
    """Per-user+character cooldown."""
    now = time.time()
    key = (owner_user_id, (char_key or "").lower())
    last = last_earn_at.get(key, 0.0)
    if now - last >= EARN_COOLDOWN_SECONDS:
        last_earn_at[key] = now
        return True
    return False

async def deposit_to_character(
    interaction_or_guild: discord.Interaction | discord.Guild,
    guild: discord.Guild,
    user_id: int,
    char_key: str,
    amount: Money,
    reason: str | None = None
) -> None:
    add_balance(user_id, amount, key=char_key)
    new_bal = get_balance(user_id, key=char_key)
    await post_receipt(bot, guild, user_id, char_key, amount, new_bal, reason)

async def withdraw_from_character(
    interaction_or_guild: discord.Interaction | discord.Guild,
    guild: discord.Guild,
    user_id: int,
    char_key: str,
    amount: Money,
    reason: str | None = None
) -> bool:
    if not subtract_if_enough(user_id, amount, key=char_key):
        return False
    new_bal = get_balance(user_id, key=char_key)
    neg = Money(-amount.knuts)
    await post_receipt(bot, guild, user_id, char_key, neg, new_bal, reason)
    return True

# ---------------- EVENTS ----------------
@bot.event
async def on_ready():
    # Guaranteed startup log
    logger.info("startup: bot ready as %s (guilds=%d, LOG_LEVEL=%s, LOG_FILE=%s)",
                bot.user, len(bot.guilds), LOG_LEVEL, LOG_FILE)
    try:
        bot.tree.copy_global_to(guild=TEST_GUILD)
        synced = await bot.tree.sync(guild=TEST_GUILD)
        logger.info(f"Synced {len(synced)} commands to {TEST_GUILD_ID}: {[c.name for c in synced]}")
    except Exception as e:
        logger.exception(f"[SYNC ERROR] {type(e).__name__}: {e}")

    if not weekly_payday.is_running():
        weekly_payday.start()
    if not flush_daily_receipts.is_running():
        flush_daily_receipts.start()

    logger.info(f"Bot ready as {bot.user} in {len(bot.guilds)} guild(s).")

@bot.event
async def on_message(message: discord.Message):
    # Ignore ourselves
    if message.author.id == bot.user.id:
        return

    dbg = _debug_enabled_for_channel(message.channel)

    # HEARTBEAT (always emit in traced channels)
    if dbg:
        logger.info(
            "debug:earn_heartbeat | "
            f"guild_id={getattr(message.guild, 'id', None)} "
            f"message_id={getattr(message, 'id', None)} "
            f"webhook_id={getattr(message, 'webhook_id', None)} "
            f"author='{getattr(message.author, 'name', None)}' "
            f"channel_id={getattr(message.channel, 'id', None)} "
            f"parent_id={getattr(message.channel, 'parent_id', None)} "
            f"len={len((getattr(message, 'content', '') or '').strip())}"
        )

    # Only award for proxied/webhook messages (Tupperbox etc.)
    if not message.webhook_id:
        if dbg:
            logger.info("debug:earn_skip reason='not_webhook'")
        await bot.process_commands(message)
        return

    # Optional: hard-ignore utility webhooks
    if message.webhook_id in IGNORED_WEBHOOK_IDS or (message.author.name or "") in IGNORED_WEBHOOK_NAMES:
        if dbg:
            logger.info(
                "debug:earn_skip reason='ignored_webhook' | "
                f"webhook_id={message.webhook_id} author='{message.author.name}'"
            )
        await bot.process_commands(message)
        return

    # Channel allowlist
    allowed, ch_details = is_earning_channel_with_details(message.channel)
    if not allowed:
        if dbg:
            logger.info(f"debug:earn_skip reason='channel_not_allowed' | {ch_details}")
        await bot.process_commands(message)
        return
    elif dbg:
        logger.info(f"debug:earn_check channel_allowed=True | {ch_details}")

    # Content length
    content = (message.content or "").strip()
    if len(content) < MIN_MESSAGE_LENGTH:
        if dbg:
            logger.info(
                "debug:earn_skip reason='too_short' "
                f"min={MIN_MESSAGE_LENGTH} actual={len(content)}"
            )
        await bot.process_commands(message)
        return

    # Character resolution
    raw_name = message.author.name or ""
    try:
        char_key = normalize_display_name(raw_name)
        linked_uid = resolve_character(raw_name)
    except Exception as e:
        if dbg:
            logger.exception(f"debug:earn_skip reason='normalize_or_resolve_exception' name='{raw_name}'")
        await bot.process_commands(message)
        return

    if not linked_uid:
        if dbg:
            logger.info(
                "debug:earn_skip reason='unlinked_character' "
                f"name='{raw_name}' char_key='{char_key}'"
            )
        await bot.process_commands(message)
        return
    elif dbg:
        logger.info(f"debug:earn_check linked user_id={linked_uid} char_key='{char_key}'")

    # Cooldown (with optional bypass while debugging)
    if not DEBUG_BYPASS_COOLDOWN and not can_payout(linked_uid, char_key):
        if dbg:
            logger.info(
                "debug:earn_skip reason='cooldown' "
                f"cooldown_s={EARN_COOLDOWN_SECONDS} user_id={linked_uid} char_key='{char_key}'"
            )
        await bot.process_commands(message)
        return
    elif dbg and DEBUG_BYPASS_COOLDOWN:
        logger.info("debug:earn_check cooldown_bypassed=True")

    # Success
    add_balance(linked_uid, EARN_PER_MESSAGE, key=char_key)
    queue_rp_earning(message.guild.id, linked_uid, char_key, EARN_PER_MESSAGE.knuts)
    if dbg:
        logger.info(
            "debug:earn_ok "
            f"delta='{EARN_PER_MESSAGE.pretty_long()}' user_id={linked_uid} char_key='{char_key}'"
        )

    await bot.process_commands(message)



# ---------------- SLASH COMMANDS ----------------
# --- Staff: withdraw from a character vault ---
@bot.tree.command(
    name="vault_withdraw",
    description="(Staff) Withdraw funds from a character's vault and post a receipt."
)
@app_commands.guilds(TEST_GUILD)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    character="Character display name",
    amount="Amount to withdraw, e.g., 2g 5s or 30s",
    note="Reason/note to include on the receipt (shown in the Vault)"
)
async def vault_withdraw_cmd(
    interaction: discord.Interaction,
    character: str,
    amount: str,
    note: str | None = None
):
    uid = resolve_character(character)
    if not uid:
        await interaction.response.send_message(f"❌ No link for **{character}**.", ephemeral=True)
        return

    key = normalize_display_name(character)

    try:
        m = Money.from_str(amount)
    except Exception:
        await interaction.response.send_message(
            "❌ Amount format not recognized. Try `2g 5s`, `15s`, or `300k`.",
            ephemeral=True
        )
        return

    if m.knuts <= 0:
        await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
        return

    reason = note.strip() if (note and note.strip()) else "Staff Withdrawal"

    ok = await withdraw_from_character(
        interaction.guild,  # interaction_or_guild
        interaction.guild,  # guild
        uid,
        key,
        m,
        reason=reason
    )
    if not ok:
        bal = get_balance(uid, key=key)
        await interaction.response.send_message(
            f"❌ Insufficient funds. {character} balance: **{bal.pretty_long()}**.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"🏦 Withdrew **{m.pretty_long()}** from **{character}**. Note: _{reason}_",
        ephemeral=True
    )

@bot.tree.command(name="debug_toggle", description="(Staff) Toggle earn debug tracing for this channel/thread.")
@app_commands.guilds(TEST_GUILD)
@app_commands.checks.has_permissions(manage_guild=True)
async def debug_toggle_cmd(interaction: discord.Interaction):
    ch = interaction.channel
    cid = getattr(ch, "id", None)
    if cid is None:
        await interaction.response.send_message("This place has no channel ID? 🤔", ephemeral=True)
        return
    if cid in DEBUG_EARNING_CHANNEL_IDS:
        DEBUG_EARNING_CHANNEL_IDS.remove(cid)
        await interaction.response.send_message(f"🔇 Debug OFF for <#{cid}>", ephemeral=True)
    else:
        DEBUG_EARNING_CHANNEL_IDS.add(cid)
        await interaction.response.send_message(f"🔊 Debug ON for <#{cid}>", ephemeral=True)

@bot.tree.command(name="debug_status", description="(Staff) Show earn-debug status for this channel/thread.")
@app_commands.guilds(TEST_GUILD)
@app_commands.checks.has_permissions(manage_guild=True)
async def debug_status_cmd(interaction: discord.Interaction):
    ch = interaction.channel
    parent = getattr(ch, "parent", None)
    cids = [
        ("this", getattr(ch, "id", None)),
        ("parent", getattr(ch, "parent_id", None)),
        ("parent.id", getattr(parent, "id", None) if parent else None),
    ]
    enabled = _debug_enabled_for_channel(ch)
    await interaction.response.send_message(
        "Earn-debug status:\n"
        f"- DEBUG_EARN_ALL: `{DEBUG_EARN_ALL}`\n"
        f"- Channel IDs considered: `{[x for (_n, x) in cids]}`\n"
        f"- Tracing enabled here: `{enabled}`\n"
        f"- Currently traced IDs: `{sorted(list(DEBUG_EARNING_CHANNEL_IDS))}`\n"
        f"- LOG_LEVEL: `{os.getenv('LOG_LEVEL', 'INFO')}`\n",
        ephemeral=True
    )


@bot.tree.command(name="debug_channel", description="(Staff) Explain why this channel/thread is or isn't allowed for RP earnings.")
@app_commands.guilds(TEST_GUILD)
@app_commands.checks.has_permissions(manage_guild=True)
async def debug_channel_cmd(interaction: discord.Interaction):
    allowed, details = is_earning_channel_with_details(interaction.channel)
    lines = [
        f"**Allowed:** {details['allowed']}",
        f"Channel type: `{details['channel_type']}`  (id: `{details['channel_id']}`)",
        f"Parent type: `{details['parent_type']}`  (id: `{details['parent_id']}`)",
        f"Category id: `{details['category_id']}`",
        f"Parent category id: `{details['parent_category_id']}`",
        f"Checked IDs: `{details['ids_checked']}`",
        f"Matched IDs: `{details['ids_matched']}`",
        f"ALLOW list size: `{len(ALLOWED_CHANNEL_IDS)}`",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(name="hello", description="Test command to verify sync.")
@app_commands.guilds(TEST_GUILD)
async def hello_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("Hello! ✅ Guild commands are synced.")

# Wallet overview (per-character)
@bot.tree.command(name="balance", description="Show your linked characters and balances.")
@app_commands.guilds(TEST_GUILD)
async def balance_cmd(interaction: discord.Interaction):
    links = all_links()  # {normalized_char_name: user_id}
    my_chars = [char for char, uid in links.items() if uid == interaction.user.id]
    if not my_chars:
        await interaction.response.send_message(
            "You have no linked characters yet. Use `/link_character` to link your Tupperbox name.",
            ephemeral=True
        )
        return

    total = Money(0)
    lines: list[str] = []
    for char in sorted(my_chars):
        bal = get_balance(interaction.user.id, key=char)
        total += bal
        lines.append(f"- **{char}** — {bal.pretty_long()}")

    await interaction.response.send_message(
        f"**Your Character Wallets**\n" + "\n".join(lines) + f"\n\n**Total**: {total.pretty_long()}",
        ephemeral=True
    )

# Specific character balance
@bot.tree.command(name="char_balance", description="Check a character's wallet (per-character).")
@app_commands.guilds(TEST_GUILD)
@app_commands.describe(name="Character display name exactly as it appears on posts")
async def char_balance_cmd(interaction: discord.Interaction, name: str):
    uid = resolve_character(name)
    if not uid:
        await interaction.response.send_message(
            f"❌ I don’t have a link for **{name}**. Ask the player to run `/link_character`.",
            ephemeral=True
        )
        return
    member = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)
    key = normalize_display_name(name)
    bal = get_balance(uid, key=key)
    await interaction.response.send_message(
        f"**{name}** (played by {member.mention}) has **{bal.pretty_long()}**.",
        ephemeral=True
    )

# Character linking
@bot.tree.command(name="link_character", description="Link a character display name (Tupperbox) to a user.")
@app_commands.guilds(TEST_GUILD)
@app_commands.describe(
    name="Character display name exactly as it appears on messages",
    user="(Optional) Link to a specific user (staff only)"
)
async def link_character_cmd(interaction: discord.Interaction, name: str, user: discord.Member | None = None):
    target = user or interaction.user
    if user and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Only staff can link characters to other users.", ephemeral=True)
        return

    # Link the character → user
    link_character(name, target.id)

    # Normalize the per-character wallet key
    key = normalize_display_name(name)

    # Grant starter funds ONLY if this character wallet is brand new (zero balance)
    granted_text = ""
    current = get_balance(target.id, key=key)
    if current.knuts == 0 and STARTER_FUNDS.knuts > 0:
        add_balance(target.id, STARTER_FUNDS, key=key)
        new_bal = get_balance(target.id, key=key)
        granted_text = f"\n💰 Starter funds added: **{STARTER_FUNDS.pretty_long()}** (New balance: **{new_bal.pretty_long()}**)."
        # Try to post a receipt in the character's vault if it exists
        try:
            await post_receipt(
                bot, interaction.guild, target.id, key,
                delta=STARTER_FUNDS,
                new_balance=new_bal,
                reason="Starter funds for new character link"
            )
        except Exception as e:
            logger.warning(f"post_receipt starter funds failed for {target.id}/{key}: {e}")

    await interaction.response.send_message(
        f"🔗 Linked **{name}** → {target.mention}. Proxied posts by **{name}** will now credit that wallet."
        + granted_text,
        ephemeral=True
    )

@bot.tree.command(name="unlink_character", description="Remove link for a character display name.")
@app_commands.guilds(TEST_GUILD)
@app_commands.describe(name="Character display name to unlink")
async def unlink_character_cmd(interaction: discord.Interaction, name: str):
    ok = unlink_character(name)
    if ok:
        await interaction.response.send_message(f"🧹 Unlinked **{name}**.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ No link found for **{name}**.", ephemeral=True)

@bot.tree.command(name="who_is", description="See which user a character name is linked to.")
@app_commands.guilds(TEST_GUILD)
@app_commands.describe(name="Character display name to look up")
async def who_is_cmd(interaction: discord.Interaction, name: str):
    uid = resolve_character(name)
    if uid:
        user = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)
        await interaction.response.send_message(f"**{name}** is linked to {user.mention}.", ephemeral=True)
    else:
        await interaction.response.send_message(f"**{name}** is not linked to anyone.", ephemeral=True)

# Shop (per-character)
@bot.tree.command(name="shop", description="Browse and buy items by town and shop.")
@app_commands.guilds(TEST_GUILD)
@app_commands.describe(
    action="Pick: towns | shops | list | buy",
    town="Town name: Diagon Alley, Knockturn Alley, Hogsmeade",
    shop="Shop name within the town",
    item="Item name to buy",
    quantity="How many to buy",
    character="Character wallet to spend from"
)
async def shop_cmd(
    interaction: discord.Interaction,
    action: str,
    town: str | None = None,
    shop: str | None = None,
    item: str | None = None,
    quantity: int | None = 1,
    character: str | None = None
):
    from shop import list_towns, list_shops, list_items, buy_item, get_price

    action = (action or "").lower().strip()

    # towns
    if action == "towns":
        towns = list_towns()
        e = discord.Embed(title="Towns", color=discord.Color.blurple())
        if towns:
            for t in towns:
                e.add_field(name=t, value="—", inline=False)
        else:
            e.description = "_No towns configured yet._"
        await interaction.response.send_message(embed=e)
        return

    # shops in a town
    if action == "shops":
        if not town:
            await interaction.response.send_message("Provide `town`.", ephemeral=True)
            return
        shops = list_shops(town)
        e = discord.Embed(title=f"Shops in {town}", color=discord.Color.blurple())
        if shops:
            for s in shops:
                e.add_field(name=s, value="—", inline=False)
        else:
            e.description = "_No shops found._"
        await interaction.response.send_message(embed=e)
        return

    # list items in shop (now as embeds)
    if action == "list":
        if not (town and shop):
            await interaction.response.send_message("Provide `town` and `shop`.", ephemeral=True)
            return
        items = list_items(town, shop)  # -> [(name, Money, qty|None), ...]
        embeds = _shop_embeds(town, shop, items)

        if len(embeds) == 1:
            await interaction.response.send_message(embed=embeds[0])
        else:
            await interaction.response.send_message(embed=embeds[0])
            for e in embeds[1:]:
                await interaction.followup.send(embed=e)
        return

    # buy
    if action == "buy":
        if not (town and shop and item and character):
            await interaction.response.send_message("Provide `town`, `shop`, `item`, and `character`.", ephemeral=True)
            return

        uid = resolve_character(character)
        if not uid:
            await interaction.response.send_message(f"❌ No link for **{character}**.", ephemeral=True)
            return
        if uid != interaction.user.id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You can only spend from your own character’s wallet.", ephemeral=True)
            return

        key = normalize_display_name(character)
        qty = max(1, quantity or 1)

        # Check price & stock first
        price = get_price(town, shop, item)
        if not price:
            await interaction.response.send_message(f"❌ **{item}** not found in **{shop}**.", ephemeral=True)
            return
        total = price * qty

        # Ensure player has funds before we decrement shop stock
        if not subtract_if_enough(uid, total, key=key):
            bal = get_balance(uid, key=key)
            await interaction.response.send_message(
                f"❌ Not enough funds. Price: **{total.pretty_long()}**, {character} balance: **{bal.pretty_long()}**",
                ephemeral=True
            )
            return

        # Try to decrement stock atomically
        from shop import buy_item as stock_buy
        charged = stock_buy(town, shop, item, qty)
        if charged is None:
            # refund if stock failed
            add_balance(uid, total, key=key)
            await interaction.response.send_message(
                f"❌ **{item}** is out of stock (requested {qty}).", ephemeral=True
            )
            return

        # Post receipt & confirm
        await post_receipt(
            bot, interaction.guild, uid, key,
            delta=Money(knuts=-charged.knuts),  # negative for withdrawal receipt display
            new_balance=get_balance(uid, key=key),
            reason=f"Shop: {item} ×{qty} @ {shop}, {town}"
        )
        await interaction.response.send_message(
            f"🧾 **{character}** bought **{qty}× {item}** from **{shop}** (*{town}*) for **{total.pretty_long()}**."
        )
        return

    await interaction.response.send_message(
        "Actions: `towns`, `shops`, `list`, `buy`.", ephemeral=True
    )

# --- Staff: add or change an item ---
@bot.tree.command(name="shop_set", description="(Staff) Create or update an item in a shop.")
@app_commands.guilds(TEST_GUILD)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    town="Town (e.g., Diagon Alley)",
    shop="Shop (e.g., Ollivanders)",
    item="Item name",
    price="e.g., 2g 5s or 30s",
    qty="Stock quantity (omit for unlimited)"
)
async def shop_set_cmd(interaction: discord.Interaction, town: str, shop: str, item: str, price: str, qty: int | None = None):
    from shop import set_item
    try:
        money = Money.from_str(price)
    except Exception:
        await interaction.response.send_message("❌ Price format not recognized. Try `2g 5s` or `30s`.", ephemeral=True)
        return
    set_item(town, shop, item, money, qty)
    stock_text = "∞" if qty is None else str(qty)
    await interaction.response.send_message(
        f"✅ Set **{item}** in **{shop}** (*{town}*) at **{money.pretty_long()}** (stock: {stock_text}).",
        ephemeral=True
    )

# --- Staff: restock ---
@bot.tree.command(name="shop_restock", description="(Staff) Adjust stock by delta (use negative to reduce).")
@app_commands.guilds(TEST_GUILD)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(town="Town", shop="Shop", item="Item name", delta="Change in quantity (e.g., 10 or -3)")
async def shop_restock_cmd(interaction: discord.Interaction, town: str, shop: str, item: str, delta: int):
    from shop import restock_item
    ok = restock_item(town, shop, item, delta)
    if not ok:
        await interaction.response.send_message("❌ Item not found.", ephemeral=True)
        return
    await interaction.response.send_message(f"🔧 Stock updated for **{item}** in **{shop}** (*{town}*).", ephemeral=True)

# --- Staff: change price ---
@bot.tree.command(name="shop_price", description="(Staff) Change an item's price.")
@app_commands.guilds(TEST_GUILD)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(town="Town", shop="Shop", item="Item name", price="New price, e.g., 1g 10s")
async def shop_price_cmd(interaction: discord.Interaction, town: str, shop: str, item: str, price: str):
    from shop import set_price
    try:
        money = Money.from_str(price)
    except Exception:
        await interaction.response.send_message("❌ Price format not recognized.", ephemeral=True)
        return
    ok = set_price(town, shop, item, money)
    if not ok:
        await interaction.response.send_message("❌ Item not found.", ephemeral=True)
        return
    await interaction.response.send_message(f"💲 Price updated for **{item}** in **{shop}** (*{town}*): {money.pretty_long()}", ephemeral=True)

# --- Staff: remove item ---
@bot.tree.command(name="shop_remove", description="(Staff) Remove an item from a shop.")
@app_commands.guilds(TEST_GUILD)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(town="Town", shop="Shop", item="Item name")
async def shop_remove_cmd(interaction: discord.Interaction, town: str, shop: str, item: str):
    from shop import remove_item
    ok = remove_item(town, shop, item)
    if not ok:
        await interaction.response.send_message("❌ Item not found.", ephemeral=True)
        return
    await interaction.response.send_message(f"🗑️ Removed **{item}** from **{shop}** (*{town}*).", ephemeral=True)

# Leaderboards
@bot.tree.command(name="leaderboard", description="Top balances (user totals or character wallets).")
@app_commands.guilds(TEST_GUILD)
@app_commands.describe(scope="Choose 'users' for total per player or 'characters' for individual wallets")
async def leaderboard_cmd(interaction: discord.Interaction, scope: str = "users"):
    scope = (scope or "users").lower().strip()
    if scope not in {"users", "characters"}:
        await interaction.response.send_message("❌ scope must be `users` or `characters`.", ephemeral=True)
        return

    if scope == "users":
        top = top_users(10)
        if not top:
            await interaction.response.send_message("No balances yet.", ephemeral=True)
            return
        lines = []
        for rank, (uid, money) in enumerate(top, 1):
            member = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)
            lines.append(f"{rank}. {member.display_name} — {money.pretty_long()}")
        await interaction.response.send_message("**Top Players (total across characters)**\n" + "\n".join(lines))
    else:
        top = top_characters(10)
        if not top:
            await interaction.response.send_message("No character wallets yet.", ephemeral=True)
            return
        lines = []
        for rank, (uid, char_key, money) in enumerate(top, 1):
            member = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)
            lines.append(f"{rank}. {member.display_name} — **{char_key}** — {money.pretty_long()}")
        await interaction.response.send_message("**Top Characters (individual wallets)**\n" + "\n".join(lines))

# Staff award (per-character)
@bot.tree.command(name="award_character", description="Award a character (staff only).")
@app_commands.guilds(TEST_GUILD)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    character="Character display name",
    amount="e.g., 2g 5s",
    note="Optional note for the receipt (defaults to 'Staff Reward')"
)
async def award_character_cmd(
    interaction: discord.Interaction,
    character: str,
    amount: str,
    note: str | None = None
):
    uid = resolve_character(character)
    if not uid:
        await interaction.response.send_message(f"❌ No link for **{character}**.", ephemeral=True)
        return

    key = normalize_display_name(character)

    try:
        money = Money.from_str(amount)
    except Exception:
        await interaction.response.send_message(
            "❌ Amount format not recognized. Try `2g 5s`, `15s`, or `300k`.",
            ephemeral=True
        )
        return

    reason = note.strip() if (note and note.strip()) else "Staff Reward"

    await deposit_to_character(
        interaction.guild,  # interaction_or_guild
        interaction.guild,  # guild
        uid,
        key,
        money,
        reason=reason
    )

    await interaction.response.send_message(
        f"✅ Awarded **{money.pretty_long()}** to **{character}**. Note: _{reason}_",
        ephemeral=True
    )


# Creates a Vault
@bot.tree.command(name="vault_create", description="Create a Gringotts Vault thread for a character.")
@app_commands.guilds(TEST_GUILD)
@app_commands.describe(character="Character display name for this vault")
async def vault_create_cmd(interaction: discord.Interaction, character: str):
    uid = resolve_character(character)
    if not uid:
        await interaction.response.send_message(f"❌ No link for **{character}**.", ephemeral=True)
        return
    if uid != interaction.user.id and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ You can only create a vault for your own character.", ephemeral=True)
        return

    key = normalize_display_name(character)

    # Forum lookup
    forum = interaction.guild.get_channel(GRINGOTTS_FORUM_ID) or await interaction.guild.fetch_channel(GRINGOTTS_FORUM_ID)
    if not isinstance(forum, discord.ForumChannel):
        await interaction.response.send_message("❌ GRINGOTTS_FORUM_ID is not a Forum channel.", ephemeral=True)
        return

    # If already has a vault, short-circuit and show it (and its number)
    from vaults import get_vault_info, set_vault_info, generate_vault_number
    existing = get_vault_info(uid, key)
    if existing:
        try:
            ch = await interaction.guild.fetch_channel(existing["thread_id"])
        except discord.errors.NotFound:
            await interaction.response.send_message(
                f"⚠️ Vault record exists for **{character}** but the channel/thread was deleted. Creating a new vault...",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"🔗 Vault already exists for **{character}** — {ch.mention} (Vault **#{existing['vault_number']}**).",
                ephemeral=True
            )
            return

    # Create new vault number and thread
    vault_number = generate_vault_number()
    title = f"Gringotts Vault {vault_number} - {character}"
    welcome_embed = discord.Embed(
        title=f"Vault #{vault_number} — {character}",
        description=(
            f"🏦 Welcome, {character}, to your vault, courtesy of Gringotts Bank.\n\n"
            f"Your Gringotts Vault Number: **{vault_number}**\n"
            f"All deposits and withdrawals will be recorded here."
        ),
        color=discord.Color.gold()
    )
    welcome_embed.set_footer(text="Gringotts Wizarding Bank")

    created = await forum.create_thread(name=title, embed=welcome_embed)
    thread_obj = created if hasattr(created, "id") else getattr(created, "thread", None)

    if not thread_obj:
        await interaction.response.send_message("❌ Unexpected response creating thread.", ephemeral=True)
        return

    # Persist mapping (thread id + vault number)
    set_vault_info(uid, key, thread_obj.id, vault_number)

    # Opening balance line
    bal = get_balance(uid, key=key)
    balance_embed = discord.Embed(
        title="Opening Balance",
        description=f"**{bal.pretty_long()}**",
        color=discord.Color.green()
    )
    balance_embed.set_footer(text="Gringotts Ledger Entry")
    await thread_obj.send(embed=balance_embed)

    await interaction.response.send_message(
        f"🏦 Vault created: {thread_obj.mention} (Vault **{vault_number}**)",
        ephemeral=True
    )

# Link a Vault
@bot.tree.command(name="vault_link", description="Link an existing Gringotts forum thread to a character. Auto-generates a vault # if needed.")
@app_commands.guilds(TEST_GUILD)
@app_commands.describe(
    character="Character display name",
    thread_id="Forum thread ID (copy link; the big number at the end)"
)
async def vault_link_cmd(interaction: discord.Interaction, character: str, thread_id: str):
    uid = resolve_character(character)
    if not uid:
        await interaction.response.send_message(f"❌ No link for **{character}**.", ephemeral=True)
        return
    if uid != interaction.user.id and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ You can only link a vault for your own character.", ephemeral=True)
        return

    key = normalize_display_name(character)

    # Parse the thread id
    try:
        tid = int(thread_id)
    except ValueError:
        await interaction.response.send_message("❌ `thread_id` must be a number.", ephemeral=True)
        return

    # Fetch and validate the channel
    try:
        ch = await interaction.guild.fetch_channel(tid)
    except Exception:
        await interaction.response.send_message("❌ That thread ID doesn't exist in this server.", ephemeral=True)
        return

    # Must be a thread (forum post = PublicThread)
    if not isinstance(ch, (discord.Thread,)):
        await interaction.response.send_message("❌ That ID is not a thread. Please supply a forum thread ID.", ephemeral=True)
        return

    # Optional: ensure it’s inside the Gringotts forum
    if ch.parent_id != GRINGOTTS_FORUM_ID:
        await interaction.response.send_message(
            "⚠️ That thread isn’t in the configured Gringotts forum. Link anyway? (Ask staff to move it.)",
            ephemeral=True
        )

    # Persist mapping (reuse existing number, or auto-generate)
    from vaults import get_vault_info, set_vault_info, generate_vault_number
    existing = get_vault_info(uid, key)
    if existing and existing.get("vault_number"):
        vault_number = existing["vault_number"]
    else:
        vault_number = generate_vault_number()

    set_vault_info(uid, key, ch.id, vault_number)

    await interaction.response.send_message(
        f"🔗 Linked **{character}** to {ch.mention}. Vault **#{vault_number}**.",
        ephemeral=True
    )

# Unlink a Vault
@bot.tree.command(name="vault_unlink", description="Unlink the vault thread from a character.")
@app_commands.guilds(TEST_GUILD)
@app_commands.describe(character="Character display name")
async def vault_unlink_cmd(interaction: discord.Interaction, character: str):
    uid = resolve_character(character)
    if not uid:
        await interaction.response.send_message(f"❌ No link for **{character}**.", ephemeral=True)
        return
    if uid != interaction.user.id and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ You can only unlink your own character's vault.", ephemeral=True)
        return
    key = normalize_display_name(character)
    ok = unlink_vault_thread(uid, key)
    await interaction.response.send_message("🧹 Unlinked." if ok else "Nothing was linked.", ephemeral=True)

@bot.tree.command(name="tip", description="Send money from one of your characters to another character.")
@app_commands.guilds(TEST_GUILD)
@app_commands.describe(
    from_character="Your character sending the tip",
    to_character="Recipient character",
    amount="e.g., 10s or 2g"
)
async def tip_cmd(interaction: discord.Interaction, from_character: str, to_character: str, amount: str):
    from_uid = resolve_character(from_character)
    to_uid = resolve_character(to_character)
    if not from_uid:
        await interaction.response.send_message(f"❌ No link for **{from_character}**.", ephemeral=True)
        return
    if not to_uid:
        await interaction.response.send_message(f"❌ No link for **{to_character}**.", ephemeral=True)
        return
    if from_uid != interaction.user.id:
        await interaction.response.send_message("❌ You can only send from your own character.", ephemeral=True)
        return

    from_key = normalize_display_name(from_character)
    to_key   = normalize_display_name(to_character)

    try:
        m = Money.from_str(amount)
    except Exception:
        await interaction.response.send_message("❌ Amount format not recognized. Try `2g 5s`, `15s`, or `300k`.", ephemeral=True)
        return
    if m.knuts <= 0:
        await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
        return

    ok = await withdraw_from_character(interaction.guild, interaction.guild, from_uid, from_key, m, reason=f"Tip to {to_character}")
    if not ok:
        bal = get_balance(from_uid, key=from_key)
        await interaction.response.send_message(
            f"❌ {from_character} lacks funds. Balance **{bal.pretty_long()}**.", ephemeral=True
        )
        return

    await deposit_to_character(interaction.guild, interaction.guild, to_uid, to_key, m, reason=f"Tip from {from_character}")
    to_member = interaction.guild.get_member(to_uid) or await interaction.guild.fetch_member(to_uid)
    await interaction.response.send_message(
        f"🤝 **{from_character}** sent **{m.pretty_long()}** to **{to_character}** ({to_member.mention})."
    )

# ---------------- HELP (slash) ----------------
class HelpSection(app_commands.Transform):
    pass

HELP_CHOICES = [
    app_commands.Choice(name="All", value="all"),
    app_commands.Choice(name="Linking Characters", value="linking"),
    app_commands.Choice(name="Vault", value="vault"),
    app_commands.Choice(name="Shopping", value="shopping"),
]

def _help_embed_linking() -> discord.Embed:
    e = discord.Embed(
        title="Linking Characters — Tupperbox/Webhook",
        description=("Link your **Tupperbox display name** to your Discord account so RP posts credit the right wallet."),
        color=discord.Color.blurple(),
    )
    e.add_field(
        name="Commands",
        value=(
            "• `/link_character name:\"Character Name\"`\n"
            "• `/unlink_character name:\"Character Name\"`\n"
            "• `/who_is name:\"Character Name\"`\n"
            "• `/balance` — shows all your characters & totals\n"
            "• `/char_balance name:\"Character Name\"` — one wallet"
        ),
        inline=False,
    )
    e.add_field(
        name="Tips",
        value=(
            "• The name must match the **display name** on the Tupperbox message.\n"
            "• We normalize fancy text/emojis internally, so names like `𝔏𝔲𝔠𝔦𝔲𝔰 ✨` still match.\n"
            "• RP earnings only count for **proxied** (webhook) posts in approved channels."
        ),
        inline=False,
    )
    return e

def _help_embed_vault() -> discord.Embed:
    e = discord.Embed(
        title="Gringotts Vaults — Forum Threads",
        description=("Each character can have a **Vault thread** in the Gringotts forum. The bot posts **receipts** there."),
        color=discord.Color.gold(),
    )
    e.add_field(
        name="Commands",
        value=(
            "• `/vault_create character:\"Name\"`\n"
            "• `/vault_link character:\"Name\" thread_id:\"123456789\"`\n"
            "• `/vault_unlink character:\"Name\"`"
        ),
        inline=False,
    )
    e.add_field(
        name="How receipts work",
        value=(
            "• **RP earnings** are **batched**: the bot credits immediately but posts a **daily summary at 00:05 UTC**.\n"
            "• **Shop purchases**, **tips**, and **staff awards** post receipts immediately."
        ),
        inline=False,
    )
    return e

def _help_embed_shopping() -> discord.Embed:
    e = discord.Embed(
        title="Shops & Inventory",
        description=("Browse towns/shops, check stock & prices, and buy items with a character wallet."),
        color=discord.Color.green(),
    )
    e.add_field(
        name="Player Commands",
        value=(
            "• `/shop action:towns`\n"
            "• `/shop action:shops town:\"Hogsmeade\"`\n"
            "• `/shop action:list town:\"Hogsmeade\" shop:\"Honeydukes\"`\n"
            "• `/shop action:buy town:\"…\" shop:\"…\" item:\"…\" quantity:1 character:\"Your Char\"`"
        ),
        inline=False,
    )
    e.add_field(
        name="Staff (Manage Server) Commands",
        value=(
            "• `/shop_set town:\"…\" shop:\"…\" item:\"…\" price:\"2g 5s\" qty:10`\n"
            "• `/shop_price town:\"…\" shop:\"…\" item:\"…\" price:\"…\"`\n"
            "• `/shop_restock town:\"…\" shop:\"…\" item:\"…\" delta:10`\n"
            "• `/shop_remove town:\"…\" shop:\"…\"`"
        ),
        inline=False,
    )
    e.add_field(
        name="Notes",
        value=(
            "• Purchases withdraw from the **selected character** only.\n"
            "• If stock fails during a buy, the bot **refunds automatically**.\n"
            "• Prices use the canon conversion: `1 galleon = 17 sickles = 493 knuts`."
        ),
        inline=False,
    )
    return e

@bot.tree.command(name="help", description="How to use the bot (linking, vault, shopping).")
@app_commands.guilds(TEST_GUILD)
@app_commands.describe(section="Pick a section or 'All'")
@app_commands.choices(section=HELP_CHOICES)
async def help_cmd(interaction: discord.Interaction, section: app_commands.Choice[str] | None = None):
    sel = (section.value if section else "all").lower()
    embeds: list[discord.Embed] = []
    if sel in ("all", "linking"):
        embeds.append(_help_embed_linking())
    if sel in ("all", "vault"):
        embeds.append(_help_embed_vault())
    if sel in ("all", "shopping"):
        embeds.append(_help_embed_shopping())

    if not embeds:
        await interaction.response.send_message("No help available.", ephemeral=True)
        return

    await interaction.response.send_message(embed=embeds[0], ephemeral=True)
    for e in embeds[1:]:
        await interaction.followup.send(embed=e, ephemeral=True)

# ---------------- WEEKLY PAYDAY ----------------
@tasks.loop(hours=168)  # weekly
async def weekly_payday():
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue
            pay = Money(0)
            if discord.utils.get(member.roles, name=ADULT_ROLE_NAME):
                pay += BASE_WEEKLY_PAY
            for role in member.roles:
                bonus = JOB_BONUSES.get(role.name)
                if bonus:
                    pay += bonus
            if pay.knuts > 0:
                add_balance(member.id, pay)
                try:
                    await member.send(f"💰 Payday! You received **{pay.pretty_long()}**.")
                except discord.Forbidden:
                    pass

@tasks.loop(time=datetime.time(hour=0, minute=5, tzinfo=datetime.timezone.utc))
async def flush_daily_receipts():
    """
    Post one summary receipt per character for yesterday's RP earnings (UTC).
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    prev_day = (now_utc - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    data = _pending_load()
    day_bucket = data.get(prev_day)
    if not day_bucket:
        logger.debug(f"flush:no_data_for_day day={prev_day}")
        return

    flush_count = 0

    for gkey, entries in day_bucket.items():
        guild_id = int(gkey)
        guild = discord.utils.get(bot.guilds, id=guild_id)
        if not guild:
            logger.warning(f"flush:missing_guild guild_id={guild_id} day={prev_day}")
            continue

        for uck, rec in entries.items():
            try:
                uid_str, char_key = uck.split(":", 1)
            except ValueError:
                logger.warning(f"flush:bad_key uck='{uck}' day={prev_day}")
                continue
            user_id = int(uid_str)
            total_knuts = int(rec.get("knuts", 0))
            msg_count = int(rec.get("count", 0))
            if total_knuts <= 0 or msg_count <= 0:
                logger.debug(f"flush:zero_totals user_id={user_id} char_key='{char_key}' day={prev_day}")
                continue

            delta = Money(knuts=total_knuts)
            new_bal = get_balance(user_id, key=char_key)
            reason = f"Daily RP earnings ({msg_count} message{'s' if msg_count != 1 else ''}) for {prev_day} UTC"

            try:
                await post_receipt(bot, guild, user_id, char_key, delta, new_bal, reason=reason)
                flush_count += 1
            except Exception:
                logger.exception(
                    f"flush:post_receipt_failed user_id={user_id} char_key='{char_key}' "
                    f"guild_id={guild_id} day={prev_day} delta_knuts={total_knuts}"
                )

    data.pop(prev_day, None)
    _pending_save_atomic(data)
    logger.info(f"flush:completed day={prev_day} posted_receipts={flush_count}")

# ---- Autocomplete helpers for /shop and staff cmds ----
from discord import app_commands as _ac
from shop import list_towns as _list_towns, list_shops as _list_shops, list_items as _list_items

async def _ac_towns(_: discord.Interaction, current: str):
    cur = (current or "").lower()
    return [_ac.Choice(name=t, value=t) for t in _list_towns() if cur in t.lower()][:25]

async def _ac_shops(interaction: discord.Interaction, current: str):
    town = None
    try:
        town = interaction.namespace.town
    except Exception:
        pass
    shops = _list_shops(town) if town else []
    cur = (current or "").lower()
    return [_ac.Choice(name=s, value=s) for s in shops if cur in s.lower()][:25]

async def _ac_items(interaction: discord.Interaction, current: str):
    town = getattr(interaction.namespace, "town", None)
    shop = getattr(interaction.namespace, "shop", None)
    items = _list_items(town, shop) if (town and shop) else []
    cur = (current or "").lower()
    names = [n for (n, _p, _q) in items]
    return [_ac.Choice(name=n, value=n) for n in names if cur in n.lower()][:25]

@shop_cmd.autocomplete("town")
async def _ac_shop_town(interaction, current: str):
    return await _ac_towns(interaction, current)

@shop_cmd.autocomplete("shop")
async def _ac_shop_shop(interaction, current: str):
    return await _ac_shops(interaction, current)

@shop_cmd.autocomplete("item")
async def _ac_shop_item(interaction, current: str):
    return await _ac_items(interaction, current)

@shop_set_cmd.autocomplete("town")
async def _ac_set_town(interaction, current: str):
    return await _ac_towns(interaction, current)

@shop_set_cmd.autocomplete("shop")
async def _ac_set_shop(interaction, current: str):
    return await _ac_shops(interaction, current)

@shop_set_cmd.autocomplete("item")
async def _ac_set_item(interaction, current: str):
    return await _ac_items(interaction, current)

@shop_restock_cmd.autocomplete("town")
async def _ac_restock_town(interaction, current: str):
    return await _ac_towns(interaction, current)

@shop_restock_cmd.autocomplete("shop")
async def _ac_restock_shop(interaction, current: str):
    return await _ac_shops(interaction, current)

@shop_restock_cmd.autocomplete("item")
async def _ac_restock_item(interaction, current: str):
    return await _ac_items(interaction, current)

@shop_price_cmd.autocomplete("town")
async def _ac_price_town(interaction, current: str):
    return await _ac_towns(interaction, current)

@shop_price_cmd.autocomplete("shop")
async def _ac_price_shop(interaction, current: str):
    return await _ac_shops(interaction, current)

@shop_price_cmd.autocomplete("item")
async def _ac_price_item(interaction, current: str):
    return await _ac_items(interaction, current)

@shop_remove_cmd.autocomplete("town")
async def _ac_remove_town(interaction, current: str):
    return await _ac_towns(interaction, current)

@shop_remove_cmd.autocomplete("shop")
async def _ac_remove_shop(interaction, current: str):
    return await _ac_shops(interaction, current)

@shop_remove_cmd.autocomplete("item")
async def _ac_remove_item(interaction, current: str):
    return await _ac_items(interaction, current)

# ---------------- RUN ----------------
if __name__ == "__main__":
    logger.info("Starting bot process...")
    bot.run(TOKEN)
