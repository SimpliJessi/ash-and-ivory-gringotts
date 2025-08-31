# bot.py
import os

from dotenv import load_dotenv
load_dotenv()  # loads variables from .env into process env

import os, time
import discord
import random
import datetime
import json

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

# at the top of each file that writes JSON
import os

DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)

# then build your file paths from DATA_DIR, e.g.:
DB_FILE = os.path.join(DATA_DIR, "balances.json")
# character_links.json, shops.json, vaults.json, pending_receipts.json, etc. all the same way


# keep with your other _BASE_DIR usage
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
PENDING_FILE = os.path.join(DATA_DIR, "pending_receipts.json")


# ---------------- CONFIG ----------------
# Keep your token out of source code. Set an env var: setx DISCORD_BOT_TOKEN "YOUR_TOKEN"
import os

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN env var not set.")


# Your server (guild-only sync = instant command availability)
TEST_GUILD_ID = 1393623189241991168
TEST_GUILD = discord.Object(id=TEST_GUILD_ID)

# Channels where messages earn money (include forum PARENT channel IDs)
ALLOWED_CHANNEL_IDS: set[int] = {
    1393688264531247277, # Hogwarts Grounds
    1393683936009257141, # Hogwarts Castle
    1393687041212153886, # Slytherin Common Room
    1393685488338210917, # Gryffindor Common Room
    1393687315162857493, # Ravenclaw Common Room
    1393687608684580874, # Hufflepuff Common Room
    1393689121666502666, # Hogsmeade Village
    1407084037843189882, # The Highlands
    1393690088835125330, # Wizarding London
    1406803659202887862, # Chaos Testing Center
}

# Channels for Gringotts Bank
GRINGOTTS_FORUM_ID = 1393690306410450975  # test forum ID

EARN_PER_MESSAGE = Money.from_str("7k")     # payout per qualifying message
EARN_COOLDOWN_SECONDS = 15                  # cooldown PER CHARACTER
MIN_MESSAGE_LENGTH = 250                    # minimum characters to count

# Weekly pay (currently user-level, not per-character)
ADULT_ROLE_NAME = "Adult"
BASE_WEEKLY_PAY = Money.from_str("1g")
JOB_BONUSES: dict[str, Money] = {
    # "Professor": Money.from_str("2g"),
    # "Shopkeeper": Money.from_str("1g 10s"),
}

STARTER_FUNDS = Money.from_str("50g")   # starting balance for a newly linked character

# ---------------- BOT SETUP ----------------
intents = discord.Intents.default()
intents.message_content = True   # to read message content length
intents.members = True           # for payday role checks
bot = commands.Bot(command_prefix="!", intents=intents)

# Per-character cooldown: (user_id, normalized_char_name) -> last_time
last_earn_at: dict[tuple[int, str], float] = {}

# ---------------- HELPERS ----------------

# ---- shop embed helpers ----
def _format_stock(qty: int | None) -> str:
    return "∞" if qty is None else str(qty)

def _shop_embeds(town: str, shop: str, items: list[tuple[str, Money, int | None]]) -> list[discord.Embed]:
    """
    Build one or more embeds for a shop's inventory.
    Discord limit: max 25 fields per embed, so we chunk if needed.
    """
    if not items:
        e = discord.Embed(
            title=f"{shop} — {town}",
            description="_No items in stock._",
            color=discord.Color.blurple(),
        )
        return [e]

    # chunk items into pages of 25
    pages: list[list[tuple[str, Money, int | None]]] = []
    CHUNK = 25
    for i in range(0, len(items), CHUNK):
        pages.append(items[i:i+CHUNK])

    embeds: list[discord.Embed] = []
    total_items = len(items)
    for idx, chunk in enumerate(pages, start=1):
        desc = f"**{shop}** — *{town}*\nShowing {len(chunk)} of {total_items} item(s)."
        e = discord.Embed(description=desc, color=discord.Color.blurple())
        e.set_author(name="Shopkeeper Retail Registry")
        if len(pages) > 1:
            e.set_footer(text=f"Page {idx}/{len(pages)}")

        for name, price, qty in chunk:
            e.add_field(
                name=name,
                value=f"**{price.pretty_long()}**  ·  stock: `{_format_stock(qty)}`",
                inline=False
            )
        embeds.append(e)
    return embeds

