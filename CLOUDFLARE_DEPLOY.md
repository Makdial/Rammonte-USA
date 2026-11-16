# Deploying Rammonte USA with Cloudflare

Cloudflare can play three roles for this app. Pick the level you need:

| Role | What it does | Required? |
| --- | --- | --- |
| **DNS + domain** | Registers/serves your domain (e.g. `fazakkirdmwm.com`) | Recommended |
| **R2 (object storage)** | Stores uploaded videos, MP3s, covers, books | Already configured |
| **D1 (database)** | Serverless SQLite database for Django metadata | Optional |
| **Workers (compute)** | Runs the Django app itself on Cloudflare | Advanced (Paid plan) |

> The Django app is a normal WSGI app. The simplest, recommended path is to
> host it on **Northflank** or **Render** (see `DEPLOYMENT.md`) and let
> Cloudflare handle DNS, R2 and (optionally) D1. Running the whole app on
> Cloudflare Workers is possible but requires a Paid plan and extra code.

---

## 1. Cloudflare DNS + custom domain (recommended)

1. Register/buy your domain at Cloudflare Registrar (or transfer it in).
2. In the Cloudflare dashboard → **DNS** → add the records Northflank/Render
   gives you for your service (usually an `A`/`CNAME` to the host).
3. Set the records to **DNS only** (grey cloud) while the host issues its TLS
   certificate, then you can proxy (orange cloud) if you want Cloudflare's CDN.
4. In Django env:
   - `ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com,127.0.0.1,localhost`
   - `CSRF_TRUSTED_ORIGINS=https://yourdomain.com,https://www.yourdomain.com`
   - `USE_X_FORWARDED_PROTO=1` (if behind a proxy) and `FORCE_HTTPS_COOKIES=1`.

---

## 2. Cloudflare R2 (media storage) — already set up

Your `.env` already points at R2. For production, copy these into the host's
environment (never commit them):

```
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=rammonte
R2_PUBLIC_BASE_URL=https://pub-xxxx.r2.dev   # optional, for direct serving
```

Steps in the Cloudflare dashboard:
1. **R2** → create a bucket (e.g. `rammonte`).
2. **Bucket → Settings → Public access** → enable and copy the **Public bucket URL**
   into `R2_PUBLIC_BASE_URL` (optional; leave empty for presigned URLs).
3. **R2 → Manage API Tokens** → create a token with **Object Read & Write**.
   Copy the Account ID, Access Key ID and Secret Access Key.

The app uses these in `videos/r2_storage.py` and `library` uploads.

---

## 3. Cloudflare D1 (database) — optional

Instead of Postgres, you can run the metadata DB on D1:

1. Cloudflare dashboard → **Workers & Pages → D1** → create a database.
2. Copy the **database ID**.
3. Set in the host environment:
   ```
   CLOUDFLARE_DATABASE_ID=<your-d1-database-id>
   CLOUDFLARE_BINDING=DB
   ```
   With `CLOUDFLARE_DATABASE_ID` set, the app switches to the
   `django_cf.db.backends.d1` backend and ignores `DATABASE_URL`.

Caveats (from `DEPLOYMENT.md`): the D1 backend does not support transactions,
has reduced Django Admin support, and the Workers Paid plan is recommended in
production. Media still lives in R2.

---

## 4. Full Cloudflare Workers deployment (advanced)

This runs the Django app itself on Cloudflare Workers via `django-cf` (alpha).
Requires a **Workers Paid plan** (Django loads many modules).

### 4.1 Create a Worker project

```bash
npm install -g wrangler
wrangler login
mkdir rammonte-worker && cd rammonte-worker
wrangler init rammonte-worker
```

### 4.2 Create D1 + R2 bindings

```bash
wrangler d1 create rammonte-db
wrangler r2 bucket create rammonte
```

### 4.3 wrangler.toml

```toml
name = "rammonte-worker"
main = "worker.py"
compatibility_date = "2025-01-01"
compatibility_flags = ["python_workers"]

[[d1_databases]]
binding = "DB"
database_name = "rammonte-db"
database_id = "<your-d1-database-id>"

[[r2_buckets]]
binding = "BUCKET"
bucket_name = "rammonte"
```

### 4.4 worker.py (entrypoint)

```python
import os
import django
from django.core.wsgi import get_wsgi_application
from django_cf import DjangoCF

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rammonte_usa.settings")
os.environ.setdefault("CLOUDFLARE_DATABASE_ID", "set")   # enables D1 backend
os.environ.setdefault("CLOUDFLARE_BINDING", "DB")
django.setup()

class Worker(DjangoCF):
    def get_app(self):
        return get_wsgi_application()

worker = Worker()
```

### 4.5 Settings adjustments for Workers

- Set `DEBUG=0`, `SECRET_KEY`, `ALLOWED_HOSTS` (your `*.workers.dev` and custom domain).
- Provide R2 credentials via Wrangler secrets:
  `wrangler secret put R2_ACCOUNT_ID`, etc.
- Static files: Workers do not run `collectstatic` automatically; either serve
  them from R2 or use WhiteNoise with collected files committed/bundled.
- Background threads (MP3 extraction, subtitles) are not available on Workers;
  those features will need to be moved to Workflows/Queues or run elsewhere.

### 4.6 Deploy

```bash
wrangler deploy
```

Then bind your custom domain in the Cloudflare dashboard → **Workers & Pages →
your worker → Settings → Domains & Routes**.

---

## 5. Recommended path for this app

Because the app relies on **background threads** (ffmpeg MP3 extraction,
automatic subtitles) that Workers do not support, the recommended production
setup is:

- **Compute**: Northflank or Render (always-on container) — see `DEPLOYMENT.md`.
- **DNS/domain**: Cloudflare (section 1).
- **Media storage**: Cloudflare R2 (section 2).
- **Database**: Northflank/Render Postgres, or Cloudflare D1 (section 3).

This keeps the heavy media processing on a real container while Cloudflare
handles global DNS, storage and (optionally) the database.
