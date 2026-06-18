"""
CLI entrypoint.

Subcommands to implement (Day 4):
    billing init                              -- create / migrate the DB
    billing customer add <name> <email> <country> [--state CODE]
    billing plan list
    billing subscribe <customer_id> <plan_id> [--trial-days N] [--discount CODE]
    billing bill run [--date YYYY-MM-DD]
    billing invoice show <invoice_id>          -- prints PLAIN TEXT invoice
    billing upgrade <subscription_id> <new_plan_id> [--date YYYY-MM-DD]   (STRETCH)
    billing demo                              -- run the scripted scenario

Use argparse with subparsers. Keep each subcommand handler in its own function.

PDF rendering is OUT OF SCOPE for the core project — `invoice show` should
print a clean PLAIN-TEXT invoice (see helper `format_invoice_text` below).
PDF generation is BONUS: see `billing_engine/pdf/renderer.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from decimal import Decimal
from typing import Optional

from billing_engine.billing.cycle import BillingCycle
from billing_engine.billing.dunning import DunningProcess
from billing_engine.db import (
    CustomerRepository,
    Database,
    DiscountRepository,
    InvoiceRepository,
    LedgerRepository,
    InvoiceLineItemRepository,
    PaymentAttemptRepository,
    PlanRepository,
    SubscriptionRepository,
    UsageRecordRepository,
)
from billing_engine.discounts import FixedAmountDiscount, FirstMonthFree, PercentageDiscount
from billing_engine.models import (
    BillingPeriod,
    Customer,
    Invoice,
    InvoiceLineItem,
    InvoiceStatus,
    LedgerDirection,
    LineItemKind,
    Plan,
    PricingType,
    Subscription,
    SubscriptionStatus,
)
from billing_engine.money import Money
from billing_engine.payments.gateway import PaymentResult, ScriptedGateway
from billing_engine.pricing import FlatRate, Freemium, Tier, TieredPricing, UsageBased
from billing_engine.pricing.base import PricingStrategy
from billing_engine.taxes import TaxCalculator, TaxContext

DEFAULT_DB_PATH = Path("billing.db")
SELLER_STATE = "MH"


def _db_path() -> Path:
    return DEFAULT_DB_PATH


def _get_db() -> Database:
    db = Database(_db_path())
    db.init_schema()
    return db


def _month_after(start: date) -> date:
    month = 1 if start.month == 12 else start.month + 1
    year = start.year + 1 if start.month == 12 else start.year
    day = min(start.day, monthrange(year, month)[1])
    return date(year, month, day)


def _year_after(start: date) -> date:
    year = start.year + 1
    day = min(start.day, monthrange(year, start.month)[1])
    return date(year, start.month, day)


def _advance_period(start: date, billing_period: BillingPeriod) -> date:
    if billing_period == BillingPeriod.MONTHLY:
        return _month_after(start)
    if billing_period == BillingPeriod.YEARLY:
        return _year_after(start)
    raise ValueError(f"Unsupported billing period: {billing_period}")


def _format_amount(amount: Money) -> str:
    return f"{amount.currency} {amount.to_storage()}"


def _strategy_from_plan(plan: Plan) -> PricingStrategy:
    config = json.loads(plan.config_json or "{}")

    if plan.pricing_type == PricingType.FLAT:
        amount = config.get("amount")
        if amount is None:
            raise ValueError(f"Plan {plan.name!r} missing config_json.amount")
        return FlatRate(Money(amount, plan.currency))

    if plan.pricing_type == PricingType.USAGE:
        unit_price = config.get("unit_price")
        if unit_price is None:
            raise ValueError(f"Plan {plan.name!r} missing config_json.unit_price")
        return UsageBased(Money(unit_price, plan.currency))

    if plan.pricing_type == PricingType.TIERED:
        tiers_config = config.get("tiers", [])
        tiers = []
        for tier in tiers_config:
            if isinstance(tier, dict):
                from_units = tier["from_units"]
                to_units = tier.get("to_units")
                price = tier["unit_price"]
            else:
                from_units, to_units, price = tier
            tiers.append(Tier(from_units, to_units, Money(price, plan.currency)))
        return TieredPricing(tiers)

    if plan.pricing_type == PricingType.FREEMIUM:
        free_quota = config.get("free_quota")
        overage_unit_price = config.get("overage_unit_price")
        if free_quota is None or overage_unit_price is None:
            raise ValueError(f"Plan {plan.name!r} missing freemium config")
        return Freemium(free_quota, UsageBased(Money(overage_unit_price, plan.currency)))

    raise ValueError(f"Unsupported pricing type: {plan.pricing_type}")


def _discount_from_row(row: Optional[dict]):
    if row is None:
        return None
    discount_type = row["discount_type"]
    if discount_type == "PERCENT":
        return PercentageDiscount(Decimal(row["value"]))
    if discount_type == "FIXED":
        return FixedAmountDiscount(Money(row["value"], row["currency"]))
    if discount_type == "FIRST_MONTH_FREE":
        return FirstMonthFree()
    raise ValueError(f"Unsupported discount type: {discount_type}")


def _tax_factory(customer: Customer):
    calculator = TaxCalculator.for_country(customer.country_code)
    context = TaxContext(
        customer_country=customer.country_code,
        customer_state=customer.state_code,
        seller_state=SELLER_STATE,
    )
    return calculator, context


def _make_repos(db: Database):
    return {
        "customers": CustomerRepository(db),
        "plans": PlanRepository(db),
        "subscriptions": SubscriptionRepository(db),
        "usage": UsageRecordRepository(db),
        "invoices": InvoiceRepository(db),
        "line_items": InvoiceLineItemRepository(db),
        "ledger": LedgerRepository(db),
        "discounts": DiscountRepository(db),
        "attempts": PaymentAttemptRepository(db),
    }


def format_invoice_text(invoice: Invoice, customer_name: str, plan_name: str) -> str:
    """Render an invoice as a plain-text receipt. Pure function — easy to test."""
    title = f"INVOICE #{invoice.id}" if invoice.id is not None else "INVOICE"
    lines = [
        title,
        "=" * 60,
        f"Customer: {customer_name}",
        f"Plan:     {plan_name}",
        f"Period:   {invoice.period_start} to {invoice.period_end}",
        "-" * 60,
    ]

    for item in invoice.line_items:
        label = item.description if item.description else item.kind.value
        lines.append(f"{label:<42}{_format_amount(item.amount):>18}")

    lines.extend([
        "-" * 60,
        f"Subtotal:{_format_amount(invoice.subtotal):>46}",
        f"Discount:{_format_amount(invoice.discount_total):>46}",
        f"Tax:{_format_amount(invoice.tax_total):>51}",
        f"TOTAL:{_format_amount(invoice.total):>49}",
        f"Status: {invoice.status.value}",
    ])
    return "\n".join(lines)


def _handle_init(args) -> int:
    db = _get_db()
    print(f"Initialized database at {db.path}")
    return 0


def _handle_customer_add(args) -> int:
    db = _get_db()
    repos = _make_repos(db)
    customer = repos["customers"].add(
        Customer(
            id=None,
            name=args.name,
            email=args.email,
            country_code=args.country,
            state_code=args.state or "",
        )
    )
    print(f"Created customer #{customer.id}: {customer.name}")
    return 0


def _handle_plan_list(args) -> int:
    db = _get_db()
    repos = _make_repos(db)
    plans = repos["plans"].list_all()
    for plan in plans:
        print(f"{plan.id}: {plan.name} [{plan.pricing_type.value}/{plan.billing_period.value}] {plan.currency}")
    return 0


def _handle_subscribe(args) -> int:
    db = _get_db()
    repos = _make_repos(db)
    customer = repos["customers"].get(args.customer_id)
    plan = repos["plans"].get(args.plan_id)
    if customer is None or plan is None:
        print("Customer or plan not found", file=sys.stderr)
        return 1

    discount_id = None
    if args.discount:
        discount = repos["discounts"].get_by_code(args.discount)
        if discount is None:
            print(f"Discount {args.discount!r} not found", file=sys.stderr)
            return 1
        discount_id = discount["id"]

    start = date.today()
    end = _advance_period(start, plan.billing_period)
    status = SubscriptionStatus.TRIAL if args.trial_days > 0 else SubscriptionStatus.ACTIVE
    trial_end = start + timedelta(days=args.trial_days) if args.trial_days > 0 else None
    subscription = repos["subscriptions"].add(
        Subscription(
            id=None,
            customer_id=customer.id,
            plan_id=plan.id,
            status=status,
            current_period_start=start,
            current_period_end=end,
            trial_end=trial_end,
            discount_id=discount_id,
        )
    )
    print(f"Created subscription #{subscription.id} for customer #{customer.id}")
    return 0


def _handle_bill_run(args) -> int:
    db = _get_db()
    repos = _make_repos(db)
    run_date = date.fromisoformat(args.date) if args.date else date.today()
    cycle = BillingCycle(
        db=db,
        customer_repo=repos["customers"],
        plan_repo=repos["plans"],
        subscription_repo=repos["subscriptions"],
        usage_repo=repos["usage"],
        invoice_repo=repos["invoices"],
        line_item_repo=repos["line_items"],
        ledger_repo=repos["ledger"],
        strategy_factory=_strategy_from_plan,
        discount_factory=_discount_from_row,
        tax_factory=_tax_factory,
    )
    result = cycle.run(run_date)
    print(
        f"Billing run complete: created={result.invoices_created}, "
        f"skipped={result.invoices_skipped_duplicate}, trials={result.trials_activated}"
    )
    return 0


def _handle_invoice_show(args) -> int:
    db = _get_db()
    repos = _make_repos(db)
    invoice = repos["invoices"].get(args.invoice_id)
    if invoice is None:
        print(f"Invoice {args.invoice_id} not found", file=sys.stderr)
        return 1

    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT c.name AS customer_name, p.name AS plan_name
            FROM invoices i
            JOIN subscriptions s ON s.id = i.subscription_id
            JOIN customers c ON c.id = s.customer_id
            JOIN plans p ON p.id = s.plan_id
            WHERE i.id = ?
            """,
            (invoice.id,),
        ).fetchone()

    if row is None:
        print(f"Unable to resolve invoice {invoice.id}", file=sys.stderr)
        return 1

    print(format_invoice_text(invoice, row["customer_name"], row["plan_name"]))
    return 0


