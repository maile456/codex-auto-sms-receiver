#!/usr/bin/env node

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function createFakeElement(selector, options = {}) {
  const classes = new Set();

  return {
    selector,
    attributes: {},
    dataset: options.dataset || {},
    disabled: false,
    files: [],
    innerHTML: "",
    listeners: {},
    style: {},
    textContent: "",
    value: "",
    classList: {
      add(name) {
        classes.add(name);
      },
      remove(name) {
        classes.delete(name);
      },
      toggle(name, force) {
        if (force) {
          classes.add(name);
        } else {
          classes.delete(name);
        }
      },
    },
    addEventListener(type, handler) {
      this.listeners[type] = handler;
    },
    append() {},
    click() {
      this.listeners.click?.({ target: this });
    },
    remove() {},
    select() {},
    setAttribute(name, value) {
      this.attributes[name] = String(value);
    },
  };
}

function loadPageScript() {
  const htmlPath = path.join(__dirname, "..", "docs", "index.html");
  const html = fs.readFileSync(htmlPath, "utf8");
  const match = html.match(/<script>\s*([\s\S]*?)\s*<\/script>\s*<\/body>/);

  assert.ok(match, "expected docs/index.html to contain one inline script");

  const elements = new Map();
  const formatButtons = ["sub2api", "cpa", "cockpit", "9router", "codex", "axonhub", "codexmanager"].map((format) =>
    createFakeElement(`[data-format="${format}"]`, { dataset: { format } })
  );

  const document = {
    body: createFakeElement("body"),
    createElement(selector) {
      return createFakeElement(selector);
    },
    execCommand() {
      return true;
    },
    querySelector(selector) {
      if (!elements.has(selector)) {
        elements.set(selector, createFakeElement(selector));
      }
      return elements.get(selector);
    },
    querySelectorAll(selector) {
      return selector === "[data-format]" ? formatButtons : [];
    },
  };

  const context = {
    TextDecoder,
    TextEncoder,
    URL: {
      createObjectURL() {
        return "blob:test";
      },
      revokeObjectURL() {},
    },
    atob,
    btoa,
    clearTimeout,
    console,
    document,
    navigator: {
      clipboard: {
        async writeText() {},
      },
    },
    setTimeout,
  };

  vm.runInNewContext(match[1], context, { filename: "docs/index.html" });

  return { elements, formatButtons };
}

function dispatch(element, type) {
  assert.equal(typeof element.listeners[type], "function", `missing ${type} listener on ${element.selector}`);
  element.listeners[type]({ target: element });
}

function jwtWithPayload(payload) {
  return [
    Buffer.from(JSON.stringify({ alg: "none", typ: "JWT" })).toString("base64url"),
    Buffer.from(JSON.stringify(payload)).toString("base64url"),
    "sig",
  ].join(".");
}

function testSub2apiAccountUsesAccessTokenExpiry() {
  const { elements } = loadPageScript();
  const input = elements.get("#session-input");
  const output = elements.get("#output");

  input.value = JSON.stringify({
    user: {
      email: "mark@example.com",
    },
    accessToken: jwtWithPayload({
      exp: 1780473960,
      "https://api.openai.com/auth": {
        chatgpt_account_id: "chatgpt-account-1",
      },
    }),
  });
  dispatch(input, "input");

  const document = JSON.parse(output.value);
  const account = document.accounts[0];

  assert.equal(document.expires_at, undefined);
  assert.equal(document.auto_pause_on_expired, undefined);
  assert.equal(document.accounts.length, 1);
  assert.equal(account.expires_at, 1780473960);
  assert.equal(account.auto_pause_on_expired, true);
}

function testSub2apiAccountsUseTheirOwnAccessTokenExpiry() {
  const { elements } = loadPageScript();
  const input = elements.get("#session-input");
  const output = elements.get("#output");

  input.value = JSON.stringify([
    {
      email: "late@example.com",
      accessToken: jwtWithPayload({
        exp: 1780473960,
        "https://api.openai.com/auth": {
          chatgpt_account_id: "chatgpt-account-late",
        },
      }),
    },
    {
      email: "early@example.com",
      accessToken: jwtWithPayload({
        exp: 1780000000,
        "https://api.openai.com/auth": {
          chatgpt_account_id: "chatgpt-account-early",
        },
      }),
    },
  ]);
  dispatch(input, "input");

  const document = JSON.parse(output.value);

  assert.equal(document.expires_at, undefined);
  assert.equal(document.auto_pause_on_expired, undefined);
  assert.equal(document.accounts.length, 2);
  assert.equal(document.accounts[0].expires_at, 1780473960);
  assert.equal(document.accounts[0].auto_pause_on_expired, true);
  assert.equal(document.accounts[1].expires_at, 1780000000);
  assert.equal(document.accounts[1].auto_pause_on_expired, true);
}

