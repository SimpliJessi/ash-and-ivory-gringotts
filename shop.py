# shop.py
from __future__ import annotations
import os, json, tempfile
from typing import Dict, List, Optional, Tuple
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
DB_FILE = os.path.join(DATA_DIR, "shops.json")


# ---------------- Storage helpers ----------------
def _load() -> dict:
    if not os.path.exists(SHOPS_FILE):
        return {}
    try:
        with open(SHOPS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}

def _save_atomic(data: dict) -> None:
    d = os.path.dirname(SHOPS_FILE) or "."
    tmp = os.path.join(d, f".tmp_{os.path.basename(SHOPS_FILE)}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SHOPS_FILE)

def _ensure_path(data: dict, town: str, shop: str) -> dict:
    t = data.setdefault(town, {})
    s = t.setdefault(shop, {})
    inv = s.setdefault("items", {})  # item -> {"price_knuts": int, "qty": int}
    return inv

# ---------------- Public API ----------------
def list_towns() -> List[str]:
    data = _load()
    return sorted(data.keys())

def list_shops(town: str) -> List[str]:
    data = _load()
    t = data.get(town, {})
    return sorted(t.keys())

def list_items(town: str, shop: str) -> List[Tuple[str, Money, Optional[int]]]:
    """
    Returns [(item_name, price Money, qty or None if unlimited), ...]
    """
    data = _load()
    inv = data.get(town, {}).get(shop, {}).get("items", {})
    out: List[Tuple[str, Money, Optional[int]]] = []
    for name, meta in inv.items():
        price_knuts = int(meta.get("price_knuts", 0))
        qty = meta.get("qty", None)
        out.append((name, Money(knuts=price_knuts), None if qty is None else int(qty)))
    out.sort(key=lambda x: x[0].lower())
    return out

def get_item(town: str, shop: str, item: str) -> Optional[dict]:
    data = _load()
    return data.get(town, {}).get(shop, {}).get("items", {}).get(item)

def get_price(town: str, shop: str, item: str) -> Optional[Money]:
    meta = get_item(town, shop, item)
    if not meta:
        return None
    return Money(knuts=int(meta.get("price_knuts", 0)))

def set_item(town: str, shop: str, item: str, price: Money, qty: Optional[int]) -> None:
    """
    Create or replace an item. qty=None => unlimited stock.
    """
    data = _load()
    inv = _ensure_path(data, town, shop)
    inv[item] = {"price_knuts": int(price.knuts), "qty": None if qty is None else int(qty)}
    _save_atomic(data)

def restock_item(town: str, shop: str, item: str, delta_qty: int) -> bool:
    data = _load()
    inv = data.get(town, {}).get(shop, {}).get("items", {})
    meta = inv.get(item)
    if not meta:
        return False
    if meta.get("qty") is None:
        return True  # unlimited, nothing to change
    meta["qty"] = int(meta.get("qty", 0)) + int(delta_qty)
    if meta["qty"] < 0:
        meta["qty"] = 0
    _save_atomic(data)
    return True

def set_price(town: str, shop: str, item: str, price: Money) -> bool:
    data = _load()
    inv = data.get(town, {}).get(shop, {}).get("items", {})
    meta = inv.get(item)
    if not meta:
        return False
    meta["price_knuts"] = int(price.knuts)
    _save_atomic(data)
    return True

def remove_item(town: str, shop: str, item: str) -> bool:
    data = _load()
    inv = data.get(town, {}).get(shop, {}).get("items", {})
    if item in inv:
        inv.pop(item, None)
        _save_atomic(data)
        return True
    return False

def buy_item(town: str, shop: str, item: str, qty: int) -> Optional[Money]:
    """
    Decrement stock if available and return total price as Money.
    Returns None if item not found or insufficient stock.
    """
    qty = max(1, int(qty))
    data = _load()
    inv = data.get(town, {}).get(shop, {}).get("items", {})
    meta = inv.get(item)
    if not meta:
        return None
    price_knuts = int(meta.get("price_knuts", 0))
    stock = meta.get("qty", None)  # None = unlimited
    if stock is not None:
        if int(stock) < qty:
            return None
        meta["qty"] = int(stock) - qty
    total = Money(knuts=price_knuts * qty)
    _save_atomic(data)
    return total

# --------- Optional: seed helpers for testing ---------
def seed_example_data() -> None:
    """
    One-time convenience to seed a few shops/items. Call manually then delete.
    """
    set_item("Diagon Alley", "Ollivanders", "Wand (phoenix feather)", Money.from_str("7g"), 5)
    set_item("Diagon Alley", "Flourish & Blotts", "2nd-year Potions Textbook", Money.from_str("2g 5s"), 10)
    set_item("Knockturn Alley", "Borgin and Burkes", "Cursed Locket", Money.from_str("15g"), 1)
    set_item("Hogsmeade", "Honeydukes", "Chocolate Frog", Money.from_str("15s"), None)  # unlimited
