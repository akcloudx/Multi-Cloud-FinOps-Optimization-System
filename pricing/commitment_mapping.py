"""
pricing/commitment_mapping.py
Maps a real Azure Reservation / Savings Plan purchase record
(ReservationPurchase / SavingsPlanPurchase - schema-matched to Azure's actual
REST API, see db/schema.py) into this app's own simplified Commitment table -
the table RI Coverage / Savings Plan Analysis actually read
(commitments/existing_commitments.py). NOT a 1:1 mirror: different quantity
units (reserved_qty = "resources covered" vs quantity/commitment_amount =
real Azure units), a different resource-type taxonomy, and (for Reservations)
a computed, not raw, hourly_usd_commitment.

Two of Azure's own real, confirmed ambiguities can't be resolved from the
purchase record alone - not a mapping gap better code would fix:
  - A SQL Database and a SQL Elastic Pool reservation share the identical
    reserved_resource_type ("SqlDatabases") and sku_name convention - Azure's
    API genuinely does not distinguish them (see db/seed.py's
    RESERVATION_PURCHASES comment on this).
  - A Savings Plan's sku_name for anything other than Compute is undocumented
    anywhere in Microsoft's own API docs (only "Compute_Savings_Plan" appears
    in official examples - see db/seed.py's note above SAVINGS_PLAN_PURCHASES).
Both are still mapped (best-effort, defaulted) rather than dropped, but
flagged via is_inferred_mapping/mapping_note (db/schema.py's Commitment
table) so the UI can show a caveat rather than presenting a guess as fact.

reserved_resource_type values this module has no mapping for at all (real
Azure enum members this app's inventory/pricing doesn't model at all, e.g.
SuseLinux, RedHat, VMwareCloudSimple, SapHana, AVS) intentionally get no
Commitment row - Commitment.hourly_usd_commitment is NOT NULL, and
fabricating a rate for a type this app can't price would be worse than
leaving it out. The raw ReservationPurchase row is stored either way (see
data/sync_pipeline.py) - only the simplified/derived row is skipped.
"""

from typing import Optional

# Reservation-side sku_name tier token -> this app's TIER code (matches
# pricing/sku_mapping.py's _SQL_RESERVATION_PREFIX convention). GP/BC pass
# through unchanged; Hyperscale is a documented naming shift specific to the
# reservation side (full word "HyperScale", not the short "HS" the
# consumption side and this app's own SKU convention use).
_SQL_TIER_TOKEN_TO_CODE = {"GP": "GP", "BC": "BC", "HYPERSCALE": "HS"}

# Reservation-side Redis Enterprise sku_name is a bare size code with no
# family prefix (e.g. "B10", not "Balanced_B10" - see db/seed.py's
# RESERVATION_PURCHASES comment). Inferred from the leading letter - NOT
# fully unambiguous for every family (e.g. "E..." could be Enterprise or
# EnterpriseFlash), so this branch always sets is_inferred_mapping=True.
_REDIS_ENTERPRISE_LETTER_TO_FAMILY = {
    "B": "Balanced", "M": "MemoryOptimized", "C": "ComputeOptimized", "F": "FlashOptimized", "E": "Enterprise",
}