# ---- pending receipts helpers ----
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
    Schema:
      {
        "YYYY-MM-DD": {
          "<guild_id>": {
            "<user_id>:<char_key>": {"knuts": int, "count": int}
          }
        }
      }
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

def is_earning_channel(message: discord.Message) -> bool:
    """Allow by channel ID, its parent (e.g., forum or text channel), or their category."""
    ch = message.channel  # can be TextChannel, ForumChannel, Thread, etc.

    ids_to_check: set[int] = set()

    # The channel itself
    ids_to_check.add(getattr(ch, "id", None))

    # Immediate parent (thread -> parent text/forum channel)
    ids_to_check.add(getattr(ch, "parent_id", None))

    # Category of this channel (TextChannel/ForumChannel have category_id)
    ids_to_check.add(getattr(ch, "category_id", None))

    # If it's a thread, also check the parent channel's own category
    parent = getattr(ch, "parent", None)
    if parent is not None:
        ids_to_check.add(getattr(parent, "id", None))
        ids_to_check.add(getattr(parent, "parent_id", None))     # usually None, but harmless
        ids_to_check.add(getattr(parent, "category_id", None))

    # Filter out Nones and test membership
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
    try:
        bot.tree.copy_global_to(guild=TEST_GUILD)  # OK even if you have no global cmds
        synced = await bot.tree.sync(guild=TEST_GUILD)
        print(f"[DEV] Synced {len(synced)} commands to {TEST_GUILD_ID}: {[c.name for c in synced]}")
    except Exception as e:
        print(f"[SYNC ERROR] {type(e).__name__}: {e}")

    if not weekly_payday.is_running():
        weekly_payday.start()
    if not flush_daily_receipts.is_running():
        flush_daily_receipts.start()


@bot.event
async def on_message(message: discord.Message):
    # Ignore ourselves
    if message.author.id == bot.user.id:
        return

    # Option B: only award for Tupperbox/webhook messages
    if not message.webhook_id:
        await bot.process_commands(message)
        return

    # Derive normalized character key and resolve owner
    raw_name = message.author.name or ""
    char_key = normalize_display_name(raw_name)
    linked_uid = resolve_character(raw_name)

    if not linked_uid:
        # Unlinked tupper: skip quietly (or send a one-time nudge if you want)
        await bot.process_commands(message)
        return

    if not is_earning_channel(message):
        await bot.process_commands(message)
        return

    content = (message.content or "").strip()
    if len(content) < MIN_MESSAGE_LENGTH:
        await bot.process_commands(message)
        return

    if not can_payout(linked_uid, char_key):
        await bot.process_commands(message)
        return

    # Credit immediately (so balances stay current)...
    add_balance(linked_uid, EARN_PER_MESSAGE, key=char_key)
    # ...but queue the receipt for the daily UTC summary
    queue_rp_earning(message.guild.id, linked_uid, char_key, EARN_PER_MESSAGE.knuts)

    print(f"[EARN] +{EARN_PER_MESSAGE.pretty_long()} -> {linked_uid}:{char_key} (queued for daily receipt)")

    await bot.process_commands(message)

# ---------------- SLASH COMMANDS ----------------
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
        except Exception:
            pass  # silent if no vault linked yet

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
            # if multiple pages, send the first then follow up with the rest
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
@app_commands.describe(character="Character display name", amount="e.g., 2g 5s")
async def award_character_cmd(interaction: discord.Interaction, character: str, amount: str):
    uid = resolve_character(character)
    if not uid:
        await interaction.response.send_message(f"❌ No link for **{character}**.", ephemeral=True)
        return
    key = normalize_display_name(character)
    try:
        money = Money.from_str(amount)
    except Exception:
        await interaction.response.send_message("❌ Amount format not recognized. Try `2g 5s`, `15s`, or `300k`.", ephemeral=True)
        return
    # Deposit ONCE (and post immediate receipt)
    await deposit_to_character(interaction.guild, interaction.guild, uid, key, money, reason="Staff award")
    await interaction.response.send_message(f"✅ Awarded **{money.pretty_long()}** to **{character}**.")

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
from discord import app_commands

