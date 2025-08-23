# shop.py
"""
Shop inventory for the currency bot.

Exposes:
- list_items() -> list[tuple[str, Money]]
- get_price(name: str) -> Money | None

Optional helpers:
- add_item(name: str, price_str: str) -> None
- remove_item(name: str) -> bool
- rename_item(old: str, new: str) -> bool
"""

from __future__ import annotations
from typing import Dict, Tuple, List
from currency import Money

# ---------------- Inventory ----------------
# Adjust items to your server’s lore. Keys are lowercase item IDs/names.
ITEMS: Dict[str, Money] = {
    "butterbeer": Money.from_str("2s"),
    "school_robes": Money.from_str("3g"),
    "wand_polish": Money.from_str("15k"),
    "owl_treats": Money.from_str("5s 10k"),
    "quills": Money.from_str("20k"),
    "ink_bottle": Money.from_str("1s 5k"),
    "cauldron": Money.from_str("7g"),
}

# Optional: aliases (so users can type variations)
ALIASES: Dict[str, str] = {
    "robes": "school_robes",
    "polish": "wand_polish",
    "ink": "ink_bottle",
}


# ---------------- Public API ----------------
def list_items() -> List[Tuple[str, Money]]:
    """Return a sorted list of (item_name, price)."""
    return sorted(ITEMS.items(), key=lambda kv: kv[0])


def get_price(name: str) -> Money | None:
    """Look up the price for an item by name or alias (case-insensitive)."""
    if not name:
        return None
    key = name.strip().lower()
    key = ALIASES.get(key, key)  # resolve alias
    return ITEMS.get(key)


# ---------------- Optional management helpers ----------------
def add_item(name: str, price_str: str) -> None:
    """
    Add/update an item. `price_str` can be '2g 5s', '15s', '300k', etc.
    Example: add_item("snitch", "10g")
    """
    key = name.strip().lower()
    ITEMS[key] = Money.from_str(price_str)


def remove_item(name: str) -> bool:
    """Remove an item by name or alias. Returns True if removed."""
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    return ITEMS.pop(key, None) is not None


def rename_item(old: str, new: str) -> bool:
    """Rename an item key; keeps the same price. Returns True if successful."""
    old_key = ALIASES.get(old.strip().lower(), old.strip().lower())
    if old_key not in ITEMS:
        return False
    new_key = new.strip().lower()
    if new_key in ITEMS:
        return False
    ITEMS[new_key] = ITEMS.pop(old_key)
    # update aliases that pointed to old_key
    for a, target in list(ALIASES.items()):
        if target == old_key:
            ALIASES[a] = new_key
    return True
