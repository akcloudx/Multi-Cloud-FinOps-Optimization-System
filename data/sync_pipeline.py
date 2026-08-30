"""
data/sync_pipeline.py — Azure Function / Cron Ingestion Pipeline

Implements the automated extraction & ingestion flow:
  AWS & Azure APIs (OIDC / SP Auth)
      ├──► Azure Function (hourly TimerTrigger, per-tenant due-check - see
      │     azure_function/function_app.py) OR the Manage Tenant dialog's
      │     "Run sync now" button (same function, called directly)
            ├──► Ingests normalized data into Star Schema Database
                  └──► Streamlit Dashboard queries Star Schema DB

Can be run as:
  1. Azure Function Timer Trigger (fires hourly; each tenant only actually
     syncs once its own CloudTenant.sync_interval_hours has elapsed)
  2. Standalone Cron Job / CLI (python data/sync_pipeline.py)
  3. Interactive trigger from the Manage Tenant dialog's "Run sync now" -
     this calls the exact same function directly and synchronously, no
     Azure Functions involved; both paths hit the real tenant live.
"""

import sys, os
from datetime import datetime, timedelta
import pandas as pd
from sqlalchemy.orm import Session

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from db.schema import (
    init_db, get_engine, CloudInventory, Commitment, SyncLog, CloudTenant,
    ReservationPurchase, SavingsPlanPurchase, AWSReservationPurchase, AWSSavingsPlanPurchase,
    CommitmentPriceCache,
)

# Live-tenant ingestion always operates in the "live" scope (db/schema.py's
# demo/live split) - this pipeline exists specifically to sync real cloud API
# data, never demo/benchmark data.
_MODE = "live"
from azure_conn.connector import (
    load_credentials_from_env, fetch_live_inventory, fetch_live_reservations,
    fetch_live_savings_plans, test_connection, AzureCredentials, _friendly_auth_error,
    list_accessible_subscriptions, check_role_assignments, status_from_role_check,
)
from aws.connector import (
    load_aws_credentials_from_env, test_aws_connection, fetch_live_inventory as fetch_live_aws_inventory,
    fetch_live_reservations as fetch_live_aws_reservations, fetch_live_savings_plans as fetch_live_aws_savings_plans,
    fetch_savings_plans_recommendation,
)
from pricing.azure_retail_api import refresh_retail_prices
from pricing.aws_price_list import refresh_aws_prices
from pricing.commitment_pricing import refresh_commitment_prices
from pricing.azure_vm_flexibility import refresh_vm_flexibility_groups
from pricing.commitment_mapping import derive_reservation_commitment_fields, derive_savings_plan_commitment_fields
from pricing.aws_commitment_mapping import derive_aws_reservation_commitment_fields, derive_aws_savings_plan_commitment_fields
from db.tenants import upsert_subscription


def _reservation_commitment_id(r: dict) -> str:
    return f"LIVE-RI-{r.get('reservation_id') or r.get('name') or r.get('reservation_order_id')}"


def _savings_plan_commitment_id(s: dict) -> str:
    return f"LIVE-SP-{s.get('savings_plan_id') or s.get('name') or s.get('savings_plan_order_id')}"


def _aws_reservation_commitment_id(r: dict) -> str:
    return f"LIVE-RI-{r.get('reserved_instance_id')}"


def _aws_savings_plan_commitment_id(s: dict) -> str:
    return f"LIVE-SP-{s.get('savings_plan_id')}"


