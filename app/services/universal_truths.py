"""
Universal Truths Checker
────────────────────────
Deterministic, rule-based pre-check that runs BEFORE the AI engine.
Identifies discrepancies across all three bureaus using three principles:

  1. ACCURACY:      Every data point must match across bureaus.
  2. COMPLETENESS:  No required fields may be missing or logically inconsistent.
  3. VERIFIABILITY: If provenance data is absent, the account cannot be verified.

These flags are injected into Step A's prompt so Claude has concrete
evidence to anchor its violation analysis against — not guesses.
"""

from datetime import date, timedelta
from collections import defaultdict

from app.models.schemas import Account, Bureau, UniversalTruthFlag, UniversalTruthsReport


# ─── Configuration ────────────────────────────────────────────────

# Fields that MUST match across bureaus (Accuracy checks)
CROSS_BUREAU_FIELDS = [
    "balance",
    "date_opened",
    "status",
    "payment_history",
    "account_number_partial",
]

# Fields that must be present on every account (Completeness checks)
REQUIRED_FIELDS = [
    "creditor",
    "status",
    "balance",
    "date_opened",
    "date_reported",
    "payment_history",
]

# Date tolerance: dates within this range across bureaus are "close enough"
DATE_TOLERANCE_DAYS = 5

# Maximum age for reported data (FCRA § 1681c — 7-year rule)
MAX_REPORTING_YEARS = 7


# ─── Main entry point ────────────────────────────────────────────

def run_universal_truths_check(
    accounts_by_bureau: dict[str, list[Account]],
) -> UniversalTruthsReport:
    """
    Run all three Universal Truth checks across bureau data.

    Args:
        accounts_by_bureau: {
            "equifax":   [Account, Account, ...],
            "experian":  [Account, Account, ...],
            "transunion": [Account, Account, ...]
        }

    Returns:
        UniversalTruthsReport with all flags.
    """
    flags: list[UniversalTruthFlag] = []
    bureaus = list(accounts_by_bureau.keys())

    # Group accounts across bureaus by creditor + partial account number
    merged = _merge_accounts_across_bureaus(accounts_by_bureau)

    total_accounts = 0
    for group in merged:
        total_accounts += 1
        flags.extend(_check_accuracy(group))
        flags.extend(_check_completeness(group))
        flags.extend(_check_verifiability(group))

    # Also check single-bureau accounts for completeness + verifiability
    for bureau, accounts in accounts_by_bureau.items():
        for account in accounts:
            flags.extend(_check_single_account_completeness(account, bureau))
            flags.extend(_check_single_account_verifiability(account, bureau))
            flags.extend(_check_obsolete_data(account, bureau))

    # Deduplicate flags
    flags = _deduplicate_flags(flags)

    return UniversalTruthsReport(
        flags=flags,
        accounts_checked=total_accounts,
        bureaus_compared=bureaus,
        accuracy_flags=sum(1 for f in flags if f.truth == "accuracy"),
        completeness_flags=sum(1 for f in flags if f.truth == "completeness"),
        verifiability_flags=sum(1 for f in flags if f.truth == "verifiability"),
    )


def check_single_bureau(accounts: list[Account], bureau: str) -> list[UniversalTruthFlag]:
    """
    Run completeness + verifiability checks on a single bureau's data.
    Used when only one report is uploaded (no cross-bureau comparison possible).
    """
    flags: list[UniversalTruthFlag] = []
    for account in accounts:
        flags.extend(_check_single_account_completeness(account, bureau))
        flags.extend(_check_single_account_verifiability(account, bureau))
        flags.extend(_check_obsolete_data(account, bureau))
    return flags


# ─── Account merging across bureaus ──────────────────────────────

def _merge_accounts_across_bureaus(
    accounts_by_bureau: dict[str, list[Account]],
) -> list[dict]:
    """
    Match the same account across different bureaus using creditor name
    and partial account number as the composite key.
    """
    index: dict[str, dict] = {}

    for bureau, accounts in accounts_by_bureau.items():
        for account in accounts:
            key = _account_key(account)
            if key not in index:
                index[key] = {"creditor": account.creditor, "bureau_data": {}}
            index[key]["bureau_data"][bureau] = account

    # Only return groups that appear on 2+ bureaus (for cross-comparison)
    return [
        group for group in index.values()
        if len(group["bureau_data"]) >= 2
    ]


def _account_key(account: Account) -> str:
    """Generate a normalized key for matching accounts across bureaus."""
    creditor = account.creditor.strip().lower()
    partial = (account.account_number_partial or "").strip().lower()
    return f"{creditor}|{partial}"


# ─── 1. ACCURACY CHECKS ──────────────────────────────────────────