function testSub2apiAccountWithRefreshTokenOmitsAccessTokenExpiry() {
  const { elements } = loadPageScript();
  const input = elements.get("#session-input");
  const output = elements.get("#output");

  input.value = JSON.stringify({
    user: {
      email: "refreshable@example.com",
    },
    accessToken: jwtWithPayload({
      exp: 1780473960,
      "https://api.openai.com/auth": {
        chatgpt_account_id: "chatgpt-account-refreshable",
      },
    }),
    refreshToken: "real-refresh-token",
    expiresAt: "2026-06-01T00:00:00.000Z",
  });
  dispatch(input, "input");

  const document = JSON.parse(output.value);
  const account = document.accounts[0];

  assert.equal(account.expires_at, undefined);
  assert.equal(account.auto_pause_on_expired, undefined);
  assert.equal(account.credentials.expires_at, undefined);
  assert.equal(account.credentials.expires_in, undefined);
}

function testSyntheticIdTokenHasCodexParseableJwtFormat() {
  const { elements, formatButtons } = loadPageScript();
  const cpaButton = formatButtons.find((button) => button.dataset.format === "cpa");
  const input = elements.get("#session-input");
  const output = elements.get("#output");

  dispatch(cpaButton, "click");
  input.value = JSON.stringify({
    user: {
      id: "user-test",
      email: "mark@example.com",
    },
    expires: "2026-08-06T14:29:36.155Z",
    account: {
      id: "00000000-0000-4000-9000-000000000000",
      planType: "plus",
    },
    accessToken: "access-token",
    sessionToken: "session-token",
  });
  dispatch(input, "input");

  const cpa = JSON.parse(output.value);
  const parts = cpa.id_token.split(".");

  assert.equal(cpa.id_token_synthetic, true);
  assert.equal(parts.length, 3);
  assert.ok(
    parts.every((part) => part.length > 0),
    "synthetic id_token must use non-empty header, payload, and signature segments"
  );

  const payload = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8"));
  assert.equal(payload.email, "mark@example.com");
  assert.equal(payload["https://api.openai.com/auth"].chatgpt_account_id, "00000000-0000-4000-9000-000000000000");
}

function testAxonHubAuthJsonUsesPlaceholderRefreshTokenWhenMissing() {
  const { elements, formatButtons } = loadPageScript();
  const axonHubButton = formatButtons.find((button) => button.dataset.format === "axonhub");
  const input = elements.get("#session-input");
  const output = elements.get("#output");

  dispatch(axonHubButton, "click");
  input.value = JSON.stringify({
    user: {
      id: "user-test",
      email: "mark@example.com",
    },
    expires: "2026-08-06T14:29:36.155Z",
    account: {
      id: "00000000-0000-4000-9000-000000000000",
      planType: "plus",
    },
    accessToken: "access-token",
    sessionToken: "session-token",
  });
  dispatch(input, "input");

  const authJson = JSON.parse(output.value);

  assert.equal(authJson.auth_mode, "chatgpt");
  assert.equal(authJson.tokens.access_token, "access-token");
  assert.equal(authJson.tokens.refresh_token, "__missing_refresh_token__");
  assert.equal(authJson.tokens.id_token.split(".").length, 3);
  assert.equal(authJson.last_refresh, "2026-08-06T13:29:36.155Z");
  assert.equal(authJson.axonhub_refresh_token_placeholder, true);
  assert.equal(authJson.axonhub_note, "refresh_token is a placeholder; access_token works only until it expires.");
}

function testAxonHubAuthJsonPreservesRealRefreshToken() {
  const { elements, formatButtons } = loadPageScript();
  const axonHubButton = formatButtons.find((button) => button.dataset.format === "axonhub");
  const input = elements.get("#session-input");
  const output = elements.get("#output");

  dispatch(axonHubButton, "click");
  input.value = JSON.stringify({
    user: {
      email: "mark@example.com",
    },
    expires: "2026-08-06T14:29:36.155Z",
    account: {
      id: "00000000-0000-4000-9000-000000000000",
      planType: "plus",
    },
    accessToken: "access-token",
    refreshToken: "real-refresh-token",
    idToken: "real.header.signature",
  });
  dispatch(input, "input");

  const authJson = JSON.parse(output.value);

  assert.equal(authJson.tokens.refresh_token, "real-refresh-token");
  assert.equal(authJson.tokens.id_token, "real.header.signature");
  assert.equal(authJson.axonhub_refresh_token_placeholder, undefined);
  assert.equal(authJson.axonhub_note, undefined);
}

