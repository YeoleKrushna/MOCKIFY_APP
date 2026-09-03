# Mockify

Mockify is a Flask application that generates AI-powered MCQ mock tests, delivers email OTP authentication through Brevo, stores history/results, and provides protected administrator analytics.

## Local development

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

Without `DATABASE_URL`, development uses `mockify.db` next to `app.py`. Do not use SQLite on Render: its filesystem is ephemeral.

Required production variables are `SECRET_KEY`, `DATABASE_URL`, `BREVO_API_KEY`, `MAIL_FROM`, `MAIL_FROM_NAME`, `GROQ_API_KEY_1`, `GROQ_MODEL`, and `SARVAM_API_KEY1`. Add further `GROQ_API_KEY_2` … `GROQ_API_KEY_5` and `SARVAM_API_KEY2` … `SARVAM_API_KEY5` only when you want key failover. Google sign-in additionally requires `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REDIRECT_URI`. Set `ENVIRONMENT=production`, `SESSION_COOKIE_SECURE=true`, `SESSION_COOKIE_SAMESITE=Lax`, `TRUST_PROXY_HEADERS=true`, and `CORS_ORIGINS=https://mockify.tech` in Render. See `.env.example` for optional tuning variables. Never commit `.env` or database files.

## PostgreSQL migration

Back up `mockify.db`, create a new empty Neon PostgreSQL database, set `DATABASE_URL` to its connection string, then run:

```powershell
$env:DATABASE_URL = "<Neon connection string>"
python migrate_sqlite_to_postgres.py
```

The importer preserves IDs and timestamps and refuses to overwrite a non-empty target database.

## Render

`render.yaml` defines a Python web service using Gunicorn and `/api/health`. In Render, add the required secret environment variables (do not place their values in Git). The start command is:

```text
gunicorn --workers 1 --threads 4 --timeout 90 --bind 0.0.0.0:$PORT app:app
```

The health endpoint returns only safe status information. Connect `mockify.tech` from the service’s Custom Domains settings, then enter precisely the DNS records Render displays.
