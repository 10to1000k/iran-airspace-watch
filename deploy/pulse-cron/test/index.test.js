import test from "node:test";
import assert from "node:assert/strict";
import worker, { DISPATCH_URL } from "../src/index.js";

function withFetch(impl) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return impl(url, init);
  };
  return {
    calls,
    restore() {
      globalThis.fetch = original;
    },
  };
}

test("a cron tick dispatches the airwatch-tick event with the PAT", async () => {
  const stub = withFetch(async () => ({
    ok: true,
    status: 204,
    text: async () => "",
  }));
  try {
    await worker.scheduled({}, { GH_PAT: "test-pat" }, {});
  } finally {
    stub.restore();
  }

  assert.equal(stub.calls.length, 1);
  assert.equal(stub.calls[0].url, DISPATCH_URL);
  assert.equal(stub.calls[0].init.method, "POST");
  assert.equal(JSON.parse(stub.calls[0].init.body).event_type, "airwatch-tick");
  assert.equal(stub.calls[0].init.headers.Authorization, "Bearer test-pat");
});

test("a failed dispatch is logged without crashing the worker", async () => {
  const stub = withFetch(async () => ({
    ok: false,
    status: 422,
    text: async () => "spammed",
  }));
  const originalError = console.error;
  const errors = [];
  console.error = (...args) => errors.push(args.join(" "));
  try {
    await worker.scheduled({}, { GH_PAT: "test-pat" }, {});
  } finally {
    stub.restore();
    console.error = originalError;
  }
  assert.equal(stub.calls.length, 1);
  assert.match(errors.join("\n"), /422 spammed/);
});
