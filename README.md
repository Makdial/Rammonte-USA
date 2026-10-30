# Rammonte USA

A small YouTube-style video portal built with Django.

Videos are stored in **Cloudflare R2** (free tier, S3-compatible) and the
metadata lives in a local **SQLite** database. Only administrators can upload
videos; every visitor can browse and play them.

## Features

- **Cloudflare R2** for video files and thumbnails (free 10 GB, no egress fees).
- Serves videos from the bucket's **public URL** when configured, or from
  **short-lived presigned URLs** otherwise â€” so public access is optional.
- Uploads up to **3 GB** per video, streamed and multipart-uploaded so they
  never sit in server memory.
- **Length badges**: every thumbnail shows the duration (`12:34`) like YouTube.
  New uploads are measured at upload time; older videos fill themselves in the
  first time a page shows them (or all at once with `python manage.py
  generatedurations`).
- **SQLite** database for titles, views, likes and users â€” zero configuration.
- **Light / dark theme toggle**, remembered per browser.
- **Four languages**: English, French, Arabic and Wolof, switchable from the navbar
  (Django i18n). Arabic is right-to-left and the whole layout mirrors for it.
- **Role-based access**: every account is either a *member*, *read-only staff*
  or *read & write staff*. Only read & write staff can upload or delete videos.
- **Playlists**: any signed-in user (administrators included) can create private
  or public playlists and save videos to them.
- **Comments**: signed-in users can comment on videos; authors can delete their
  own comments and staff can moderate any comment.
- **User management**: administrators can block/unblock a user, grant or revoke
  read / read & write access, and delete accounts. Deleting a video or a user
  removes the matching rows from the database *and* the files from R2.
- **Administrator dashboard** (`/manage/`): storage usage against a quota (used,
  free and remaining space for both Cloudflare R2 and the SQLite database), plus
  the videos, comments and users in one place. Largest videos and files orphaned
  by interrupted uploads can be deleted to **free space before it runs out**.
- **Storage alerts**: an automatic email warns the administrator (once the usage
  crosses a configurable threshold, and again when the storage is full) so the
  bucket never fills up unnoticed.
- Auto-generated thumbnails (captured from the video in the browser).
- **MP3 audio track extracted automatically**: uploads get their audio pulled out
  right away, and any video that is missing one (older uploads, a failed job) is
  converted in the background the first time it is played. Viewers can switch to
  "Listen (MP3)" or download the video / MP3.
- **Download MP3 on every video card**: the audio can be saved straight from the
  home page or a channel page, without opening the video first.
- **Offline listening & installable app (PWA)**: Rammonte USA can be installed from
  the browser, and any video's MP3 can be **saved for offline playback**. The
  app shell and saved audio keep working with no connection at all.
- **Subtitles & lyrics**: upload `.vtt`, `.srt` or `.txt` files per language
  (Arabic, French, English) or as lyrics; viewers can turn each track on or off.
- **Automatic captions (CC), YouTube-style**: with one API key the server
  transcribes a video's audio in the spoken language and machine-translates it
  into every target language, so viewers pick a language from the same
  **Subtitles** row. Three back ends ship: **OpenAI** (Whisper), **Google
  Cloud** (Speech-to-Text + Cloud Translation) and **ElevenLabs** (Scribe,
  source language only).
- **Responsive layout**: works on phones and tablets, with a collapsible
  hamburger menu, fluid video grid, stacked player controls and touch-friendly
  tap targets.
