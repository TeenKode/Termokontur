# Security

Please avoid creating public issues containing credentials, database files or MAX user IDs. Report vulnerabilities privately to the repository maintainer via GitHub Security Advisories if available.

The dashboard is designed for a trusted LAN. Use VPN or HTTPS before external access. Never set TERMOKONTUR_SECURE_COOKIE=0 for a publicly exposed HTTPS service. Never commit .env or a production SQLite database.

Long Polling in MAX is for testing only; webhooks with trusted HTTPS are required for production per MAX documentation.
