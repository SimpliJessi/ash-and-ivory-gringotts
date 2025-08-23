# bank.py
"""
Simple JSON-backed balance store for your Discord currency bot.

- Stores KNUT totals per user_id (as strings) in balances.json
- Thread-safe with a process-local lock
- Atomic writes to avoid file corruption
"""

from __future__ import annotations
import json, os, threading, tempfile
from typing import Dict, List, Tuple
from currency import Money

# Put the DB next to this file (not dependent on current working dir)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(_BASE_DIR, "balances.json")

_lock = threading.Lock()


def _load() -> Dict[str, int]:
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            # Corrupt or empty file? Start fresh.
            return {}
    # Ensure ints
    return {str(k): int(v) for k, v in data.items()}


def _atomic_write(data: Dict[str, int]) -> None:
    # Write to a temp file then replace to avoid partial writes
    dir_ = os.path.dirname(DB_FILE) or "."
    with tempfile.NamedTemporaryFile("w", delete=False, dir=dir_, encoding="utf-8") as tmp:
        json.dump(data, tmp, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, DB_FILE)


def _save(data: Dict[str, int]) -> None:
    _atomic_write(data)


def get_balance(user_id: int) -> Money:
    """Return the user's balance as a Money object (0 if missing)."""
    with _lock:
        data = _load()
        return Money(knuts=int(data.get(str(user_id), 0)))


def set_balance(user_id: int, amount: Money) -> None:
    """Set the user's balance to an exact amount (in knuts)."""
    with _lock:
        data = _load()
        data[str(user_id)] = int(amount.knuts)
        _save(data)


def add_balance(user_id: int, amount: Money) -> None:
    """Add (or subtract, if negative) an amount to the user's balance."""
    if amount.knuts == 0:
        return
    with _lock:
        data = _load()
        key = str(user_id)
        cur = int(data.get(key, 0))
        data[key] = cur + int(amount.knuts)
        _save(data)


def subtract_if_enough(user_id: int, price: Money) -> bool:
    """
    Subtract price if the user has enough funds.
    Returns True on success, False if insufficient.
    """
    need = int(price.knuts)
    if need <= 0:
        return True
    with _lock:
        data = _load()
        key = str(user_id)
        cur = int(data.get(key, 0))
        if cur < need:
            return False
        data[key] = cur - need
        _save(data)
        return True


def transfer(sender_id: int, receiver_id: int, amount: Money) -> bool:
    """
    Atomic transfer from sender to receiver.
    Returns True if successful; False if sender had insufficient funds or amount <= 0.
    """
    amt = int(amount.knuts)
    if amt <= 0 or sender_id == receiver_id:
        return False
    with _lock:
        data = _load()
        s_key, r_key = str(sender_id), str(receiver_id)
        s_cur = int(data.get(s_key, 0))
        if s_cur < amt:
            return False
        data[s_key] = s_cur - amt
        data[r_key] = int(data.get(r_key, 0)) + amt
        _save(data)
        return True


def top_balances(n: int = 10) -> List[Tuple[int, Money]]:
    """
    Return a leaderboard list of (user_id, Money) for the top N users.
    Note: user_ids are returned as ints; you'll still need to resolve them to members in Discord.
    """
    with _lock:
        data = _load()
    items = sorted(((int(uid), Money(knuts=knuts)) for uid, knuts in data.items()),
                   key=lambda x: x[1].knuts, reverse=True)
    return items[:max(1, n)]

