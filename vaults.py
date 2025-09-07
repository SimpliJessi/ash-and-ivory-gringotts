# vaults.py
from __future__ import annotations
import os, json, tempfile, random, threading
from typing import Optional, Dict
import discord
from currency import Money
import logging

# Child logger (parent configured in bot.py)
logger = logging.getLogger("gringotts.vaults")

# ---------- Storage paths ----------
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)

VAULTS_FILE = os.path.join(DATA_DIR, "vaults.json")
_lock = threading.Lock()

# ---------------- I/O ----------------
def _load() -> dict:
    if not os.path.exists(VAULTS_FILE):
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"load:missing_file path='{VAULTS_FILE}' -> {{}}")
        return {}
    try:
        with open(VAULTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"load:ok path='{VAULTS_FILE}' keys={len(data)}")
            return data
    except json.JSONDecodeError as e:
        logger.exception(f"load:json_decode_error file='{VAULTS_FILE}': {e}")
        return {}
    except Exception as e:
        logger.exception(f"load:error file='{VAULTS_FILE}': {e}")
        return {}

def _save_atomic(data: dict) -> None:
    d = os.path.dirname(VAULTS_FILE) or "."
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, dir=d, encoding="utf-8") as tmp:
            json.dump(data, tmp, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, VAULTS_FILE)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"save:ok path='{VAULTS_FILE}' keys={len(data)}")
    except Exception as e:
        logger.exception(f"save:error path='{VAULTS_FILE}': {e}")
        try:
            if "tmp_path" in locals() and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

# ---------------- Helpers ----------------
def _key(user_id: int) -> str:
    return str(user_id)

def _ensure_user(data: dict, user_id: int) -> dict:
    u = data.get(_key(user_id))
    if not isinstance(u, dict):
        u = {}
        data[_key(user_id)] = u
    return u

# ---------------- Public API ----------------
def get_vault_info(user_id: int, char_key: str) -> Optional[Dict]:
    """
    Returns a dict with {"thread_id": int, "vault_number": str} or None.
    """
    data = _load()
    u = data.get(_key(user_id), {})
    info = u.get(char_key)
    if isinstance(info, dict) and "thread_id" in info and "vault_number" in info:
        out = {"thread_id": int(info["thread_id"]), "vault_number": str(info["vault_number"])}
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"get_vault_info:hit user_id={user_id} char_key='{char_key}' thread_id={out['thread_id']} vault='{out['vault_number']}'")
        return out
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"get_vault_info:miss user_id={user_id} char_key='{char_key}'")
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
    prev = u.get(char_key)
    u[char_key] = {"thread_id": int(thread_id), "vault_number": str(vault_number)}
    _save_atomic(data)
    if prev is None:
        logger.info(f"vault:set user_id={user_id} char_key='{char_key}' thread_id={thread_id} vault='{vault_number}'")
    else:
        logger.info(f"vault:update user_id={user_id} char_key='{char_key}' thread_id={thread_id} vault='{vault_number}' (was {prev})")

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
    if existing:
        logger.info(f"vault:link_thread user_id={user_id} char_key='{char_key}' thread_id={thread_id} vault='{vn}' (kept existing number)")
    else:
        logger.info(f"vault:link_thread user_id={user_id} char_key='{char_key}' thread_id={thread_id} vault='{vn}' (generated number)")

def unlink_vault_thread(user_id: int, char_key: str) -> bool:
    data = _load()
    u = data.get(_key(user_id), {})
    if char_key in u:
        removed = u.pop(char_key, None)
        data[_key(user_id)] = u
        _save_atomic(data)
        logger.info(f"vault:unlink user_id={user_id} char_key='{char_key}' removed={removed}")
        return True
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"vault:unlink_miss user_id={user_id} char_key='{char_key}'")
    return False

def generate_vault_number() -> str:
    """
    Generate a pseudo-random Gringotts vault number.
    Format: 1-3 digits (e.g., "845").
    """
    vn = str(random.randint(1, 999))
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"vault:generate_number -> '{vn}'")
    return vn

# ---------------- Discord side-effect: posting receipts ----------------
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
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"receipt:skip_no_vault user_id={user_id} char_key='{char_key}' delta_knuts={delta.knuts}")
        return

    thread_id = info["thread_id"]
    vault_number = info["vault_number"]

    try:
        channel = await guild.fetch_channel(thread_id)
    except Exception as e:
        logger.exception(f"receipt:fetch_channel_failed thread_id={thread_id} user_id={user_id} char_key='{char_key}': {e}")
        return

    title = "Deposit" if delta.knuts >= 0 else "Withdrawal"
    sign = "+" if delta.knuts >= 0 else "−"

    embed = discord.Embed(title=f"Gringotts {title}", color=0x9b59b6)
    embed.add_field(name="Vault", value=f"#{vault_number}", inline=True)
    embed.add_field(
        name="Amount",
        value=f"{sign}{(delta if delta.knuts >= 0 else Money(-delta.knuts)).pretty_long()}",
        inline=True
    )
    embed.add_field(name="New Balance", value=new_balance.pretty_long(), inline=True)
    if reason:
        embed.add_field(name="Note", value=reason, inline=False)

    try:
        await channel.send(embed=embed)
        logger.info(
            f"receipt:posted thread_id={thread_id} user_id={user_id} char_key='{char_key}' "
            f"delta_knuts={delta.knuts} new_knuts={new_balance.knuts} reason='{reason or ''}'"
        )
    except Exception as e:
        logger.exception(
            f"receipt:send_failed thread_id={thread_id} user_id={user_id} char_key='{char_key}' "
            f"delta_knuts={delta.knuts} new_knuts={new_balance.knuts}: {e}"
        )
