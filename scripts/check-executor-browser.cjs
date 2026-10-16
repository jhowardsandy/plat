// Disposable DEMO-EXECUTOR fixture only. Uses a real authenticated executor daemon.
const { chromium } = require("playwright");
const fs = require("fs");
(async () => {
  const browser = await chromium.launch({
    channel: process.env.PLAT_BROWSER_CHANNEL || "chrome",
    headless: true,
  });
  const page = await browser.newPage({
    viewport: { width: 1280, height: 900 },
  });
  const token = fs
    .readFileSync(process.env.PLAT_BROWSER_TOKEN_FILE, "utf8")
    .trim();
  const origin = process.env.PLAT_BROWSER_URL || "http://127.0.0.1:17318";
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(origin);
  const board = await page.evaluate(
    async (token) =>
      (
        await fetch("/api/board", {
          headers: { Authorization: "Bearer " + token },
        })
      ).json(),
    token,
  );
  if (board.plats.some((p) => !p.anchor.startsWith("DEMO-")))
    throw Error("Disposable demo-only database required");
  const p = board.plats.find((p) => p.anchor === "DEMO-EXECUTOR");
  if (!p || p.status !== "planned")
    throw Error("Fresh DEMO-EXECUTOR pause fixture required");
  await page.goto(origin + "/#room=" + p.id);
  await page.reload();
  await page.locator("#token").fill(token);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await page.getByRole("button", { name: "Execution", exact: true }).click();
  await page.getByRole("button", { name: "Pause", exact: true }).waitFor();
  const sizes = await Promise.all(
    ["Start", "Pause", "Resume"].map((n) =>
      page
        .getByRole("button", { name: n, exact: true })
        .evaluate((el) => el.getBoundingClientRect().width),
    ),
  );
  if (Math.max(...sizes) - Math.min(...sizes) > 1)
    throw Error("Control sizes differ");
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  await page
    .getByLabel("Reason", { exact: true })
    .fill("Browser qualification\nPause without launching any agent.");
  let lost = false;
  await page.route("**/api/execution", async (route) => {
    if (!lost) {
      lost = true;
      await route.fetch();
      await route.abort("failed");
    } else await route.continue();
  });
  await page
    .getByRole("button", { name: "Request pause", exact: true })
    .click();
  await page.locator("#retry").waitFor({ state: "visible" });
  await page.locator("#retry").click();
  await page
    .getByRole("heading", { name: "PAUSE · completed", exact: true })
    .waitFor({ timeout: 30000 });
  const room = await page.evaluate(
    async ({ token, id }) =>
      (
        await fetch("/api/room?id=" + id, {
          headers: { Authorization: "Bearer " + token },
        })
      ).json(),
    { token, id: p.id },
  );
  if (
    room.execution.commands.length !== 1 ||
    !room.execution.commands[0].acknowledged ||
    !room.execution.commands[0].finished
  )
    throw Error("Missing acknowledgement/outcome or duplicate execution");
  if (room.attempts.length) throw Error("Pause started an agent");
  await page.screenshot({
    path: "/tmp/plat-executor-browser.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  if (
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth + 1,
    )
  )
    throw Error("Mobile overflow");
  if (errors.length) throw Error(errors.join("\n"));
  console.log(
    "Browser execution controls passed: equal widths, preview, multiline reason, lost-response idempotency, machine ACK, completed pause, no agents, mobile layout.",
  );
  await browser.close();
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
