"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const bridgePath = path.join(__dirname, "..", "manager", "static", "converter_bridge.js");
const bridgeSource = fs.readFileSync(bridgePath, "utf8");
const codexDocument = {
  auth_mode: "chatgpt",
  tokens: {
    access_token: "fixture-access-not-a-real-token",
    refresh_token: "fixture-refresh-not-a-real-token",
    id_token: "fixture-id-not-a-real-token",
    account_id: "fixture-account",
  },
};

function element(tagName = "div") {
  return {
    tagName: tagName.toUpperCase(),
    attributes: new Map(),
    listeners: new Map(),
    textContent: "",
    value: "",
    disabled: false,
    className: "",
    id: "",
    style: {},
    setAttribute(name, value) { this.attributes.set(name, String(value)); },
    getAttribute(name) { return this.attributes.get(name) ?? null; },
    addEventListener(name, listener) { this.listeners.set(name, listener); },
    async click() {
      this.clickCount = (this.clickCount || 0) + 1;
      const listener = this.listeners.get("click");
      if (listener) await listener({ preventDefault() {} });
    },
  };
}

function harness({ outputValue = JSON.stringify(codexDocument), confirmed = true, response } = {}) {
  const output = element("textarea");
  const outputStatus = element("div");
  const downloadButton = element("button");
  const codexButton = element("button");
  const previousFormatButton = element("button");
  const fetchCalls = [];
  let installedButton = null;

  previousFormatButton.setAttribute("aria-pressed", "true");
  previousFormatButton.click = async function clickPrevious() {
    this.clickCount = (this.clickCount || 0) + 1;
  };
  codexButton.click = async function clickCodex() {
    this.clickCount = (this.clickCount || 0) + 1;
    output.value = outputValue;
  };
  downloadButton.insertAdjacentElement = (_position, child) => { installedButton = child; };

  const document = {
    createElement: (tagName) => element(tagName),
    querySelector(selector) {
      return {
        '[data-format="codex"]': codexButton,
        '[data-format][aria-pressed="true"]': previousFormatButton,
        "#output": output,
        "#output-status": outputStatus,
        "#download-output": downloadButton,
        "#save-local-credentials": installedButton,
      }[selector] || null;
    },
  };
  const context = {
    document,
    window: { confirm: () => confirmed },
    fetch: async (url, options) => {
      fetchCalls.push({ url, options });
      return response || {
        ok: true,
        status: 200,
        json: async () => ({ ok: true, imported: 1, duplicates: 0, total: 1 }),
      };
    },
    JSON,
    Error,
  };
  vm.runInNewContext(bridgeSource, context, { filename: bridgePath });
  return {
    fetchCalls,
    outputStatus,
    previousFormatButton,
    saveButton: installedButton,
  };
}

async function testSuccessfulConfirmedSave() {
  const state = harness();
  assert.ok(state.saveButton, "bridge installs a save button");
  assert.equal(state.saveButton.style.minHeight, "44px");

  await state.saveButton.click();

  assert.equal(state.fetchCalls.length, 1);
  assert.equal(state.fetchCalls[0].url, "/api/manager/credentials/import");
  assert.deepEqual(JSON.parse(state.fetchCalls[0].options.body), {
    confirmed: true,
    documents: codexDocument,
  });
  assert.equal(state.previousFormatButton.clickCount, 1, "restore prior output format");
  assert.equal(state.outputStatus.textContent, "已保存 1 个凭证，跳过 0 个重复项。");
}

async function testCancellationNeverPersists() {
  const state = harness({ confirmed: false });
  await state.saveButton.click();
  assert.equal(state.fetchCalls.length, 0);
  assert.equal(state.previousFormatButton.clickCount, 1);
  assert.equal(state.outputStatus.textContent, "已取消保存，转换内容仍只在浏览器中。");
}

async function testEmptyCodexOutputIsRejected() {
  const state = harness({ outputValue: "" });
  await state.saveButton.click();
  assert.equal(state.fetchCalls.length, 0);
  assert.equal(state.outputStatus.textContent, "没有可保存的 Codex 转换结果。");
}

async function testInvalidJsonIsRejected() {
  const state = harness({ outputValue: "not-json" });
  await state.saveButton.click();
  assert.equal(state.fetchCalls.length, 0);
  assert.equal(state.outputStatus.textContent, "Codex 转换结果不是有效 JSON，未发送任何内容。");
}

async function testBackendErrorIsShownWithoutResponseDetails() {
  const state = harness({
    response: {
      ok: false,
      status: 400,
      json: async () => ({ ok: false, error: "第 1 项缺少 access_token" }),
    },
  });
  await state.saveButton.click();
  assert.equal(state.fetchCalls.length, 1);
  assert.equal(state.outputStatus.textContent, "保存失败：第 1 项缺少 access_token");
}

(async () => {
  await testSuccessfulConfirmedSave();
  await testCancellationNeverPersists();
  await testEmptyCodexOutputIsRejected();
  await testInvalidJsonIsRejected();
  await testBackendErrorIsShownWithoutResponseDetails();
  console.log("manager bridge tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
