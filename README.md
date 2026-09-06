# Multi-Cloud FinOps Optimization System

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Deploy](https://img.shields.io/badge/deploy-Azure-0078D4.svg)](#deploy)

A live, multi-tenant cost and inventory dashboard for organizations running on **both Azure and AWS** — real inventory, real pricing, real Reserved Instance / Savings Plan coverage, rightsizing, and FinOps maturity, in one place.

![Architecture](docs/images/architecture.svg)

Deeper diagrams (components, data flow, schema): [PROJECT_ARCHITECTURE.md](PROJECT_ARCHITECTURE.md)

## Features

Live Azure + AWS ingestion (9 KQL query groups · 14+ AWS resource types) · real retail pricing · RI/SP coverage with correct per-provider scope matching · VM/EC2 rightsizing from live metrics · recommendations with quantified $ impact · FinOps Maturity scoring · passwordless DB auth · automated hourly sync

## Deploy

```bash
git clone https://github.com/akcloudx/Multi-Cloud-FinOps-Optimization-System.git
cd Multi-Cloud-FinOps-Optimization-System/azure_deploy
./deploy_all_resources_cloudshell.ps1
```

Run from [Azure Cloud Shell](https://shell.azure.com) — nothing to install, prompts once for a SQL password, fully automated after that. Every default SKU is free-tier eligible.

Three other ways to deploy (your own machine, code-only redeploy, GitHub Actions), plus prerequisites and troubleshooting: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**

## Local Development

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt   # Windows: venv\Scripts\pip
streamlit run app.py
```

Sign in with **Demo Mode** — no cloud credentials needed.

## Security

No standing credentials (Managed Identity only) · read-only IAM, verified against each provider's policy simulator · git history audited with [gitleaks](https://github.com/gitleaks/gitleaks) + [betterleaks](https://github.com/betterleaks/betterleaks). Found an issue? Please use a private security advisory, not a public one.

## Tech Stack

Python 3.12 · Streamlit · SQLAlchemy (`mssql-python`) · Azure SQL (Serverless) · `boto3` · `azure-identity`/`azure-mgmt-*` · Azure App Service + Function App

## License

MIT — see [LICENSE](LICENSE). Issues and PRs welcome.
