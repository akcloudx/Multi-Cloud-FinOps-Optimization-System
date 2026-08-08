"""
azure_function/function_app.py — Azure Function 24-Hour Cron Ingestion Trigger

Executes automatically every 24 hours at 00:00 UTC (schedule="0 0 0 * * *").
Extracts cloud resource metadata via Azure Resource Graph / AWS APIs,
normalizes payload into FOCUS format, and ingests into Azure SQL Star Schema.
"""

import logging
import datetime
import azure.functions as func
from data.sync_pipeline import run_ingestion_pipeline
from db.tenants import list_tenants
from azure_conn.connector import AzureCredentials

app = func.FunctionApp()

@app.timer_trigger(schedule="0 0 0 * * *", arg_name="myTimer", run_on_startup=False, use_monitor=False)
def finops_24h_cron_sync(myTimer: func.TimerRequest) -> None:
    utc_timestamp = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()

    if myTimer.past_due:
        logging.info('The 24h cron timer is running past due!')

    logging.info(f'Starting FinOps 24-Hour Automated Extraction Pipeline at {utc_timestamp}...')

    # Sync EVERY connected Azure tenant (registry lives in SQL DB, db/tenants.py),
    # each tagged with its own tenant_id so tenants' inventories never mix.
    azure_tenants = list_tenants("Azure")
    if not azure_tenants:
        logging.info("No Azure tenants connected - skipping Azure sync.")
    for t in azure_tenants:
        creds = AzureCredentials(t.tenant_id, t.subscription_id, t.client_id, t.client_secret)
        res = run_ingestion_pipeline("Azure", creds=creds, tenant_db_id=t.id)
        logging.info(f"Azure Sync Result [{t.tenant_name}]: {res['message']}")

    # AWS live fetch isn't implemented yet (aws/connector.py) - this currently
    # no-ops per tenant, kept here so it starts working automatically once it is.
    for t in list_tenants("AWS"):
        res = run_ingestion_pipeline("AWS", tenant_db_id=t.id)
        logging.info(f"AWS Sync Result [{t.tenant_name}]: {res['message']}")

    logging.info(f'FinOps 24-Hour Automated Extraction Pipeline completed cleanly at {utc_timestamp}.')