def derive_reservation_commitment_fields(purchase: dict) -> Optional[dict]:
    """Maps one ReservationPurchase-shaped dict (see db/schema.py) into a
    Commitment-shaped dict, MINUS hourly_usd_commitment and commitment_id.
    Reservations carry no $ amount at all in their own API response - the
    caller (data/sync_pipeline.py) must fetch that separately via
    pricing/commitment_pricing.py, using the returned '_pricing_lookup'
    sub-dict as the query key, and set hourly_usd_commitment (and
    commitment_id) before writing the row. Returns None for a
    reserved_resource_type this app has no pricing/inventory model for at
    all - see module docstring."""
    resource_type = (purchase.get("reserved_resource_type") or "").strip()
    sku_name = (purchase.get("sku_name") or "").strip()
    quantity = purchase.get("quantity") or 0
    region = purchase.get("location") or ""
    term_iso = purchase.get("term") or "P1Y"
    term_display = "3-year" if term_iso == "P3Y" else "1-year"

    is_inferred = False
    note = None

    if resource_type == "VirtualMachines":
        # quantity IS the real instance count - identical semantics to this
        # app's reserved_qty="resources covered" convention, no conversion.
        scope_resource_type, scope_sku, reserved_qty = "Compute", sku_name, quantity

    elif resource_type == "DedicatedHost":
        # Reservation-side sku_name uses a space ("DSv3 Type3" - confirmed
        # against db/seed.py's real example); this app's SKU convention
        # (pricing/sku_mapping.py's _plan_dedicated_host) uses a hyphen.
        scope_resource_type, scope_sku, reserved_qty = "Azure Dedicated Host", sku_name.replace(" ", "-"), quantity

    elif resource_type == "RedisCache":
        if sku_name.upper().startswith("P") and sku_name[1:2].isdigit():
            # Classic Redis only ever sells Reservations for the Premium
            # tier (verified in pricing/sku_mapping.py's _plan_redis), so a
            # bare "P2"-style reservation code is unambiguously Premium -
            # reconstruct the "{code}_Premium" shape that resolver expects.
            scope_resource_type, scope_sku, reserved_qty = "Azure Cache for Redis", f"{sku_name}_Premium", quantity
        else:
            letter = sku_name[:1].upper()
            family = _REDIS_ENTERPRISE_LETTER_TO_FAMILY.get(letter, "Enterprise")
            scope_resource_type = "Azure Cache for Redis Enterprise"
            scope_sku, reserved_qty = f"{family}_{sku_name}", quantity
            is_inferred = True
            note = (f"Redis Enterprise family inferred from sku_name's leading letter ('{letter}' -> {family}) - "
                    "the reservation API's bare code doesn't carry the family name, and this isn't unambiguous "
                    "for every family (e.g. Enterprise vs EnterpriseFlash both start with 'E').")

    elif resource_type == "SqlDataWarehouse":
        # Reservations are only ever sold at the DW100c unit (pricing/
        # sku_mapping.py's _plan_synapse) - quantity IS the real number of
        # DW100c units purchased. analysis/engine.py's coverage matching
        # requires scope_sku to equal an inventory resource's SKU EXACTLY
        # (e.g. "DW500c") - it does not scale by reserved_qty - so this
        # reconstructs the target pool size as "DW{quantity*100}c" (matching
        # this app's existing demo Commitment convention: a 5-unit purchase
        # -> "DW500c"). _plan_synapse's own DWU regex then auto-computes the
        # correct reservation_multiplier from that SKU string, so the rate
        # lookup below comes back already scaled to the full pool - no
        # separate scaling needed here.
        if not quantity:
            return None
        scope_resource_type, scope_sku, reserved_qty = "Azure Synapse Analytics", f"DW{quantity * 100}c", 1

    elif resource_type == "SqlDatabases":
        parts = sku_name.split("_")
        if len(parts) < 2:
            return None
        family_token, tier_token = parts[0].upper(), parts[1].upper()
        generation = parts[-1] if len(parts) >= 4 else "Gen5"
        tier_code = _SQL_TIER_TOKEN_TO_CODE.get(tier_token)
        if not tier_code or not quantity:
            return None
        # quantity (real vCores purchased) is folded into scope_sku here -
        # this purchase covers exactly 1 resource sized at that many vCores,
        # matching this app's existing demo Commitment convention exactly
        # (e.g. purchase quantity=4 -> scope_sku "GP_Gen5_4", reserved_qty=1).
        scope_sku = f"{tier_code}_{generation}_{quantity}"
        reserved_qty = 1
        if family_token == "SQLMI":
            scope_resource_type = "Azure SQL Managed Instance"
        else:
            # Azure's Reservation API genuinely cannot distinguish a SQL
            # Database reservation from a SQL Elastic Pool reservation -
            # both report the identical reserved_resource_type/sku_name
            # (confirmed in db/seed.py's RESERVATION_PURCHASES comments).
            # Defaulting to SQL Database rather than dropping the row.
            scope_resource_type = "Azure SQL Database"
            is_inferred = True
            note = ("Azure's reservation API cannot distinguish a SQL Database reservation from a SQL Elastic Pool "
                    "reservation for this SKU pattern - defaulted to SQL Database.")

    else:
        return None

    # scope_os: the purchase record carries no OS field at all - it's a
    # property of the covered resource, not the reservation. For Compute
    # this genuinely doesn't affect pricing either (pricing/sku_mapping.py's
    # os_license_is_separable always resolves Reservations to the
    # OS-agnostic compute-only meter), so "N/A" here is honest, not a guess
    # with a pricing consequence.
    scope_os = "N/A"
    # scope_redundancy: only SQL Database/Elastic Pool price differently by
    # redundancy, and that setting also isn't in the purchase record -
    # defaulted to the more common Locally Redundant, flagged as inferred.
    scope_redundancy = "N/A"
    if resource_type == "SqlDatabases":
        scope_redundancy = "Locally Redundant"
        redundancy_note = ("Redundancy (Zone vs Locally Redundant) isn't in the reservation purchase record - "
                            "defaulted to Locally Redundant; verify against the actual covered resource.")
        note = f"{note} {redundancy_note}" if note else redundancy_note
        is_inferred = True

    return {
        "commitment_type":     "Reserved Instance" if resource_type in ("VirtualMachines", "DedicatedHost", "RedisCache") else "Reserved Capacity",
        "scope_sku":           scope_sku,
        "scope_resource_type": scope_resource_type,
        "scope_region":        region,
        "scope_os":            scope_os,
        "scope_redundancy":    scope_redundancy,
        "reserved_qty":        reserved_qty,
        "term":                term_display,
        "expiry_date":         purchase.get("expiry_date"),
        "is_inferred_mapping": is_inferred,
        "mapping_note":        note,
        "_pricing_lookup": {
            "resource_type": scope_resource_type, "sku": scope_sku, "region": region,
            "os": scope_os, "redundancy": scope_redundancy,
            "term_key": "3yr" if term_iso == "P3Y" else "1yr",
        },
    }