class HelpSection(app_commands.Transform):
    # optional nicer display in UI (not strictly needed)
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
        description=(
            "Link your **Tupperbox display name** to your Discord account so RP posts credit the right wallet."
        ),
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
        description=(
            "Each character can have a **Vault thread** in the Gringotts forum. "
            "The bot posts **receipts** (deposits/withdrawals) there."
        ),
        color=discord.Color.gold(),
    )
    e.add_field(
        name="Commands",
        value=(
            "• `/vault_create character:\"Name\"` — makes a vault thread with a welcome embed\n"
            "• `/vault_link character:\"Name\" thread_id:\"123456789\"` — link an existing forum thread\n"
            "• `/vault_unlink character:\"Name\"` — unlink the vault"
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
        description=(
            "Browse towns/shops, check stock & prices, and buy items with a character wallet. "
            "Some items have **unlimited stock (∞)**; others are limited."
        ),
        color=discord.Color.green(),
    )
    e.add_field(
        name="Player Commands",
        value=(
            "• `/shop action:towns` — list towns\n"
            "• `/shop action:shops town:\"Hogsmeade\"` — list shops in a town\n"
            "• `/shop action:list town:\"Hogsmeade\" shop:\"Honeydukes\"` — view inventory (embed)\n"
            "• `/shop action:buy town:\"…\" shop:\"…\" item:\"…\" quantity:1 character:\"Your Char\"`"
        ),
        inline=False,
    )
    e.add_field(
        name="Staff (Manage Server) Commands",
        value=(
            "• `/shop_set town:\"…\" shop:\"…\" item:\"…\" price:\"2g 5s\" qty:10` — add/update item\n"
            "• `/shop_price town:\"…\" shop:\"…\" item:\"…\" price:\"…\"` — change price\n"
            "• `/shop_restock town:\"…\" shop:\"…\" item:\"…\" delta:10` — adjust stock (+/-)\n"
            "• `/shop_remove town:\"…\" shop:\"…\" item:\"…\"` — remove item\n"
            "• `/shop_seed` — load demo catalog (if enabled)"
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

    # Send the first embed, then follow-ups if we have multiple pages
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
                # Deposits to USER-LEVEL wallet (no character key) by design.
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
    # summarize the FULL previous day
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    prev_day = (now_utc - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    data = _pending_load()
    day_bucket = data.get(prev_day)
    if not day_bucket:
        return

    # Walk guilds -> entries
    for gkey, entries in day_bucket.items():
        guild_id = int(gkey)
        guild = discord.utils.get(bot.guilds, id=guild_id)
        if not guild:
            continue

        for uck, rec in entries.items():
            try:
                uid_str, char_key = uck.split(":", 1)
            except ValueError:
                continue
            user_id = int(uid_str)
            total_knuts = int(rec.get("knuts", 0))
            msg_count = int(rec.get("count", 0))
            if total_knuts <= 0 or msg_count <= 0:
                continue

            # Build Money from raw knuts
            delta = Money(knuts=total_knuts)
            new_bal = get_balance(user_id, key=char_key)
            reason = f"Daily RP earnings ({msg_count} message{'s' if msg_count != 1 else ''}) for {prev_day} UTC"

            # Post receipt into the character's vault thread (if linked)
            try:
                await post_receipt(bot, guild, user_id, char_key, delta, new_bal, reason=reason)
            except Exception:
                # Swallow to avoid breaking the whole flush if one post fails
                pass

    # Remove the flushed day and save
    data.pop(prev_day, None)
    _pending_save_atomic(data)

# ---- Autocomplete helpers for /shop and staff cmds ----
from discord import app_commands as _ac
from shop import list_towns as _list_towns, list_shops as _list_shops, list_items as _list_items

async def _ac_towns(_: discord.Interaction, current: str):
    cur = (current or "").lower()
    return [_ac.Choice(name=t, value=t) for t in _list_towns() if cur in t.lower()][:25]

async def _ac_shops(interaction: discord.Interaction, current: str):
    # Try to read the 'town' option from the interaction for better filtering
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

# Wire to /shop
@shop_cmd.autocomplete("town")
async def _ac_shop_town(interaction, current: str):
    return await _ac_towns(interaction, current)

@shop_cmd.autocomplete("shop")
async def _ac_shop_shop(interaction, current: str):
    return await _ac_shops(interaction, current)

@shop_cmd.autocomplete("item")
async def _ac_shop_item(interaction, current: str):
    return await _ac_items(interaction, current)

# Wire to staff cmds
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
    bot.run(TOKEN)