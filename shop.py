# shop.py
from __future__ import annotations
import os, json, tempfile
from typing import Dict, List, Optional, Tuple
from currency import Money
import logging

# Child logger (parent configured in bot.py)
logger = logging.getLogger("gringotts.shop")

# ---------- Storage path ----------
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)

SHOPS_FILE = os.path.join(DATA_DIR, "shops.json")

# ---------------- Storage helpers ----------------
def _load() -> dict:
    if not os.path.exists(SHOPS_FILE):
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"load:missing_file path='{SHOPS_FILE}' -> {{}}")
        return {}
    try:
        with open(SHOPS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if logger.isEnabledFor(logging.DEBUG):
                # rough counts for visibility
                towns = len(data)
                shops = sum(len(v) for v in data.values() if isinstance(v, dict))
                items = sum(len(v.get("items", {})) for t in data.values() for v in ([t] if isinstance(t, dict) else []) for _k in [1])
                logger.debug(f"load:ok path='{SHOPS_FILE}' towns={towns} shops≈{shops}")
            return data
    except json.JSONDecodeError as e:
        logger.exception(f"load:json_decode_error file='{SHOPS_FILE}': {e}")
        return {}
    except Exception as e:
        logger.exception(f"load:error file='{SHOPS_FILE}': {e}")
        return {}

def _save_atomic(data: dict) -> None:
    d = os.path.dirname(SHOPS_FILE) or "."
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, dir=d, encoding="utf-8") as tmp:
            json.dump(data, tmp, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, SHOPS_FILE)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"save:ok path='{SHOPS_FILE}'")
    except Exception as e:
        logger.exception(f"save:error path='{SHOPS_FILE}': {e}")
        try:
            if "tmp_path" in locals() and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

def _ensure_path(data: dict, town: str, shop: str) -> dict:
    t = data.setdefault(town, {})
    s = t.setdefault(shop, {})
    inv = s.setdefault("items", {})  # item -> {"price_knuts": int, "qty": int|None}
    return inv

# ---------------- Public API ----------------
def list_towns() -> List[str]:
    data = _load()
    towns = sorted(data.keys())
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"list_towns -> {len(towns)} towns")
    return towns

def list_shops(town: str) -> List[str]:
    data = _load()
    t = data.get(town, {})
    shops = sorted(t.keys())
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"list_shops town='{town}' -> {len(shops)} shops")
    return shops

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
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"list_items town='{town}' shop='{shop}' -> {len(out)} items")
    return out

def get_item(town: str, shop: str, item: str) -> Optional[dict]:
    data = _load()
    meta = data.get(town, {}).get(shop, {}).get("items", {}).get(item)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"get_item town='{town}' shop='{shop}' item='{item}' -> {'hit' if meta else 'miss'}")
    return meta

def get_price(town: str, shop: str, item: str) -> Optional[Money]:
    meta = get_item(town, shop, item)
    if not meta:
        return None
    price = Money(knuts=int(meta.get("price_knuts", 0)))
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"get_price town='{town}' shop='{shop}' item='{item}' -> {price.knuts} knuts")
    return price

def set_item(town: str, shop: str, item: str, price: Money, qty: Optional[int]) -> None:
    """
    Create or replace an item. qty=None => unlimited stock.
    """
    data = _load()
    inv = _ensure_path(data, town, shop)
    prev = inv.get(item)
    inv[item] = {"price_knuts": int(price.knuts), "qty": None if qty is None else int(qty)}
    _save_atomic(data)
    if prev is None:
        logger.info(f"item:set town='{town}' shop='{shop}' item='{item}' price_knuts={int(price.knuts)} qty={'∞' if qty is None else int(qty)}")
    else:
        logger.info(f"item:update town='{town}' shop='{shop}' item='{item}' price_knuts={int(price.knuts)} qty={'∞' if qty is None else int(qty)} prev={prev}")

def restock_item(town: str, shop: str, item: str, delta_qty: int) -> bool:
    data = _load()
    inv = data.get(town, {}).get(shop, {}).get("items", {})
    meta = inv.get(item)
    if not meta:
        logger.info(f"restock:miss town='{town}' shop='{shop}' item='{item}' delta={delta_qty}")
        return False
    if meta.get("qty") is None:
        logger.info(f"restock:unlimited town='{town}' shop='{shop}' item='{item}' delta={delta_qty} -> no-op (∞)")
        return True  # unlimited, nothing to change
    old = int(meta.get("qty", 0))
    new = max(0, old + int(delta_qty))
    meta["qty"] = new
    _save_atomic(data)
    logger.info(f"restock:ok town='{town}' shop='{shop}' item='{item}' delta={delta_qty} qty_old={old} qty_new={new}")
    return True

def set_price(town: str, shop: str, item: str, price: Money) -> bool:
    data = _load()
    inv = data.get(town, {}).get(shop, {}).get("items", {})
    meta = inv.get(item)
    if not meta:
        logger.info(f"price:miss town='{town}' shop='{shop}' item='{item}'")
        return False
    old = int(meta.get("price_knuts", 0))
    meta["price_knuts"] = int(price.knuts)
    _save_atomic(data)
    logger.info(f"price:ok town='{town}' shop='{shop}' item='{item}' old_knuts={old} new_knuts={int(price.knuts)}")
    return True

def remove_item(town: str, shop: str, item: str) -> bool:
    data = _load()
    inv = data.get(town, {}).get(shop, {}).get("items", {})
    if item in inv:
        removed = inv.pop(item, None)
        _save_atomic(data)
        logger.info(f"item:remove town='{town}' shop='{shop}' item='{item}' removed={removed}")
        return True
    logger.info(f"item:remove_miss town='{town}' shop='{shop}' item='{item}'")
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
        logger.info(f"buy:miss town='{town}' shop='{shop}' item='{item}' qty={qty}")
        return None

    price_knuts = int(meta.get("price_knuts", 0))
    stock = meta.get("qty", None)  # None = unlimited
    if stock is not None:
        stock = int(stock)
        if stock < qty:
            logger.info(f"buy:insufficient town='{town}' shop='{shop}' item='{item}' have={stock} need={qty}")
            return None
        meta["qty"] = stock - qty

    total_knuts = price_knuts * qty
    total = Money(knuts=total_knuts)
    _save_atomic(data)

    logger.info(
        f"buy:ok town='{town}' shop='{shop}' item='{item}' qty={qty} "
        f"unit_knuts={price_knuts} total_knuts={total_knuts} new_stock={'∞' if meta.get('qty') is None else meta.get('qty')}"
    )
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
