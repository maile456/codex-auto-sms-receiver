(() => {
  "use strict";

  if (document.querySelector("#save-local-credentials")) return;

  const codexButton = document.querySelector('[data-format="codex"]');
  const output = document.querySelector("#output");
  const outputStatus = document.querySelector("#output-status");
  const downloadButton = document.querySelector("#download-output");
  if (!codexButton || !output || !outputStatus || !downloadButton) return;

  const saveButton = document.createElement("button");
  saveButton.id = "save-local-credentials";
  saveButton.className = "secondary-button";
  saveButton.type = "button";
  saveButton.textContent = "保存到本地凭证库";
  saveButton.setAttribute(
    "title",
    "只有在确认后，当前 Codex 格式结果才会保存到本机 data/codex_accounts"
  );
  downloadButton.insertAdjacentElement("afterend", saveButton);

  const setStatus = (message) => {
    outputStatus.textContent = message;
  };

  saveButton.addEventListener("click", async () => {
    if (saveButton.disabled) return;
    saveButton.disabled = true;
    const previousFormat = document.querySelector(
      '[data-format][aria-pressed="true"]'
    );
    let raw = "";
    try {
      codexButton.click();
      raw = String(output.value || "").trim();
    } finally {
      if (previousFormat && previousFormat !== codexButton) {
        previousFormat.click();
      }
    }

    if (!raw) {
      setStatus("没有可保存的 Codex 转换结果。");
      saveButton.disabled = false;
      return;
    }

    let documents;
    try {
      documents = JSON.parse(raw);
    } catch (_error) {
      setStatus("Codex 转换结果不是有效 JSON，未发送任何内容。");
      saveButton.disabled = false;
      return;
    }

    const confirmed = window.confirm(
      "确认把当前 Codex 凭证保存到本机 data/codex_accounts？\n\n" +
      "这会从纯浏览器临时转换切换为本机持久化。内容不会发送到外部服务器。"
    );
    if (!confirmed) {
      setStatus("已取消保存，转换内容仍只在浏览器中。");
      saveButton.disabled = false;
      return;
    }

    setStatus("正在保存到本机凭证库…");
    try {
      const response = await fetch("/api/manager/credentials/import", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirmed: true, documents }),
      });
      let result = {};
      try {
        result = await response.json();
      } catch (_error) {
        result = {};
      }
      if (!response.ok || result.ok !== true) {
        const reason = typeof result.error === "string"
          ? result.error.slice(0, 240)
          : `本机接口返回 HTTP ${response.status}`;
        throw new Error(reason);
      }
      setStatus(
        `已保存 ${Number(result.imported) || 0} 个凭证，` +
        `跳过 ${Number(result.duplicates) || 0} 个重复项。`
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : "本机接口不可用";
      setStatus(`保存失败：${message}`);
    } finally {
      saveButton.disabled = false;
    }
  });
})();
