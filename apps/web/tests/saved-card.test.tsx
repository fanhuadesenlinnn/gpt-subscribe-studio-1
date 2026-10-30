import assert from "node:assert/strict";
import test from "node:test";
import { MantineProvider } from "@mantine/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  getPlusSavedCard,
  preparePlusSavedCard,
  refreshPlusSavedCardCheckout,
  verifyOrder,
  type BillingInput,
  type Order,
  type SavedCardState,
  type SavedCardStatus,
} from "../src/api/client";
import {
  confirmSavedCardSetup,
  isSavedCard,
  readSavedCardReturn,
  savedCardReturnUrl,
  SavedCardStatusUnknownError,
  type StripeSetupClient,
} from "../src/card-payment/stripeSetup";
import { PlusSavedCardPanel } from "../src/components/PlusSavedCardPanel";

const billing: BillingInput = {
  name: "Fixture Holder", email: "fixture@example.invalid", line1: "1 Fixture Street", city: "Berlin",
  state: "BE", postal_code: "10115", country: "DE",
};
const returnUrl = "https://studio.example.invalid/?setup_order=fixture";
const saved: SavedCardStatus = { state: "saved", payment_method_id: "pm_fixture", card: { brand: "visa", last4: "4242" } };
const order: Order = {
  id: "fixture", plan: "plus", billing_country: "DE", status: "awaiting_payment",
  checkout_url: "https://checkout.example.invalid/fresh", amount: "20", currency: "usd",
};

test("setup submits Elements, confirms with real billing and then verifies account status", async () => {
  const events: string[] = [];
  const elements = { submit: async () => { events.push("submit"); return {}; } };
  const stripe: StripeSetupClient = {
    confirmSetup: async (input) => {
      events.push("confirm");
      assert.equal(input.elements, elements);
      assert.equal(input.redirect, "if_required");
      assert.equal(input.confirmParams.return_url, returnUrl);
      assert.deepEqual(input.confirmParams.payment_method_data.billing_details, {
        name: billing.name, email: billing.email,
        address: { line1: billing.line1, city: billing.city, state: billing.state, postal_code: billing.postal_code, country: billing.country },
      });
      return { setupIntent: { id: "seti_fixture", status: "succeeded" } };
    },
  };
  const result = await confirmSavedCardSetup({ stripe, elements, billing, returnUrl,
    readStatus: async () => { events.push("status"); return saved; } });
  assert.deepEqual(events, ["submit", "confirm", "status"]);
  assert.equal(result.status, saved);
  assert.equal(isSavedCard(result.status), true);
  assert.equal(result.confirmationError, undefined);
});

test("Elements validation error stops confirmation and never marks the card saved", async () => {
  let confirms = 0;
  let reads = 0;
  await assert.rejects(confirmSavedCardSetup({
    stripe: { confirmSetup: async () => { confirms += 1; return {}; } },
    elements: { submit: async () => ({ error: { message: "fixture incomplete card" } }) }, billing, returnUrl,
    readStatus: async () => { reads += 1; return saved; },
  }), /fixture incomplete card/);
  assert.equal(confirms, 0);
  assert.equal(reads, 0);
});

test("a succeeded SetupIntent alone never becomes a saved account marker", async () => {
  const result = await confirmSavedCardSetup({
    stripe: { confirmSetup: async () => ({ setupIntent: { id: "seti_fixture", status: "succeeded" } }) },
    elements: { submit: async () => ({}) }, billing, returnUrl,
    readStatus: async () => ({ state: "sync_pending", setup_status: "succeeded" }),
  });
  assert.equal(result.status.state, "sync_pending");
  assert.equal(isSavedCard(result.status), false);
});

test("Stripe validation failure still reads the server and preserves its unsaved state", async () => {
  let reads = 0;
  const result = await confirmSavedCardSetup({
    stripe: { confirmSetup: async () => ({ error: { message: "fixture bank declined setup" } }) },
    elements: { submit: async () => ({}) }, billing, returnUrl,
    readStatus: async () => { reads += 1; return { state: "requires_payment_method" }; },
  });
  assert.equal(reads, 1);
  assert.equal(isSavedCard(result.status), false);
  assert.equal(result.confirmationError, "fixture bank declined setup");
});

test("an unknown confirmation result is checked once without replaying confirmation", async () => {
  const events: string[] = [];
  const result = await confirmSavedCardSetup({
    stripe: { confirmSetup: async () => { events.push("confirm"); throw new Error("fixture connection lost"); } },
    elements: { submit: async () => { events.push("submit"); return {}; } }, billing, returnUrl,
    readStatus: async () => { events.push("status"); return { state: "processing" }; },
  });
  assert.deepEqual(events, ["submit", "confirm", "status"]);
  assert.equal(isSavedCard(result.status), false);
  assert.equal(result.confirmationError, "fixture connection lost");
});

