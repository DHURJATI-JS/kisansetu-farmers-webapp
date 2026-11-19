"""Income-tax / TDS on salary (section 192 style): annualised estimate → slab tax → rebate → cess → per-day rate.
Slabs, rebate, standard deduction and cess come from superadmin settings (verify every financial year)."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

D0 = Decimal("0")


def annual_tax(taxable: Decimal, cfg: dict[str, Any]) -> Decimal:
    taxable = max(D0, Decimal(taxable))
    tax, prev = D0, D0
    for upto, pct in cfg["slabs"]:
        rate = Decimal(str(pct)) / 100
        if upto is None or taxable <= Decimal(str(upto)):
            tax += (taxable - prev) * rate
            break
        tax += (Decimal(str(upto)) - prev) * rate
        prev = Decimal(str(upto))
    rebate_limit = Decimal(str(cfg.get("rebate_limit", 0)))
    if taxable <= rebate_limit:
        tax = max(D0, tax - Decimal(str(cfg.get("rebate_max", 0))))
    elif rebate_limit > 0:
        tax = min(tax, taxable - rebate_limit)  # marginal relief just above the rebate limit
    tax += tax * Decimal(str(cfg.get("cess_pct", 0))) / 100
    return tax.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def annualised_gross(salary_type: str, amount: Decimal) -> Decimal:
    amount = Decimal(amount)
    return amount * 12 if salary_type == "monthly" else amount * 312  # 26 working days × 12


def tds_rate(salary_type: str, amount: Decimal, cfg: dict[str, Any]) -> Decimal:
    gross = annualised_gross(salary_type, amount)
    if gross <= 0:
        return D0
    taxable = gross - Decimal(str(cfg.get("standard_deduction", 0)))
    tax = annual_tax(taxable, cfg)
    return (tax / gross).quantize(Decimal("0.000001"))
