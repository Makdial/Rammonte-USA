"use strict";

/**
 * Shared recording engine for the Fazakkir walkthrough videos.
 *
 * It drives a real Chromium through the app, performs the clicks and typing a
 * person would, and screenshots on a fixed interval so the result can be
 * encoded to a smooth MP4 with ffmpeg. An on-screen caption strip (and an
 * optional highlighted "spotlight" around the element being talked about)
 * stands in for narration.
 */

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const BASE = process.env.DEMO_BASE || "http://127.0.0.1:8912";
const FPS = 10;

class Recorder {
  constructor({ page, outDir, lang, dir }) {
    this.page = page;
    this.outDir = outDir;
    this.lang = lang;
    this.dir = dir; // "ltr" | "rtl"
    this.frames = 0;
    this.started = false;
  }

  /** Inject the caption strip + spotlight styles once per page. */
  async installOverlay() {
    const already = await this.page.evaluate(
      () => Boolean(document.getElementById("demo-caption"))
    );
    if (already) {
      this.started = true;
      return;
    }
    await this.page.addStyleTag({
      content: `
        #demo-caption {
          position: fixed; left: 0; right: 0; bottom: 0; z-index: 2147483647;
          padding: 18px 28px 22px;
          background: linear-gradient(transparent, rgba(0,0,0,.88) 45%);
          color: #fff;
          font: 600 25px/1.45 "Segoe UI", system-ui, sans-serif;
          direction: ${this.dir};
          text-align: ${this.dir === "rtl" ? "right" : "left"};
          text-shadow: 0 2px 6px rgba(0,0,0,.7);
          opacity: 0; transition: opacity .25s ease;
          pointer-events: none;
          max-width: 100vw;
        }
        #demo-caption.show { opacity: 1; }
        #demo-caption .step {
          display: inline-block;
          margin-${this.dir === "rtl" ? "left" : "right"}: 12px;
          padding: 3px 11px; border-radius: 999px;
          background: #d32f2f; font-size: 16px; vertical-align: middle;
        }
        #demo-spot {
          position: fixed; z-index: 2147483646; pointer-events: none;
          border: 3px solid #ff5252; border-radius: 10px;
          box-shadow: 0 0 0 9999px rgba(0,0,0,.45), 0 0 22px rgba(255,82,82,.85);
          opacity: 0; transition: opacity .2s ease, all .25s ease;
        }
        #demo-spot.show { opacity: 1; }
        #demo-badge {
          position: fixed; top: 14px; ${this.dir === "rtl" ? "left" : "right"}: 16px;
          z-index: 2147483647; pointer-events: none;
          padding: 6px 14px; border-radius: 999px;
          background: rgba(0,0,0,.72); color: #fff;
          font: 700 15px "Segoe UI", system-ui, sans-serif; letter-spacing: .04em;
        }
      `,
    });
    await this.page.evaluate(() => {
      const cap = document.createElement("div");
      cap.id = "demo-caption";
      document.body.appendChild(cap);
      const spot = document.createElement("div");
      spot.id = "demo-spot";
      document.body.appendChild(spot);
      const badge = document.createElement("div");
      badge.id = "demo-badge";
      badge.textContent = "FAZAKKIR";
      document.body.appendChild(badge);
    });
    this.started = true;
  }

  async caption(step, text) {
    if (!this.started) await this.installOverlay();
    await this.page.evaluate(
      ({ step, text }) => {
        const cap = document.getElementById("demo-caption");
        cap.innerHTML = (step ? `<span class="step">${step}</span>` : "") + text;
        cap.classList.add("show");
      },
      { step, text }
    );
  }

  async hush() {
    await this.page.evaluate(() => {
      const cap = document.getElementById("demo-caption");
      if (cap) cap.classList.remove("show");
      const spot = document.getElementById("demo-spot");
      if (spot) spot.classList.remove("show");
    });
  }

  /** Draw a spotlight box around a selector (or clear it with null). */
  async spotlight(selector) {
    if (!selector) {
      await this.page.evaluate(() => {
        const s = document.getElementById("demo-spot");
        if (s) s.classList.remove("show");
      });
      return;
    }
    const box = await this.page.locator(selector).first().boundingBox();
    if (!box) return;
    await this.page.evaluate(
      (b) => {
        const s = document.getElementById("demo-spot");
        const pad = 6;
        s.style.left = b.x - pad + "px";
        s.style.top = b.y - pad + "px";
        s.style.width = b.width + pad * 2 + "px";
        s.style.height = b.height + pad * 2 + "px";
        s.classList.add("show");
      },
      box
    );
  }

  /** Hold the current frame for `seconds`, screenshotting along the way. */
  async hold(seconds) {
    const total = Math.round(seconds * 1000);
    const tick = Math.round(1000 / FPS);
    for (let waited = 0; waited < total; waited += tick) {
      await this.page.screenshot({
        path: path.join(this.outDir, `f${String(this.frames).padStart(5, "0")}.png`),
      });
      this.frames += 1;
      await this.page.waitForTimeout(tick / 1000);
    }
  }

  /** Caption + spotlight + hold, the usual shape of a step. */
  async step({ text, step = "", selector = null, seconds = 3, before = null }) {
    if (before) await before();
    await this.caption(step, text);
    await this.spotlight(selector);
    await this.hold(seconds);
  }

  async goto(url, waitUntil = "networkidle") {
    await this.page.goto(BASE + url, { waitUntil });
    // The page owns a caption strip; re-install after every navigation.
    await this.installOverlay();
  }

  async login(username, password) {
    await this.goto("/accounts/login/");
    await this.page.fill("#id_username", username);
    await this.page.fill("#id_password", password);
    await Promise.all([
      this.page.waitForNavigation({ waitUntil: "networkidle" }),
      this.page.click(".auth-form button[type=submit]"),
    ]);
    await this.installOverlay();
  }

  /** Switch the site language the way a visitor would. */
  async setLanguage(code) {
    const button = this.page.locator(`.lang-option`, { hasText: new RegExp(`^${code}$`, "i") });
    if ((await button.count()) === 0) return;
    await Promise.all([
      this.page.waitForNavigation({ waitUntil: "networkidle" }).catch(() => {}),
      button.first().click(),
    ]);
    await this.installOverlay();
  }
}

async function record({ name, lang, dir, scenario, outRoot }) {
  const outDir = path.join(outRoot, name);
  fs.rmSync(outDir, { recursive: true, force: true });
  fs.mkdirSync(outDir, { recursive: true });

  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    deviceScaleFactor: 1,
    locale: lang === "ar" ? "ar" : lang === "fr" ? "fr-FR" : "en-US",
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));

  const rec = new Recorder({ page, outDir, lang, dir });
  await rec.installOverlay();

  try {
    await scenario(rec, { BASE, lang });
  } finally {
    await rec.hold(0.4);
    await browser.close();
  }

  if (errors.length) {
    console.log(`   page errors in ${name}:`, errors);
  }
  return { outDir, frames: rec.frames, errors };
}

module.exports = { Recorder, record, BASE, FPS };
