"""Deterministic calculator, unit converter, mead ABV, tip/split, and dice/coin tool."""
from __future__ import annotations

import ast
import math
import operator
import random
import re


class CalcError(Exception):
    pass


_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}

_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}


def _replace_number_words(text: str) -> str:
    """Convert spoken English numbers (including compounds like 'twenty one') to digits."""
    # Compound tens + ones first: "twenty one" / "twenty-one" -> "21"
    tens_pat = "|".join(_TENS.keys())
    ones_pat = "|".join(k for k, v in _ONES.items() if 1 <= v <= 9)
    text = re.sub(
        rf"\b({tens_pat})[\s-]+({ones_pat})\b",
        lambda m: str(_TENS[m.group(1)] + _ONES[m.group(2)]),
        text,
    )
    # "two hundred", "a hundred"
    all_small = {**_ONES, **_TENS}
    small_pat = "|".join(all_small.keys())
    text = re.sub(
        rf"\b({small_pat})\s+hundred\b",
        lambda m: str(all_small[m.group(1)] * 100),
        text,
    )
    text = re.sub(r"\ba\s+hundred\b", "100", text)
    # Single tens and ones
    for word, val in {**_TENS, **_ONES}.items():
        text = re.sub(rf"\b{word}\b", str(val), text)
    text = re.sub(r"\bhalf\b", "0.5", text)
    text = re.sub(r"\bquarter\b", "0.25", text)
    return text

# Unit -> (category, factor_to_base, display_label)
# Base units: volume=ml, mass=g, length=m
_UNITS: dict[str, tuple[str, float, str]] = {
    # Volume (base: ml)
    "ml": ("vol", 1.0, "ml"),
    "milliliter": ("vol", 1.0, "ml"),
    "milliliters": ("vol", 1.0, "ml"),
    "l": ("vol", 1000.0, "L"),
    "liter": ("vol", 1000.0, "L"),
    "liters": ("vol", 1000.0, "L"),
    "tsp": ("vol", 4.92892159375, "tsp"),
    "teaspoon": ("vol", 4.92892159375, "tsp"),
    "teaspoons": ("vol", 4.92892159375, "tsp"),
    "tbsp": ("vol", 14.78676478125, "tbsp"),
    "tablespoon": ("vol", 14.78676478125, "tbsp"),
    "tablespoons": ("vol", 14.78676478125, "tbsp"),
    "floz": ("vol", 29.5735295625, "fl oz"),
    "cup": ("vol", 236.5882365, "cups"),
    "cups": ("vol", 236.5882365, "cups"),
    "pt": ("vol", 473.176473, "pt"),
    "pint": ("vol", 473.176473, "pt"),
    "pints": ("vol", 473.176473, "pt"),
    "qt": ("vol", 946.352946, "qt"),
    "quart": ("vol", 946.352946, "qt"),
    "quarts": ("vol", 946.352946, "qt"),
    "gal": ("vol", 3785.411784, "gal"),
    "gallon": ("vol", 3785.411784, "gal"),
    "gallons": ("vol", 3785.411784, "gal"),
    # Mass (base: g)
    "g": ("mass", 1.0, "g"),
    "gram": ("mass", 1.0, "g"),
    "grams": ("mass", 1.0, "g"),
    "kg": ("mass", 1000.0, "kg"),
    "kilogram": ("mass", 1000.0, "kg"),
    "kilograms": ("mass", 1000.0, "kg"),
    "oz": ("mass", 28.349523125, "oz"),
    "ounce": ("mass", 28.349523125, "oz"),
    "ounces": ("mass", 28.349523125, "oz"),
    "lb": ("mass", 453.59237, "lb"),
    "lbs": ("mass", 453.59237, "lb"),
    "pound": ("mass", 453.59237, "lb"),
    "pounds": ("mass", 453.59237, "lb"),
    # Length (base: m)
    "mm": ("len", 0.001, "mm"),
    "millimeter": ("len", 0.001, "mm"),
    "millimeters": ("len", 0.001, "mm"),
    "cm": ("len", 0.01, "cm"),
    "centimeter": ("len", 0.01, "cm"),
    "centimeters": ("len", 0.01, "cm"),
    "m": ("len", 1.0, "m"),
    "meter": ("len", 1.0, "m"),
    "meters": ("len", 1.0, "m"),
    "km": ("len", 1000.0, "km"),
    "kilometer": ("len", 1000.0, "km"),
    "kilometers": ("len", 1000.0, "km"),
    "in": ("len", 0.0254, "in"),
    "inch": ("len", 0.0254, "in"),
    "inches": ("len", 0.0254, "in"),
    "ft": ("len", 0.3048, "ft"),
    "foot": ("len", 0.3048, "ft"),
    "feet": ("len", 0.3048, "ft"),
    "yd": ("len", 0.9144, "yd"),
    "yard": ("len", 0.9144, "yd"),
    "yards": ("len", 0.9144, "yd"),
    "mi": ("len", 1609.344, "mi"),
    "mile": ("len", 1609.344, "mi"),
    "miles": ("len", 1609.344, "mi"),
}

