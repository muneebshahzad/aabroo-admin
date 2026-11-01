# Aabroo Admin Portal

Standalone, read-only Aabroo order and settlement portal operated by Al Karamat. It mirrors the Sleek Space → AghaJe relationship without carrying unrelated Al Karamat services into the deployment.

## Railway deployment

1. Create a Railway project from this GitHub repository.
2. Add a PostgreSQL service.
3. Configure every variable listed in `.env.example`.
4. Deploy and add the custom domain `admin.aabroo.store`.

The application starts with Gunicorn using `railway.json`; `/health` is the deployment health check.