function testCodexAuthJsonMatchesNativeShapeWhenMissingRefreshToken() {
  const { elements, formatButtons } = loadPageScript();
  const codexButton = formatButtons.find((button) => button.dataset.format === "codex");
  const input = elements.get("#session-input");
  const output = elements.get("#output");

  dispatch(codexButton, "click");
  input.value = JSON.stringify({
    user: {
      id: "user-test",
      email: "mark@example.com",
    },
    expires: "2026-08-06T14:29:36.155Z",
    account: {
      id: "00000000-0000-4000-9000-000000000000",
      planType: "plus",
    },
    accessToken: "access-token",
    sessionToken: "session-token",
  });
  dispatch(input, "input");

  const authJson = JSON.parse(output.value);

  assert.equal(authJson.auth_mode, "chatgpt");
  assert.equal(authJson.OPENAI_API_KEY, null);
  assert.equal(authJson.tokens.access_token, "access-token");
  assert.equal(authJson.tokens.refresh_token, "");
  assert.equal(authJson.tokens.id_token.split(".").length, 3);
  assert.equal(authJson.tokens.account_id, "00000000-0000-4000-9000-000000000000");
  assert.match(authJson.last_refresh, /^\d{4}-\d{2}-\d{2}T/);
}

function testCodexAuthJsonPreservesRealRefreshTokenAndIdToken() {
  const { elements, formatButtons } = loadPageScript();
  const codexButton = formatButtons.find((button) => button.dataset.format === "codex");
  const input = elements.get("#session-input");
  const output = elements.get("#output");

  dispatch(codexButton, "click");
  input.value = JSON.stringify({
    user: {
      email: "mark@example.com",
    },
    accessToken: "access-token",
    refreshToken: "real-refresh-token",
    idToken: "real.header.signature",
    tokens: {
      account_id: "chatgpt-account-1",
    },
  });
  dispatch(input, "input");

  const authJson = JSON.parse(output.value);

  assert.equal(authJson.auth_mode, "chatgpt");
  assert.equal(authJson.OPENAI_API_KEY, null);
  assert.equal(authJson.tokens.access_token, "access-token");
  assert.equal(authJson.tokens.refresh_token, "real-refresh-token");
  assert.equal(authJson.tokens.id_token, "real.header.signature");
  assert.equal(authJson.tokens.account_id, "chatgpt-account-1");
}

function testCodexManagerAuthJsonUsesEmptyRefreshTokenWhenMissing() {
  const { elements, formatButtons } = loadPageScript();
  const codexManagerButton = formatButtons.find((button) => button.dataset.format === "codexmanager");
  const input = elements.get("#session-input");
  const output = elements.get("#output");

  dispatch(codexManagerButton, "click");
  input.value = JSON.stringify({
    user: {
      id: "user-test",
      email: "mark@example.com",
    },
    expires: "2026-08-06T14:29:36.155Z",
    account: {
      id: "00000000-0000-4000-9000-000000000000",
      planType: "plus",
    },
    accessToken: "access-token",
    sessionToken: "session-token",
  });
  dispatch(input, "input");

  const authJson = JSON.parse(output.value);

  assert.equal(authJson.tokens.access_token, "access-token");
  assert.equal(authJson.tokens.refresh_token, "");
  assert.equal(authJson.tokens.id_token, "");
  assert.equal(authJson.tokens.account_id, "00000000-0000-4000-9000-000000000000");
  assert.equal(authJson.meta.label, "mark@example.com");
  assert.equal(authJson.meta.note, "Imported from ChatGPT session");
}

function testCodexManagerAuthJsonPreservesRealRefreshAndMetadata() {
  const { elements, formatButtons } = loadPageScript();
  const codexManagerButton = formatButtons.find((button) => button.dataset.format === "codexmanager");
  const input = elements.get("#session-input");
  const output = elements.get("#output");

  dispatch(codexManagerButton, "click");
  input.value = JSON.stringify({
    user: {
      email: "mark@example.com",
    },
    accessToken: "access-token",
    refreshToken: "real-refresh-token",
    idToken: "real.header.signature",
    workspaceId: "workspace-1",
    chatgptAccountId: "chatgpt-account-1",
  });
  dispatch(input, "input");

  const authJson = JSON.parse(output.value);

  assert.equal(authJson.tokens.refresh_token, "real-refresh-token");
  assert.equal(authJson.tokens.id_token, "real.header.signature");
  assert.equal(authJson.tokens.chatgpt_account_id, "chatgpt-account-1");
  assert.equal(authJson.meta.workspace_id, "workspace-1");
  assert.equal(authJson.meta.chatgpt_account_id, "chatgpt-account-1");
}

testSub2apiAccountUsesAccessTokenExpiry();
testSub2apiAccountsUseTheirOwnAccessTokenExpiry();
testSub2apiAccountWithRefreshTokenOmitsAccessTokenExpiry();
testSyntheticIdTokenHasCodexParseableJwtFormat();
testAxonHubAuthJsonUsesPlaceholderRefreshTokenWhenMissing();
testAxonHubAuthJsonPreservesRealRefreshToken();
testCodexAuthJsonMatchesNativeShapeWhenMissingRefreshToken();
testCodexAuthJsonPreservesRealRefreshTokenAndIdToken();
testCodexManagerAuthJsonUsesEmptyRefreshTokenWhenMissing();
testCodexManagerAuthJsonPreservesRealRefreshAndMetadata();
console.log("convert-session tests passed");
