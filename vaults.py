# vaults.py
from __future__ import annotations
import os, json, tempfile, random
from typing import Optional, Tuple, Dict
import discord
from currency import Money

# at the top of each file that writes JSON
import os

DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)

# then build your file paths from DATA_DIR, e.g.:
DB_FILE = os.path.join(DATA_DIR, "balances.json")
# character_links.json, shops.json, vaults.json, pending_receipts.json, etc. all the same way


DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "vaults.json")


def _load() -> dict:
    if not os.path.exists(VAULTS_FILE):
        return {}
    try:
        with open(VAULTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}

def _save_atomic(data: dict) -> None:
    d = os.path.dirname(VAULTS_FILE) or "."
    tmp = os.path.join(d, f".tmp_{os.path.basename(VAULTS_FILE)}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, VAULTS_FILE)

def _key(user_id: int) -> str:
    return str(user_id)

def _ensure_user(data: dict, user_id: int) -> dict:
    u = data.get(_key(user_id))
    if not isinstance(u, dict):
        u = {}
        data[_key(user_id)] = u
    return u

def get_vault_info(user_id: int, char_key: str) -> Optional[Dict]:
    """
    Returns a dict with {"thread_id": int, "vault_number": str} or None.
    """
    data = _load()
    u = data.get(_key(user_id), {})
    info = u.get(char_key)
    if isinstance(info, dict) and "thread_id" in info and "vault_number" in info:
        # coerce types
        info = {"thread_id": int(info["thread_id"]), "vault_number": str(info["vault_number"])}
        return info
    return None

def get_vault_thread(user_id: int, char_key: str) -> Optional[int]:
    info = get_vault_info(user_id, char_key)
    return info["thread_id"] if info else None

def get_vault_number(user_id: int, char_key: str) -> Optional[str]:
    info = get_vault_info(user_id, char_key)
    return info["vault_number"] if info else None

def set_vault_info(user_id: int, char_key: str, thread_id: int, vault_number: str) -> None:
    data = _load()
    u = _ensure_user(data, user_id)
    u[char_key] = {"thread_id": int(thread_id), "vault_number": str(vault_number)}
    _save_atomic(data)

def set_vault_thread(user_id: int, char_key: str, thread_id: int) -> None:
    """
    Backward-compat: if called without a number, preserve existing number or create one.
    """
    data = _load()
    u = _ensure_user(data, user_id)
    existing = u.get(char_key) or {}
    vn = str(existing.get("vault_number") or generate_vault_number())
    u[char_key] = {"thread_id": int(thread_id), "vault_number": vn}
    _save_atomic(data)

def unlink_vault_thread(user_id: int, char_key: str) -> bool:
    data = _load()
    u = data.get(_key(user_id), {})
    if char_key in u:
        u.pop(char_key, None)
        data[_key(user_id)] = u
        _save_atomic(data)
        return True
    return False

def generate_vault_number() -> str:
    """
    Generate a pseudo-random Gringotts vault number.
    Format: 1-3 digits (e.g., "845").
    """
    return str(random.randint(1, 999))

async def post_receipt(
    bot: discord.Client,
    guild: discord.Guild,
    user_id: int,
    char_key: str,
    delta: Money,
    new_balance: Money,
    reason: str | None = None,
) -> None:
    """If a vault thread exists, post a Gringotts-style receipt embed (includes Vault # if known)."""
    info = get_vault_info(user_id, char_key)
    if not info:
        return
    thread_id = info["thread_id"]
    vault_number = info["vault_number"]

    try:
        channel = await guild.fetch_channel(thread_id)
    except Exception:
        return

    title = "Deposit" if delta.knuts >= 0 else "Withdrawal"
    sign = "+" if delta.knuts >= 0 else "−"
    embed = discord.Embed(title=f"Gringotts {title}", color=0x9b59b6)
    embed.add_field(name="Vault", value=f"#{vault_number}", inline=True)
    embed.add_field(name="Amount", value=f"{sign}{(delta if delta.knuts>=0 else Money(-delta.knuts)).pretty_long()}", inline=True)
    embed.add_field(name="New Balance", value=new_balance.pretty_long(), inline=True)
    if reason:
        embed.add_field(name="Note", value=reason, inline=False)
    await channel.send(embed=embed)
