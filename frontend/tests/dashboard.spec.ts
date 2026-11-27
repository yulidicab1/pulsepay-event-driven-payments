import { expect, test, type Page } from "@playwright/test";

const password = "Synthetic-browser-password-2026!";
async function signIn(page: Page, merchant = "alpha") {
  await page.goto("/");
  await page.getByLabel("Email", { exact: true }).fill(`e2e-${merchant}@example.com`);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("button", { name: "Sign out", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: `View payment ${merchant === "alpha" ? "stripe-completed" : "beta-private-payment"}`, exact: true })).toBeVisible();
}
async function select(page: Page, reference: string) {
  await page.getByRole("button", { name: `View payment ${reference}`, exact: true }).click();
  await expect(page.locator("aside").getByRole("heading", { name: reference, exact: true })).toBeVisible();
}

test.beforeEach(async ({ context, page }) => {
  await context.route("**/*", (route) => {
    const host = new URL(route.request().url()).hostname;
    return ["127.0.0.1", "localhost"].includes(host) ? route.continue() : route.abort();
  });
  await page.emulateMedia({ reducedMotion: "reduce" });
});

test("Stripe states distinguish uncertainty, review and confirmed failures", async ({ page }) => {
  await signIn(page);
  await select(page, "stripe-unknown");
  await expect(page.locator("aside").getByText("Result not confirmed", { exact: true })).toBeVisible();
  await expect(page.locator("aside .status-processing").first()).toBeVisible();
  await select(page, "stripe-review");
  await expect(page.locator("aside").getByText("Review required", { exact: true })).toBeVisible();
  await expect(page.locator("aside")).toContainText("not a confirmed payment failure");
  await expect(page.locator("aside")).not.toContainText("Failure information");
  await page.getByRole("button", { name: "REVIEW REQUIRED", exact: true }).click();
  await expect(page.getByRole("button", { name: "View payment stripe-review", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "View payment stripe-failed", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "All payments", exact: true }).click();
  await select(page, "stripe-completed");
  await expect(page.locator("aside")).toContainText("Stripe confirmed this PaymentIntent");
  await expect(page.locator("aside .provider-reference")).toHaveText(/^pi_synthetic/);
  await select(page, "stripe-failed");
  await expect(page.locator("aside")).toContainText("Failure information");
  await select(page, "stripe-action");
  await expect(page.locator("aside")).toContainText("Provider action required");
  await select(page, "historical-unclassified");
  await expect(page.locator("aside")).toContainText("historical payment has no classified provider");
  await expect(page.locator("body")).not.toContainText("acct_synthetic");
});

test("lost creation response and double submit retain one HTTP operation", async ({ page }) => {
  await signIn(page);
  const reference = `browser-lost-${Date.now()}`;
  const keys: string[] = [];
  await page.route("**/api/v1/payments", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]);
    expect(Boolean(route.request().headers()["x-csrf-token"])).toBe(true);
    expect("authorization" in route.request().headers()).toBe(false);
    const response = await route.fetch();
    if (keys.length === 1) return route.abort("failed");
    return route.fulfill({ response });
  });
  await page.getByLabel("Merchant reference", { exact: true }).fill(reference);
  await page.getByLabel("Amount", { exact: true }).fill("12.00");
  await page.getByLabel("Customer email", { exact: true }).fill("synthetic@example.com");
  await page.locator("form.payment-form").evaluate((form) => {
    (form as HTMLFormElement).requestSubmit(); (form as HTMLFormElement).requestSubmit();
  });
  await expect(page.getByRole("alert")).toContainText("Creation could not be confirmed");
  expect(keys.length).toBe(1);
  await expect(page.getByLabel("Merchant reference", { exact: true })).toHaveValue(reference);
  await page.getByRole("button", { name: "Create payment", exact: true }).click();
  await expect(page.getByText("Payment saved. Follow its status in the payment list.", { exact: true })).toBeVisible();
  expect(keys.length).toBe(2); expect(new Set(keys).size).toBe(1);
  const records = await (await page.request.get("/api/v1/payments")).json();
  const matches = records.filter((record: { merchant_reference: string }) => record.merchant_reference === reference);
  expect(matches.length).toBe(1);
  expect((await (await page.request.get(`/api/v1/payments/${matches[0].id}/events`)).json()).length).toBe(1);
  await expect(page.locator("aside .status-completed").first()).toBeVisible({ timeout: 12000 });
});

