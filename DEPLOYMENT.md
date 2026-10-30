# Deploying Rammonte USA

This guide covers **where to host the site**, with a focus on the platforms that
behave like Replit: **connect a GitHub repository and the app deploys itself on
every push.**

---

## 1. Short answer

**Use [Northflank](https://northflank.com/)'s free Sandbox tier.** It connects
straight to your GitHub repo, **never sleeps** (so the background MP3 and
subtitle jobs are not interrupted), includes a **free PostgreSQL database**, and
gives you a real HTTPS `*.code.run` URL.

The repository already contains everything needed: a `Dockerfile`, a
`requirements.txt`, and a `/healthz/` probe for the platform's health check.

---

## 2. Deploying to Northflank (step by step)

### Step 1 â€” Create the account and project

1. Sign up at <https://northflank.com> with your GitHub account.
2. **Create project** â†’ name it `fazakkir` â†’ choose **Northflank Cloud** â†’ pick a
   region near you â†’ **Create Project**.

### Step 2 â€” Add the free PostgreSQL database

1. In the project, open the **Addons** tab â†’ **Add database** â†’ **PostgreSQL**.
2. Choose the smallest (free) size and create it.
3. Copy its **`DATABASE_URL`** connection string (find it in the database's
   **Environment** tab).

> You do **not** need an external database such as Neon here. Northflank's free
> tier includes one database per project.

### Step 3 â€” Create the service from your GitHub repo

1. **Services** tab â†’ **Create service** â†’ **Combined service**.
2. **Source**: choose GitHub and connect `Makdial/fazakkir-dmwm`, branch `master`.
3. **Build options**:
   - Build type: **Dockerfile**
   - Dockerfile path: `/Dockerfile` (repository root)
   - Build context: repository root
4. **Resources**: the smallest free instance.
5. **Networking** â†’ add a port:

   | Port | Protocol | Public domain |
   | --- | --- | --- |
   | `8000` | HTTP | enable |

6. **Health checks** â†’ path `/healthz/` on port `8000`. This endpoint also checks
   the database, so Northflank holds traffic back until the database is ready.

7. **Environment** â†’ add the variables below. Use a **Secrets group** so they are
   stored once and never shown again.

8. **Create service**. Northflank builds the image and deploys it.

### Step 4 â€” Environment variables

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | from the PostgreSQL addon (step 2) |
| `SECRET_KEY` | `python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"` |
| `DEBUG` | `0` |
| `ALLOWED_HOSTS` | every hostname the site answers on, plus `127.0.0.1,localhost` for the health probe |
| `CSRF_TRUSTED_ORIGINS` | `https://` + each of those hostnames, comma separated |
| `R2_ACCOUNT_ID` | from your local `rammonte_usa/.env` |
| `R2_ACCESS_KEY_ID` | from your local `rammonte_usa/.env` |
| `R2_SECRET_ACCESS_KEY` | from your local `rammonte_usa/.env` |
| `R2_BUCKET` | `fazakkir` |

Optional:

| Variable | Enables |
| --- | --- |
| `OPENAI_API_KEY` (with `SUBTITLE_PROVIDER=openai`) | automatic subtitles and translations |
| `GOOGLE_API_KEY` (with `SUBTITLE_PROVIDER=google`) | automatic subtitles via Cloud Speech-to-Text + Cloud Translation |
| `ELEVENLABS_API_KEY` (with `SUBTITLE_PROVIDER=elevenlabs`) | automatic captions via Scribe (source language only) |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` | real storage alert emails (use a Gmail **App password**) |
| `JOB_STALE_MINUTES` | how long an interrupted conversion waits before being retried (default 30) |

The R2 values are deliberately **not** in the GitHub repository (`.env` is
git-ignored). Copy them from your own `.env`.

### Step 5 â€” Create your administrator

Open the service's **Shell** tab and run:

```bash
python manage.py createsuperuser
```

> In practice the admin was created with the CLI (`northflank exec service`)
> running the same code inside the container, which avoids copy/paste errors.

### Step 6 â€” Verify

Open the public URL Northflank gave you and check:

- the home page is styled (WhiteNoise is serving the collected CSS/JS);
- a video plays;
- `/healthz/` returns `{"status": "ok", "database": true}`;
- `/manage/` loads once you are signed in.

### Step 7 â€” Updating the site

Every `git push` to `master` triggers a rebuild and redeploy. The container's
start command runs `python manage.py migrate --noinput` before starting
gunicorn, so schema changes are applied automatically.

### The live deployment

The site is deployed and running on its own domain:

| | |
| --- | --- |
| **URL** | `https://fazakkirdmwm.com` |
| Also serves | `https://www.fazakkirdmwm.com` |
| Fallback | `https://http--fazakkir-web--7c9pf6vp6x6y.code.run` |
| Project / service | `fazakkir` / `fazakkir-web` |
| Database | `fazakkir-db` (PostgreSQL 18, free tier, europe-west) |
| Region | `europe-west` |
| Admin | `MakhouTech` (superuser, read & write) |
| Certificate | Let's Encrypt, issued automatically and renewed by Northflank |

The domain is registered at Cloudflare Registrar and served through
Northflank, which terminates TLS. Cloudflare remains the DNS provider
(DNS-only records) so Northflank can issue and renew the certificate.

### Why the custom domain matters

Some ISPs return `NXDOMAIN` for `*.code.run` when queried over IPv6 while
resolving it correctly over IPv4. Windows prefers IPv6, so the site appeared to
be unreachable on an otherwise healthy network. A domain behind Cloudflare's
DNS avoids that class of problem entirely, because the resolver is asked for a
name it already serves rather than one it has never cached.

Three gotchas learned during the real deployment, worth remembering for any
similar host:

- **`ALLOWED_HOSTS` must include `127.0.0.1` and `localhost`.** Northflank's
  health check agent probes the container on `127.0.0.1:8000`; without those
  entries every probe returns `400 DisallowedHost` and the platform keeps
  restarting the container.
- **The port must be `public`.** A port created with `"public": false` has no
  live DNS record, so the `code.run` name never resolves even though the
  container is healthy.
- **`POST /runtime-environment` replaces, it does not merge.** Every call wipes
  the variables set by previous calls, so the last write wins and everything
  else silently disappears. Send the **complete** map in one request. Losing
  `DATABASE_URL` this way is especially nasty: the service falls back to SQLite
  on the container's ephemeral disk, `/healthz/` still answers
  `{"status": "ok", "database": true}`, and all data is erased on the next
  deploy â€” while looking perfectly healthy.

  Always confirm afterwards by reading the environment back, and check the
  engine the app actually resolved:

  ```bash
  northflank exec service --project fazakkir --service fazakkir-web \
    --cmd 'python -c "import django;django.setup();from django.conf import settings;print(settings.DATABASES[\"default\"][\"ENGINE\"])"'
  ```

### `SECURE_SSL_REDIRECT` must stay off on Northflank

Northflank terminates TLS at its load balancer and forwards plain HTTP to the
container. `USE_X_FORWARDED_PROTO=1` makes Django treat public requests as
HTTPS, so the redirect is not needed â€” and turning it on actively breaks the
health probe, which arrives at `127.0.0.1:8000` with no `X-Forwarded-Proto`
header and would be bounced to `https://127.0.0.1:8000`, where nothing is
listening on TLS. The container then restarts in a loop.

Keep `FORCE_HTTPS_COOKIES=1` and `SECURE_SSL_REDIRECT=0`.

### Driving Northflank from the CLI

The official CLI can create the whole stack once you are signed in:

```bash
npm install -g @northflank/cli
northflank login                 # opens a browser; your token never reaches me
northflank create project --name fazakkir
northflank create service combined --project fazakkir   # combined = build + run
```

Prefer the dashboard (or the API) for anything involving **secrets**: the
Cloudflare R2 keys and `SECRET_KEY` belong in the Northflank environment, not on
a command line where they would end up in shell history.

---

## 3. What Northflank's free tier gives you

| | Behaviour |
| --- | --- |
| Deploys from GitHub | Yes, on every push |
| Sleeps when idle | **No** â€” background conversions are never cut off |
| Free database | 1 PostgreSQL addon per project |
| Persistent disk | Not needed, because the database is external |
| Free resources | 2 services, 2 jobs, 1 addon |
| HTTPS | Automatic on the `*.code.run` domain |
| Cost | Volumes are billed (~$0.15/GB/month) and are **not** needed here |

Northflank suits this app better than a sleeping host because the MP3 extraction
and the automatic subtitles run as background threads; a host that sleeps would
interrupt them. (The app can recover from that â€” see "Background jobs" below â€”
but it means re-running a conversion.)

---

## 4. Render, as an alternative

If you prefer Render, the repository ships a `render.yaml` blueprint and the same
`Dockerfile` also works there. One important difference: Render's free web
services **sleep after 15 minutes** and have an **ephemeral filesystem**, so you
must set `DATABASE_URL` to a hosted Postgres such as
[Neon](https://neon.com) (free forever, no card). The app supports
`DATABASE_URL` natively.

### Background jobs and sleeping hosts

The MP3 extraction and the automatic subtitles run as background threads inside
the web process. When the host sleeps or redeploys, those threads are killed
mid-`ffmpeg`. The app handles this: a job stuck "processing" for longer than
`JOB_STALE_MINUTES` (30 by default) is considered abandoned, marked retryable,
and picked up again on the next page view. You do not have to intervene, but a
very long conversion of a large file may need to be started twice.

---

## 5. The alternatives, honestly

| Option | GitHub deploy | Always on | Free database | Verdict |
| --- | :---: | :---: | :---: | --- |
| **Northflank Sandbox** | Yes | **Yes** | Yes (1 addon) | **Best fit for this app** |
| Render + Neon | Yes | No (sleeps) | Via Neon | Good, but sleeps |
| PythonAnywhere | No | Yes | Yes (SQLite) | Easiest, but manual, no GitHub link |
| Railway | Yes | Yes | No free tier | Trial credits only |
| Koyeb | Yes | â€” | Volumes now **paid** | Pivoted to AI hosting |
| Replit | Yes | Yes | Yes | Free tier publishes **one** app, credit-metered |

### If you want zero setup: PythonAnywhere

It does **not** connect to GitHub â€” you clone and configure once by hand â€” but it
needs no database service, no Dockerfile and no secrets dashboard. The full
walkthrough is in [the last section](#6-pythonanywhere-walkthrough-free).

---

## 6. PythonAnywhere walkthrough (free)

1. Sign up at <https://www.pythonanywhere.com/user/signup/> â†’ **Beginner** (free)
   plan, then verify your email.
2. Open a **Bash console**:

   ```bash
   git clone https://github.com/Makdial/fazakkir-dmwm.git
   cd fazakkir-dmwm/rammonte_usa
   python3.13 -m venv ~/fazakkir-venv
   source ~/fazakkir-venv/bin/activate
   pip install -e .
   ```

3. Create `.env`:

   ```ini
   DEBUG=0
   SECRET_KEY=<generated>
   ALLOWED_HOSTS=<yourusername>.pythonanywhere.com
   R2_ACCOUNT_ID=...
   R2_ACCESS_KEY_ID=...
   R2_SECRET_ACCESS_KEY=...
   R2_BUCKET=fazakkir
   ```

   PythonAnywhere's persistent disk means SQLite is safe here, so `DATABASE_URL`
   can stay empty.

4. Prepare the database and static files:

   ```bash
   python manage.py migrate
   python manage.py collectstatic --noinput
   python manage.py compiletranslations
   python manage.py createsuperuser
   ```

5. On the **Web** tab: add a **Manual configuration** Python 3.13 app, set the
   virtualenv to `/home/<yourusername>/fazakkir-venv`, and map static files:

   | URL | Directory |
   | --- | --- |
   | `/static/` | `/home/<yourusername>/fazakkir-dmwm/rammonte_usa/staticfiles` |

6. Replace the **WSGI configuration file** with:

   ```python
   import os
   import sys

   sys.path.insert(0, "/home/<yourusername>/fazakkir-venv/bin")
   sys.path.insert(1, "/home/<yourusername>/fazakkir-venv/lib/python3.13/site-packages")
   os.environ["DJANGO_SETTINGS_MODULE"] = "rammonte_usa.settings"
   sys.path.insert(0, "/home/<yourusername>/fazakkir-dmwm/rammonte_usa")

   from rammonte_usa.wsgi import application
   ```

7. Press **Reload** and open `https://<yourusername>.pythonanywhere.com/`.

> PythonAnywhere free can only reach an allowlist of external sites over
> HTTP(S). Cloudflare R2 (`r2.cloudflarestorage.com`) and GitHub are both on
> that list, so storage and deploys work.

---

## 7. Updating the site later

With Northflank and GitHub connected this is just:

```bash
git push
```

Northflank rebuilds and redeploys automatically, and the container runs
`manage.py migrate` on start.

Manually (PythonAnywhere or SSH):

```bash
git pull
source ~/fazakkir-venv/bin/activate
pip install -e .
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py compiletranslations
```

---

## 8. Operations

### Backups

Videos and MP3s are in R2 and are safe. On Northflank the database is a managed
PostgreSQL addon; take a dump from the database's **Backups** tab. With SQLite
(PythonAnywhere) or a local dev setup, dump it yourself:

```bash
python manage.py dumpdata --natural-foreign --natural-primary > backup.json
```

### HTTPS

Northflank and PythonAnywhere both provide HTTPS automatically. The PWA install
prompt, offline listening and the background playback controls only work in a
secure context, so this is required, not optional.

### Before you go live

- Set a private `SECRET_KEY` (never reuse the development one).
- Set your own administrator credentials.
- Add `EMAIL_HOST_PASSWORD` (a Gmail **App password**) so storage alerts are
  actually sent; until then they print to the log.

---

## Quick reference

| Task | How |
| --- | --- |
| Deploy a change | `git push` |
| Apply migrations | automatic on Northflank (start command); `manage.py migrate` elsewhere |
| Back up the database | Northflank database â†’ Backups; or `manage.py dumpdata > backup.json` |
| Create an admin | `python manage.py createsuperuser` |
| Convert missing MP3s | `python manage.py generateaudio` |
| Generate subtitles | `python manage.py generatesubtitles` |
| Check storage | `python manage.py checkstorage` |
| Health probe | `/healthz/` |