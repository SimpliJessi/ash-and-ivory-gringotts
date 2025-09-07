# links.py
"""
Character ↔ User links for Tupperbox/webhook messages.
Maps a character display name (case-insensitive, Unicode-normalized) to a Discord user_id.

Features
- Unicode NFKC fold, lowercasing, trimming
- Removes variation selectors & zero-width joiners
- Strips combining marks (decorations above letters)
- Removes emoji/pictographs & common symbols
- Drops trailing bracketed decorations (e.g., "Name [Status]")
- Splits on common separators (|, —, " - ", •, ·, –) and keeps the left side
- Keeps only letters/digits/space/'/- and collapses whitespace
- Thread-safe JSON storage with atomic writes
- Aliases: map multiple display variants to the same user
"""

from __future__ import annotations
import json
import os
import re
import threading
import tempfile
import unicodedata
from typing import Dict, Optional

# at the top of each file that writes JSON
import os

DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)

# ---------- Storage path ----------
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "character_links.json")


_lock = threading.Lock()

# ---------- Normalization helpers ----------
def _normalize_or_fail(name: str) -> str:
    key = normalize_display_name(name)
    if not key:
        # Fallback: try a “softer” normalization so pure emoji names don’t collapse to empty
        soft = _nfkc_lower(name)
        soft = re.sub(r"\s+", " ", soft).strip()
        # Keep only letters/digits/spaces/'/-
        soft = re.sub(r"[^a-z0-9\s'\-]", "", soft)
        soft = re.sub(r"\s+", " ", soft).strip()
        if soft:
            return soft
        # Still empty? Refuse to link.
        raise ValueError("Character name normalizes to empty; please add at least one letter/number.")
    return key


def _nfkc_lower(s: str) -> str:
    """Base normalize + lowercase + trim."""
    return unicodedata.normalize("NFKC", (s or "")).lower().strip()

# Common separators/decorations seen in Tupperbox display names
_DECOR_DELIMS = ["|", "—", " - ", "•", "·", "–"]
# Strip *trailing* bracketed decorations like "Name [Something]" or "Name (Something)"
_BRACKETS_RE = re.compile(r"\s*[\(\[\{].*?[\)\]\}]\s*$")

# Emoji / pictograph ranges (broad but safe)
_EMOJI_RE = re.compile(
    "["                                 # start char class
    "\U0001F1E6-\U0001F1FF"             # flags
    "\U0001F300-\U0001F5FF"             # symbols & pictographs
    "\U0001F600-\U0001F64F"             # emoticons
    "\U0001F680-\U0001F6FF"             # transport & map
    "\U0001F700-\U0001F77F"             # alchemical
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002700-\U000027BF"             # dingbats
    "\U00002600-\U000026FF"             # misc symbols
    "\U00002B00-\U00002BFF"             # arrows etc
    "]",
    flags=re.UNICODE
)

def _strip_variations_and_zwj(s: str) -> str:
    """Remove Variation Selectors and Zero-Width Joiner."""
    return s.replace("\u200d", "").replace("\ufe0e", "").replace("\ufe0f", "")

def _strip_combining(s: str) -> str:
    """Remove combining marks (accent overlays etc.)."""
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")

def normalize_display_name(name: str) -> str:
    """
    Smart normalizer for Tupperbox names:
    - NFKC fold, lowercase, trim
    - drop VS/ZWJ, combining marks
    - remove emoji/pictographs
    - strip trailing bracketed bits
    - split on common separators and take left-most
    - keep only letters/digits/space/'/-, collapse spaces
    """
    s = _nfkc_lower(name)
    s = _strip_variations_and_zwj(s)
    s = _strip_combining(s)
    s = _EMOJI_RE.sub("", s)

    # Strip trailing bracketed decorations repeatedly
    while True:
        new_s = _BRACKETS_RE.sub("", s)
        if new_s == s:
            break
        s = new_s

    # Split on known separators and take the left part
    for d in _DECOR_DELIMS:
        if d in s:
            s = s.split(d, 1)[0]

    # Keep only safe chars (letters, digits, space, apostrophe, hyphen)
    s = re.sub(r"[^a-z0-9\s'\-]", " ", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _norm_variants(name: str) -> list[str]:
    """
    Candidate keys to try when resolving:
    - base NFKC-lowered
    - smart normalized (decorations removed)
    """
    a = _nfkc_lower(name)
    b = normalize_display_name(name)
    return [a] if a == b else [a, b]

# ---------- Disk I/O (thread-safe & atomic) ----------

def _load() -> Dict[str, int]:
    """Load the mapping {normalized_name: user_id} from disk."""
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return {}
    # Ensure canonical types
    return {str(k): int(v) for k, v in data.items()}

def _atomic_write(data: Dict[str, int]) -> None:
    """Write JSON atomically to avoid partial/corrupt files."""
    dir_ = os.path.dirname(DB_FILE) or "."
    with tempfile.NamedTemporaryFile("w", delete=False, dir=dir_, encoding="utf-8") as tmp:
        json.dump(data, tmp, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, DB_FILE)

def _save(data: Dict[str, int]) -> None:
    _atomic_write(data)

# ---------- Public API ----------

def link_character(name: str, user_id: int) -> None:
    key = _normalize_or_fail(name)
    with _lock:
        data = _load()
        data[key] = int(user_id)
        _save(data)

def link_alias(alias_name: str, user_id: int) -> None:
    key = _normalize_or_fail(alias_name)
    with _lock:
        data = _load()
        data[key] = int(user_id)
        _save(data)


def unlink_character(name: str) -> bool:
    """Remove a link. Returns True if it existed."""
    candidates = _norm_variants(name)
    with _lock:
        data = _load()
        existed = False
        for k in candidates:
            if k in data:
                existed = True
                data.pop(k, None)
        if existed:
            _save(data)
        return existed

def resolve_character(name: str) -> Optional[int]:
    with _lock:
        data = _load()
    # Try smart-normalized first (most strict), then soft-lowered variant
    for k in (normalize_display_name(name), _nfkc_lower(name)):
        if k and k in data:
            return int(data[k])
    return None


def all_links() -> Dict[str, int]:
    """
    Return a copy of all mappings {normalized_character_name: user_id}.
    Keys are smart-normalized display names.
    """
    with _lock:
        return dict(_load())

# ---------- Optional: debugging helper ----------

def debug_dump() -> str:
    """Return a human-readable dump of all links (one per line)."""
    with _lock:
        data = _load()
    return "\n".join(f"{name} -> {uid}" for name, uid in sorted(data.items()))