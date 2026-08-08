# azure_deploy/seed_azure_sql.py
# Direct Azure SQL Database Schema Initializer and Data Seeder using pymssql

import sys
import os
import urllib.parse
from datetime import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from db.schema import Base, RetailPrice
from db.seed import seed_if_empty, INVENTORY
from db.aws_seed import seed_aws_if_empty, AWS_INVENTORY

server   = "finops-sql-e0b96fd6.database.windows.net"
database = "finops-db"
username = "finopsadmin"
password = urllib.parse.quote_plus("***REMOVED-SECRET***")

db_url = f"mssql+pymssql://{username}:{password}@{server}:1433/{database}"

print(f"Connecting to Azure SQL Server '{server}'...")
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