def _handle_upgrade(args) -> int:
    db = _get_db()
    repos = _make_repos(db)
    cycle = BillingCycle(
        db=db,
        customer_repo=repos["customers"],
        plan_repo=repos["plans"],
        subscription_repo=repos["subscriptions"],
        usage_repo=repos["usage"],
        invoice_repo=repos["invoices"],
        line_item_repo=repos["line_items"],
        ledger_repo=repos["ledger"],
        strategy_factory=_strategy_from_plan,
        discount_factory=_discount_from_row,
        tax_factory=_tax_factory,
    )
    switch_date = date.fromisoformat(args.date) if args.date else date.today()
    cycle.upgrade_subscription(args.subscription_id, args.new_plan_id, switch_date)
    print(f"Upgraded subscription #{args.subscription_id} to plan #{args.new_plan_id}")
    return 0


def run_demo() -> int:
    """Scripted end-to-end scenario for the `demo` subcommand."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.init_schema()
    repos = _make_repos(db)

    try:
        print("Demo: initializing data")
        customer = repos["customers"].add(Customer(None, "Alice", "alice@example.com", "AE"))
        pro = repos["plans"].add(Plan(None, "Pro", PricingType.FLAT, BillingPeriod.MONTHLY, "INR", '{"amount":"1000"}'))
        enterprise = repos["plans"].add(Plan(None, "Enterprise", PricingType.FLAT, BillingPeriod.MONTHLY, "INR", '{"amount":"2000"}'))
        subscription = repos["subscriptions"].add(
            Subscription(
                None,
                customer.id,
                pro.id,
                SubscriptionStatus.ACTIVE,
                date(2026, 1, 1),
                date(2026, 2, 1),
            )
        )
        print(f"Created customer #{customer.id}, plans #{pro.id}/{enterprise.id}, subscription #{subscription.id}")

        cycle = BillingCycle(
            db=db,
            customer_repo=repos["customers"],
            plan_repo=repos["plans"],
            subscription_repo=repos["subscriptions"],
            usage_repo=repos["usage"],
            invoice_repo=repos["invoices"],
            line_item_repo=repos["line_items"],
            ledger_repo=repos["ledger"],
            strategy_factory=_strategy_from_plan,
            discount_factory=_discount_from_row,
            tax_factory=_tax_factory,
        )
        result = cycle.run(date(2026, 2, 1))
        print(f"Billing cycle: created {result.invoices_created}, skipped {result.invoices_skipped_duplicate}")

        invoice = repos["invoices"].get(1)
        if invoice is not None:
            print(format_invoice_text(invoice, customer.name, pro.name))

        dunning = DunningProcess(
            gateway=ScriptedGateway([PaymentResult(False, "INSUFFICIENT_FUNDS"), PaymentResult(True)]),
            invoice_repo=repos["invoices"],
            ledger_repo=repos["ledger"],
            subscription_repo=repos["subscriptions"],
            attempt_repo=repos["attempts"],
        )
        print("Payment attempt #1:", dunning.attempt(invoice, customer.id, datetime(2026, 2, 1, 10, 0)).state.value)
        print("Payment attempt #2:", dunning.attempt(invoice, customer.id, datetime(2026, 2, 2, 10, 0)).state.value)

        print("Upgrading subscription mid-cycle")
        cycle.upgrade_subscription(subscription.id, enterprise.id, date(2026, 2, 15))
        proration_invoice = repos["invoices"].get(2)
        if proration_invoice is not None:
            print(format_invoice_text(proration_invoice, customer.name, enterprise.name))

        print("Ledger snapshot")
        for entry in repos["ledger"].list_for_customer(customer.id):
            sign = "+" if entry.direction == LedgerDirection.CREDIT else "-"
            print(f"{entry.direction.value}: {sign}{_format_amount(entry.amount)} ({entry.reason})")

        return 0
    finally:
        db_path = Path(path)
        db_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="billing", description="Subscription Billing CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init_parser = sub.add_parser("init", help="initialize the database")
    init_parser.set_defaults(func=_handle_init)

    demo_parser = sub.add_parser("demo", help="run the demo scenario")
    demo_parser.set_defaults(func=lambda args: run_demo())

    customer_parser = sub.add_parser("customer", help="customer commands")
    customer_sub = customer_parser.add_subparsers(dest="customer_cmd", required=True)
    customer_add = customer_sub.add_parser("add", help="add a customer")
    customer_add.add_argument("name")
    customer_add.add_argument("email")
    customer_add.add_argument("country")
    customer_add.add_argument("--state", default="")
    customer_add.set_defaults(func=_handle_customer_add)

    plan_parser = sub.add_parser("plan", help="plan commands")
    plan_sub = plan_parser.add_subparsers(dest="plan_cmd", required=True)
    plan_list = plan_sub.add_parser("list", help="list plans")
    plan_list.set_defaults(func=_handle_plan_list)

    subscribe_parser = sub.add_parser("subscribe", help="create a subscription")
    subscribe_parser.add_argument("customer_id", type=int)
    subscribe_parser.add_argument("plan_id", type=int)
    subscribe_parser.add_argument("--trial-days", type=int, default=0)
    subscribe_parser.add_argument("--discount", default=None)
    subscribe_parser.set_defaults(func=_handle_subscribe)

    bill_parser = sub.add_parser("bill", help="billing commands")
    bill_sub = bill_parser.add_subparsers(dest="bill_cmd", required=True)
    bill_run = bill_sub.add_parser("run", help="run the billing cycle")
    bill_run.add_argument("--date", default=None)
    bill_run.set_defaults(func=_handle_bill_run)

    invoice_parser = sub.add_parser("invoice", help="invoice commands")
    invoice_sub = invoice_parser.add_subparsers(dest="invoice_cmd", required=True)
    invoice_show = invoice_sub.add_parser("show", help="show an invoice")
    invoice_show.add_argument("invoice_id", type=int)
    invoice_show.set_defaults(func=_handle_invoice_show)

    upgrade_parser = sub.add_parser("upgrade", help="upgrade a subscription")
    upgrade_parser.add_argument("subscription_id", type=int)
    upgrade_parser.add_argument("new_plan_id", type=int)
    upgrade_parser.add_argument("--date", default=None)
    upgrade_parser.set_defaults(func=_handle_upgrade)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
