// Does the picture picker survive a slow upload?
//
// The bug this guards: askForArt() raced its own cancel-detection timer. On
// any link slower than 700ms the file was POSTed, stored, and then the answer
// was thrown away — the button looked like it had done nothing, with no error,
// because nothing had failed. On a fast link it worked, which is why it read
// as flaky rather than broken. The people most likely to hit it are on mobile
// data, i.e. most of them.
//
// It drives the REAL app in headless Chrome, intercepts the file chooser, and
// hands it a real image, so it tests the shipping code path and not a copy of
// it. Zero dependencies: Chrome is spoken to over the DevTools Protocol.
//
//   node tools/check_upload_race.mjs --url http://127.0.0.1:8000/
//
// Exit 0 = the picker settles correctly, 1 = regression, 2 = could not run.
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const APP = arg("url", "http://127.0.0.1:8000/");
const PORT = Number(arg("port", 9411));

/* The image to upload. Any real PNG will do — the server sniffs magic bytes,
   so a fixture from data/media is as valid as a photograph. */
function pickImage() {
  const explicit = arg("image", "");
  if (explicit) return resolve(explicit);
  const media = resolve("data", "media");
  if (existsSync(media)) {
    const hit = readdirSync(media).find((f) => f.endsWith(".png"));
    if (hit) return join(media, hit);
  }
  return "";
}

const CHROME = [
  process.env.CHROME_PATH,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  process.env.LOCALAPPDATA
    ? join(process.env.LOCALAPPDATA, "Google", "Chrome", "Application", "chrome.exe")
    : "",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].filter(Boolean).find((p) => existsSync(p));

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.events = [];
    ws.addEventListener("message", (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && this.pending.has(m.id)) {
        const { resolve, reject } = this.pending.get(m.id);
        this.pending.delete(m.id);
        m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
      } else if (m.method) {
        this.events.push(m);
      }
    });
  }

  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression, userGesture = false) {
    const r = await this.send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
      userGesture,
    });
    if (r.exceptionDetails) {
      throw new Error(
        "page threw: " +
          (r.exceptionDetails.exception?.description || JSON.stringify(r.exceptionDetails))
      );
    }
    return r.result.value;
  }

  async waitFor(expression, tries = 60, every = 250) {
    for (let i = 0; i < tries; i++) {
      await sleep(every);
      try {
        if (await this.evaluate(expression)) return true;
      } catch {
        /* mid-navigation */
      }
    }
    return false;
  }

  close() {
    try {
      this.ws.close();
    } catch {
      /* gone */
    }
  }
}

async function connect(port, deadlineMs = 20000) {
  const deadline = Date.now() + deadlineMs;
  while (Date.now() < deadline) {
    try {
      const v = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
      if (v.webSocketDebuggerUrl) break;
    } catch {
      /* not up */
    }
    await sleep(200);
  }
  let page = null;
  while (!page && Date.now() < deadline) {
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
    if (!page) await sleep(200);
  }
  if (!page) throw new Error("no page target on the CDP endpoint");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res, { once: true });
    ws.addEventListener("error", () => rej(new Error("CDP websocket refused")), { once: true });
  });
  const cdp = new CDP(ws);
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  return cdp;
}

