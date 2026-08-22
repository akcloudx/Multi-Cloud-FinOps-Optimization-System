"""
azure_deploy/grant_managed_identity_access.py — Grants Managed Identity DB access.

Idempotently grants one or more Managed Identity principals (Web App, Function
App) access to the Azure SQL Database via CREATE USER ... FROM EXTERNAL
PROVIDER + db_datareader/db_datawriter/db_ddladmin role membership - the one
step in deploy_all_resources.ps1 that needs a real SQL connection, not just an
ARM/az CLI resource operation.

Authenticates with AzureCliCredential (the same `az login` session
deploy_all_resources.ps1 already requires), NOT a hardcoded credential -
matches this project's standing rule of no secrets in source. Requires the
caller to already be the SQL Server's Microsoft Entra admin (see
deploy_all_resources.ps1's `az sql server ad-admin create` step) and the
caller's own IP to be firewall-allowed for the duration of this connection -
both handled by deploy_all_resources.ps1 before invoking this script.

Usage: python grant_managed_identity_access.py <server_fqdn> <database> <principal> [<principal> ...]
"""

import sys

import mssql_python
from azure.identity import AzureCliCredential

ROLES = ["db_datareader", "db_datawriter", "db_ddladmin"]


def main() -> None:
    if len(sys.argv) < 4:
        print(
            "Usage: grant_managed_identity_access.py <server_fqdn> <database> <principal> [<principal> ...]",
            file=sys.stderr,
        )
        sys.exit(1)

    server, database = sys.argv[1], sys.argv[2]
    principals = sys.argv[3:]

    # "tcp:" prefix + explicit port is required, not cosmetic - without it,
    # Windows ODBC clients can default to trying the Named Pipes protocol
    # first for an unqualified Server value, which Azure SQL Database does
    # not support at all (TCP:1433 only) - fails with "Named Pipes
    # Provider: Could not open a connection to SQL Server [64]" before ever
    # attempting TCP. Confirmed as the real cause of a live deployment
    # failure 2026-08-23 (protocol-ordering issue, not a credentials or
    # firewall problem - the same connection succeeds once forced to TCP).
    conn = mssql_python.connect(
        f"Server=tcp:{server},1433;Database={database};Encrypt=yes",
        token_provider=AzureCliCredential(),
        autocommit=True,
    )
    cursor = conn.cursor()

    for principal in principals:
        cursor.execute("SELECT 1 FROM sys.database_principals WHERE name = ?", principal)
        if not cursor.fetchone():
            cursor.execute(f"CREATE USER [{principal}] FROM EXTERNAL PROVIDER")
            print(f"Created user [{principal}]")
        else:
            print(f"User [{principal}] already exists - skipped")

        for role in ROLES:
            cursor.execute(
                """
                SELECT 1 FROM sys.database_role_members rm
                JOIN sys.database_principals r ON rm.role_principal_id = r.principal_id
                JOIN sys.database_principals m ON rm.member_principal_id = m.principal_id
                WHERE r.name = ? AND m.name = ?
                """,
                role,
                principal,
            )
            if not cursor.fetchone():
                cursor.execute(f"ALTER ROLE {role} ADD MEMBER [{principal}]")
                print(f"  Added [{principal}] to {role}")
            else:
                print(f"  [{principal}] already in {role} - skipped")

    cursor.close()
    conn.close()
    print("Managed Identity database access granted.")


if __name__ == "__main__":
    main()
