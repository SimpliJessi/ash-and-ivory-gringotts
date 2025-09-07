# bank.py
"""
JSON-backed balance store for your Discord currency bot.

Key features
------------
- Thread-safe, atomic writes
- Per-character balances via an optional `key` (e.g., "user_id:character_key")
- Safe helpers for adding, subtracting, transferring, and listing balances
- Leaderboards for users (total across characters) and for individual characters

Storage format
--------------
balances.json is a flat dict:
{
  "123456789012345678": 150,                 # optional user-level wallet (knuts)
  "123456789012345678:dominic sullivan": 90, # per-character wallet (knuts)
  "234567890123456789:amelia bones": 493
}
"""

from __future__ import annotations
import json
import os
import threading
import tempfile
from typing import Dict, List, Tuple, Optional
from currency import Money
import logging

# Child logger (parent configured in bot.py)
logger = logging.getLogger("gringotts.bank")

# ---------- Storage path ----------
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "balances.json")

_lock = threading.Lock()

# ---------------- Internal I/O ----------------
def _load() -> Dict[str, int]:
    if not os.path.exists(DB_FILE):
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"load:missing_file path='{DB_FILE}' -> {{}}")
        return {}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        logger.exception(f"load:json_decode_error file='{DB_FILE}': {e}")
        return {}
    except Exception as e:
        logger.exception(f"load:error file='{DB_FILE}': {e}")
        return {}

    # Ensure canonical types
    out: Dict[str, int] = {}
    bad = 0
    for k, v in data.items():
        try:
            out[str(k)] = int(v)
        except Exception:
            bad += 1
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"load:ok count={len(out)} bad={bad}")
    return out


def _atomic_write(data: Dict[str, int]) -> None:
    """Write JSON atomically to avoid partial/corrupt files."""
    dir_ = os.path.dirname(DB_FILE) or "."
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, dir=dir_, encoding="utf-8") as tmp:
            json.dump(data, tmp, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, DB_FILE)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"save:ok path='{DB_FILE}' count={len(data)}")
    except Exception as e:
        logger.exception(f"save:error path='{DB_FILE}': {e}")
        try:
            if "tmp_path" in locals() and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _save(data: Dict[str, int]) -> None:
    _atomic_write(data)


def _k(user_id: int, key: Optional[str]) -> str:
    """Build the storage key. When `key` is None, it's a user-level wallet."""
    if not key:
        return f"{user_id}"
    return f"{user_id}:{key.strip().lower()}"

# ---------------- Core API (user/character) ----------------
def get_balance(user_id: int, key: Optional[str] = None) -> Money:
    """Return the balance for (user[, character key]) as Money."""
    kk = _k(user_id, key)
    with _lock:
        data = _load()
        v = int(data.get(kk, 0))
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"get_balance key='{kk}' knuts={v}")
    return Money(knuts=v)


def set_balance(user_id: int, amount: Money, key: Optional[str] = None) -> None:
    """Set the balance to an exact amount (overwrites)."""
    kk = _k(user_id, key)
    with _lock:
        data = _load()
        data[kk] = int(amount.knuts)
        _save(data)
    logger.info(f"set_balance key='{kk}' knuts={int(amount.knuts)}")


def add_balance(user_id: int, amount: Money, key: Optional[str] = None) -> None:
    """Add (or subtract if negative) to the balance."""
    if amount.knuts == 0:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"add_balance:noop zero_amount user_id={user_id} key='{key}'")
        return
    kk = _k(user_id, key)
    with _lock:
        data = _load()
        cur = int(data.get(kk, 0))
        data[kk] = cur + int(amount.knuts)
        _save(data)
    logger.info(
        f"add_balance key='{kk}' delta_knuts={int(amount.knuts)} new_knuts={cur + int(amount.knuts)} prev_knuts={cur}"
    )


def subtract_if_enough(user_id: int, price: Money, key: Optional[str] = None) -> bool:
    """Subtract `price` iff there is enough balance. Returns True on success."""
    need = int(price.knuts)
    if need <= 0:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"subtract:trivial need={need} user_id={user_id} key='{key}' -> True")
        return True

    kk = _k(user_id, key)
    with _lock:
        data = _load()
        cur = int(data.get(kk, 0))
        if cur < need:
            logger.info(f"subtract:insufficient key='{kk}' need={need} have={cur} -> False")
            return False
        data[kk] = cur - need
        _save(data)
        newv = cur - need
    logger.info(f"subtract:ok key='{kk}' need={need} new_knuts={newv} prev_knuts={cur}")
    return True