- Simple like counter and per-video view counter.

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (recommended) or plain `pip`
- A free [Cloudflare](https://dash.cloudflare.com/sign-up) account with R2 enabled

## Setup

### 1. Install dependencies

```bash
uv sync
```

(or `pip install -e .`)

### 2. Create the R2 bucket

1. In the Cloudflare dashboard open **R2** and create a bucket named `fazakkir`.
2. *(Optional)* To serve videos from a public URL, open the bucket â†’
   **Settings â†’ Public access** and enable it, then copy the **Public bucket
   URL** (`https://pub-xxxxxxxx.r2.dev`). If you skip this step, the app
   generates short-lived presigned URLs instead and no public access is needed.
3. Go to **R2 â†’ API â†’ Manage API tokens** and create a token with
   **Object Read & Write** permission. Note the **Access Key ID** and
   **Secret Access Key**.
4. Your **account id** is shown in the R2 overview (and in the dashboard URL).

### 3. Configure the environment

Copy `.env.example` to `.env` and fill in the values:

```
R2_ACCOUNT_ID=your-cloudflare-account-id
R2_ACCESS_KEY_ID=your-r2-access-key-id
R2_SECRET_ACCESS_KEY=your-r2-secret-access-key
R2_BUCKET=fazakkir
R2_PUBLIC_BASE_URL=            # optional; leave empty to use presigned URLs
```

> The secret access key is server-side only â€” never commit `.env` or expose
> it in the browser. `.env` is already listed in `.gitignore`.

The dashboard can also email you before the storage fills up. This is optional:
until `EMAIL_HOST` is set the messages are simply printed to the console.

```
DEFAULT_FROM_EMAIL=Rammonte USA <no-reply@example.com>
EMAIL_HOST=smtp.gmail.com      # leave empty to print emails to the console
EMAIL_PORT=587
EMAIL_HOST_USER=you@gmail.com
EMAIL_HOST_PASSWORD=your-app-password   # Gmail: create an "App password"
EMAIL_USE_TLS=1

STORAGE_ALERT_EMAIL=makhoutech@gmail.com   # who receives the alerts
STORAGE_ALERT_THRESHOLD_PERCENT=90         # warn once this % is used
STORAGE_QUOTA_BYTES=10737418240            # 10 GB (R2 free tier)
DATABASE_QUOTA_BYTES=1073741824            # 1 GB soft limit for SQLite
```

### 4. Migrate the database

```bash
cd rammonte_usa
python manage.py migrate
```

### 5. Compile the translations

This project ships its own dependency-free `.po â†’ .mo` compiler, so no GNU
`gettext` installation is required:

```bash
python manage.py compiletranslations
```

Run this again whenever you edit a file in `locale/*/LC_MESSAGES/*.po`.

The site ships **four languages**: English, French, Arabic and Wolof. They live in
`locale/fr`, `locale/ar` and `locale/wo` (English is the source language, so it
has no catalogue). To add or change a language:

1. Add it to `LANGUAGES` in `rammonte_usa/settings.py`.
2. Copy an existing `locale/<lang>/LC_MESSAGES/django.po` as a starting point and
   fill in the `msgstr` lines.
3. Run `python manage.py compiletranslations`.

Arabic is right-to-left, so `<html>` gains `dir="rtl"` and `static/css/rtl.css`
mirrors the navbar, cards, tables and player controls. If you add another
right-to-left language, add its code to `LANGUAGE_BIDI` in the settings.

### 6. Create an administrator

```bash
python manage.py createsuperuser
```

Accounts created this way have `is_staff = True` and a *read & write* profile,
which is what unlocks the upload pages. Regular users who register through the
site are *members* and cannot upload.

### 7. Run the development server

```bash
python manage.py runserver
```

Open <http://127.0.0.1:8000/>.

## Deploying to production

The app is a standard WSGI Django project (`rammonte_usa.wsgi:application`), so
it runs on any host that supports Python.

> **Full walkthrough:** see **[DEPLOYMENT.md](DEPLOYMENT.md)** for a step-by-step
> deployment and a comparison of the free hosting options.
>
> **Recommended: [Northflank](https://northflank.com/).** Sign in with GitHub,
> connect `Makdial/fazakkir-dmwm`, and every `git push` redeploys. Its free tier
> includes a PostgreSQL database and â€” unlike Render's â€” **does not sleep**, so
> the background MP3 and subtitle conversions are never interrupted.
>
> The repository ships a `Dockerfile`, a `requirements.txt` and a `/healthz/`
> probe, so there is nothing extra to write.
>
> âš ï¸ On hosts whose filesystem is **ephemeral** (Render free and most container
> platforms), the local SQLite database is deleted on every restart. Set
> `DATABASE_URL` to a hosted Postgres or your accounts, playlists and comments
> will be lost.

Four things must change from the development defaults, and all of them are
driven by `.env`:

| Variable                     | Development          | Production                                     |
| ---------------------------- | -------------------- | ---------------------------------------------- |
| `DEBUG`                      | `1`                  | `0`                                            |
| `SECRET_KEY`                 | dev fallback         | a private key, unique per deployment           |
| `ALLOWED_HOSTS`              | `127.0.0.1,localhost`| your real hostnames, comma separated           |
| `CSRF_TRUSTED_ORIGINS`       | empty                | `https://your-domain` when served over HTTPS   |
| `DATABASE_URL`               | empty (SQLite)       | hosted Postgres on hosts with ephemeral disks  |

```bash
# 1. A private secret key. Never reuse the development one, and never commit it.
python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"

# 2. Production settings in .env
DEBUG=0
SECRET_KEY=<the key you just generated>
ALLOWED_HOSTS=your-domain.example
CSRF_TRUSTED_ORIGINS=https://your-domain.example

# 3. Bring the database up to date and collect the static files.
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py compiletranslations
```

Django **refuses to start** with `DEBUG=0` while still using the development
secret key, so a forgotten key cannot silently ship to production.

Behind a TLS-terminating proxy also set `USE_X_FORWARDED_PROTO=1`, and once the
site is served over HTTPS everywhere `SECURE_SSL_REDIRECT=1` and
`SECURE_HSTS_SECONDS=31536000`.

> **If the host serves plain HTTP**, set `FORCE_HTTPS_COOKIES=0`. Otherwise the
> session and CSRF cookies are marked `Secure`, the browser refuses to send them
> back over `http://`, and **every login fails with a 403**.

### Keep the database backed up

Videos and MP3s live in Cloudflare R2, so the only irreplaceable state is the
SQLite database. Copy it periodically:

```bash
python manage.py dumpdata --natural-foreign --natural-primary > backup.json
# or simply copy db.sqlite3 while the app is not writing to it
```

### Before you go live

- Put the app behind HTTPS. The PWA (install prompt, offline listening) and the
  background playback controls only work in a **secure context**.
- Replace the administrator credentials created in step 6 with your own.
- Set `EMAIL_HOST` and friends so the storage alerts actually leave the server;
  until then they are printed to the console.

## Roles & permissions

Every account has a `Profile` with one of three access levels:

| Level          | Can view `/manage/` and the user list | Can upload / delete / moderate |
| -------------- | :-----------------------------------: | :----------------------------: |
| Member         |                  âœ—                    |               âœ—                |
| Read-only      |                  âœ“                    |               âœ—                |
| Read & write   |                  âœ“                    |               âœ“                |

Superusers always have full access. A read & write account is also given Django
admin access (`is_staff = True`); read-only accounts are not. Administrators
change a user's level from the **Users** page in the navigation bar (visible to
staff), where they can also block (deactivate) or delete accounts. You cannot
change, block or delete your own account, and only a superuser can manage another
superuser.

## Using the app

| Action                | Who                    | Where                       |
| --------------------- | ---------------------- | --------------------------- |
| Browse / watch videos | everyone               | `/`                         |
| Comment               | everyone               | video page                  |
| Playlists             | everyone               | `/playlists/`               |
| Upload a video        | read & write staff     | `/upload/`                  |
| Delete a video        | read & write staff     | video page                  |
| Add subtitles/lyrics  | read & write staff     | video page                  |
| Dashboard & storage   | read-only / read&write | `/manage/`                  |
| Download video / MP3  | everyone               | video page & video cards    |
| Offline listening     | everyone               | `/offline/`                 |
| Manage users          | read-only / read&write | `/accounts/manage/users/`   |
| Django admin          | read & write staff     | `/admin/`                   |
| Register / sign in    | administrators         | `/accounts/...`             |

Visitors no longer need an account: each browser session is given an automatic
guest identity, so comments, likes and playlists work immediately. The
**Upload**, **Dashboard** and **Users** links only appear in the navigation bar
for the accounts that can use them. Videos, playlists (marked public) and
comments are visible to everyone, whether signed in or not.

### Playlists

Open **Playlists** to create a list, then use the **Save** button under any video
to add it to (or remove it from) one of your playlists. Private playlists are
only visible to their owner; public ones can be viewed by anyone.

### Comments

Signed-in users see a comment box under each video. Authors can delete their own
comments, and staff can delete any comment. When a comment is deleted the count
updates in place without a page reload.

### Subtitles & lyrics

Subtitles and lyrics are managed from the video page (read & write staff only):
pick a language, choose a `.vtt`, `.srt` or `.txt` file and upload. SRT files are
converted to WebVTT automatically, and a plain text file becomes a single
full-length cue (handy for lyrics). Every viewer sees a **Subtitles** row above
the player where each track can be turned on or off; the last choice is
remembered per browser â€” so viewers can enable or disable subtitles/lyrics
freely.

> Note: browsers can display one text track at a time, so enabling a new track
> replaces the previous one.

### Automatic captions & translation

Instead of uploading a file, read & write staff can let the server build the
subtitles. On the video page press **Automatic subtitles**: the MP3 is cut into
chunks, each chunk is transcribed in the spoken language
(`SUBTITLE_SOURCE_LANGUAGE`, Arabic by default) and every cue is translated into
the languages listed in `SUBTITLE_TARGET_LANGUAGES`. The result is one WebVTT
track per language, so viewers choose the caption language from the ordinary
**Subtitles** row above the player. The site knows these caption languages:
Arabic, Chinese, English, French, German, Hindi, Indonesian, Italian, Japanese,
Korean, Portuguese, Russian, Spanish, Turkish and Urdu (plus the special
`lyrics` track). They are defined once in `videos/subtitles.py`
(`CAPTION_LANGUAGES`), which drives both the manual-upload dropdown and the
labels shown in the player; automatic generation accepts any target code, and
one outside this list simply displays its own code.

Pick the engine with `SUBTITLE_PROVIDER`:

| Provider | Speech-to-text | Translation | Key |
| --- | --- | --- | --- |
| `openai` (default) | `whisper-1` | a chat model | `OPENAI_API_KEY` |
| `google` | Cloud Speech-to-Text | Cloud Translation (NMT) | `GOOGLE_API_KEY` |
| `elevenlabs` | Scribe | none (source track only) | `ELEVENLABS_API_KEY` |

For Google, create an API key in the Cloud console and enable both
**Speech-to-Text** and **Cloud Translation** on its project, then set:

```
SUBTITLE_PROVIDER=google
GOOGLE_API_KEY=your-key
SUBTITLE_SOURCE_LANGUAGE=ar
SUBTITLE_TARGET_LANGUAGES=fr,en
```

Optional Google tuning: `GOOGLE_SPEECH_MODEL` (default `latest_long`),
`GOOGLE_SPEECH_LANGUAGE` (an exact BCP-47 code such as `ar-EG` when a dialect
matters), `GOOGLE_SPEECH_ALTERNATIVE_LANGUAGES` (extra codes to accept) and
`GOOGLE_TRANSLATE_MODEL` (`nmt`, the default, or the legacy `base`).

For ElevenLabs, copy a key from **Settings â†’ API keys** and set:

```
SUBTITLE_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=your-key
SUBTITLE_SOURCE_LANGUAGE=wo
ELEVENLABS_LANGUAGE_CODE=auto
```

Optional ElevenLabs tuning: `ELEVENLABS_STT_MODEL` (default `scribe_v2`) and
`ELEVENLABS_LANGUAGE_CODE` (force one language, or `auto` to let Scribe detect
it; empty passes `SUBTITLE_SOURCE_LANGUAGE` through). ElevenLabs translates
audio (dubbing), not text, so it produces the source-language track only and
`SUBTITLE_TARGET_LANGUAGES` is skipped â€” the run still succeeds and saves the
captions it did produce. Pick `openai` or `google` when you need translated
tracks too.

Google's synchronous recogniser caps a single request at about a minute, so the
audio is chunked more finely for Google than for the others; the per-chunk
timestamps are shifted back onto the full timeline and the word timings are
grouped into readable lines. The pipeline is pluggable â€” a provider only
implements `transcribe` and `translate` (see `videos/transcription.py`).

To do every video at once, for example after switching provider, run:

```bash
python manage.py generatesubtitles              # every video with an MP3
python manage.py generatesubtitles --id 3       # just one video
python manage.py generatesubtitles --force      # retry failures too
```

### Length badges

Every video card shows its running time in the corner of the thumbnail, exactly
like YouTube. The length is measured once, when the file is uploaded, and stored
with the video.

Videos uploaded before this existed have no length recorded yet. They fill
themselves in automatically: the first time a page displays such a video, its
length is measured in the background and the badge appears on the next visit.
To do the whole library in one go instead, run:

```bash
python manage.py generatedurations          # only videos missing a length
python manage.py generatedurations --all    # re-measure every video
python manage.py generatedurations --id 3   # just one video
```

### Automatic MP3 conversion

Every upload has its audio extracted immediately. If a video still has no MP3 â€”
for example a video that was uploaded before this feature existed, or one whose
first extraction failed â€” Rammonte USA converts it automatically the first time the
page is played: the video page shows a short "Preparing the audio (MP3)â€¦"
message, a background worker streams the video from R2 through `ffmpeg`, stores
the MP3 back in R2, and the **Listen (MP3)** / **Download MP3** controls appear
as soon as it is ready. Videos that genuinely have no audio stream are marked so
they are not retried on every view; a failed conversion offers a **Try again**
button.

To convert existing videos in bulk (or retry failures) run:

```bash
python manage.py generateaudio                 # all videos without an MP3
python manage.py generateaudio --id 3          # just one video
python manage.py generateaudio --include-no-audio   # also retry silent videos
```

Administrators can do the same from the browser: open **Dashboard â†’ Videos**
(`/manage/videos/`), where an **MP3** column shows every video's audio state and
a **Generate missing MP3s** button converts whatever is still missing. The button
reports how many videos need an MP3, runs the conversions in the background one
at a time (so a large backlog does not stall the request), and shows a progress
bar until the run finishes. Only one run can be active at a time.

### Offline listening & installable app

Every video card (home page and channel pages) and the video page have a
**Download MP3** button, so anyone can keep the audio on their device. In
addition, the video page has a **Save offline** button that stores the MP3 in
the browser's own cache; open **Offline** (`/offline/`) to play it back with no
connection. Saved tracks are listed per device and can be removed again with the
âœ• button.

Because the app is a **PWA**, browsers also offer to install it (look for the
install icon in the address bar, or "Add to Home Screen" on mobile). A service
worker then keeps the interface available offline and serves the saved MP3s from
the local cache.

### Background playback

Both the video and the MP3 keep playing when the screen locks, when you switch
tabs, or when the app is sent to the background. The video page registers a
**Media Session**: the title, channel and thumbnail show up in the lock-screen
and notification-panel controls on mobile, tablet and desktop, with working
play/pause, Â±10 s seek and stop. Switching between **Video** and **Listen
(MP3)** moves those controls to whichever element is playing.

> Background playback means *the player keeps running while you use other apps*.
> It does not follow you across pages â€” leaving the video page stops playback.

> Offline features need a *secure context* (`https://` or `http://localhost`).
> They also only work per browser/device â€” saving on a phone does not download
> to a laptop.

### Deleting data

Deleting a video removes its row from the database together with its comments,
subtitle tracks and playlist entries (via `on_delete=CASCADE`), and deletes the
video, thumbnail and MP3 files from R2. Deleting a user does the same for every
video they uploaded, then removes the account and its playlists, comments and
likes.

### Dashboard & storage

Open **Dashboard** (`/manage/`) to see, at a glance:

- how many videos, users, comments and files are stored;
- the **used / free / remaining** space for the media bucket (Cloudflare R2)
  and for the SQLite database, with progress bars and a colour-coded status
  (OK / nearly full / full);
- the **largest videos**, each with a delete button, and any **files left
  behind by interrupted uploads** that can be reclaimed with one click â€” both
  ways to free space *before* it runs out;
- tabs to manage **videos**, **comments** and **users** (deletes here go through
  the same code as the rest of the site, so the database and R2 stay in sync);
- on the **Videos** tab, the MP3 state of every video and a one-click
  **Generate missing MP3s** run (see [Automatic MP3 conversion](#automatic-mp3-conversion)).

The **Alerts** panel (read & write staff) lets you set the quota, the alert
threshold and the notification email, and send a test email.

#### Storage alert emails

Rammonte USA measures the storage after every upload and whenever an administrator
opens the dashboard. When the used space crosses the configured threshold it
emails `STORAGE_ALERT_EMAIL` **once** (and again only if it becomes completely
full, or after space is freed and fills up again â€” no spam). To also check on a
schedule, run the management command and point cron / Task Scheduler at it:

```bash
python manage.py checkstorage            # measure and alert if needed
python manage.py checkstorage --test-email
```

## Project layout

```
rammonte_usa/
â”œâ”€â”€ accounts/            # registration, login, Profile (roles) & user management
â”œâ”€â”€ core/                # project utilities: permissions, manifest, service worker
â”‚   â””â”€â”€ views.py              # /manifest.webmanifest and /sw.js
â”œâ”€â”€ dashboard/           # admin dashboard: storage accounting & email alerts
â”‚   â”œâ”€â”€ models.py             # StorageMonitor (quota, measurement, alert state)
â”‚   â”œâ”€â”€ storage.py            # measure R2/SQLite usage, find reclaimable files
â”‚   â”œâ”€â”€ notifications.py      # storage alert emails
â”‚   â””â”€â”€ management/commands/checkstorage.py   # cron-able storage check
â”œâ”€â”€ videos/              # Video / VideoLike / Playlist / Comment models & views
â”‚   â”œâ”€â”€ audio.py              # ffmpeg MP3 extraction (local file or URL)
â”‚   â”œâ”€â”€ audio_tasks.py        # background MP3 generation, on-play + bulk sweeps
â”‚   â”œâ”€â”€ r2_storage.py         # Cloudflare R2 (S3) storage client
â”‚   â””â”€â”€ decorators.py         # admin_required (alias of write_required)
â”œâ”€â”€ locale/{fr,ar,wo}/  # translations (.po source + compiled .mo), see compiletranslations
â”œâ”€â”€ static/css|js/       # styling + theme toggle, rtl.css, offline.js, social/manage/dashboard CSS
â”œâ”€â”€ static/icons/        # app icon (used by the web app manifest)
â””â”€â”€ templates/base.html  # layout, navbar, theme + language toggles
```

## Notes

- Maximum video size is 3 GB (see `MAX_VIDEO_SIZE` in `videos/forms.py`).
- R2's free tier includes 10 GB of storage; the public bucket URL has no
  egress fees. The dashboard measures real usage against `STORAGE_QUOTA_BYTES`
  (change it to match your plan) and warns you before it is full.
- If `R2_PUBLIC_BASE_URL` is empty, playback and thumbnail URLs are generated
  as presigned links that live for 12 hours. They are recreated on every page
  load, so nothing needs to be refreshed manually. Set `R2_PUBLIC_BASE_URL`
  (with public access enabled) to switch to plain public URLs instead.
- The MP3 is streamed through Django at `/<video-id>/audio-stream/` (rather than
  straight from R2). This keeps it same-origin so seeking works reliably and the
  service worker can cache it; videos themselves still stream directly from R2.
- Background MP3 conversion runs in a short-lived thread inside the web process
  (via `videos/audio_tasks.py`). The `audio_status` column doubles as a lock, so
  each video is converted once even with multiple workers; use
  `python manage.py generateaudio` to do it out-of-band instead.
- Offline playback and installing the app require a secure context â€” `https://`
  in production, or `http://localhost` / `http://127.0.0.1` while developing.
- Videos uploaded before switching storage providers still point at their old
  URLs; re-upload them to store them in R2.
- To use a different bucket name, change `R2_BUCKET`.

### Testing the mobile layout

The dev server only listens on localhost, so a phone on the same Wi-Fi cannot
reach it by default. To test the responsive layout on a real device:

```bash
python manage.py runserver 0.0.0.0:8000
```

Then open `http://<your-computer-LAN-IP>:8000/` on the phone. You will also need
to add that IP (or `*`) to `ALLOWED_HOSTS` in `rammonte_usa/settings.py`, even
with `DEBUG = True`. In the browser's devtools you can simulate a phone or
tablet without a real device (toggle device toolbar, e.g. Ctrl+Shift+M).

Breakpoints used: **768px** (mobile menu, stacked player controls) and **480px**
(small phones).
