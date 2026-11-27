#!/usr/bin/env node
/*
 * End-to-end test of the JFLP UI: drives the browser like a user and checks what
 * the pages show. Also writes the README screenshots.
 *
 *   JFLP_JAR=target/jflp.jar [D4J_HOME=/path/to/defects4j] [SHOTS=docs/img] node src/it/ui/e2e.js
 *
 * Flows (each one asserts its outcome; the script exits 1 on the first failure):
 *   1. local folder (src/it/sample) with branches and def-use coverage
 *   2. Defects4J Lang-1, whose real buggy line must rank in the top 10  (needs D4J_HOME)
 *   3. Bears-232 from the Bears catalog                                   (needs network)
 *   4. a git repository at a ref (the sample project's own repository)
 * Needs Node with Playwright and a JDK 8+ on the PATH.
 */
"use strict";
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "../../..");
const JAR = path.resolve(process.env.JFLP_JAR || path.join(ROOT, "target/jflp.jar"));
const SHOTS = path.resolve(process.env.SHOTS || path.join(ROOT, "docs/img"));
const D4J = process.env.D4J_HOME;
const ONLY = (process.env.FLOWS || "local,defects4j,bears,git").split(",");
const WORKSPACE = process.env.E2E_WORKSPACE ? path.resolve(process.env.E2E_WORKSPACE)
  : fs.mkdtempSync(path.join(os.tmpdir(), "jflp-e2e-"));

let page;
let base;
const results = [];

function check(cond, what) {
  if (!cond) throw new Error("check failed: " + what);
  results.push("ok   " + what);
  console.log("  ok  " + what);
}

async function shot(name) {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(SHOTS, name + ".png") });
  console.log("  shot " + name + ".png");
}

async function apiGet(p) {
  return (await page.request.get(base + "api/" + p)).json();
}

async function startServer() {
  const proc = spawn("java", ["-jar", JAR, "ui", "--port", "0", "--workspace", WORKSPACE, "--no-browser"],
    { stdio: ["ignore", "pipe", "pipe"] });
  proc.stderr.on("data", () => {});
  base = await new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("UI did not start")), 30000);
    proc.stdout.on("data", (d) => {
      const m = String(d).match(/JFLP UI: (http:\/\/127\.0\.0\.1:\d+\/)/);
      if (m) { clearTimeout(t); resolve(m[1]); }
    });
  });
  return proc;
}

