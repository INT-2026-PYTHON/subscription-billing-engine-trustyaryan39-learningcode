"""
FixedAmountDiscount — e.g., flat ₹500 off.

CAPPING RULE: if the fixed amount exceeds the subtotal, return subtotal
(so the discounted total never goes below zero).
"""

from billing_engine.money import Money
from billing_engine.discounts.base import Discount, DiscountContext


class FixedAmountDiscount(Discount):
    def __init__(self, amount: Money) -> None:
        if amount.is_negative():
            raise ValueError("Discount amount must be non-negative")
        self.amount = amount

    def apply(self, subtotal: Money, context: DiscountContext) -> Money:
        if self.amount.currency != subtotal.currency:
            raise ValueError("Discount amount and subtotal must have the same currency")
        return min(self.amount, subtotal)