_TEMP_ALIASES = {
    "f": "F",
    "fahrenheit": "F",
    "degf": "F",
    "degreesf": "F",
    "c": "C",
    "celsius": "C",
    "centigrade": "C",
    "degc": "C",
    "degreesc": "C",
}


def _fmt_num(val: float) -> str:
    if abs(val - round(val)) < 1e-9:
        return str(int(round(val)))
    if abs(val) >= 100:
        return f"{val:.1f}".rstrip("0").rstrip(".")
    return f"{val:.2f}".rstrip("0").rstrip(".")


def _normalize_unit(u: str) -> str:
    s = re.sub(r"[^a-z]", "", u.lower())
    if s in ("fluidounce", "fluidounces", "floz"):
        return "floz"
    return s


def _convert_units(amount: float, src_raw: str, dst_raw: str) -> str | None:
    src = _normalize_unit(src_raw)
    dst = _normalize_unit(dst_raw)

    if src in _TEMP_ALIASES and dst in _TEMP_ALIASES:
        s_t, d_t = _TEMP_ALIASES[src], _TEMP_ALIASES[dst]
        if s_t == d_t:
            return f"{_fmt_num(amount)}°{d_t}."
        if s_t == "F" and d_t == "C":
            out = (amount - 32.0) * 5.0 / 9.0
        else:
            out = amount * 9.0 / 5.0 + 32.0
        return f"{_fmt_num(amount)}°{s_t} is {_fmt_num(out)}°{d_t}."

    # Treat "oz" paired with a volume unit as fluid ounces (e.g. "12 oz in ml")
    if src == "oz" and dst in _UNITS and _UNITS[dst][0] == "vol":
        src = "floz"
    if dst == "oz" and src in _UNITS and _UNITS[src][0] == "vol":
        dst = "floz"

    if src in _UNITS and dst in _UNITS:
        cat_s, fac_s, lbl_s = _UNITS[src]
        cat_d, fac_d, lbl_d = _UNITS[dst]
        if cat_s != cat_d:
            raise CalcError(f"Cannot convert {lbl_s} to {lbl_d}.")
        out = amount * fac_s / fac_d
        return f"{_fmt_num(amount)} {lbl_s} is {_fmt_num(out)} {lbl_d}."
    return None


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _eval_ast(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval_ast(node.left)
        right = _eval_ast(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise CalcError("Exponent too large.")
        try:
            return float(_BIN_OPS[type(node.op)](left, right))
        except ZeroDivisionError as e:
            raise CalcError("Division by zero.") from e
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return float(_UNARY_OPS[type(node.op)](_eval_ast(node.operand)))
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("sqrt", "abs", "round")
        and len(node.args) == 1
    ):
        arg = _eval_ast(node.args[0])
        if node.func.id == "sqrt":
            if arg < 0:
                raise CalcError("Cannot take square root of a negative number.")
            return math.sqrt(arg)
        if node.func.id == "abs":
            return abs(arg)
        return float(round(arg))
    raise CalcError("Unsupported expression.")


def evaluate(expression: str) -> str:
    """Evaluate a spoken math, conversion, ABV, split/tip, or dice/coin request."""
    raw = expression.strip()
    if not raw:
        raise CalcError("Empty expression.")

    low = _replace_number_words(raw.lower())

    # 1. Coin flip
    if re.search(r"\b(flip\s+a?\s*coin|coin\s+flip|heads\s+or\s+tails)\b", low):
        return f"{random.choice(['Heads', 'Tails'])}."

    # 2. Dice roll: "roll 2d6 + 3", "roll a d20", "2d8"
    m_dice = re.search(r"\b(\d*)\s*d\s*(\d+)(?:\s*([+-])\s*(\d+))?\b", low)
    if m_dice and ("roll" in low or re.match(r"^\s*\d*d\d+", low)):
        count = int(m_dice.group(1)) if m_dice.group(1) else 1
        sides = int(m_dice.group(2))
        if count < 1 or count > 100 or sides < 2 or sides > 10000:
            raise CalcError("Dice must be 1-100 dice with 2-10000 sides.")
        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls)
        mod_str = ""
        if m_dice.group(3) and m_dice.group(4):
            mod = int(m_dice.group(4))
            total = total + mod if m_dice.group(3) == "+" else total - mod
            mod_str = f" {m_dice.group(3)} {mod}"
        label = f"{count}d{sides}{mod_str}" if count > 1 or mod_str else f"d{sides}"
        if count == 1 and not mod_str:
            return f"Rolled {label}: {total}."
        breakdown = " + ".join(str(r) for r in rolls)
        return f"Rolled {label}: {breakdown}{mod_str} = {total}."

    # 3. Mead / brewing ABV from specific gravity: "abv 1.105 to 1.000"
    m_abv = re.search(r"\b(1\.\d{2,3})\b.*?\b(0\.\d{2,3}|1\.\d{2,3})\b", low)
    if m_abv and any(k in low for k in ("abv", "gravity", "og", "fg", "sg", "mead")):
        g1, g2 = float(m_abv.group(1)), float(m_abv.group(2))
        og, fg = max(g1, g2), min(g1, g2)
        abv = (og - fg) * 131.25
        return f"{og:.3f} to {fg:.3f} is {abv:.1f}% ABV."

    # 4. Bill split (with optional tip): "split 84 3 ways", "split 120 4 ways with 20% tip"
    m_split = re.search(
        r"\bsplit\s+\$?(\d+(?:\.\d+)?)\s+(?:between\s+|among\s+)?(\d+)\s*(?:ways|people|persons)?",
        low,
    )
    if m_split:
        bill = float(m_split.group(1))
        ways = int(m_split.group(2))
        if ways < 1:
            raise CalcError("Must split at least 1 way.")
        m_tip_pct = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s*tip", low)
        if m_tip_pct:
            pct = float(m_tip_pct.group(1))
            total = bill * (1.0 + pct / 100.0)
            each = total / ways
            return f"${bill:.2f} + {_fmt_num(pct)}% tip is ${total:.2f} (${each:.2f} each for {ways})."
        each = bill / ways
        return f"${bill:.2f} split {ways} ways is ${each:.2f} each."

    # 5. Tip calculation: "20% tip on 64.50", "64.50 with 20% tip", or "tip on 64.50"
    m_tip = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s*tip\s+(?:on|for)\s+\$?(\d+(?:\.\d+)?)",
        low,
    )
    if not m_tip:
        m_tip_rev = re.search(
            r"\$?(\d+(?:\.\d+)?)\s+(?:with|plus)\s+(?:a\s+)?(\d+(?:\.\d+)?)\s*(?:%|percent)\s*tip",
            low,
        )
        if m_tip_rev:
            pct = float(m_tip_rev.group(2))
            bill = float(m_tip_rev.group(1))
            tip = bill * pct / 100.0
            return f"{_fmt_num(pct)}% tip on ${bill:.2f} is ${tip:.2f} (total ${bill + tip:.2f})."
    if m_tip:
        pct = float(m_tip.group(1))
        bill = float(m_tip.group(2))
        tip = bill * pct / 100.0
        return f"{_fmt_num(pct)}% tip on ${bill:.2f} is ${tip:.2f} (total ${bill + tip:.2f})."

    m_tip_default = re.search(r"\btip\s+(?:on|for)\s+\$?(\d+(?:\.\d+)?)", low)
    if m_tip_default:
        bill = float(m_tip_default.group(1))
        parts = [f"{p}%: ${bill * p / 100.0:.2f} (total ${bill * (1 + p / 100.0):.2f})" for p in (15, 18, 20)]
        return f"Tip on ${bill:.2f}: " + "; ".join(parts) + "."

    # 6. Unit conversions:
    #    - "convert 350 F to C", "12 oz in ml"
    #    - "how many ml in 12 oz"
    m_how_many = re.search(
        r"how\s+many\s+([a-z ]+?)\s+(?:are\s+)?in\s+(-?\d+(?:\.\d+)?)\s*([a-z ]+?)\s*[?.!]*$",
        low,
    )
    if m_how_many:
        conv = _convert_units(float(m_how_many.group(2)), m_how_many.group(3), m_how_many.group(1))
        if conv:
            return conv

    m_conv = re.search(
        r"(-?\d+(?:\.\d+)?)\s*°?\s*(?:degrees?\s+)?([a-z]+(?:\s+oz|\s+ounces?)?)\s+(?:to|in|into|as)\s+°?\s*(?:degrees?\s+)?([a-z]+(?:\s+oz|\s+ounces?)?)\s*[?.!]*$",
        low,
    )
    if m_conv:
        conv = _convert_units(float(m_conv.group(1)), m_conv.group(2), m_conv.group(3))
        if conv:
            return conv

    # 7. Percentage discount & percentage of: "15% off $80", "15% of $240"
    m_pct_off = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s+off\s+(\$)?(\d+(?:\.\d+)?)", low)
    if m_pct_off:
        pct = float(m_pct_off.group(1))
        is_money = bool(m_pct_off.group(2))
        base = float(m_pct_off.group(3))
        savings = base * pct / 100.0
        final = base - savings
        if is_money:
            return f"{_fmt_num(pct)}% off ${base:.2f} is ${final:.2f} (save ${savings:.2f})."
        return f"{_fmt_num(pct)}% off {_fmt_num(base)} is {_fmt_num(final)} (save {_fmt_num(savings)})."

    m_pct = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:%|percent)\s+of\s+(\$)?(-?\d+(?:\.\d+)?)", low)
    if m_pct:
        pct = float(m_pct.group(1))
        is_money = bool(m_pct.group(2))
        base = float(m_pct.group(3))
        res = base * pct / 100.0
        if is_money:
            return f"{_fmt_num(pct)}% of ${base:.2f} is ${res:.2f}."
        return f"{_fmt_num(pct)}% of {_fmt_num(base)} is {_fmt_num(res)}."

    # 8. General arithmetic expression
    expr = low
    expr = re.sub(r"^(?:what\s+is|whats|calculate|compute|evaluate)\s+", "", expr)
    expr = expr.replace(",", "").replace("^", "**")
    expr = re.sub(r"\bsquare\s+root\s+of\s+(-?\d+(?:\.\d+)?)", r"sqrt(\1)", expr)
    expr = re.sub(r"\bto\s+the\s+power\s+of\b", "**", expr)
    expr = re.sub(r"\bsquared\b", "**2", expr)
    expr = re.sub(r"\bcubed\b", "**3", expr)
    expr = re.sub(r"\b(?:multiplied\s+by|times)\b", "*", expr)
    expr = re.sub(r"\b(?:divided\s+by|over)\b", "/", expr)
    expr = re.sub(r"\bplus\b", "+", expr)
    expr = re.sub(r"\bminus\b", "-", expr)
    expr = re.sub(r"(?<=\d)\s*x\s*(?=\d)", "*", expr)
    expr = expr.strip(" ?.!")

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise CalcError(f"Could not parse '{raw}'.") from e
    val = _eval_ast(tree)
    return f"{_fmt_num(val)}."