async function main() {
  if (!CHROME) {
    console.error("No Chrome found. Set CHROME_PATH.");
    return 2;
  }
  const image = pickImage();
  if (!image) {
    console.error("No image to upload. Pass --image <path.png>.");
    return 2;
  }
  try {
    const head = await fetch(APP);
    if (!head.ok) throw new Error(String(head.status));
  } catch (e) {
    console.error(`The app is not answering at ${APP} — start it first. (${e})`);
    return 2;
  }

  const profile = join(tmpdir(), `storyliver-upload-${process.pid}`);
  mkdirSync(profile, { recursive: true });
  const chrome = spawn(
    CHROME,
    [
      "--headless=new",
      `--remote-debugging-port=${PORT}`,
      // Chrome 111+ refuses a CDP websocket that carries an Origin header.
      "--remote-allow-origins=*",
      `--user-data-dir=${profile}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-extensions",
      "--disable-gpu",
      "--window-size=1400,900",
      "about:blank",
    ],
    { stdio: "ignore" }
  );

  const checks = [];
  try {
    const cdp = await connect(PORT);
    await cdp.send("DOM.enable");
    await cdp.send("Network.enable");
    await cdp.send("Page.setInterceptFileChooserDialog", { enabled: true });
    // askForArt() builds a detached input and clicks it. Keep a handle on it so
    // the dismissed-picker paths can be exercised without a real dialog.
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
      source: `(() => {
        const orig = HTMLInputElement.prototype.click;
        HTMLInputElement.prototype.click = function () {
          if (this.type === 'file') window.__fileInput = this;
          return orig.apply(this, arguments);
        };
      })();`,
    });

    await cdp.send("Page.navigate", { url: APP });
    if (!(await cdp.waitFor("typeof window.askForArt === 'function'"))) {
      throw new Error("the page loaded but window.askForArt is missing");
    }
    await sleep(800);

    const chooser = async () => {
      for (let i = 0; i < 80; i++) {
        const hit = cdp.events.find((e) => e.method === "Page.fileChooserOpened");
        if (hit) return hit.params;
        await sleep(50);
      }
      throw new Error("the file chooser never opened");
    };

    const start = () =>
      cdp.evaluate(
        `(() => { window.__r = 'pending'; window.__t0 = performance.now();
          window.askForArt().then(v => { window.__r = v; window.__t1 = performance.now(); });
          return 'started'; })()`,
        true
      );

    const result = () =>
      cdp.evaluate(
        `({ r: window.__r, ms: Math.round((window.__t1 || performance.now()) - window.__t0) })`
      );

    async function trial(label, { latency = 0, feed = "file", dismiss = "cancel" }) {
      cdp.events.length = 0;
      await cdp.send("Network.emulateNetworkConditions", {
        offline: false,
        latency,
        downloadThroughput: -1,
        uploadThroughput: -1,
      });
      await start();
      const c = await chooser();

      if (feed === "file") {
        await cdp.send("DOM.setFileInputFiles", {
          files: [image],
          backendNodeId: c.backendNodeId,
        });
        // A real OS dialog takes window focus and hands it back on close. That
        // is the event the app's fallback listens for, so fire it.
        await cdp.evaluate(`window.dispatchEvent(new Event('focus')); 'ok'`);
      } else if (dismiss === "cancel") {
        await cdp.evaluate(
          `window.__fileInput.dispatchEvent(new Event('cancel')); 'ok'`
        );
      } else {
        // No `cancel` — the old-browser path: focus returns, nothing chosen.
        await cdp.evaluate(`window.dispatchEvent(new Event('focus')); 'ok'`);
      }

      await sleep(4000);
      const got = await result();
      await cdp.send("Network.emulateNetworkConditions", {
        offline: false,
        latency: 0,
        downloadThroughput: -1,
        uploadThroughput: -1,
      });
      return { label, ...got };
    }

    const fast = await trial("a fast link stores the picture", { latency: 0 });
    const slow = await trial("a slow link stores the picture", { latency: 1500 });
    const cancel = await trial("a dismissed picker returns nothing", {
      feed: "none",
      dismiss: "cancel",
    });
    const legacy = await trial("a dismissed picker (no cancel event) returns nothing", {
      feed: "none",
      dismiss: "focus",
    });

    const isUrl = (v) => typeof v === "string" && v.startsWith("/media/");
    checks.push({
      name: fast.label,
      ok: isUrl(fast.r),
      detail: `got ${JSON.stringify(fast.r)} in ${fast.ms}ms`,
    });
    checks.push({
      name: slow.label,
      ok: isUrl(slow.r),
      detail:
        `got ${JSON.stringify(slow.r)} in ${slow.ms}ms` +
        (slow.r === null ? " — the upload was discarded" : ""),
    });
    checks.push({
      name: cancel.label,
      ok: cancel.r === null && cancel.ms < 1500,
      detail: `got ${JSON.stringify(cancel.r)} in ${cancel.ms}ms`,
    });
    checks.push({
      name: legacy.label,
      ok: legacy.r === null && legacy.ms < 2500,
      detail: `got ${JSON.stringify(legacy.r)} in ${legacy.ms}ms`,
    });

    cdp.close();
  } finally {
    chrome.kill();
  }

  console.log("\n  StoryLiver — picture upload\n  " + "-".repeat(56));
  for (const c of checks) {
    console.log(`  ${c.ok ? "PASS" : "FAIL"}  ${c.name}\n        ${c.detail}`);
  }
  const failed = checks.filter((c) => !c.ok).length;
  console.log("  " + "-".repeat(56));
  console.log(`  ${checks.length - failed}/${checks.length} passed\n`);
  return failed ? 1 : 0;
}

process.exitCode = await main();