test("logout revokes the session and the next merchant sees only its own data", async ({ page, browser, context }) => {
  await signIn(page);
  const data = await (await page.request.get("/api/v1/payments")).json();
  const foreignId = data.find((record: { merchant_reference: string }) => record.merchant_reference === "stripe-completed").id;
  const saved = await context.storageState();
  const cookies = await context.cookies();
  expect(cookies.filter((cookie) => cookie.name.includes("pulsepay")).every((cookie) => cookie.httpOnly && cookie.sameSite === "Lax")).toBe(true);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeVisible();
  const revoked = await browser.newContext({ storageState: saved });
  expect((await revoked.request.get(new URL("/api/v1/payments", page.url()).toString())).status()).toBe(401);
  await revoked.close();
  await page.getByLabel("Email", { exact: true }).fill("e2e-beta@example.com");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("button", { name: "View payment beta-private-payment", exact: true })).toBeVisible();
  await expect(page.locator("body")).not.toContainText("stripe-completed");
  for (const suffix of ["", "/events", "/processing"]) expect((await page.request.get(`/api/v1/payments/${foreignId}${suffix}`)).status()).toBe(404);
  const denied = await page.request.post("/api/v1/payments", { data: { merchant_reference: "blocked-csrf", amount: "12.00", currency: "EUR", customer_email: "synthetic@example.com" } });
  expect(denied.status()).toBe(403);
});

test("slow polling reads never overlap and avoid redundant detail requests", async ({ page }) => {
  await signIn(page);
  let active = 0; let peak = 0; let entered = 0; let redundant = 0;
  page.on("request", (request) => { if (/\/metrics$|\/processing$|\/payments\/[a-f0-9-]+$/.test(new URL(request.url()).pathname)) redundant += 1; });
  await page.route("**/api/v1/payments", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    active += 1; peak = Math.max(peak, active); entered += 1;
    await new Promise((resolve) => setTimeout(resolve, 5500));
    try { await route.continue(); } finally { active -= 1; }
  });
  await expect.poll(() => entered).toBe(1);
  await new Promise((resolve) => setTimeout(resolve, 4500));
  expect(peak).toBe(1); expect(entered).toBe(1); expect(redundant).toBe(0);
  await expect.poll(() => active).toBe(0);
});

test("read errors are safe and recover without discarding the last known payments", async ({ page }) => {
  await signIn(page);
  let injected = false;
  await page.route("**/api/v1/payments", async (route) => {
    if (!injected && route.request().method() === "GET") {
      injected = true;
      return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "SYNTHETIC_PRIVATE_ERROR" }) });
    }
    return route.continue();
  });
  await expect(page.getByRole("alert")).toContainText("service is temporarily unavailable", { timeout: 10000 });
  await expect(page.locator("body")).not.toContainText("SYNTHETIC_PRIVATE_ERROR");
  await expect(page.getByRole("button", { name: "View payment stripe-completed", exact: true })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0, { timeout: 10000 });
});

test("keyboard selection and mobile layout remain usable without a visual redesign", async ({ page }, testInfo) => {
  await signIn(page);
  const reference = page.getByRole("button", { name: "View payment stripe-review", exact: true });
  await reference.focus(); await page.keyboard.press("Enter");
  await expect(page.locator("aside").getByRole("heading", { name: "stripe-review", exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("desktop-review.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.getByRole("button", { name: "UNKNOWN", exact: true }).click();
  await expect(page.getByRole("button", { name: "UNKNOWN", exact: true })).toHaveAttribute("aria-pressed", "true");
  await select(page, "stripe-unknown");
  await page.screenshot({ path: testInfo.outputPath("mobile-unknown.png"), fullPage: true });
});


test("unconfirmed logout hides merchant data and offers safe revocation retry", async ({ page }) => {
  await signIn(page);
  let calls = 0;
  await page.route("**/api/v1/auth/logout", async (route) => {
    calls += 1;
    return calls === 1 ? route.abort("failed") : route.continue();
  });
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Sign out could not be confirmed");
  await expect(page.locator("body")).not.toContainText("stripe-completed");
  await expect(page.locator(".payments-table")).toHaveCount(0);
  await page.getByRole("button", { name: "Retry sign out", exact: true }).click();
  await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeVisible();
  expect(calls).toBe(2);
});
