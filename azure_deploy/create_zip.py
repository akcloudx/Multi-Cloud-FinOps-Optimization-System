# azure_deploy/create_zip.py
# Positional POSIX Zip Packager for Azure App Service (Linux)
# Converts all Windows backslashes to Linux forward slashes in ZIP headers.

import os
import sys
import zipfile

app_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
temp_dir = os.environ.get("TEMP", os.environ.get("TMPDIR", "/tmp"))
zip_path = os.path.join(temp_dir, "finops_deploy.zip")

items = ["app.py", "startup.sh", "ui", "pricing", "data", "db", "azure_conn", "aws", "commitments", "analysis", "azure_sdk_vendor", ".streamlit", "requirements.txt"]

if os.path.exists(zip_path):
    try:
        os.remove(zip_path)
    except Exception:
        pass

with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
    count = 0
    for item in items:
        item_path = os.path.join(app_root, item)
        if os.path.isfile(item_path):
            zf.write(item_path, item)
            count += 1
        elif os.path.isdir(item_path):
            for root, _, files in os.walk(item_path):
                if "__pycache__" in root or ".git" in root:
                    continue
                for file in files:
                    if file.endswith(".pyc"):
                        continue
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, app_root).replace("\\", "/")
                    zf.write(full_path, rel_path)
                    count += 1

print(f"ZIP package created cleanly with {count} files using Linux POSIX paths at: {zip_path}")
