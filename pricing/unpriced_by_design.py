"""
pricing/unpriced_by_design.py
Registry of resource types this app deliberately, PERMANENTLY never
computes a flat $/hr PAYG rate for - distinct from a genuine, fixable
pricing gap (missing cache entry, transient API failure, not synced
yet) that a fresh sync could resolve.

Added 2026-09-02 after real feedback: a UI message ("Can't price this
by design") was first written narrowly for the one case actually being
tested (Azure SQL Database Serverless) - correctly pointed out that the
SAME underlying situation applies to other resource types too (AWS
Aurora Serverless v2 is the confirmed sibling case, already disclosed
in db/aws_seed.py), and a future tenant could hit a completely
different one. Centralizing the check here, extensible by just adding
an entry, rather than re-deriving a one-off pattern match per call site
every time a new case is found.

Every entry here must be a REAL, already-researched, disclosed reason
documented elsewhere in this codebase - never a guess. See each entry's
own comment for where that reasoning actually lives.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _Entry:
    provider: str
    resource_type: str
    sku_pattern: "re.Pattern | None"   # None = matches on resource_type alone, any SKU
    reason: str


_REGISTRY = [
    # Azure SQL Database Serverless (General Purpose + Hyperscale) - see
    # pricing/azure_retail_api.py's own module-level comment for the full
    # reasoning: bills per-vCore-second with auto-pause, can be enrolled
    # in Azure's real free-limits program (confirmed live, 2026-09-02 -
    # this app's own deployed finops-db). A flat $/hr rate would
    # systematically overstate cost for exactly the light/dev workloads
    # Serverless is chosen for - deliberately reverted from ever being
    # computed, not merely unbuilt yet.
    _Entry(
        "Azure", "Azure SQL Database", re.compile(r"^(GP|HS)_S_Gen\d+_\d+$"),
        "bills per-vCore-second (not a flat rate) and may be on Azure's free-limits program",
    ),
    # AWS Aurora Serverless v2 (MySQL/PostgreSQL) - see db/aws_seed.py's
    # AWS_RI_COVERAGE_NOTES entries for these two resource_types: "PAYG
    # rate not currently computed either (no fixed instanceType to look
    # up) - live ACU-hour usage stays unpriced until a dedicated
    # capacity-floor pricing function exists". Framed there as a
    # follow-up (not yet built) rather than "will never price" the way
    # Azure SQL Serverless above is - included here because, as of
    # today, its practical effect is identical: $0 PAYG, permanently,
    # until that follow-up is actually built (tracked separately, not
    # started).
    _Entry("AWS", "Amazon Aurora (MySQL Serverless)", None,
           "bills per-ACU-hour (not a flat rate) - no fixed instance class to price yet"),
    _Entry("AWS", "Amazon Aurora (PostgreSQL Serverless)", None,
           "bills per-ACU-hour (not a flat rate) - no fixed instance class to price yet"),
]


def unpriced_by_design_reason(provider: str, resource_type: str, sku: str) -> str | None:
    """Returns the disclosed reason if (provider, resource_type, sku) is a
    KNOWN, deliberately-permanent pricing gap - None if it's just a normal
    resource that either has a rate or is missing one for a genuine,
    fixable reason (the caller should treat None as "might be fixable by
    a sync", never as "confirmed fixable" - this function only answers
    the negative case with confidence)."""
    for entry in _REGISTRY:
        if entry.provider != provider or entry.resource_type != resource_type:
            continue
        if entry.sku_pattern is None or entry.sku_pattern.match(sku or ""):
            return entry.reason
    return None
