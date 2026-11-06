"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");

const { harness, wslEvent } = require("./ipc-registrar-test-helpers");

const PICK_REJECTIONS = [
  ["wrong origin", {}, "https://remote.example/chat"],
  ["remote window", { wslOwner: { fake: "remote-window" }, wslGatewayLocal: false }, undefined],
  ["foreign port owner", { primaryPortOwner: "foreign" }, undefined],
  ["unknown port owner", { primaryPortOwner: "unknown" }, undefined],
];

// The data-home repair asks the OS for admin rights on this machine, so both
// channels take the full local-dashboard gate, and neither reaches the repair
// from a rejected sender.
function fakeDataHomeFix(route = { kind: "pkexec" }) {
  const calls = [];
  return {
    calls,
    elevationRoute: () => route,
    fixDataHome: async (opts) => {
      calls.push(opts);
      return { ok: true };
    },
  };
}

test("data-home:fix is refused outside the local dashboard and never runs the repair", async () => {
  for (const [label, options, url] of PICK_REJECTIONS) {
    const dataHomeFix = fakeDataHomeFix();
    const h = harness({ ...options, dataHomeFix });
    h.registrar.registerShell();
    await assert.rejects(
      () => h.handlers.get("data-home:fix")(wslEvent(url)),
      { message: "data-home:fix is restricted to the local dashboard" },
      label,
    );
    assert.equal(dataHomeFix.calls.length, 0, `${label}: a rejected sender must not run the repair`);
    assert.equal(await h.handlers.get("data-home:can-fix")(wslEvent(url)), false, label);
  }
});

test("data-home channels on the local dashboard run the repair and take no renderer input", async () => {
  const dataHomeFix = fakeDataHomeFix();
  const h = harness({ primaryPortOwner: "kirocrew", dataHomeFix });
  h.registrar.registerShell();
  assert.equal(await h.handlers.get("data-home:can-fix")(wslEvent()), true);
  assert.deepEqual(await h.handlers.get("data-home:fix")(wslEvent(), "/etc"), { ok: true });
  assert.equal(dataHomeFix.calls.length, 1);
  assert.deepEqual(Object.keys(dataHomeFix.calls[0]), ["log"]);
});

test("data-home:can-fix answers false where the host offers no admin prompt", async () => {
  const h = harness({ primaryPortOwner: "kirocrew", dataHomeFix: fakeDataHomeFix(null) });
  h.registrar.registerShell();
  assert.equal(await h.handlers.get("data-home:can-fix")(wslEvent()), false);
});
