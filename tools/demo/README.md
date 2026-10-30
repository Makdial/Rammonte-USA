# Walkthrough video recorder

Generates the two "how to use Rammonte USA" videos â€” one for viewers, one for
admins â€” in English, French, Arabic and Wolof.

A real Chromium is driven through the app with Playwright. Every click and
keystroke is genuine, the frames are screenshotted on a fixed interval, and
ffmpeg stitches them into an MP4. Because there is no microphone in the loop,
each step is explained by an on-screen caption strip (and a spotlight around the
element being talked about), so the videos work with the sound off â€” which is
how most people watch instructions anyway.

## Output

`tools/demo/out/` receives eight files, roughly 40â€“60 seconds each:

| File               | Audience | Language |
| ------------------ | -------- | -------- |
| `user-en.mp4`      | Viewer   | English  |
| `user-fr.mp4`      | Viewer   | French   |
| `user-ar.mp4`      | Viewer   | Arabic (right-to-left) |
| `user-wo.mp4`      | Viewer   | Wolof    |
| `admin-en.mp4`     | Admin    | English  |
| `admin-fr.mp4`     | Admin    | French   |
| `admin-ar.mp4`     | Admin    | Arabic   |
| `admin-wo.mp4`     | Admin    | Wolof    |

## Prerequisites

```bash
# One-time: the browser automation.
cd tools/demo
npm install playwright
npx playwright install chromium

# One-time: demo content, so the recordings do not depend on real data.
cd ../../rammonte_usa
python ../tools/demo/seed_demo.py
```

## Recording

The dev server has to be running first, on the port the engine expects:

```bash
cd rammonte_usa
python manage.py runserver 127.0.0.1:8912
```

Then, from `tools/demo`:

```bash
node record.js              # everything
node record.js user         # both viewer videos
node record.js admin ar     # just the Arabic admin video
```

`DEMO_BASE` overrides the server URL, and `FFMPEG` overrides the ffmpeg binary
(by default the one bundled with `imageio-ffmpeg` is used, so nothing else has
to be installed).

## Editing the words

All captions live in `captions.js`, one block per language. Change a sentence
there and re-record; nothing else has to move. The two scenarios â€”
`scenario-user.js` and `scenario-admin.js` â€” decide *which* steps are shown and
*which* on-screen element each one points at.

## Cleaning up

```bash
cd rammonte_usa
python ../tools/demo/seed_demo.py --clean
```

That removes the demo users, videos and playlist, and the generated media
files. Nothing it creates is ever mixed into real content: every demo video
title starts with `[demo]`, so it is easy to spot (and to filter out) on the
home page.

## A note on the app changes this needed

Two small things had to be added to the app itself to make local recording
possible, and neither affects production:

* `MEDIA_URL` / `MEDIA_ROOT` in `settings.py`, plus a `DEBUG`-only route in
  `urls.py`, so the generated sample files can play in a browser. In production
  the media is served from Cloudflare R2, exactly as before.

The recording also surfaced a real bug: the share links put the *kind*
(`video` / `audio`) into the `data-share-network` attribute, so the script
looked up a network that did not exist and threw before it could fill in a
single href. The links now carry both values, and the script skips any network
it does not recognise.
