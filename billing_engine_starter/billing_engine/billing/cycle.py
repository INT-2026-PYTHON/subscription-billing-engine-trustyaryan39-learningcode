"""
BillingCycle — finds due subscriptions, generates invoices, posts ledger DEBITs,
advances the subscription period. Must be IDEMPOTENT (safe to run twice).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

from billing_engine.db import (
    Database,
    CustomerRepository, PlanRepository, SubscriptionRepository,
    UsageRecordRepository, InvoiceRepository, InvoiceLineItemRepository,
    LedgerRepository,
)
from billing_engine.models import Subscription


@dataclass
class BillingResult:
    invoices_created: int
    invoices_skipped_duplicate: int
    trials_activated: int


class BillingCycle:
    """Day-3 deliverable. Day-4 stretch: add `upgrade_subscription(...)`."""

    def __init__(
        self,
        db: Database,
        customer_repo: CustomerRepository,
        plan_repo: PlanRepository,
        subscription_repo: SubscriptionRepository,
        usage_repo: UsageRecordRepository,
        invoice_repo: InvoiceRepository,
        line_item_repo: InvoiceLineItemRepository,
        ledger_repo: LedgerRepository,
        strategy_factory: Callable,    # given a Plan, returns a PricingStrategy
        discount_factory: Callable,    # given a discount_id or None, returns a Discount or None
        tax_factory: Callable,         # given a Customer, returns (TaxCalculator, TaxContext)
    ) -> None:
        self.db = db
        self.customer_repo = customer_repo
        self.plan_repo = plan_repo
        self.subscription_repo = subscription_repo
        self.usage_repo = usage_repo
        self.invoice_repo = invoice_repo
        self.line_item_repo = line_item_repo
        self.ledger_repo = ledger_repo
        self.strategy_factory = strategy_factory
        self.discount_factory = discount_factory
        self.tax_factory = tax_factory

    # --------------------------------------------------------
    def run(self, as_of: date) -> BillingResult:
        import sqlite3
        from calendar import monthrange

        from billing_engine.billing.pipeline import build_invoice
        from billing_engine.db import queries as q
        from billing_engine.models import BillingPeriod, InvoiceStatus, LedgerDirection, SubscriptionStatus

        def advance_period(start: date, billing_period: BillingPeriod) -> date:
            if billing_period == BillingPeriod.MONTHLY:
                month = 1 if start.month == 12 else start.month + 1
                year = start.year + 1 if start.month == 12 else start.year
                day = min(start.day, monthrange(year, month)[1])
                return date(year, month, day)

            if billing_period == BillingPeriod.YEARLY:
                year = start.year + 1
                day = min(start.day, monthrange(year, start.month)[1])
                return date(year, start.month, day)

            raise ValueError(f"Unsupported billing period: {billing_period}")

        invoices_created = 0
        invoices_skipped_duplicate = 0
        trials_activated = 0

        for sub in self.subscription_repo.list_all():
            if (
                sub.status == SubscriptionStatus.TRIAL
                and sub.trial_end is not None
                and sub.trial_end <= as_of
            ):
                self.subscription_repo.update_status(sub.id, SubscriptionStatus.ACTIVE)
                trials_activated += 1

        due = self.subscription_repo.get_due_for_billing(as_of)

        for sub in due:
            plan = self.plan_repo.get(sub.plan_id)
            customer = self.customer_repo.get(sub.customer_id)
            if plan is None or customer is None:
                raise LookupError(f"Missing plan/customer for subscription {sub.id}")

            strategy = self.strategy_factory(plan)
            discount = self.discount_factory(sub.discount_id)
            tax_calc, tax_context = self.tax_factory(customer)

            usage_quantity = self.usage_repo.sum_for_period(
                sub.id,
                "units",
                sub.current_period_start,
                sub.current_period_end,
            )
            invoice_count = self.invoice_repo.count_for_subscription(sub.id)

            draft = build_invoice(
                subscription=sub,
                plan=plan,
                strategy=strategy,
                discount=discount,
                tax_calc=tax_calc,
                tax_context=tax_context,
                usage_quantity=usage_quantity,
                period_start=sub.current_period_start,
                period_end=sub.current_period_end,
                invoice_count_so_far=invoice_count,
            )

            next_period_start = sub.current_period_end
            next_period_end = advance_period(next_period_start, plan.billing_period)

            try:
                with self.db.transaction() as conn:
                    invoice_id = q.insert_invoice(
                        conn,
                        draft.subscription_id,
                        draft.period_start.isoformat(),
                        draft.period_end.isoformat(),
                        draft.total.currency,
                        draft.subtotal.to_storage(),
                        draft.discount_total.to_storage(),
                        draft.tax_total.to_storage(),
                        draft.total.to_storage(),
                        InvoiceStatus.ISSUED.value,
                        None,
                        draft.pdf_path,
                    )

                    for item in draft.line_items:
                        q.insert_invoice_line_item(
                            conn,
                            invoice_id,
                            item.description,
                            item.amount.to_storage(),
                            item.kind.value,
                        )

                    q.insert_ledger_entry(
                        conn,
                        invoice_id,
                        customer.id,
                        draft.total.to_storage(),
                        draft.total.currency,
                        LedgerDirection.DEBIT.value,
                        "Invoice issued",
                    )

                    q.update_subscription_period(
                        conn,
                        sub.id,
                        next_period_start.isoformat(),
                        next_period_end.isoformat(),
                    )

                invoices_created += 1
            except sqlite3.IntegrityError:
                invoices_skipped_duplicate += 1

        return BillingResult(
            invoices_created=invoices_created,
            invoices_skipped_duplicate=invoices_skipped_duplicate,
            trials_activated=trials_activated,
        )

    # --------------------------------------------------------
    def upgrade_subscription(self, subscription_id: int, new_plan_id: int, switch_date: date) -> None:
        """Mid-cycle upgrade — Day 4 stretch."""
        # TODO Day 4
        raise NotImplementedError("Day 4: implement BillingCycle.upgrade_subscription")
