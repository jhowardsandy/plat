// Run only against a disposable, demo-seeded database. Requires Playwright.
const { chromium } = require("playwright");
const fs = require("fs");
(async () => {
  const browser = await chromium.launch({
    channel: process.env.PLAT_BROWSER_CHANNEL || undefined,
    headless: true,
  });
  const page = await browser.newPage({
    viewport: { width: 1280, height: 900 },
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const token = fs
    .readFileSync(process.env.PLAT_BROWSER_TOKEN_FILE, "utf8")
    .trim();
  async function connect() {
    await page.locator("#token").fill(token);
    await page.getByRole("button", { name: "Connect", exact: true }).click();
    await page.locator("#workspace").waitFor();
  }
  await page.goto(process.env.PLAT_BROWSER_URL || "http://127.0.0.1:17318");
  await connect();
  const anchors = await page.evaluate(
    async (token) =>
      (
        await (
          await fetch("/api/board", {
            headers: { Authorization: "Bearer " + token },
          })
        ).json()
      ).plats.map((p) => p.anchor),
    token,
  );
  if (!anchors.length || anchors.some((a) => !a.startsWith("DEMO-")))
    throw Error("This check requires an isolated demo-only database");
  for (const name of [
    "Needs attention",
    "My work",
    "Products",
    "Engineers",
    "Services",
    "Repositories",
    "Reviews",
    "Delivery",
    "Tickets",
  ]) {
    await page.getByRole("button", { name, exact: true }).click();
  }
  await page.locator("#board button").first().click();
  for (const name of [
    "Evidence",
    "Sessions",
    "Decisions",
    "Coordinate",
    "Execution",
    "Overview",
  ])
    await page.getByRole("button", { name, exact: true }).click();
  await page.getByRole("button", { name: "Coordinate", exact: true }).click();
  await page
    .getByText("Assign, ask, review or leave a note", { exact: true })
    .click();
  await page
    .getByLabel("Assignment brief / message")
    .fill("Draft survives refresh.\nSecond line.");
  await page
    .getByRole("button", { name: "Expand editor", exact: true })
    .last()
    .click();
  await page.screenshot({
    path:
      (process.env.PLAT_BROWSER_OUTPUT || "/tmp") + "/plat-room-assignment.png",
    fullPage: true,
  });
  page.on("dialog", (d) => d.accept());
  await page.getByRole("button", { name: /Back to workspace/ }).click();
  const view = "Retry-" + Date.now();
  await page.locator("#list details summary").click();
  await page.getByLabel("Name", { exact: true }).fill(view);
  await page.route(
    "**/api/workspace",
    async (route) => {
      await route.fetch();
      await route.abort("failed");
    },
    { times: 1 },
  );
  await page.getByRole("button", { name: "Save view", exact: true }).click();
  await page.locator("#retry").waitFor();
  await page.reload();
  await connect();
  await page.locator("#retry").waitFor();
  await page.locator("#retry").click();
  await page.locator("#retry").waitFor({ state: "hidden" });
  const matches = await page
    .locator("#saved")
    .getByRole("button", { name: view, exact: true })
    .count();
  if (matches !== 1) throw Error("Lost-response retry duplicated saved view");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(800);
  if ((await page.locator("header").count()) !== 1)
    throw Error("Repeated shell");
  if (
    await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
  )
    throw Error("Overflow");
  await page.screenshot({
    path:
      (process.env.PLAT_BROWSER_OUTPUT || "/tmp") +
      "/plat-room-mobile-viewport.png",
  });
  if (errors.length) throw Error(errors.join("\n"));
  console.log(
    "All navigation/room tabs; assignment editor; durable retry after lost response + reload; mobile layout passed.",
  );
  await browser.close();
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
