/* Minimal CDP driver to exercise the SealGauge segment UI without extra deps. */
const { spawn } = require("child_process");
const http = require("http");

const CHROME = "/opt/pointwise-env/browsers/chromium_headless_shell-1255/chrome-headless-shell-linux64/chrome-headless-shell";
const BASE = process.env.SEALGAUGE_BASE || "http://127.0.0.1:8123";

function getJson(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => resolve(JSON.parse(data)));
    }).on("error", reject);
  });
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function main() {
  const chrome = spawn(CHROME, [
    "--headless", "--disable-gpu", "--no-sandbox", "--remote-debugging-port=9333",
    "--remote-allow-origins=*", "about:blank",
  ]);
  await sleep(1500);
  const list = await getJson("http://127.0.0.1:9333/json");
  const target = list.find((t) => t.type === "page");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));
  let id = 0;
  const pending = new Map();
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
    }
  };
  const send = (method, params = {}) =>
    new Promise((resolve) => {
      const mid = ++id;
      pending.set(mid, resolve);
      ws.send(JSON.stringify({ id: mid, method, params }));
    });
  const evalJs = async (expression) => {
    const reply = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (reply.result && reply.result.exceptionDetails) throw new Error(JSON.stringify(reply.result.exceptionDetails));
    return reply.result.result.value;
  };

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", { url: BASE + "/" });
  await sleep(1200);

  const outcome = await evalJs(`(async () => {
    const log = [];
    // Open the practice card
    document.getElementById("practice").click();
    await new Promise((r) => setTimeout(r, 1200));
    log.push("panel hidden? " + document.querySelector(".segment").hidden);
    // Fill segment request and plan
    document.getElementById("seg-length").value = 20;
    document.getElementById("seg-width").value = 2;
    document.getElementById("segment-form").requestSubmit();
    await new Promise((r) => setTimeout(r, 1200));
    const plan = window.__none;
    const summary = document.getElementById("segment-summary").textContent;
    log.push("summary: " + summary.slice(0, 80));
    const options = [...document.querySelectorAll(".segment-option")].map((n) => ({
      text: n.querySelector(".seg-head").textContent,
      pressed: n.getAttribute("aria-pressed"),
      tag: n.querySelector(".seg-tag") ? n.querySelector(".seg-tag").textContent : "",
    }));
    log.push("options: " + JSON.stringify(options));
    log.push("preview disabled? " + document.getElementById("seg-preview").disabled);
    log.push("send disabled? " + document.getElementById("seg-send").disabled);
    // Pick the second candidate if present
    const buttons = document.querySelectorAll(".segment-option");
    if (buttons.length > 1) {
      buttons[1].click();
      await new Promise((r) => setTimeout(r, 300));
      log.push("after pick, pressed: " + [...buttons].map((b) => b.getAttribute("aria-pressed")).join(","));
    }
    // Open the actual-edge preview dialog
    document.getElementById("seg-preview").click();
    await new Promise((r) => setTimeout(r, 1500));
    const dialog = document.getElementById("segment-dialog");
    const img = document.getElementById("segment-preview-img");
    log.push("dialog open? " + dialog.open);
    log.push("dialog meta: " + document.getElementById("segment-dialog-meta").textContent);
    log.push("img complete/natural: " + img.complete + " " + img.naturalWidth + "x" + img.naturalHeight);
    dialog.close();
    // Send the cropped frame to a preparation project
    window.open = (url) => { log.push("opened prepare url: " + url); return { close() {} }; };
    document.getElementById("seg-send").click();
    await new Promise((r) => setTimeout(r, 1500));
    log.push("notice: " + document.getElementById("notice").textContent.slice(0, 60));
    // Request an impossible length to exercise the no-candidate path
    document.getElementById("seg-length").value = 200;
    document.getElementById("segment-form").requestSubmit();
    await new Promise((r) => setTimeout(r, 1000));
    log.push("no-candidate summary: " + document.getElementById("segment-summary").textContent.slice(0, 90));
    log.push("buttons disabled after none? " + document.getElementById("seg-send").disabled);
    return log;
  })()`);
  console.log(outcome.join("\n"));
  ws.close();
  chrome.kill();
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