test("an unavailable status after confirmation requires reconciliation before retry", async () => {
  let confirms = 0;
  await assert.rejects(confirmSavedCardSetup({
    stripe: { confirmSetup: async () => { confirms += 1; return { setupIntent: { id: "seti_fixture", status: "succeeded" } }; } },
    elements: { submit: async () => ({}) }, billing, returnUrl,
    readStatus: async () => { throw new Error("fixture API unavailable"); },
  }), SavedCardStatusUnknownError);
  assert.equal(confirms, 1);
});

test("only saved and checkout_ready server states allow checkout refresh", () => {
  const unsaved: SavedCardState[] = ["not_started", "requires_payment_method", "requires_confirmation", "requires_action", "processing", "canceled", "sync_pending", "prepare_unknown"];
  for (const state of unsaved) assert.equal(isSavedCard({ state }), false, state);
  assert.equal(isSavedCard(null), false);
  assert.equal(isSavedCard({ state: "saved" }), true);
  assert.equal(isSavedCard({ state: "checkout_ready" }), true);
});

test("the bank return URL carries only a recoverable order id", () => {
  const url = new URL(savedCardReturnUrl("https://studio.example.invalid/app?client_secret=discarded#discarded", "order_fixture"));
  assert.equal(url.origin, "https://studio.example.invalid");
  assert.equal(url.pathname, "/app");
  assert.deepEqual([...url.searchParams], [["setup_order", "order_fixture"]]);
  assert.equal(url.hash, "");
});

test("return parsing strips secrets and redirect status without trusting a success query", () => {
  const result = readSavedCardReturn("https://studio.example.invalid/app?setup_order=order_fixture&setup_intent=seti_fixture&setup_intent_client_secret=secret_fixture&client_secret=secret_fixture&redirect_status=succeeded&view=order#panel");
  assert.deepEqual(result, { orderId: "order_fixture", cleanUrl: "/app?setup_order=order_fixture&view=order#panel" });
  assert.equal(readSavedCardReturn("https://studio.example.invalid/app?redirect_status=succeeded"), null);
});

test("saved-card API calls use only the three order routes and send no card payload", async () => {
  const originalFetch = globalThis.fetch;
  const calls: { url: string; method: string; body: unknown }[] = [];
  const responses = [
    { state: "requires_payment_method", setup_intent_id: "seti_fixture", client_secret: "fixture_secret", publishable_key: "pk_test_fixture" },
    saved,
    { order, payment_method_id: "pm_fixture", card: saved.card },
  ];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), method: init?.method || "GET", body: init?.body });
    return new Response(JSON.stringify(responses[calls.length - 1]), { status: 200 });
  };
  try {
    assert.equal((await preparePlusSavedCard("fixture")).setup_intent_id, "seti_fixture");
    assert.equal((await getPlusSavedCard("fixture")).state, "saved");
    assert.equal((await refreshPlusSavedCardCheckout("fixture")).order.checkout_url, order.checkout_url);
    assert.deepEqual(calls, [
      { url: "/api/orders/fixture/saved-card/prepare", method: "POST", body: undefined },
      { url: "/api/orders/fixture/saved-card", method: "GET", body: undefined },
      { url: "/api/orders/fixture/saved-card/refresh-checkout", method: "POST", body: undefined },
    ]);
  } finally { globalThis.fetch = originalFetch; }
});

test("saved-card API failures propagate the server message", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: { message: "fixture card not synced" } }), { status: 409 });
  try { await assert.rejects(refreshPlusSavedCardCheckout("fixture"), /fixture card not synced/); }
  finally { globalThis.fetch = originalFetch; }
});

test("manual subscription verification never asserts that saving a card paid an order", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = async (url, init) => {
    calls.push(String(url));
    assert.equal(init?.method, "POST");
    assert.equal(init?.body, undefined);
    return new Response(JSON.stringify({ ...order, subscription: { active: false } }), { status: 200 });
  };
  try {
    const checked = await verifyOrder("fixture");
    assert.deepEqual(calls, ["/api/orders/fixture/verify"]);
    assert.equal(checked.status, "awaiting_payment");
    assert.equal(checked.subscription?.active, false);
  } finally { globalThis.fetch = originalFetch; }
});

test("the panel is independent of the enclosing form and renders explicit non-submit controls", () => {
  const html = renderToStaticMarkup(<MantineProvider><form><PlusSavedCardPanel order={order} onOrder={() => {}} /></form></MantineProvider>);
  assert.equal((html.match(/<form\b/g) || []).length, 1);
  assert.ok(html.includes('data-testid="plus-saved-card-panel"'));
  assert.ok(html.includes("已保存，不代表已付款"));
  assert.ok(html.includes("重新提链"));
  assert.equal((html.match(/<button\b/g) || []).length, (html.match(/<button\b[^>]*type="button"/g) || []).length);
  assert.ok(!html.includes('data-testid="stripe-setup-element"'));
  assert.ok(!html.includes('type="submit"'));
  assert.ok(!html.includes("fixture_secret"));
});
