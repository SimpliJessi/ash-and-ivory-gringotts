# currency.py
"""
Wizarding World currency utilities.
- Canon rates: 1 galleon = 17 sickles; 1 sickle = 29 knuts.
- Internal storage = KNUTS (integers) to avoid rounding issues.

Supports parsing inputs like:
    "3g 2s 10k", "2 galleons 5 sickles", "15s", "500k", "1g",
    "2 galleons, 5 sickles and 3 knuts", "-2g", "1,000k", "+3s"
"""

from __future__ import annotations
from dataclasses import dataclass
import re

# at the top of each file that writes JSON
import os

DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)

# then build your file paths from DATA_DIR, e.g.:
DB_FILE = os.path.join(DATA_DIR, "balances.json")
# character_links.json, shops.json, vaults.json, pending_receipts.json, etc. all the same way


# ---- Canon conversion ----
KNUTS_PER_SICKLE: int = 29
SICKLES_PER_GALLEON: int = 17
KNUTS_PER_GALLEON: int = SICKLES_PER_GALLEON * KNUTS_PER_SICKLE  # 493


@dataclass(frozen=True, slots=True)
class Money:
    """Immutable money amount stored in KNUTS."""
    knuts: int = 0

    # ---------- Constructors ----------
    @staticmethod
    def zero() -> "Money":
        return Money(0)

    @staticmethod
    def from_gsk(galleons: int = 0, sickles: int = 0, knuts: int = 0) -> "Money":
        """Create from Galleons/Sickles/Knuts."""
        total = (
            int(galleons) * KNUTS_PER_GALLEON
            + int(sickles) * KNUTS_PER_SICKLE
            + int(knuts)
        )
        return Money(total)

    @staticmethod
    def from_knuts(n: int) -> "Money":
        return Money(int(n))

    @staticmethod
    def from_str(text: str) -> "Money":
        """
        Parse human input into Money.

        Accepts forms like:
            "3g 2s 10k", "2 galleons 5 sickles", "15s", "500k", "1g",
            "2 galleons, 5 sickles and 3 knuts", "-2g", "1,000k", "+3s"

        Rules:
        - Units (case-insensitive): g, gal, galleon(s); s, sickle(s); k, knut(s)
        - Numbers may have + or - sign and commas.
        - Any number without a unit (leftover) is treated as knuts.
        """
        if not isinstance(text, str) or not text.strip():
            return Money.zero()

        t = text.lower()

        # Regex to capture "<num><optional space><unit>"
        # unit groups: g/gal/galleon(s), s/sickle(s), k/knut(s)
        token_re = re.compile(
            r'([+-]?\d[\d,]*)\s*('
            r'g(?:al(?:leons?)?)?|galleons?|galleon|'
            r's(?:ickles?)?|sickles?|sickle|'
            r'k(?:nuts?)?|knuts?|knut'
            r')\b'
        )

        g = s = k = 0
        consumed_spans: list[tuple[int, int]] = []

        for m in token_re.finditer(t):
            num_txt, unit = m.group(1), m.group(2)
            num = int(num_txt.replace(",", ""))
            consumed_spans.append(m.span())

            if unit.startswith("g"):
                g += num
            elif unit.startswith("s"):
                s += num
            elif unit.startswith("k"):
                k += num

        # Handle any leftover bare numbers as knuts
        leftover = t
        # Remove consumed spans from leftover by masking
        mask = bytearray(b'1' * len(t))
        for a, b in consumed_spans:
            for i in range(a, b):
                mask[i] = 0
        cleaned = "".join(ch if mask[i] else " " for i, ch in enumerate(t))
        for part in re.findall(r'[+-]?\d[\d,]*', cleaned):
            k += int(part.replace(",", ""))

        return Money.from_gsk(g, s, k)

    # ---------- Arithmetic ----------
    def __add__(self, other: "Money") -> "Money":
        return Money(self.knuts + other.knuts)

    def __sub__(self, other: "Money") -> "Money":
        return Money(self.knuts - other.knuts)

    def __mul__(self, m: int) -> "Money":
        if not isinstance(m, int):
            raise TypeError("Can only multiply Money by an int.")
        return Money(self.knuts * m)

    __rmul__ = __mul__

    def __floordiv__(self, m: int) -> "Money":
        if not isinstance(m, int):
            raise TypeError("Can only floor-divide Money by an int.")
        return Money(self.knuts // m)

    def __mod__(self, m: int) -> "Money":
        if not isinstance(m, int):
            raise TypeError("Can only modulo Money by an int.")
        return Money(self.knuts % m)

    # ---------- Comparisons ----------
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Money) and self.knuts == other.knuts

    def __lt__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.knuts < other.knuts

    def __le__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.knuts <= other.knuts

    def __gt__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.knuts > other.knuts

    def __ge__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.knuts >= other.knuts

    def __bool__(self) -> bool:
        return self.knuts != 0

    # ---------- Conversions & formatting ----------
    def to_gsk(self) -> tuple[int, int, int]:
        """Return (galleons, sickles, knuts) in canonical mixed form."""
        g, remainder = divmod(self.knuts, KNUTS_PER_GALLEON)
        s, k = divmod(remainder, KNUTS_PER_SICKLE)
        return g, s, k

    def pretty(self) -> str:
        """Short form like '1g 2s 3k' (omits zero parts, never empty)."""
        g, s, k = self.to_gsk()
        parts: list[str] = []
        if g:
            parts.append(f"{g}g")
        if s:
            parts.append(f"{s}s")
        if k or not parts:
            parts.append(f"{k}k")
        return " ".join(parts)

    def pretty_long(self) -> str:
        """Long form like '1 galleon 2 sickles 3 knuts' with pluralization."""
        g, s, k = self.to_gsk()
        parts: list[str] = []
        if g:
            parts.append(f"{g} galleon{'s' if g != 1 else ''}")
        if s:
            parts.append(f"{s} sickle{'s' if s != 1 else ''}")
        if k or not parts:
            parts.append(f"{k} knut{'s' if k != 1 else ''}")
        return " ".join(parts)

    # ---------- Utilities ----------
    def is_negative(self) -> bool:
        return self.knuts < 0

    def clamp_min(self, minimum: int = 0) -> "Money":
        """Clamp to a minimum number of knuts (default 0)."""
        return Money(self.knuts if self.knuts >= minimum else minimum)

    def __str__(self) -> str:
        return self.pretty()

    def __repr__(self) -> str:
        g, s, k = self.to_gsk()
        return f"Money(knuts={self.knuts} -> {g}g {s}s {k}k)"