/** Adds a source from the New analysis page; returns the project id once it is ready. */
async function addSource(type, fill) {
  await page.goto(base + "#/new?type=" + type);
  await page.waitForSelector("#srcform");
  await fill();
  await page.click("#srcform button[type=submit]");
  await page.waitForURL(/#\/project\//);
  const id = page.url().split("#/project/")[1];
  const t0 = Date.now();
  for (;;) {
    const p = await apiGet("projects/" + id);
    if (p.status === "ready") break;
    if (p.status === "error") throw new Error("source failed: " + p.error);
    if (Date.now() - t0 > 30 * 60e3) throw new Error("source took too long");
    await page.waitForTimeout(1500);
  }
  await page.goto(base + "#/project/" + id);
  await page.waitForSelector("#runform");
  return id;
}

/** Starts a run with the given switches; returns the run id once it is done. */
async function runAnalysis({ branches = false, duas = false, jdkMajor = null, liveShot = null } = {}) {
  if (branches) await page.check("input[name=branches]", { force: true });
  if (duas) await page.check("input[name=duas]", { force: true });
  if (jdkMajor) {
    const value = await page.$eval("select[name=jdk]", (s, m) => {
      const o = Array.from(s.options).find((x) => x.textContent.startsWith("Java " + m + " "));
      return o ? o.value : s.value;
    }, jdkMajor);
    await page.selectOption("select[name=jdk]", value);
  }
  await page.click("#runform button[type=submit]");
  await page.waitForURL(/#\/run\//);
  const id = page.url().split("#/run/")[1].split("/")[0];
  if (liveShot) {
    await page.waitForSelector(".step.running", { timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(2500);
    await shot(liveShot);
  }
  const t0 = Date.now();
  for (;;) {
    const r = await apiGet("runs/" + id);
    if (r.status === "done") break;
    if (r.status === "failed" || r.status === "cancelled") throw new Error("run " + r.status + ": " + r.error);
    if (Date.now() - t0 > 60 * 60e3) throw new Error("run took too long");
    await page.waitForTimeout(2000);
  }
  await page.goto(base + "#/run/" + id + "/overview");
  await page.waitForSelector(".tabs");
  return id;
}

async function flowLocal() {
  console.log("flow: local folder with branches and DUAs");
  await addSource("local", async () => {
    await page.fill("input[name=path]", path.join(ROOT, "src/it/sample"));
    await page.fill("input[name=name]", "sample-calc");
  });
  await shot("project");
  const run = await runAnalysis({ branches: true, duas: true, liveShot: "run-live" });
  const s = await apiGet("runs/" + run + "/summary");
  check(s.tests === 8 && s.failed === 3, "sample: 8 tests, 3 failing (got " + s.tests + "/" + s.failed + ")");
  check(s.branches > 0 && s.duas > 0, "sample: branches and DUAs measured");
  const rk = await apiGet("runs/" + run + "/ranking?limit=5");
  check(rk.rows.some((r) => r["class"] === "sample.Calc" && r.line === 15),
    "sample: the faulty line Calc:15 is among the 5 most suspicious");
  check((await page.textContent(".tabs")).includes("DUAs"), "sample: the DUAs tab is shown");
  await page.goto(base + "#/run/" + run + "/code?file=sample/Calc.java&line=15");
  await page.waitForSelector("#L15");
  check(await page.$eval("#L15", (tr) => tr.classList.contains("hit")), "sample: Calc:15 is highlighted in the code view");
  await page.goto(base + "#/run/" + run + "/duas");
  await page.waitForSelector("#tab table");
  await shot("run-duas");
  await page.goto(base + "#/run/" + run + "/ranking");
  await page.waitForSelector("#rk table");
  await page.selectOption("#h", "tarantula");
  await page.waitForFunction(() => document.querySelector("#rk") && document.querySelector("#rk").textContent.includes("1.000"));
  check(true, "sample: switching the heuristic to tarantula re-ranks without re-running");
}

async function flowDefects4j() {
  console.log("flow: Defects4J Lang-1 (known fix)");
  await page.goto(base + "#/settings");
  await page.waitForSelector("#sf");
  await page.fill("input[name=defects4jHome]", D4J);
  await page.click("#sf button[type=submit]");
  await page.waitForTimeout(1500);
  await shot("environment");
  await addSource("defects4j", async () => {
    await page.waitForFunction(() => document.querySelectorAll("#d4jp option").length > 5);
    await page.selectOption("#d4jp", "Lang");
    await page.waitForFunction(() => Array.from(document.querySelectorAll("#d4jb option")).some((o) => o.value === "1"));
    await page.selectOption("#d4jb", "1");
    await shot("new-source");
  });
  const run = await runAnalysis({ jdkMajor: 11 });
  const s = await apiGet("runs/" + run + "/summary");
  check(s.failing.includes("TestLang747(org.apache.commons.lang3.math.NumberUtilsTest)"),
    "Lang-1: the trigger test TestLang747 failed");
  check(s.truth && s.truth.bestRank != null && s.truth.bestRank <= 10,
    "Lang-1: the real buggy line ranks in the top 10 (rank " + (s.truth && s.truth.bestRank) + ")");
  await shot("run-overview");
  await page.goto(base + "#/run/" + run + "/ranking");
  await page.waitForSelector("#rk table");
  check(await page.$("#rk tr.buggy") !== null, "Lang-1: the ranking marks the buggy line");
  await shot("run-ranking");
  const top = (await apiGet("runs/" + run + "/ranking?limit=50")).rows.find((r) => r.buggy);
  await page.goto(base + "#/run/" + run + "/code?file=" + encodeURIComponent(top.source) + "&line=" + top.line);
  await page.waitForSelector("#L" + top.line);
  check(await page.$eval("#L" + top.line, (tr) => tr.classList.contains("bug")), "Lang-1: the code view marks the buggy line");
  await shot("run-code");
}

async function flowBears() {
  console.log("flow: Bears-232 from the catalog");
  await addSource("bears", async () => {
    await page.waitForFunction(() => Array.from(document.querySelectorAll("#bears option")).some((o) => o.value === "Bears-232"), null, { timeout: 600000 });
    await page.selectOption("#bears", "Bears-232");
  });
  const run = await runAnalysis({ jdkMajor: 11 });
  const s = await apiGet("runs/" + run + "/summary");
  check(s.failed === 3 && s.expectedMissing && s.expectedMissing.length === 0,
    "Bears-232: the 3 failing tests Bears lists all failed");
  check(s.truth && s.truth.bestRank != null, "Bears-232: the fix's buggy line is ranked (#" + (s.truth && s.truth.bestRank) + ")");
}

async function flowGit() {
  console.log("flow: git repository at a ref");
  // The sample project lives in this repository; clone it from the local checkout
  // at a known commit and run its Maven build from the module folder.
  const head = require("child_process").execSync("git rev-parse HEAD", { cwd: ROOT }).toString().trim();
  await addSource("git", async () => {
    await page.fill("input[name=url]", ROOT);
    await page.fill("input[name=ref]", head);
    await page.fill("input[name=module]", "src/it/sample");
    await page.fill("input[name=name]", "jflp-sample (git)");
  });
  const run = await runAnalysis({ jdkMajor: 8 });
  const s = await apiGet("runs/" + run + "/summary");
  check(s.tests === 8 && s.failed === 3, "git: the cloned module ran its 8 tests (3 failing)");
}

(async function main() {
  console.log("workspace " + WORKSPACE);
  const server = await startServer();
  const browser = await chromium.launch();
  page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  let failed = null;
  try {
    if (ONLY.includes("local")) await flowLocal();
    if (ONLY.includes("defects4j") && D4J) await flowDefects4j();
    else if (ONLY.includes("defects4j")) console.log("flow: Defects4J skipped (set D4J_HOME)");
    if (ONLY.includes("bears")) await flowBears();
    if (ONLY.includes("git")) await flowGit();
    await page.goto(base);
    await page.waitForSelector(".hero");
    await shot("dashboard");
    check(errors.length === 0, "no JavaScript errors in the pages (" + errors.join("; ") + ")");
  } catch (e) {
    failed = e;
    await page.screenshot({ path: path.join(WORKSPACE, "failure.png") }).catch(() => {});
    console.error("FAILED: " + e.message + " (screenshot " + path.join(WORKSPACE, "failure.png") + ")");
  } finally {
    await browser.close();
    server.kill();
  }
  console.log(results.length + " checks passed" + (failed ? ", then a failure" : ""));
  process.exit(failed ? 1 : 0);
})();