def derive_savings_plan_commitment_fields(purchase: dict) -> dict:
    """Maps one SavingsPlanPurchase-shaped dict into a full Commitment-shaped
    dict, hourly_usd_commitment included - unlike Reservations, a Savings
    Plan's own commitment.amount (at commitment.grain == 'Hourly') IS
    directly the real $/hr rate, no separate pricing lookup needed.
    hourly_usd_commitment comes back None when commitment.grain isn't
    'Hourly' (defensive - Azure's docs don't show this happening in
    practice) - the caller must skip writing the row in that case, same
    "don't fabricate a rate" discipline as the Reservation side."""
    sku_name = (purchase.get("sku_name") or "").strip()
    term_iso = purchase.get("term") or "P1Y"
    is_inferred = sku_name != "Compute_Savings_Plan"
    commitment_type = "Savings Plan for Databases" if is_inferred else "Savings Plan for Compute"
    note = None
    if is_inferred:
        note = ("Savings Plan type inferred by elimination - Azure's documented sku_name examples only cover "
                f"Compute ('Compute_Savings_Plan'); this purchase's sku_name is '{sku_name}'.")

    grain = (purchase.get("commitment_grain") or "").strip()
    amount = purchase.get("commitment_amount")
    hourly_rate = amount if grain == "Hourly" and amount is not None else None

    expiry_date_time = purchase.get("expiry_date_time") or ""
    return {
        "commitment_type":       commitment_type,
        "scope_sku":             "Any Compute" if commitment_type == "Savings Plan for Compute" else "Any Database",
        "scope_resource_type":   None,
        "scope_region":          "Global",
        "scope_os":              "Any" if commitment_type == "Savings Plan for Compute" else "N/A",
        "scope_redundancy":      "N/A",
        "hourly_usd_commitment": hourly_rate,
        "reserved_qty":          0,
        "term":                  "3-year" if term_iso == "P3Y" else "1-year",
        "expiry_date":           expiry_date_time[:10] if expiry_date_time else None,
        "is_inferred_mapping":   is_inferred,
        "mapping_note":          note,
    }