def _check_accuracy(group: dict) -> list[UniversalTruthFlag]:
    """Flag fields that differ across bureaus for the same account."""
    flags = []
    creditor = group["creditor"]
    bureau_data: dict[str, Account] = group["bureau_data"]

    for field in CROSS_BUREAU_FIELDS:
        values = {}
        for bureau, account in bureau_data.items():
            val = getattr(account, field, None)
            if val is not None:
                values[bureau] = val

        if len(values) < 2:
            continue  # Not enough data points to compare

        # Check if values are all consistent
        if field in ("date_opened", "date_reported"):
            if not _dates_match(values):
                flags.append(UniversalTruthFlag(
                    truth="accuracy",
                    field=field,
                    account_creditor=creditor,
                    details=f"{field} differs across bureaus: {_format_values(values)}",
                    mismatched_values={b: str(v) for b, v in values.items()},
                ))
        elif field == "balance":
            if not _balances_match(values):
                flags.append(UniversalTruthFlag(
                    truth="accuracy",
                    field=field,
                    account_creditor=creditor,
                    details=f"Balance differs across bureaus: {_format_values(values)}",
                    mismatched_values={b: str(v) for b, v in values.items()},
                ))
        else:
            unique_values = set(str(v).strip().lower() for v in values.values())
            if len(unique_values) > 1:
                flags.append(UniversalTruthFlag(
                    truth="accuracy",
                    field=field,
                    account_creditor=creditor,
                    details=f"{field} differs across bureaus: {_format_values(values)}",
                    mismatched_values={b: str(v) for b, v in values.items()},
                ))

    return flags


def _dates_match(values: dict[str, date]) -> bool:
    """Check if dates are within tolerance."""
    date_list = list(values.values())
    for i in range(len(date_list)):
        for j in range(i + 1, len(date_list)):
            if isinstance(date_list[i], date) and isinstance(date_list[j], date):
                diff = abs((date_list[i] - date_list[j]).days)
                if diff > DATE_TOLERANCE_DAYS:
                    return False
    return True


def _balances_match(values: dict[str, float]) -> bool:
    """Check if balances match exactly (no tolerance for dollar amounts)."""
    unique = set(values.values())
    return len(unique) == 1


# ─── 2. COMPLETENESS CHECKS ──────────────────────────────────────

def _check_completeness(group: dict) -> list[UniversalTruthFlag]:
    """
    Flag accounts where a field is present on one bureau but missing on another.
    This is a cross-bureau completeness check.
    """
    flags = []
    creditor = group["creditor"]
    bureau_data: dict[str, Account] = group["bureau_data"]

    for field in REQUIRED_FIELDS:
        present_on = []
        missing_on = []

        for bureau, account in bureau_data.items():
            val = getattr(account, field, None)
            if val is not None and str(val).strip():
                present_on.append(bureau)
            else:
                missing_on.append(bureau)

        if present_on and missing_on:
            flags.append(UniversalTruthFlag(
                truth="completeness",
                field=field,
                account_creditor=creditor,
                details=(
                    f"{field} is reported on {', '.join(present_on)} "
                    f"but missing on {', '.join(missing_on)}"
                ),
            ))

    return flags


def _check_single_account_completeness(
    account: Account,
    bureau: str,
) -> list[UniversalTruthFlag]:
    """Check a single account for missing required fields."""
    flags = []
    for field in REQUIRED_FIELDS:
        val = getattr(account, field, None)
        if val is None or (isinstance(val, str) and not val.strip()):
            flags.append(UniversalTruthFlag(
                truth="completeness",
                field=field,
                account_creditor=account.creditor,
                bureau=Bureau(bureau) if bureau in Bureau._value2member_map_ else None,
                details=f"Required field '{field}' is missing on {bureau} report",
            ))

    # Logical consistency: date_reported should be after date_opened
    if account.date_opened and account.date_reported:
        if account.date_reported < account.date_opened:
            flags.append(UniversalTruthFlag(
                truth="completeness",
                field="date_reported",
                account_creditor=account.creditor,
                bureau=Bureau(bureau) if bureau in Bureau._value2member_map_ else None,
                details=(
                    f"date_reported ({account.date_reported}) is before "
                    f"date_opened ({account.date_opened}) — logically impossible"
                ),
            ))

    # Balance on a closed/paid account should be $0
    if account.status and account.status.lower() in ("paid", "closed", "paid in full"):
        if account.balance and account.balance > 0:
            flags.append(UniversalTruthFlag(
                truth="completeness",
                field="balance",
                account_creditor=account.creditor,
                bureau=Bureau(bureau) if bureau in Bureau._value2member_map_ else None,
                details=(
                    f"Account status is '{account.status}' but balance is "
                    f"${account.balance:,.2f} — should be $0.00"
                ),
            ))

    return flags


# ─── 3. VERIFIABILITY CHECKS ─────────────────────────────────────

