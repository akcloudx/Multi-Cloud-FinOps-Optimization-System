"""
db/__init__.py
Database layer package for Multi-Cloud FinOps Optimization System.
Uses SQLite via SQLAlchemy — swap connection string for Azure SQL migration.
"""
from db.schema import init_db, get_engine

__all__ = ["init_db", "get_engine"]