def transfer(
    sender_id: int,
    receiver_id: int,
    amount: Money,
    from_key: Optional[str] = None,
    to_key: Optional[str] = None,
) -> bool:
    """
    Atomic transfer between wallets.
    - from_key / to_key let you move between specific character wallets or user-level wallets.
    - Returns True if successful, False otherwise (insufficient funds or invalid amount).
    """
    amt = int(amount.knuts)
    if amt <= 0 or (sender_id == receiver_id and (from_key or "") == (to_key or "")):
        logger.info(
            f"transfer:invalid amt={amt} sender={sender_id} receiver={receiver_id} from='{from_key}' to='{to_key}'"
        )
        return False

    s_key = _k(sender_id, from_key)
    r_key = _k(receiver_id, to_key)
    with _lock:
        data = _load()
        s_cur = int(data.get(s_key, 0))
        if s_cur < amt:
            logger.info(
                f"transfer:insufficient sender_key='{s_key}' have={s_cur} need={amt} -> False"
            )
            return False

        data[s_key] = s_cur - amt
        data[r_key] = int(data.get(r_key, 0)) + amt
        _save(data)
        r_new = int(data[r_key])
    logger.info(
        f"transfer:ok sender_key='{s_key}' -> receiver_key='{r_key}' amt={amt} "
        f"sender_new={s_cur - amt} receiver_new={r_new}"
    )
    return True

# ---------------- Introspection & Utilities ----------------
def user_total(user_id: int) -> Money:
    """
    Sum all balances belonging to a user across user-level and all character keys.
    """
    uid_prefix = f"{user_id}"
    total = 0
    with _lock:
        data = _load()
    for k, v in data.items():
        if k == uid_prefix or k.startswith(uid_prefix + ":"):
            total += int(v)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"user_total user_id={user_id} knuts={total}")
    return Money(knuts=total)


def character_balances(user_id: int) -> Dict[str, Money]:
    """
    Return a dict of {character_key(lowercased): Money} for the given user.
    (Does not include the user-level wallet with no key.)
    """
    uid_prefix = f"{user_id}:"
    out: Dict[str, Money] = {}
    with _lock:
        data = _load()
    for k, v in data.items():
        if k.startswith(uid_prefix):
            char_key = k[len(uid_prefix):]
            out[char_key] = Money(knuts=int(v))
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"character_balances user_id={user_id} count={len(out)}")
    return out


def rename_character_key(user_id: int, old_key: str, new_key: str) -> bool:
    """
    Rename a character's key for a user.
    Returns True if successful (old existed and new did not).
    """
    oldk = _k(user_id, old_key)
    newk = _k(user_id, new_key)
    with _lock:
        data = _load()
        if oldk not in data or newk in data:
            logger.info(
                f"rename_key:failed user_id={user_id} old='{oldk}' new='{newk}' "
                f"exists_old={oldk in data} exists_new={newk in data}"
            )
            return False
        data[newk] = data.pop(oldk)
        _save(data)
    logger.info(f"rename_key:ok user_id={user_id} old='{oldk}' new='{newk}'")
    return True

# ---------------- Leaderboards ----------------
def top_users(n: int = 10) -> List[Tuple[int, Money]]:
    """
    Top users by TOTAL balance across all their wallets.
    Returns a list of (user_id, Money).
    """
    with _lock:
        data = _load()

    totals: Dict[int, int] = {}
    for k, v in data.items():
        uid_str = k.split(":", 1)[0]
        uid = int(uid_str)
        totals[uid] = totals.get(uid, 0) + int(v)

    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    res = [(uid, Money(knuts=knuts)) for uid, knuts in ranked[:max(1, n)]]
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"top_users n={n} returned={len(res)}")
    return res


def top_characters(n: int = 10) -> List[Tuple[int, str, Money]]:
    """
    Top character wallets (not aggregated).
    Returns a list of (user_id, character_key, Money).
    """
    out: List[Tuple[int, str, Money]] = []
    with _lock:
        data = _load()
    for k, v in data.items():
        if ":" in k:
            uid_str, char_key = k.split(":", 1)
            out.append((int(uid_str), char_key, Money(knuts=int(v))))
    out.sort(key=lambda t: t[2].knuts, reverse=True)
    res = out[:max(1, n)]
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"top_characters n={n} returned={len(res)}")
    return res
