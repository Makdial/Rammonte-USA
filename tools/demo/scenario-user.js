"use strict";

/** What a visitor does: find a video, watch it, read captions, save it. */
module.exports = async function userScenario(rec, { captions }) {
  const c = captions;

  await rec.goto("/");
  await rec.hush();
  await rec.step({
    step: "1",
    text: c.welcome,
    selector: ".navbar-brand",
    seconds: 3.5,
  });

  await rec.step({
    step: "2",
    text: c.home,
    selector: ".video-grid, .video-list, .video-card, .home-grid",
    seconds: 4,
  });

  // Open the first demo video from the home page.
  await rec.spotlight('.video-card a, .video-grid a[href]');
  await rec.caption("3", c.play);
  await rec.page.locator('.video-card a, .video-grid a[href]').first().click();
  await rec.page.waitForLoadState("networkidle");
  await rec.installOverlay();
  await rec.hush();
  await rec.hold(3);

  // Keep the caption strip while showing the player controls.
  await rec.page.evaluate(() => {
    const bar = document.getElementById("playerBar");
    const stage = document.getElementById("playerStage");
    if (bar) bar.classList.add("is-visible");
    if (stage) stage.classList.add("controls-visible");
  });
  await rec.step({
    step: "3",
    text: c.play,
    selector: "#playerBar",
    seconds: 4,
  });

  // The captions control, which is the whole point of the recent work.
  await rec.step({
    step: "4",
    text: c.cc,
    selector: "#subtitleCcBtn",
    seconds: 4,
    before: async () => {
      await rec.page.locator("#subtitleCcBtn").scrollIntoViewIfNeeded();
    },
  });

  await rec.spotlight("#subtitleCcList");
  await rec.caption("5", c.ccLang);
  await rec.page.locator("#subtitleCcBtn").click();
  await rec.hold(2);
  // Choose the second entry so a real language (not "Off") is selected.
  const items = rec.page.locator("#subtitleCcList .cc-item");
  if ((await items.count()) > 1) {
    await items.nth(1).click();
  }
  await rec.hush();
  await rec.hold(3);
  await rec.step({
    step: "5",
    text: c.ccLang,
    selector: "#subtitleCcBtn",
    seconds: 3,
  });

  // Play so captions actually render over the picture.
  await rec.spotlight("#video-player");
  await rec.caption("6", c.ccLang);
  await rec.page.locator("#video-player").click({ position: { x: 640, y: 300 } }).catch(() => {});
  await rec.hold(4);

  // Listen-only mode.
  await rec.step({
    step: "7",
    text: c.audio,
    selector: "#mode-audio",
    seconds: 4,
    before: async () => {
      const btn = rec.page.locator("#mode-audio");
      if (await btn.count()) await btn.click();
    },
  });

  await rec.step({
    step: "8",
    text: c.speed,
    selector: "#rateSelect",
    seconds: 3,
    before: async () => {
      await rec.page.locator("#rateSelect").selectOption("1.5").catch(() => {});
    },
  });

  await rec.step({
    step: "9",
    text: c.download,
    selector: ".player-downloads",
    seconds: 3.5,
  });

  // Sign in, because liking / playlists / comments need an account.
  await rec.login("demo_viewer", "demo-viewer-123");
  await rec.hush();

  // Back to the same video, now signed in.
  await rec.goto("/");
  await rec.hush();
  await rec.page.locator('a[href="/5"]').first().click().catch(async () => {
    await rec.goto("/5");
  });
  await rec.page.waitForLoadState("networkidle");
  await rec.installOverlay();
  await rec.hush();
  await rec.page.evaluate(() => {
    const bar = document.getElementById("playerBar");
    if (bar) bar.classList.add("is-visible");
  });
  await rec.step({
    step: "10",
    text: c.like,
    selector: ".video-actions",
    seconds: 3,
    before: async () => {
      await rec.page.locator(".video-actions").scrollIntoViewIfNeeded().catch(() => {});
      await rec.page.locator('[data-vote="like"]').click().catch(() => {});
    },
  });

  await rec.step({
    step: "11",
    text: c.playlist,
    selector: "#saveToggle",
    seconds: 3,
    before: async () => {
      await rec.page.locator("#saveToggle").click().catch(() => {});
    },
  });

  await rec.step({
    step: "12",
    text: c.offline,
    selector: "#offlineToggle",
    seconds: 3.5,
    before: async () => {
      await rec.page.locator("#offlineToggle").scrollIntoViewIfNeeded().catch(() => {});
    },
  });

  // A real comment.
  await rec.spotlight(".comment-form");
  await rec.caption("13", c.like);
  await rec.page.locator("#commentBody").scrollIntoViewIfNeeded().catch(() => {});
  await rec.page.fill("#commentBody", "Barakallahu feek, very useful.");
  await rec.hold(2);
  await rec.page.locator(".comment-form button[type=submit]").click().catch(() => {});
  await rec.hush();
  await rec.hold(3);

  // Search.
  await rec.step({
    step: "14",
    text: c.search,
    selector: ".nav-search",
    seconds: 4,
    before: async () => {
      await rec.page.goto(rec.page.url().split("/")[0] + "//" + rec.page.url().split("/")[2] + "/", { waitUntil: "networkidle" });
      await rec.installOverlay();
      await rec.page.fill(".nav-search-input", "khutba");
    },
  });
  await rec.page.locator(".nav-search-btn").click().catch(() => {});
  await rec.page.waitForLoadState("networkidle");
  await rec.installOverlay();
  await rec.hush();
  await rec.hold(3.5);

  // A search that finds nothing here offers to continue on YouTube. Run it on
  // the search page itself, where a miss is actually possible, and point at the
  // link the app hands the visitor.
  await rec.spotlight(null);
  await rec.goto("/search/?q=" + encodeURIComponent("nahnu fi dhille rasulillah"));
  await rec.hush();
  await rec.step({
    step: "15",
    text: c.youtube,
    selector: ".youtube-fallback-link",
    seconds: 5,
  });

  await rec.step({
    step: "16",
    text: c.done,
    selector: ".navbar-brand",
    seconds: 4,
  });
};
