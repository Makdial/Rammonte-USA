"use strict";

/** What an administrator does: check the overview, manage content and users. */
module.exports = async function adminScenario(rec, { captions }) {
  const c = captions;

  await rec.goto("/");
  await rec.hush();
  await rec.step({
    step: "1",
    text: c.login,
    selector: ".navbar",
    seconds: 3.5,
  });

  await rec.login("demo_admin", "demo-admin-123");
  await rec.hush();
  await rec.hold(2);

  // The dashboard overview.
  await rec.goto("/manage/");
  await rec.step({
    step: "2",
    text: c.dash,
    selector: ".stat-grid",
    seconds: 4.5,
  });

  await rec.step({
    step: "3",
    text: c.storage,
    selector: ".storage-card",
    seconds: 4.5,
  });

  // Videos list and the home order.
  await rec.goto("/manage/videos/");
  await rec.step({
    step: "4",
    text: c.videos,
    selector: ".dash-tabs",
    seconds: 4,
  });

  await rec.goto("/manage/videos/order/");
  await rec.step({
    step: "5",
    text: c.videos,
    selector: ".dash-tabs",
    seconds: 4,
  });

  // Upload.
  await rec.goto("/upload/");
  await rec.step({
    step: "6",
    text: c.upload,
    selector: ".upload-form, form",
    seconds: 4,
  });

  // A video's own page: subtitles + audio generation.
  await rec.goto("/5");
  await rec.page.evaluate(() => {
    const bar = document.getElementById("playerBar");
    if (bar) bar.classList.add("is-visible");
  });
  await rec.step({
    step: "7",
    text: c.subtitle,
    selector: ".subtitle-admin",
    seconds: 4.5,
    before: async () => {
      await rec.page.locator(".subtitle-admin").scrollIntoViewIfNeeded().catch(() => {});
    },
  });

  await rec.step({
    step: "8",
    text: c.audio,
    selector: ".player-downloads",
    seconds: 3.5,
    before: async () => {
      await rec.page.locator(".player-downloads").scrollIntoViewIfNeeded().catch(() => {});
    },
  });

  // Comments moderation.
  await rec.goto("/manage/comments/");
  await rec.step({
    step: "9",
    text: c.comments,
    selector: ".dash-tabs",
    seconds: 4.5,
  });

  // User management.
  await rec.goto("/accounts/manage/users/");
  await rec.step({
    step: "10",
    text: c.users,
    selector: ".manage-table, table, .user-list, main",
    seconds: 4.5,
  });

  // Grant read & write to the read-only helper, changing a real control.
  await rec.spotlight("select, .access-select");
  await rec.caption("11", c.access);
  const access = rec.page.locator("select").first();
  if (await access.count()) {
    await access.selectOption("write").catch(() => {});
  }
  await rec.hold(3.5);
  await rec.hush();

  await rec.step({
    step: "12",
    text: c.settings,
    selector: ".storage-card, .dash-tabs",
    seconds: 4,
    before: async () => {
      await rec.goto("/manage/");
      await rec.page.locator(".storage-card").scrollIntoViewIfNeeded().catch(() => {});
    },
  });

  await rec.step({
    step: "13",
    text: c.done,
    selector: ".navbar-brand",
    seconds: 4,
  });
};
