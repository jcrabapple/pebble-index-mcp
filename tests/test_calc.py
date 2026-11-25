import pytest

from pebble_index_mcp.calc import CalcError, evaluate


def test_arithmetic_and_spoken_operators():
    assert evaluate("what is 14 times 12") == "168."
    assert evaluate("(100 - 25) / 3") == "25."
    assert evaluate("square root of 144") == "12."
    assert evaluate("2^10") == "1024."
    assert evaluate("twenty one plus thirty five") == "56."


def test_percentage_tip_and_split():
    assert evaluate("15% of 240") == "15% of 240 is 36."
    assert evaluate("15% of $240") == "15% of $240.00 is $36.00."
    assert evaluate("20% off $80") == "20% off $80.00 is $64.00 (save $16.00)."
    assert evaluate("15% off 80") == "15% off 80 is 68 (save 12)."
    assert evaluate("20% tip on 64.50") == "20% tip on $64.50 is $12.90 (total $77.40)."
    assert evaluate("$64.50 with 20% tip") == "20% tip on $64.50 is $12.90 (total $77.40)."
    assert evaluate("tip on $50") == "Tip on $50.00: 15%: $7.50 (total $57.50); 18%: $9.00 (total $59.00); 20%: $10.00 (total $60.00)."
    assert evaluate("split 84 three ways") == "$84.00 split 3 ways is $28.00 each."
    assert (
        evaluate("split 100 four ways with 20% tip")
        == "$100.00 + 20% tip is $120.00 ($30.00 each for 4)."
    )


def test_unit_conversions():
    assert evaluate("350 F to C") == "350°F is 176.7°C."
    assert evaluate("20 C in F") == "20°C is 68°F."
    assert evaluate("how many ml in 12 oz") == "12 fl oz is 354.9 ml."
    assert evaluate("5 miles in km") == "5 mi is 8.05 km."
    assert evaluate("2.5 lbs to grams") == "2.5 lb is 1134 g."


def test_mead_abv():
    assert evaluate("abv 1.105 to 1.000") == "1.105 to 1.000 is 13.8% ABV."


def test_coin_and_dice(monkeypatch):
    monkeypatch.setattr("pebble_index_mcp.calc.random.choice", lambda seq: "Heads")
    assert evaluate("flip a coin") == "Heads."

    monkeypatch.setattr("pebble_index_mcp.calc.random.randint", lambda a, b: 4)
    assert evaluate("roll 2d6 + 3") == "Rolled 2d6 + 3: 4 + 4 + 3 = 11."
    assert evaluate("roll a d20") == "Rolled d20: 4."


def test_invalid_raises():
    with pytest.raises(CalcError):
        evaluate("   ")
    with pytest.raises(CalcError):
        evaluate("import os")
    with pytest.raises(CalcError):
        evaluate("10 / 0")
    with pytest.raises(CalcError):
        evaluate("5 miles to grams")
