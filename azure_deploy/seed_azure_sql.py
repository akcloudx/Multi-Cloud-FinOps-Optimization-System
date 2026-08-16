# azure_deploy/seed_azure_sql.py
# Direct Azure SQL Database Schema Initializer and Data Seeder using pymssql
#
# Reads the connection string from DATABASE_URL - this used to have the real
# Azure SQL admin username/password hardcoded directly in this file (found
# and fixed 2026-08, alongside the same credential duplicated in
# db/schema.py and deploy_all_resources.ps1). Run it as:
#   DATABASE_URL="mssql+pymssql://<user>:<password>@<server>.database.windows.net:1433/<database>" python azure_deploy/seed_azure_sql.py

import sys
import os
from datetime import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from db.schema import Base, RetailPrice
from db.seed import seed_if_empty, INVENTORY
from db.aws_seed import seed_aws_if_empty, AWS_INVENTORY

db_url = os.getenv("DATABASE_URL")
if not db_url:
    raise SystemExit(
        "DATABASE_URL is not set. Example:\n"
        '  DATABASE_URL="mssql+pymssql://<user>:<password>@<server>.database.windows.net:1433/<database>" '
        "python azure_deploy/seed_azure_sql.py"
    )

print(f"Connecting to Azure SQL Server via DATABASE_URL...")
engine = create_engine(db_url, echo=False, future=True)

with engine.connect() as conn:
    print("[OK] Connection test successful!")

print("Creating database tables (cloud_inventory, commitments, reconciliation_log, recommendations, sync_log, retail_prices)...")
Base.metadata.create_all(engine)
print("[OK] Tables created successfully in Azure SQL Database!")

print("Seeding initial benchmark datasets into Azure SQL Database...")
import db.schema
db.schema._engines["AZURE"] = engine
db.schema._engines["AWS"]   = engine

seed_if_empty()
seed_aws_if_empty()

# Seed retail_prices table
now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
with Session(engine) as session:
    if session.query(RetailPrice).count() == 0:
        price_rows = []
        for r in INVENTORY + AWS_INVENTORY:
            price_rows.append(RetailPrice(
                sku=r["sku"],
                region=r["region"],
                os=r["os"],
                payg_rate_usd=r["payg_hourly_usd"],
                provider=r["provider"],
                fetched_at=now_str
            ))
        session.bulk_save_objects(price_rows)
        session.commit()
        print(f"[OK] Seeded {len(price_rows)} pricing records into 'retail_prices' table in Azure SQL DB.")
    else:
        print("[INFO] 'retail_prices' table already contains pricing records -- skipping.")

print("[OK] Azure SQL Database successfully initialized and seeded with all tables and pricing data!")