def run_ingestion_pipeline(provider: str = "Azure", creds=None, force_mock: bool = False, tenant_db_id=None) -> dict:
    """
    Executes the 24-hour data extraction, normalization, and ingestion pipeline.

    Data Flow:
      Cloud API (Resource Graph / AWS API) ──► Extract & Normalize ──► Star Schema DB (SQLite / Azure SQL)

    tenant_db_id: the cloud_tenants.id this sync belongs to. Every ingested
      CloudInventory row is tagged with it, and any previously-ingested rows for
      the SAME tenant that are no longer present in this sync (deleted/renamed in
      Azure) are removed, so live inventory never accumulates stale resources.
      None only for the legacy no-tenant demo-refresh path.
    """
    engine = get_engine(provider, _MODE)
    init_db(provider, _MODE)
    now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    is_azure = (provider.upper() == "AZURE")
    live_creds = creds or (load_credentials_from_env() if is_azure else load_aws_credentials_from_env())
    has_credentials = bool(live_creds and hasattr(live_creds, 'is_complete') and live_creds.is_complete)

    status = "SUCCESS"
    synced_count = 0
    message = ""

    if has_credentials and not force_mock:
        # Live Ingestion Path (Azure Function / Cron via API)
        try:
            if is_azure:
                df_live = fetch_live_inventory(live_creds)
                records = df_live.to_dict(orient="records")
            else:
                df_live = fetch_live_aws_inventory(live_creds)
                records = df_live.to_dict(orient="records")

            # Reservations/Savings Plans require SEPARATE, elevated tenant-level
            # RBAC (Reservations Reader / Savings Plan Reader at
            # /providers/Microsoft.Capacity and /providers/Microsoft.BillingBenefits
            # respectively - see REQUIRED_TENANT_ROLES) that a real Service
            # Principal very often won't have even when subscription-level
            # Reader/Cost Management Reader (all inventory needs) is fully
            # granted - assigning it requires User Access Administrator at the
            # tenant root, a materially higher bar. Confirmed live 2026-08: a
            # missing tenant-level role must NOT abort inventory sync, which
            # has nothing to do with that permission - caught a real
            # regression where this whole function's single try/except let
            # exactly that happen (a real tenant with valid Reader/CMR showed
            # zero inventory because the RI/SP fetch below threw first).
            reservation_records, savings_plan_records = [], []
            ri_sp_error = None
            if is_azure:
                try:
                    reservation_records = fetch_live_reservations(live_creds).to_dict(orient="records")
                    savings_plan_records = fetch_live_savings_plans(live_creds).to_dict(orient="records")
                except Exception as e:
                    ri_sp_error = str(e)[:300]
                    reservation_records, savings_plan_records = [], []
            else:
                # No separate elevated-permission gap on the AWS side the
                # way Azure's tenant-level Reservations/Savings Plan Reader
                # roles are (ec2:DescribeReservedInstances/
                # rds:DescribeReservedDBInstances/savingsplans:DescribeSavingsPlans
                # are all already in REQUIRED_AWS_POLICIES, same as
                # inventory) - still isolated in its own try/except so a
                # transient failure here can't blow up an otherwise-
                # successful inventory sync, same discipline as the Azure
                # branch above.
                try:
                    reservation_records = fetch_live_aws_reservations(live_creds).to_dict(orient="records")
                    savings_plan_records = fetch_live_aws_savings_plans(live_creds).to_dict(orient="records")
                except Exception as e:
                    ri_sp_error = str(e)[:300]
                    reservation_records, savings_plan_records = [], []

            # PAYG rate lookup - Azure's Retail Prices API needs no
            # credentials (public endpoint), AWS's Price List Query API
            # needs the tenant's own real AWS credentials (pricing:GetProducts,
            # see aws/connector.py's REQUIRED_AWS_POLICIES and
            # pricing/aws_price_list.py's module docstring for the full
            # research trail). The two rate dicts use different key shapes
            # - Azure's is a plain (sku, region, os) triple; AWS's also
            # carries resource_type/redundancy since RDS Multi-AZ vs
            # Single-AZ genuinely have different rates for the same
            # instanceType/region/OS (see db/schema.py's RetailPrice
            # comment) - so each provider builds its own per-record lookup
            # key to match.
            # Real bug fixed 2026-08-30, caught live "again" by the user on a
            # production deployment - same regression class as the ri_sp_error
            # isolation above, just a different unprotected stretch of this
            # same try block: refresh_commitment_prices()/
            # refresh_vm_flexibility_groups()/commitment-field derivation
            # below were the ONE remaining piece of this function not isolated
            # - a failure in any of them (e.g. the same missing tenant-wide
            # role hitting a live Reservation/Savings Plan price lookup, or
            # any transient Azure API error) propagated all the way to the
            # OUTER except at the bottom of this function, discarding the
            # inventory `records` already fetched above and reporting the
            # WHOLE sync as FAILED with zero rows written - even though
            # inventory itself was never the problem. Isolated in its own
            # try/except now, same principle as ri_sp_error: pricing/
            # flex-group enrichment is real and useful, but secondary to
            # inventory ever getting saved at all. reservation_commitments/
            # savings_plan_commitments moved OUTSIDE this try (not declared
            # inside it) so they're always real empty lists, never undefined,
            # if an exception fires partway through.
            reservation_commitments = []
            savings_plan_commitments = []
            enrichment_error = None
            try:
                if is_azure:
                    rates = refresh_retail_prices(engine, records, provider=provider) if records else {}
                    for r in records:
                        key = (r.get("SKU"), r.get("Region"), r.get("OS"))
                        if key in rates:
                            r["PAYG Hourly Cost USD"] = rates[key]
                else:
                    rates = refresh_aws_prices(engine, records, live_creds) if records else {}
                    for r in records:
                        key = (r.get("Resource Type"), r.get("SKU"), r.get("Region"), r.get("OS"), r.get("Redundancy") or "N/A")
                        if key in rates:
                            r["PAYG Hourly Cost USD"] = rates[key]

                # Real 1yr/3yr Savings Plan + Reserved Instance rates for every
                # SKU/region/OS just synced - what savings_plan_analysis() and
                # reservation_analysis() use instead of a flat safety-buffer guess.
                # AWS branch added 2026-08-28: only Reserved Instance rates
                # actually get populated for AWS (aws_creds threaded through for
                # pricing/aws_ri_offerings.py's live Describe*Offerings calls) -
                # AWS Savings Plans pricing stays on its own separate mechanism
                # (see pricing/commitment_pricing.py's module docstring), so
                # _fetch_aws_sp_rates stays a no-op stub either way.
                if records:
                    refresh_commitment_prices(engine, records, provider=provider, aws_creds=(None if is_azure else live_creds))

                # VM Reserved Instance instance-size-flexibility group/ratio
                # cache (Azure only - AWS's equivalent is hardcoded, no live
                # fetch needed, see analysis/engine.py::_apply_aws_size_flexibility).
                if is_azure and records:
                    refresh_vm_flexibility_groups(engine, records, live_creds)

                # Derive this app's simplified Commitment rows from the raw
                # purchase records. Azure's Reservations carry no $ amount at
                # all - pricing/commitment_mapping.py needs a real 1yr/3yr rate
                # looked up separately (Retail Prices API). AWS's Reservations
                # already carry UsagePrice/FixedPrice/Duration, so
                # pricing/aws_commitment_mapping.py computes the effective
                # hourly rate directly - no separate lookup step, and no
                # "(fields, lookup)" tuple shape needed the way Azure's does.
                # Both providers' Savings Plans already carry their own $/hr
                # rate and need no lookup either way.
                if is_azure:
                    ri_pricing_lookup_rows = []
                    for r in reservation_records:
                        fields = derive_reservation_commitment_fields(r)
                        if fields is None:
                            continue
                        lookup = fields.pop("_pricing_lookup")
                        fields["commitment_id"] = _reservation_commitment_id(r)
                        reservation_commitments.append((fields, lookup))
                        ri_pricing_lookup_rows.append({
                            "resource_type": lookup["resource_type"], "sku": lookup["sku"],
                            "region": lookup["region"], "os": lookup["os"], "redundancy": lookup["redundancy"],
                        })
                    if ri_pricing_lookup_rows:
                        refresh_commitment_prices(engine, ri_pricing_lookup_rows, provider="Azure")

                    for s in savings_plan_records:
                        fields = derive_savings_plan_commitment_fields(s)
                        if fields.get("hourly_usd_commitment") is None:
                            continue   # commitment.grain wasn't 'Hourly' - can't fabricate a rate, skip rather than guess.
                        fields["commitment_id"] = _savings_plan_commitment_id(s)
                        savings_plan_commitments.append(fields)
                else:
                    for r in reservation_records:
                        fields = derive_aws_reservation_commitment_fields(r)
                        if fields is None:
                            continue   # unrecognized service, or duration wasn't a real 1yr/3yr value - see pricing/aws_commitment_mapping.py.
                        fields["commitment_id"] = _aws_reservation_commitment_id(r)
                        reservation_commitments.append(fields)

                    for s in savings_plan_records:
                        fields = derive_aws_savings_plan_commitment_fields(s)
                        if fields is None:
                            continue   # SageMaker/Database Savings Plan type - no bucket in this app's UI, see pricing/aws_commitment_mapping.py.
                        fields["commitment_id"] = _aws_savings_plan_commitment_id(s)
                        savings_plan_commitments.append(fields)
            except Exception as e:
                enrichment_error = str(e)[:300]
                reservation_commitments, savings_plan_commitments = [], []

            with Session(engine) as session:
                # Replace this tenant's prior snapshot so removed/renamed Azure
                # resources don't linger as stale rows across re-syncs.
                if tenant_db_id is not None:
                    session.query(CloudInventory).filter(
                        CloudInventory.tenant_id == tenant_db_id
                    ).delete(synchronize_session=False)
                    # Only replace the RI/SP snapshot when THIS run's fetch
                    # actually succeeded (ri_sp_error is None) - if it failed
                    # (e.g. a permission gap), any previously-synced RI/SP
                    # data for this tenant is left alone rather than wiped
                    # out by an unrelated failure.
                    if ri_sp_error is None:
                        purchase_model = ReservationPurchase if is_azure else AWSReservationPurchase
                        sp_purchase_model = SavingsPlanPurchase if is_azure else AWSSavingsPlanPurchase
                        session.query(purchase_model).filter(
                            purchase_model.tenant_id == tenant_db_id
                        ).delete(synchronize_session=False)
                        session.query(sp_purchase_model).filter(
                            sp_purchase_model.tenant_id == tenant_db_id
                        ).delete(synchronize_session=False)
                        session.query(Commitment).filter(
                            Commitment.tenant_id == tenant_db_id
                        ).delete(synchronize_session=False)

                for r in records:
                    session.merge(CloudInventory(
                        resource_id=r["Resource ID"],
                        resource_name=r["Resource Name"],
                        resource_type=r["Resource Type"],
                        resource_state=r["Resource State"],
                        region=r["Region"],
                        os=r["OS"],
                        sku=r["SKU"],
                        redundancy=r.get("Redundancy", "N/A") or "N/A",
                        ha_replica_count=r.get("HA Replicas", 0) or 0,
                        payg_hourly_usd=r.get("PAYG Hourly Cost USD", 0.0),
                        avg_daily_running_hours=r.get("Avg Daily Running Hours", 24),
                        subscription=r.get("Subscription", ""),
                        resource_group=r.get("Resource Group", "") or "",
                        availability_zone=r.get("Availability Zone", "") or "",
                        provider=provider,
                        is_orphaned=r.get("Is Orphaned", False),
                        tenant_id=tenant_db_id,
                    ))
                synced_count = len(records)

                if is_azure:
                    for r in reservation_records:
                        session.add(ReservationPurchase(**r, tenant_id=tenant_db_id))
                    for s in savings_plan_records:
                        session.add(SavingsPlanPurchase(**s, tenant_id=tenant_db_id))
                else:
                    for r in reservation_records:
                        session.add(AWSReservationPurchase(**r, tenant_id=tenant_db_id))
                    for s in savings_plan_records:
                        session.add(AWSSavingsPlanPurchase(**s, tenant_id=tenant_db_id))

                ri_written = 0
                if is_azure:
                    # Real 1yr/3yr Reservation rates, looked up from the
                    # cache refresh_commitment_prices() just populated above
                    # - keyed identically to CommitmentPriceCache's own
                    # columns. AWS needs no equivalent lookup - its
                    # reservation_commitments entries already carry a real
                    # hourly_usd_commitment, computed directly from the
                    # purchase record itself (see pricing/aws_commitment_mapping.py).
                    ri_cache = {
                        (row.resource_type, row.region, row.sku, row.os, row.redundancy, row.term): row.effective_hourly_rate_usd
                        for row in session.query(CommitmentPriceCache).filter(
                            CommitmentPriceCache.provider == "Azure", CommitmentPriceCache.instrument == "ReservedInstance",
                        ).all()
                    }
                    for fields, lookup in reservation_commitments:
                        rate = ri_cache.get((lookup["resource_type"], lookup["region"], lookup["sku"], lookup["os"], lookup["redundancy"], lookup["term_key"]))
                        if rate is None:
                            continue   # no real rate found for this SKU/region/term - don't fabricate one.
                        session.add(Commitment(**fields, hourly_usd_commitment=rate, provider="Azure", tenant_id=tenant_db_id))
                        ri_written += 1
                else:
                    for fields in reservation_commitments:
                        session.add(Commitment(**fields, provider="AWS", tenant_id=tenant_db_id))
                        ri_written += 1

                for fields in savings_plan_commitments:
                    session.add(Commitment(**fields, provider=provider, tenant_id=tenant_db_id))

                # PARTIAL (not FAILED) when inventory synced fine but RI/SP
                # fetch or pricing/flex-group enrichment hit its own error -
                # the inventory portion is real, useful progress and
                # shouldn't be reported as a failed sync.
                status = "PARTIAL" if (ri_sp_error or enrichment_error) else "SUCCESS"
                session.add(SyncLog(
                    synced_at=now_iso,
                    provider=provider,
                    status=status,
                    records_synced=synced_count,
                    source="Live API ingestion"   # caller-agnostic - triggered by either the hourly cron or the Manage Tenant "Run sync now" button, no way to distinguish here
                ))
                session.commit()

            # Subscription discovery + per-subscription RBAC check - moved
            # here from being a UI-only action (the Manage Tenant dialog's
            # "Sync subscriptions" button) so EVERY sync keeps it current,
            # not just a one-off click. Real gap found live 2026-08: a newly
            # added tenant showed "0 subscriptions" until someone manually
            # triggered this separately, and the hourly cron (which also
            # calls this same function, via azure_function/function_app.py)
            # never refreshed it AT ALL even after new subscriptions were
            # granted to the Service Principal later - each sync cycle
            # should just keep this current on its own, like everything
            # else here. Isolated in its own try/except, same reasoning as
            # the RI/SP fetch above - a failure here must not blow up an
            # otherwise-successful inventory sync.
            subscription_sync_error = None
            synced_subscription_count = 0
            if is_azure and tenant_db_id is not None:
                try:
                    live_subs = list_accessible_subscriptions(live_creds)
                    for s in live_subs:
                        role_check = check_role_assignments(live_creds, s["subscription_id"])
                        sub_status, missing, assigned = status_from_role_check(role_check)
                        upsert_subscription(
                            provider=provider, mode=_MODE, tenant_db_id=tenant_db_id,
                            subscription_id=s["subscription_id"], subscription_name=s["display_name"],
                            permission_status=sub_status, missing_role=missing, assigned_roles=assigned,
                        )
                    synced_subscription_count = len(live_subs)
                except Exception as e:
                    subscription_sync_error = str(e)[:300]

            # AWS real-time Savings Plans pricing (Compute, SageMaker, and
            # Database pools). Isolated in its own try/except, same
            # reasoning as the RI/SP fetch above - a failure here must not
            # blow up an otherwise-successful inventory sync. Staleness-
            # checked (skips re-fetching if refreshed within the last 7
            # days) since ce:GetSavingsPlansPurchaseRecommendation is a
            # PAID Cost Explorer call (see aws/connector.py's
            # fetch_savings_plans_recommendation docstring) - not worth
            # spending 5 calls on every single sync. Database is fetched
            # 1yr-only - Database Savings Plans are a 1-year-term-only
            # product (confirmed via AWS's own Savings Plans FAQ).
            if not is_azure and tenant_db_id is not None:
                try:
                    with Session(engine) as sp_session:
                        tenant = sp_session.get(CloudTenant, tenant_db_id)
                        needs_refresh = True
                        if tenant is not None and tenant.aws_sp_pricing_updated_at:
                            try:
                                last_updated = datetime.strptime(
                                    tenant.aws_sp_pricing_updated_at.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S"
                                )
                                needs_refresh = (datetime.utcnow() - last_updated) > timedelta(days=7)
                            except ValueError:
                                needs_refresh = True
                        if tenant is not None and needs_refresh:
                            tenant.aws_sp_compute_discount_1yr = fetch_savings_plans_recommendation(
                                live_creds, "COMPUTE_SP", "ONE_YEAR"
                            )
                            tenant.aws_sp_compute_discount_3yr = fetch_savings_plans_recommendation(
                                live_creds, "COMPUTE_SP", "THREE_YEARS"
                            )
                            tenant.aws_sp_sagemaker_discount_1yr = fetch_savings_plans_recommendation(
                                live_creds, "SAGEMAKER", "ONE_YEAR"
                            )
                            tenant.aws_sp_sagemaker_discount_3yr = fetch_savings_plans_recommendation(
                                live_creds, "SAGEMAKER", "THREE_YEARS"
                            )
                            tenant.aws_sp_database_discount_1yr = fetch_savings_plans_recommendation(
                                live_creds, "DB_COMPUTE_SP", "ONE_YEAR"
                            )
                            tenant.aws_sp_pricing_updated_at = now_iso
                            sp_session.commit()
                except Exception:
                    pass   # best-effort - the "Estimate only" fallback caption already covers this cleanly, no need to surface a sync-level error for it.

            if ri_sp_error:
                message = (
                    f"Inventory synced: {synced_count} resource(s). Reservation/Savings Plan fetch skipped - "
                    f"{ri_sp_error}"
                )
            elif enrichment_error:
                message = (
                    f"Inventory synced: {synced_count} resource(s). Pricing/flexibility-group enrichment "
                    f"skipped - {enrichment_error}"
                )
            else:
                message = (
                    f"Live API ingestion completed cleanly. Synced {synced_count} resources, "
                    f"{ri_written} reservation(s), and {len(savings_plan_commitments)} savings plan(s) from {provider}."
                )
            if is_azure and tenant_db_id is not None:
                if subscription_sync_error:
                    message += f" Subscription sync skipped - {subscription_sync_error}"
                else:
                    message += f" Verified {synced_subscription_count} subscription(s)."
        except Exception as e:
            status = "FAILED"
            # Same plain-language translation used everywhere else a stored
            # credential authenticates (Manage Tenant's permission checks,
            # test_connection) - a real tenant's broken client secret should
            # read the same way here as it does everywhere else, not as a
            # raw AADSTS error code dump.
            message = f"API Sync Failed: {_friendly_auth_error(str(e))}"
            with Session(engine) as session:
                session.add(SyncLog(
                    synced_at=now_iso,
                    provider=provider,
                    status="FAILED",
                    records_synced=0,
                    source="Live API ingestion"   # caller-agnostic - triggered by either the hourly cron or the Manage Tenant "Run sync now" button, no way to distinguish here
                ))
                session.commit()
    else:
        # Demo / Re-seed Sync Path
        with Session(engine) as session:
            count = session.query(CloudInventory).count()
            synced_count = count
            session.add(SyncLog(
                synced_at=now_iso,
                provider=provider,
                status="SUCCESS",
                records_synced=synced_count,
                source="Demo Sync Engine (24h Scheduled)"
            ))
            session.commit()
        message = f"24-Hour Demo sync completed cleanly. Refreshed Star Schema DB for {provider} ({synced_count} resources)."

    return {
        "status": status,
        "synced_at": now_iso,
        "records_synced": synced_count,
        "message": message,
        "provider": provider,
    }


def get_latest_sync_log(provider: str = "Azure") -> dict:
    """Returns the timestamp and status of the most recent cron ingestion run
    across ALL tenants of this provider (SyncLog isn't tenant-scoped) - not
    currently surfaced in the UI (the Manage Tenant dialog reads the
    per-tenant CloudTenant.last_sync_status/last_sync_message instead, via
    db/tenants.py's record_sync_result, which is what actually needs a
    single tenant's own result). Kept for any future global/ops view."""
    engine = get_engine(provider, _MODE)
    init_db(provider, _MODE)
    with Session(engine) as session:
        latest = session.query(SyncLog).filter_by(provider=provider).order_by(SyncLog.id.desc()).first()
        if latest:
            return {
                "synced_at": latest.synced_at,
                "status": latest.status,
                "records_synced": latest.records_synced,
                "source": latest.source,
            }
    return {
        "synced_at": "Not yet run",
        "status": "IDLE",
        "records_synced": 0,
        "source": "Azure Function (hourly cron, per-tenant interval)",
    }


if __name__ == "__main__":
    res = run_ingestion_pipeline("Azure")
    print(res)