def _check_verifiability(group: dict) -> list[UniversalTruthFlag]:
    """Cross-bureau verifiability: flag accounts missing provenance everywhere."""
    flags = []
    creditor = group["creditor"]
    bureau_data: dict[str, Account] = group["bureau_data"]

    # If no bureau has the original creditor, the debt chain is unverifiable
    has_original = any(
        account.original_creditor
        for account in bureau_data.values()
    )

    # Only flag for collection/charged-off accounts where original creditor matters
    statuses = {
        account.status.lower()
        for account in bureau_data.values()
        if account.status
    }
    is_collection = statuses & {"collection", "charged off", "charge off", "transferred"}

    if is_collection and not has_original:
        flags.append(UniversalTruthFlag(
            truth="verifiability",
            field="original_creditor",
            account_creditor=creditor,
            details=(
                f"Account is in {'/'.join(statuses)} status but no bureau "
                f"reports the original creditor — debt chain is unverifiable"
            ),
        ))

    return flags


def _check_single_account_verifiability(
    account: Account,
    bureau: str,
) -> list[UniversalTruthFlag]:
    """Check a single account for verifiability issues."""
    flags = []

    # Collection/charged-off without original creditor
    if account.status and account.status.lower() in (
        "collection", "charged off", "charge off", "transferred"
    ):
        if not account.original_creditor:
            flags.append(UniversalTruthFlag(
                truth="verifiability",
                field="original_creditor",
                account_creditor=account.creditor,
                bureau=Bureau(bureau) if bureau in Bureau._value2member_map_ else None,
                details=(
                    f"Account is in '{account.status}' but no original creditor "
                    f"is listed — furnisher cannot verify the debt chain"
                ),
            ))

    # Missing account number entirely
    if not account.account_number_partial:
        flags.append(UniversalTruthFlag(
            truth="verifiability",
            field="account_number_partial",
            account_creditor=account.creditor,
            bureau=Bureau(bureau) if bureau in Bureau._value2member_map_ else None,
            details="No account number (even partial) is reported — account identity is unverifiable",
        ))

    return flags


# ─── Obsolete data check (FCRA § 1681c — 7-year rule) ────────────

def _check_obsolete_data(
    account: Account,
    bureau: str,
) -> list[UniversalTruthFlag]:
    """Flag accounts that may exceed FCRA's 7-year reporting limit."""
    flags = []

    if account.date_opened:
        age_years = (date.today() - account.date_opened).days / 365.25
        if age_years > MAX_REPORTING_YEARS:
            # Only flag negative accounts — positive accounts can report indefinitely
            if account.status and account.status.lower() in (
                "collection", "charged off", "charge off", "late",
                "delinquent", "past due", "transferred",
            ):
                flags.append(UniversalTruthFlag(
                    truth="accuracy",
                    field="date_opened",
                    account_creditor=account.creditor,
                    bureau=Bureau(bureau) if bureau in Bureau._value2member_map_ else None,
                    details=(
                        f"Negative account opened {account.date_opened} is "
                        f"{age_years:.1f} years old — may exceed FCRA's 7-year "
                        f"reporting limit under 15 U.S.C. § 1681c(a)"
                    ),
                ))

    return flags


# ─── Helpers ──────────────────────────────────────────────────────

def _format_values(values: dict) -> str:
    """Format a dict of bureau → value for display."""
    return ", ".join(f"{b}: {v}" for b, v in values.items())


def _deduplicate_flags(flags: list[UniversalTruthFlag]) -> list[UniversalTruthFlag]:
    """Remove duplicate flags (same truth + field + creditor + details)."""
    seen = set()
    unique = []
    for flag in flags:
        key = (flag.truth, flag.field, flag.account_creditor, flag.details)
        if key not in seen:
            seen.add(key)
            unique.append(flag)
    return unique


def format_flags_for_prompt(flags: list[UniversalTruthFlag]) -> str:
    """
    Format Universal Truths flags as structured text for injection
    into the Step A system prompt.
    """
    if not flags:
        return "(No Universal Truths discrepancies detected for this account)"

    lines = ["PRE-IDENTIFIED DISCREPANCIES (from deterministic cross-bureau analysis):"]

    for i, flag in enumerate(flags, 1):
        truth_label = flag.truth.upper()
        lines.append(
            f"\n  [{truth_label} #{i}] Field: {flag.field} | "
            f"Creditor: {flag.account_creditor}\n"
            f"    {flag.details}"
        )
        if flag.mismatched_values:
            for bureau, val in flag.mismatched_values.items():
                lines.append(f"      {bureau}: {val}")

    lines.append(
        "\nUse these discrepancies as evidence when identifying FCRA violations. "
        "Each discrepancy may map to one or more statutory violations in the "
        "reference material."
    )

    return "\n".join(lines)
