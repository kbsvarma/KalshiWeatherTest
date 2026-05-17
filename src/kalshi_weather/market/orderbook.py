from __future__ import annotations

from decimal import Decimal
from typing import Iterable


Ladder = tuple[tuple[Decimal, Decimal], ...]


def derive_implied_ask_ladders(
    yes_bids_ladder: Ladder,
    no_bids_ladder: Ladder,
) -> tuple[Ladder, Ladder]:
    implied_yes_asks = tuple(
        sorted(
            ((Decimal("1") - price, size) for price, size in no_bids_ladder),
            key=lambda item: item[0],
        )
    )
    implied_no_asks = tuple(
        sorted(
            ((Decimal("1") - price, size) for price, size in yes_bids_ladder),
            key=lambda item: item[0],
        )
    )
    return implied_yes_asks, implied_no_asks


def best_implied_ask_price(ladder: Ladder) -> Decimal | None:
    if not ladder:
        return None
    return min(price for price, _size in ladder)


def executable_wap_for_buy(ladder: Ladder, quantity: Decimal) -> tuple[Decimal | None, int]:
    if quantity <= 0:
        return None, 0
    remaining = quantity
    total_cost = Decimal("0")
    levels = 0
    for price, size in ladder:
        if remaining <= 0:
            break
        take = min(size, remaining)
        if take <= 0:
            continue
        total_cost += price * take
        remaining -= take
        levels += 1
    if remaining > 0:
        return None, levels
    return total_cost / quantity, levels
