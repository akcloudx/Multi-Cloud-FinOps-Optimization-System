"""
azure_function/function_app.py — Azure Function Cron Ingestion Trigger

Fires every hour (schedule="0 0 * * * *") - NOT because every tenant syncs
hourly, but because a single Azure Function TimerTrigger schedule is fixed
at deploy time and can't vary per tenant on its own. Each firing loops every
connected tenant and only actually calls run_ingestion_pipeline() for the
ones that are DUE, based on that tenant's own CloudTenant.sync_interval_hours
(set per-tenant from the Manage Tenant dialog's Sync card, default 24h) and
CloudTenant.last_synced_at. This lets different tenants run on different
cadences (hourly through daily) without needing a separate Function or
Durable Functions orchestration per tenant - the standard lightweight
pattern for per-resource scheduling at this scale.

Extracts cloud resource metadata via Azure Resource Graph / Reservations /
Billing Benefits / AWS APIs and ingests into the SQL star schema.
"""

import logging
import datetime
import azure.functions as func
from data.sync_pipeline import run_ingestion_pipeline
from db.tenants import list_tenants, get_tenant_credentials, record_sync_result
from azure_conn.connector import AzureCredentials

app = func.FunctionApp()

_DEFAULT_INTERVAL_HOURS = 24


def _is_due(t) -> bool:
    """True if this tenant has never synced, or enough time has passed
    since last_synced_at per its own sync_interval_hours (NULL/unset treated
    as the 24h default - matches app.py's Manage Tenant dialog fallback)."""
    if not t.last_synced_at:
        return True
    interval_hours = t.sync_interval_hours or _DEFAULT_INTERVAL_HOURS
    try:
        last = datetime.datetime.strptime(t.last_synced_at, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return True   # malformed/unexpected timestamp - sync rather than get stuck never syncing again.
    return (datetime.datetime.now(datetime.timezone.utc) - last) >= datetime.timedelta(hours=interval_hours)


@app.timer_trigger(schedule="0 0 * * * *", arg_name="myTimer", run_on_startup=False, use_monitor=False)
def finops_hourly_cron_sync(myTimer: func.TimerRequest) -> None:
    utc_timestamp = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()

    if myTimer.past_due:
        logging.info('The hourly cron timer is running past due!')

    logging.info(f'Starting FinOps automated extraction pipeline at {utc_timestamp}...')

    # Sync EVERY connected Azure tenant in the "live" scope (registry lives in
    # SQL DB, db/tenants.py - tenants exist in "demo" too now, but the cron
    # job only ever syncs real ones) that's actually due, each tagged with
    # its own tenant_id so tenants' inventories never mix.
    azure_tenants = list_tenants("Azure", "live")
    if not azure_tenants:
        logging.info("No Azure tenants connected - skipping Azure sync.")
    for t in azure_tenants:
        if not _is_due(t):
            logging.info(f"Azure tenant '{t.tenant_name}' not due yet (interval {t.sync_interval_hours or _DEFAULT_INTERVAL_HOURS}h) - skipping.")
            continue
        # client_secret is encrypted at rest (db/crypto.py) - decrypt right
        # here at the point of use, never store the decrypted value.
        creds = AzureCredentials(t.tenant_id, t.subscription_id, t.client_id, get_tenant_credentials(t))
        res = run_ingestion_pipeline("Azure", creds=creds, tenant_db_id=t.id)
        record_sync_result("Azure", "live", t.id, res["status"], res["message"])
        logging.info(f"Azure Sync Result [{t.tenant_name}]: {res['message']}")

    # AWS live fetch isn't implemented yet (aws/connector.py) - this currently
    # no-ops per tenant, kept here so it starts working automatically once it is.
    for t in list_tenants("AWS", "live"):
        if not _is_due(t):
            continue
        res = run_ingestion_pipeline("AWS", tenant_db_id=t.id)
        record_sync_result("AWS", "live", t.id, res["status"], res["message"])
        logging.info(f"AWS Sync Result [{t.tenant_name}]: {res['message']}")

    logging.info(f'FinOps automated extraction pipeline completed cleanly at {utc_timestamp}.')
