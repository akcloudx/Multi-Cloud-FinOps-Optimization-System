# NOT part of the live deployment path - confirmed 2026-08-23 via a
# repo-wide grep that no CI workflow, deploy script, or doc references
# building or running this image. The real deployment is 100% Kudu ZIP
# deploy to Azure App Service via azure_deploy/deploy_all_resources.ps1 /
# .github/workflows/deploy.yml, with Oryx doing the remote build - see
# startup.sh for that path's own real entry point. This file also
# disagrees with the live deployment on every specific: Python 3.11 here
# vs 3.12 everywhere else, port 8501 here vs the real app's 8000
# (STREAMLIT_SERVER_PORT), and a hardcoded WORKDIR that startup.sh's own
# comments explain is wrong for Oryx's compressed-destination-dir build.
# Left in place as a possible starting point for a future container-based
# deployment, but treat every value below as unverified against the real
# app until it's actually used for something.
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Expose port
EXPOSE 8501

# Run Streamlit
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
