from __future__ import annotations
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from ocr_evidence import match_source_field

CENT = Decimal("0.01")
TOLERANCE = Decimal("0.01")
FIELDS = (
    "invoice_number", "invoice_date", "due_date", "vendor_name",
    "customer_name", "subtotal", "discount", "tax", "total"
)
MONEY_FIELDS = ("subtotal", "discount", "tax", "total")


def number(value: Any) -> Decimal:
    if value is None or not str(value).strip():
        raise ValueError("Missing numeric value")
    cleaned = re.sub(r"(?i)\b(?:SAR|USD|AED|EUR|GBP)\b|[$€£]", "", str(value))
    cleaned = cleaned.replace(",", "").strip()
    try:
        result = Decimal(cleaned)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid numeric value") from exc
    if not result.is_finite():
        raise ValueError("Invalid numeric value")
    return result


def rounded(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_date(value: Any) -> date:

    match = re.fullmatch(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*", str(value or ""))
    if not match:
        raise ValueError("Expected YYYY-MM-DD format")
    try:
        return date(*(int(v) for v in match.groups()))
    except ValueError as exc:
        raise ValueError("Invalid calendar date") from exc


def validate_invoice(fields: dict, items=None, discount_rate=None,
                     tax_rate=None, ocr_lines=None) -> dict:

    checks: dict[str, dict] = {}

    def record(rule, status, message, independent=False):
        checks[rule] = {"status": status, "message": message,
                        "independent": bool(independent)}

    def compare(rule, actual, expected):
        delta = abs(actual - expected)
        record(rule, "Valid" if delta <= TOLERANCE else "Needs Review",
               f"Expected {rounded(expected)}; extracted {actual}; difference {rounded(delta)}.",
               independent=True)

    numeric = {}
    for key in MONEY_FIELDS:
        try:
            value = number(fields.get(key))
            numeric[key] = value


            record(f"Precision {key}",
                   "Valid" if value == rounded(value) else "Needs Review",
                   "Monetary precision is acceptable." if value == rounded(value)
                   else f"{key} has more than two decimal places.")
        except ValueError:
            record(f"Precision {key}", "Needs Review", f"Invalid {key} amount.")

    dates = {}
    for key in ("invoice_date", "due_date"):
        try:
            dates[key] = parse_date(fields.get(key))
            record(f"Format {key}", "Valid", "Valid calendar date / ISO format.")
        except ValueError as exc:
            record(f"Format {key}", "Needs Review", str(exc))
    if len(dates) == 2:
        record("Due Date Chronology",
               "Valid" if dates["due_date"] >= dates["invoice_date"] else "Needs Review",
               "Due date must be on or after invoice date.")
    else:
        record("Due Date Chronology", "Not Verified",
               "Both dates are needed to check chronology.")

    if "subtotal" in numeric:
        record("Nonnegative subtotal",
               "Valid" if numeric["subtotal"] >= 0 else "Needs Review",
               "Subtotal must not be negative.")
    if "tax" in numeric:
        record("Nonnegative tax",
               "Valid" if numeric["tax"] >= 0 else "Needs Review",
               "Tax must not be negative.")
    if "discount" in numeric and "subtotal" in numeric:
        record("Discount Range",
               "Valid" if 0 <= numeric["discount"] <= numeric["subtotal"]
               else "Needs Review", "Discount must be between 0 and subtotal.")

    verified_lines = []
    if items:
        for index, item in enumerate(items, 1):
            try:
                qty = number(item["quantity"])
                price = number(item["unit_price"])
                line_total = number(item["line_total"])
                if qty < 0 or price < 0:
                    raise ValueError("Negative quantity or unit price.")
                calculated = rounded(qty * price)
                compare(f"Line Total {index}", line_total, calculated)
                if checks[f"Line Total {index}"]["status"] == "Valid":
                    verified_lines.append(calculated)
            except (KeyError, ValueError):
                record(f"Line Total {index}", "Needs Review",
                       "Missing, invalid or negative line-item amount.")
        if len(verified_lines) == len(items) and "subtotal" in numeric:
            compare("Subtotal Equation", numeric["subtotal"], sum(verified_lines, Decimal("0")))
        else:
            record("Subtotal Equation", "Not Verified",
                   "Complete, consistent line items and subtotal are required.")
    else:
        record("Subtotal Equation", "Not Verified",
               "Item-level data are not available from the current Donut output.")

    if discount_rate is not None:
        try:
            rate = number(discount_rate)
            if not 0 <= rate <= 100:
                raise ValueError("Discount rate outside 0–100%.")
            if "discount" in numeric and "subtotal" in numeric:
                compare("Discount Equation", numeric["discount"],
                        rounded(numeric["subtotal"] * rate / 100))
            else:
                record("Discount Equation", "Not Verified", "Financial inputs are missing.")
        except ValueError:
            record("Discount Equation", "Needs Review", "Invalid explicit discount rate.")
    else:
        record("Discount Equation", "Not Verified",
               "No explicitly printed discount rate was available.")

    taxable = None
    if "subtotal" in numeric and "discount" in numeric:
        taxable = numeric["subtotal"] - numeric["discount"]
        record("Taxable Range", "Valid" if taxable >= 0 else "Needs Review",
               "Taxable amount (subtotal − discount) must not be negative.")
    if tax_rate is not None:
        try:
            rate = number(tax_rate)
            if not 0 <= rate <= 100:
                raise ValueError("Tax rate outside 0–100%.")
            if taxable is not None and "tax" in numeric:


                compare("Tax Equation", numeric["tax"], rounded(taxable * rate / 100))
            else:
                record("Tax Equation", "Not Verified", "Financial inputs are missing.")
        except ValueError:
            record("Tax Equation", "Needs Review", "Invalid explicit tax rate.")
    else:
        record("Tax Equation", "Not Verified", "No explicit tax rate available.")

    if taxable is not None and "tax" in numeric and "total" in numeric:
        compare("Total Equation", numeric["total"], rounded(taxable + numeric["tax"]))
    else:
        record("Total Equation", "Not Verified",
               "Subtotal, discount, tax and total are required.")

    total_consistent = checks["Total Equation"]["status"] == "Valid"
    for key in FIELDS:
        evidence = match_source_field(key, fields.get(key), ocr_lines or [],
                                      arithmetic_consistent=total_consistent)
        record(f"Source {key}", evidence["status"], evidence["message"],
               independent=evidence["status"] == "Valid")
    return checks


FIELD_RULES = {
    "invoice_number": ["Source invoice_number"],
    "invoice_date": ["Format invoice_date", "Source invoice_date"],
    "due_date": ["Format due_date", "Due Date Chronology", "Source due_date"],
    "vendor_name": ["Source vendor_name"],
    "customer_name": ["Source customer_name"],
    "subtotal": ["Precision subtotal", "Nonnegative subtotal", "Subtotal Equation", "Source subtotal"],
    "discount": ["Precision discount", "Discount Range", "Discount Equation", "Source discount"],
    "tax": ["Precision tax", "Nonnegative tax", "Tax Equation", "Source tax"],
    "total": ["Precision total", "Total Equation", "Source total"],
}


def validate_fields(fields, items=None, discount_rate=None, tax_rate=None,
                    ocr_lines=None) -> dict:

    checks = validate_invoice(fields, items=items, discount_rate=discount_rate,
                              tax_rate=tax_rate, ocr_lines=ocr_lines)
    results = {}
    for key in FIELDS:
        val = fields.get(key)
        if val is None or not str(val).strip():
            results[key] = {"status": "Needs Review", "reason_code": "missing_value",
                            "rule": "Required field",
                            "message": "The extraction did not supply this required field."}
            continue
        selected = [(name, checks[name]) for name in FIELD_RULES[key] if name in checks]
        failures = [(name, c) for name, c in selected if c["status"] == "Needs Review"]
        verified = [(name, c) for name, c in selected
                    if c["status"] == "Valid" and c["independent"]]
        if failures:
            status = "Needs Review"
            details = failures
        elif verified:
            status = "Valid"
            details = verified
        else:
            status = "Not Verified"


            details = [(name, c) for name, c in selected
                       if c["status"] == "Not Verified"] or selected
        results[key] = {
            "status": status,
            "reason_code": ("mismatch" if failures else
                            "confirmed" if verified else "insufficient_evidence"),
            "rule": ", ".join(name for name, _ in details) or "No independent check",
            "message": "; ".join(f"{name}: {c['message']}" for name, c in details),
        }
    return results


def changed_fields(original: dict | None, current: dict) -> set[str]:

    if original is None:
        return set()
    changes = set()
    for key in FIELDS:
        previous = str(original.get(key) or "").strip()
        present = str(current.get(key) or "").strip()
        if key in MONEY_FIELDS:
            try:
                same = number(previous) == number(present)
            except ValueError:
                same = previous == present
        elif key in {"invoice_date", "due_date"}:
            try:
                same = parse_date(previous) == parse_date(present)
            except ValueError:
                same = previous == present
        else:
            same = previous == present
        if not same:
            changes.add(key)
    return changes


def calculate_projection(fields: dict, tax_rate=None, modified_keys=None) -> dict:


    try:
        subtotal = number(fields.get("subtotal"))
        discount = number(fields.get("discount"))
        extracted_total = number(fields.get("total"))
        if subtotal < 0 or not 0 <= discount <= subtotal:
            raise ValueError("Subtotal/discount values are out of range.")
        taxable = subtotal - discount
        manual_tax_edit = "tax" in (modified_keys or set())
        implied_tax = None
        if tax_rate is not None:
            rate = number(tax_rate)
            if not 0 <= rate <= 100:
                raise ValueError("Tax rate must be between 0 and 100.")
            implied_tax = rounded(taxable * rate / Decimal("100"))
            if manual_tax_edit:
                tax = number(fields.get("tax"))
                assumption = ("Manually edited tax is used for the total preview; "
                              "check it against the tax implied by the printed rate.")
                tax_mode = "edited"
            else:
                tax = implied_tax
                assumption = ("Estimated tax uses the printed rate on the "
                              "post-discount amount (this template's rule).")
                tax_mode = "recalculated"
        else:
            tax = number(fields.get("tax"))
            assumption = "No verified tax rate: the currently entered tax is held constant."
            tax_mode = "recorded"
        if tax < 0:
            raise ValueError("Tax cannot be negative.")
        total = rounded(taxable + tax)
        return {"available": True, "taxable": str(rounded(taxable)),
                "tax": str(rounded(tax)), "expected_total": str(total),
                "current_total": str(rounded(extracted_total)),
                "difference": str(rounded(total - extracted_total)),
                "matches": abs(total - extracted_total) <= TOLERANCE,
                "tax_mode": tax_mode, "assumption": assumption,
                "tax_from_printed_rate": (str(implied_tax) if implied_tax is not None else None)}
    except ValueError as exc:
        return {"available": False, "error": str(exc)}


def public_status(detail: dict) -> str:

    return "Valid" if detail["status"] == "Valid" else "Needs Review"


def public_reason(detail: dict) -> str:

    code = detail.get("reason_code")
    if code == "confirmed":
        return "Passed an available source or calculation check."
    if code == "missing_value":
        return "This value is missing. Check the original invoice."
    if code == "insufficient_evidence" or detail["status"] == "Not Verified":
        return "The system could not confidently confirm this value from the available evidence."
    if code == "mismatch":
        rules = detail.get("rule", "")
        if "Source invoice_number" in rules:
            return "Invoice number differs from the number read on the image."
        if "Source customer_name" in rules:
            return "Customer name differs from the customer section on the image."
        if "Source vendor_name" in rules:
            return "Supplier name differs from the supplier section on the image."
        if "Source invoice_date" in rules or "Source due_date" in rules:
            return "Entered date differs from the date read on the image."
        if "Due Date Chronology" in rules:
            return "Due date is earlier than the invoice date."
        if any("Source " + key in rules for key in MONEY_FIELDS):
            return "Entered amount differs from the labeled amount on the invoice."
        if "Equation" in rules:
            return "An amount differs from a linked calculation. Review related fields."
        return "A validation check found a discrepancy. Review this value."
    return "Please review this value."


def recalculate_linked_fields(fields: dict, changed_key: str,
                              original: dict | None, tax_rate=None,
                              manual_tax_override: bool = False) -> tuple[dict, str]:


    result = dict(fields)
    if changed_key not in {"subtotal", "discount", "tax"}:
        return result, ""

    subtotal = number(result.get("subtotal"))
    discount = number(result.get("discount"))
    tax = number(result.get("tax"))
    if subtotal < 0 or not 0 <= discount <= subtotal:
        raise ValueError("Subtotal must be nonnegative and discount must be between 0 and subtotal.")
    if tax < 0:
        raise ValueError("Tax must not be negative.")

    note = ""
    if changed_key in {"subtotal", "discount"} and not manual_tax_override:
        original = original or {}
        try:
            original_subtotal = number(original.get("subtotal"))
            original_discount = number(original.get("discount"))
            original_tax = number(original.get("tax"))
            rate = number(tax_rate) if tax_rate is not None else None


            if (rate is not None and 0 <= rate <= 100 and
                    abs(rounded((original_subtotal - original_discount) * rate / 100)
                        - original_tax) <= TOLERANCE):
                tax = rounded((subtotal - discount) * rate / 100)
                result["tax"] = f"{tax:.2f}"
            else:
                note = "Tax was kept unchanged because its calculation could not be confirmed."
        except ValueError:
            note = "Tax was kept unchanged because its calculation could not be confirmed."
    elif changed_key in {"subtotal", "discount"}:
        note = "Your manually entered tax was kept unchanged."

    total = rounded(subtotal - discount + tax)
    result["total"] = f"{total:.2f}"
    return result, note


def check_editable_money(fields: dict) -> None:


    subtotal = number(fields.get("subtotal"))
    discount = number(fields.get("discount"))
    tax = number(fields.get("tax"))
    total = number(fields.get("total"))
    if subtotal < 0 or not 0 <= discount <= subtotal:
        raise ValueError("Subtotal must be nonnegative and discount must be between 0 and subtotal.")
    if tax < 0 or total < 0:
        raise ValueError("Tax and total must be nonnegative.")
