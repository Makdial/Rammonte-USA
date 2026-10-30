"use strict";

/**
 * Record the Fazakkir walkthrough videos.
 *
 *   node tools/demo/record.js            # both videos, all four languages
 *   node tools/demo/record.js user en    # one video, one language
 *
 * Frames are captured to tools/demo/frames and encoded to tools/demo/out.
 * The dev server must already be running on http://127.0.0.1:8912.
 */

const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const { record, FPS } = require("./engine");
const { LANGUAGES, USER, ADMIN } = require("./captions");
const userScenario = require("./scenario-user");
const adminScenario = require("./scenario-admin");

const ROOT = __dirname;
const FRAMES = path.join(ROOT, "frames");
const OUT = path.join(ROOT, "out");

function ffmpeg() {
  const candidates = [
    process.env.FFMPEG,
    path.join(
      ROOT, "..", "..", ".venv", "Lib", "site-packages",
      "imageio_ffmpeg", "binaries", "ffmpeg-win-x86_64-v7.1.exe"
    ),
  ].filter(Boolean);
  for (const c of candidates) if (fs.existsSync(c)) return c;
  return "ffmpeg";
}

function encode(framesDir, outFile) {
  const pattern = path.join(framesDir, "f%05d.png");
  execFileSync(ffmpeg(), [
    "-y", "-v", "error",
    "-framerate", String(FPS),
    "-i", pattern,
    "-c:v", "libx264", "-pix_fmt", "yuv420p",
    "-preset", "medium", "-crf", "20",
    "-movflags", "+faststart",
    outFile,
  ]);
}

async function run() {
  const only = process.argv.slice(2);
  const wantVideo = only[0];
  const wantLang = only[1];

  fs.mkdirSync(FRAMES, { recursive: true });
  fs.mkdirSync(OUT, { recursive: true });

  const videos = [
    { key: "user", captions: USER, scenario: userScenario },
    { key: "admin", captions: ADMIN, scenario: adminScenario },
  ];
  const langs = Object.keys(LANGUAGES);

  for (const video of videos) {
    if (wantVideo && video.key !== wantVideo) continue;
    for (const lang of langs) {
      if (wantLang && lang !== wantLang) continue;
      const name = `${video.key}-${lang}`;
      console.log(`\n=== recording ${name} (${LANGUAGES[lang].name}) ===`);
      const { outDir, frames, errors } = await record({
        name,
        lang,
        dir: LANGUAGES[lang].dir,
        scenario: (rec) => video.scenario(rec, { captions: video.captions[lang] }),
        outRoot: FRAMES,
      });
      console.log(`   frames: ${frames}`);
      const outFile = path.join(OUT, `${video.key}-${lang}.mp4`);
      encode(outDir, outFile);
      const size = (fs.statSync(outFile).size / 1024 / 1024).toFixed(2);
      console.log(`   wrote ${path.relative(ROOT, outFile)} (${size} MB, ~${(frames / FPS).toFixed(1)}s)`);
      fs.rmSync(outDir, { recursive: true, force: true });
    }
  }
  console.log("\nDone.");
}

run().catch((e) => {
  console.error(e);
  process.exit(1);
});
