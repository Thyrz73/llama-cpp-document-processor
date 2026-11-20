// Run with Node 22: node --test tests/test_polling.js (no npm install required).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { beforeEach, describe, test } from "node:test";
import {
  createBrowserHarness,
  descendants,
  flush,
  job,
} from "./helpers/browser.js";

const template = readFileSync(
  new URL("../templates/index.html", import.meta.url),
  "utf8",
);
const translations = JSON.parse(
  readFileSync(new URL("../static/translations.json", import.meta.url), "utf8"),
);

const selectionCheckboxes = (browser) =>
  descendants(browser.elements.jobs).filter((node) => node.dataset.selectJob);
const setSelected = (node, value = true) => {
  node.checked = value;
  node.handlers.change[0]();
};
const toggleSelection = (browser) =>
  browser.document.getElementById("toggleSelection").handlers.click[0]();

const nativeGlobals = new Map(
  [
    "document",
    "fetch",
    "setTimeout",
    "clearTimeout",
    "FormData",
    "localStorage",
  ].map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]),
);

// Production modules share state and browser globals: scenarios run sequentially,
// with a new harness and automatic cleanup for every individual test.
describe("Browser interface", { concurrency: false }, () => {
  let createBrowser;
  beforeEach((context) => {
    createBrowser = createBrowserHarness(context, { template, translations });
  });
  describe("Browser simulation and preferences", () => {
    test("browser storage uses the supplied Map and browser string semantics", async () => {
      const storage = new Map([["saved", "initial"]]);
      createBrowser({ storage });
      assert.ok(
        localStorage.getItem("saved") === "initial" &&
          localStorage.getItem("missing") === null,
        "Read saved and absent keys",
      );
      localStorage.setItem(12, 34);
      assert.ok(
        storage.get("12") === "34" && localStorage.getItem(12) === "34",
        "String keys and values",
      );
      storage.set("saved", "updated");
      assert.equal(
        localStorage.getItem("saved"),
        "updated",
        "Use the supplied Map by reference",
      );
      localStorage.removeItem(12);
      assert.ok(!storage.has("12"), "Remove from the same Map");
      localStorage.clear();
      assert.equal(storage.size, 0, "Clear the same Map");
    });
    test("simulated DOM moves nodes and tracks hidden ancestors and disconnection", async () => {
      const browser = createBrowser();
      const first = browser.document.createElement("div");
      const second = browser.document.createElement("div");
      const child = browser.document.createElement("button");
      first.append(child);
      second.append(child);
      browser.elements.jobs.append(second);
      assert.ok(
        first.children.length === 0 &&
          second.children.length === 1 &&
          child.parentElement === second,
        "Append moves rather than duplicates nodes",
      );
      assert.ok(child.isConnected, "Connected through its parent");
      second.hidden = true;
      assert.equal(child.closest("[hidden]"), second, "Find a hidden ancestor");
      second.replaceChildren();
      assert.ok(
        !child.isConnected && child.parentElement === null,
        "Replacing children detaches them",
      );
    });

    test("scenario cleanup restores browser globals and native timers", () => {
      for (const [name, descriptor] of nativeGlobals) {
        assert.deepEqual(
          Object.getOwnPropertyDescriptor(globalThis, name),
          descriptor,
          name,
        );
      }
    });

    test("the clock advances polling only when its deadline is reached", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job()] });
      await browser.clock.advance(5999);
      assert.equal(browser.clock.now, 5999);
      assert.equal(
        browser.requests.length,
        1,
        "No request before the polling deadline",
      );
      await browser.clock.advance(1);
      assert.equal(browser.clock.now, 6000);
      assert.equal(browser.requests.length, 2);
      assert.equal(
        browser.polls().length,
        0,
        "No overlap while GET is pending",
      );
      await browser.respond(1, { jobs: [job("done", 100)] });
      await browser.clock.advance(60000);
      assert.equal(browser.requests.length, 2, "Terminal lists remain idle");
    });

    test("a fresh browser releases old polling and ignores the previous page response", async () => {
      const previous = createBrowser();
      await previous.respond(0, { jobs: [job()] });
      previous.api.toggleLive("a".repeat(32));
      const oldOutput = previous.document.getElementById("live-content");
      const current = createBrowser();
      assert.equal(previous.polls().length, 0);
      await previous.respond(1, { content: "Old page", active: true });
      await current.respond(0, { jobs: [job("done", 100, "b".repeat(32))] });
      assert.equal(oldOutput.textContent, "Waiting for streamed output…");
      assert.equal(current.requests.length, 1);
      assert.equal(current.polls().length, 0);
      assert.equal(current.livePolls().length, 0);
      assert.equal(current.document.getElementById("live-content"), null);
    });

    test("theme defaults to light and remembers explicit choices across visits", async () => {
      for (const initial of [null, "system", "invalid", "light", "dark"]) {
        const storage = new Map(
          initial === null ? [] : [["document-processor-theme", initial]],
        );
        const browser = createBrowser({ storage });
        assert.equal(
          browser.elements.theme.value,
          initial === "dark" ? "dark" : "light",
          "Default or remembered theme",
        );
        assert.equal(
          browser.document.documentElement.dataset.theme,
          browser.elements.theme.value,
          "Apply theme on startup",
        );
        browser.elements.theme.value = "dark";
        browser.elements.theme.handlers.change[0]();
        assert.equal(
          storage.get("document-processor-theme"),
          "dark",
          "Remember dark selection",
        );
        assert.equal(
          createBrowser({ storage }).document.documentElement.dataset.theme,
          "dark",
          "Restore dark on next visit",
        );
        browser.elements.theme.value = "light";
        browser.elements.theme.handlers.change[0]();
        assert.equal(
          createBrowser({ storage }).document.documentElement.dataset.theme,
          "light",
          "Restore light on next visit",
        );
      }
    });
    test("early page, modules and report agree on explicit themes and legacy system fallback", async () => {
      const reportScript = readFileSync(
        new URL("../static/report.js", import.meta.url),
        "utf8",
      );
      for (const systemDark of [false, true])
        for (const initial of [null, "system", "invalid", "light", "dark"]) {
          const key = "document-processor-theme";
          const storage = new Map(initial === null ? [] : [[key, initial]]);
          const browser = createBrowser({ storage, systemDark });
          const expected = initial === "dark" ? "dark" : "light";
          assert.ok(
            browser.earlyTheme === expected &&
              browser.elements.theme.value === expected &&
              browser.document.documentElement.dataset.theme === expected,
            "Early paint and modules must agree",
          );
          browser.systemTheme.matches = !systemDark;
          for (const fn of browser.systemTheme.handlers) fn();
          assert.equal(
            browser.document.documentElement.dataset.theme,
            expected,
            "OS changes must not override explicit or migrated themes",
          );
          if (initial === null) storage.delete(key);
          else storage.set(key, initial);
          new Function(reportScript)();
          assert.equal(
            browser.document.documentElement.dataset.theme,
            expected,
            "Report uses the same saved or legacy fallback",
          );
        }
    });
    test("language and theme persist together across independently started pages", async () => {
      const storage = new Map([
        ["document-processor-language", "fr"],
        ["document-processor-theme", "dark"],
      ]);
      const first = createBrowser({ withSettings: true, storage });
      assert.ok(
        first.document.documentElement.lang === "fr" &&
          first.elements.theme.value === "dark",
        "Restore both saved preferences",
      );
      first.elements.uiLanguage.value = "en";
      first.elements.uiLanguage.handlers.change[0]();
      first.elements.theme.value = "light";
      first.elements.theme.handlers.change[0]();
      const second = createBrowser({ withSettings: true, storage });
      assert.ok(
        second.document.documentElement.lang === "en" &&
          second.elements.theme.value === "light",
        "New page uses the same saved preferences",
      );
    });
  });

  describe("Jobs polling and mutations", () => {
    test("initial load stops for empty, completed, failed and mixed terminal lists", async () => {
      for (const jobs of [
        [],
        [job("done", 100)],
        [job("failed")],
        [job("done", 100), job("failed", 20, "b".repeat(32))],
      ]) {
        const browser = createBrowser();
        assert.equal(browser.requests.length, 1, "Initial request required");
        await browser.respond(0, { jobs });
        assert.equal(browser.polls().length, 0, "Terminal list must not poll");
      }
    });
    test("queued and running jobs poll until the last active job terminates", async () => {
      for (const status of ["queued", "running"]) {
        const browser = createBrowser();
        await browser.respond(0, { jobs: [job(status)] });
        assert.equal(browser.polls().length, 1, "Active list needs one timer");
        await browser.tick(6000);
        assert.equal(browser.polls().length, 0, "No timer during request");
        await browser.respond(1, {
          jobs: [job("done", 100), job("running", 80, "b".repeat(32))],
        });
        assert.equal(
          browser.polls().length,
          1,
          "Another active job still needs polling",
        );
        await browser.tick(6000);
        await browser.respond(2, {
          jobs: [job("done", 100), job("failed", 80, "b".repeat(32))],
        });
        assert.equal(browser.polls().length, 0, "Last active job terminated");
      }
    });
    test("active jobs outside the visible list keep polling without rebuilding cards", async () => {
      const browser = createBrowser();
      const jobs = [job("done", 100)];
      await browser.respond(0, { jobs, has_active_jobs: true });
      assert.equal(
        browser.polls().length,
        1,
        "An older active job still needs polling",
      );
      const article = browser.article();
      await browser.tick(6000);
      await browser.respond(1, { jobs, has_active_jobs: false });
      assert.equal(
        browser.polls().length,
        0,
        "Stop once all jobs are terminal",
      );
      assert.equal(
        browser.article(),
        article,
        "Metadata-only changes must not rebuild cards",
      );
    });
    test("manual and timer refreshes share an in-flight request and a single timer", async () => {
      const browser = createBrowser();
      const pending = browser.api.refresh();
      assert.equal(
        browser.api.refresh(),
        pending,
        "Requests must share a promise",
      );
      browser.click();
      assert.equal(
        browser.requests.length,
        1,
        "Concurrent initial refreshes must coalesce",
      );
      await browser.respond(0, { jobs: [job()] });
      browser.click();
      browser.click();
      browser.api.refresh();
      assert.ok(
        browser.requests.length === 2 && browser.polls().length === 0,
        "Only one manual request",
      );
      await browser.respond(1, { jobs: [job()] });
      assert.equal(browser.polls().length, 1, "Only one replacement timer");
    });
    test("unchanged data preserves DOM and completed report attributes", async () => {
      const browser = createBrowser();
      const jobs = [job("done", 100)];
      await browser.respond(0, { jobs });
      const article = browser.article();
      const replacements = browser.elements.jobs.replacements;
      browser.click();
      await browser.respond(1, { jobs });
      assert.equal(
        browser.elements.jobs.replacements,
        replacements,
        "Unchanged data must not rebuild DOM",
      );
      assert.equal(browser.article(), article, "Preserve article identity");
      const track = article.children.find(
        (c) => c.attributes.role === "progressbar",
      );
      assert.ok(
        track.attributes["aria-valuenow"] === "100" &&
          track.children[0].style.width === "100%",
        "Completed progress",
      );
      const link = descendants(article).find(
        (c) => c.tag === "a" && c.download !== undefined,
      );
      assert.ok(
        link.href === `/api/jobs/${jobs[0].id}/report` && link.download === "",
        "Native report download",
      );
      const view = descendants(article).find(
        (c) => c.tag === "a" && c.target === "_blank",
      );
      assert.ok(
        view.href === `/api/jobs/${jobs[0].id}/report/view?lang=en` &&
          view.rel.includes("noopener"),
        "Secure separate preview link",
      );
    });
    test("active and history groups show empty states and move completed analyses", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [] });
      const groups = browser.elements.jobs.children;
      assert.ok(
        groups[0].className === "job-group job-group-active" &&
          groups[1].className === "job-group job-group-history",
        "Active group precedes distinct history",
      );
      assert.equal(
        groups[0].children[1].children[0].textContent,
        "No analyses in progress.",
        "Empty active state",
      );
      assert.equal(
        groups[1].children[1].children[1].textContent,
        "No analysis history yet.",
        "Empty history state",
      );
      browser.click();
      await browser.respond(1, {
        jobs: [
          job("done"),
          job("running", 25, "b".repeat(32)),
          job("queued", 0, "c".repeat(32)),
        ],
      });
      assert.equal(
        browser.elements.jobs.children[0].children[1].children.length,
        2,
        "Both active analyses shown first",
      );
      assert.equal(
        browser.elements.jobs.children[1].children[1].children.filter(
          (c) => c.tag === "article",
        ).length,
        1,
        "Completed analysis in history",
      );
      browser.click();
      await browser.respond(2, {
        jobs: [
          job("done"),
          job("done", 100, "b".repeat(32)),
          job("queued", 0, "c".repeat(32)),
        ],
      });
      assert.equal(
        browser.elements.jobs.children[1].children[1].children.filter(
          (c) => c.tag === "article",
        ).length,
        2,
        "A finished analysis moves into history",
      );
    });
    test("saved prompt text is collapsible, literal and present for active and historical jobs", async () => {
      for (const status of ["running", "done"]) {
        const browser = createBrowser();
        const request = {
          ...job(status),
          question: "Private <script>alert(1)</script>\nDeuxième ligne",
          question_custom: 1,
          report_name: "Subject_rapport.md",
        };
        await browser.respond(0, { jobs: [request] });
        const details = descendants(browser.elements.jobs).find(
          (node) => node.className === "custom-prompt",
        );
        assert.ok(
          details.tag === "details" && !details.open,
          "Prompt starts collapsed",
        );
        assert.ok(
          details.children[1].textContent === request.question &&
            details.children[1].children.length === 0,
          "Prompt rendered as literal text",
        );
        details.open = true;
        details.handlers.toggle[0]();
        browser.click();
        await browser.respond(1, {
          jobs: [{ ...request, progress_percent: 70 }],
        });
        assert.ok(
          descendants(browser.elements.jobs).find(
            (node) => node.className === "custom-prompt",
          ).open,
          "Keep prompt expanded during progress updates",
        );
        if (status === "done") {
          const download = descendants(browser.elements.jobs).find(
            (node) => node.download !== undefined,
          );
          assert.equal(
            download.download,
            request.report_name,
            "Displayed filename matches native download",
          );
        }
      }
      for (const question_custom of [0, null]) {
        const browser = createBrowser();
        await browser.respond(0, {
          jobs: [
            {
              ...job("done"),
              question: question_custom === 0 ? "Default instructions" : null,
              question_custom,
            },
          ],
        });
        assert.ok(
          !descendants(browser.elements.jobs).some(
            (node) => node.className === "custom-prompt",
          ),
          "No empty or default prompt blocks",
        );
      }
    });
    test("cancellation disables duplicate clicks and awaits backend confirmation", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job()] });
      const pending = browser.api.cancelJob("a".repeat(32));
      browser.api.cancelJob("a".repeat(32));
      const cancel = descendants(browser.elements.jobs).find(
        (node) => node.className === "button-danger" && !node.id,
      );
      assert.ok(
        cancel.disabled && cancel.attributes["aria-busy"] === "true",
        "Pending cancellation is disabled and accessible",
      );
      assert.equal(
        cancel.textContent,
        "Requesting cancellation…",
        "Do not claim cancellation before confirmation",
      );
      assert.equal(
        browser.requests.filter((request) => request.options?.method === "POST")
          .length,
        1,
        "Only one cancellation request",
      );
      await browser.respond(1, { status: "running", cancel_requested: true });
      await browser.respond(2, { jobs: [{ ...job(), cancel_requested: 1 }] });
      await pending;
      const requested = descendants(browser.elements.jobs).find(
        (node) => node.className === "button-danger" && !node.id,
      );
      assert.ok(
        requested.disabled &&
          requested.textContent === "Cancellation requested",
        "Running work remains pending after backend acknowledgment",
      );
      browser.api.cancelJob("a".repeat(32));
      assert.equal(
        browser.requests.length,
        3,
        "Confirmed requests cannot be repeated",
      );
      browser.click();
      await browser.respond(3, { jobs: [job("cancelled")] });
      assert.ok(
        !descendants(browser.elements.jobs).some(
          (node) => node.className === "button-danger" && !node.id,
        ),
        "Final jobs have no cancel action",
      );
      browser.api.cancelJob("a".repeat(32));
      assert.equal(
        browser.requests.length,
        4,
        "Final jobs cannot be cancelled",
      );
    });
    test("failed cancellation restores the control without changing job status", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job()] });
      const pending = browser.api.cancelJob("a".repeat(32));
      browser.requests[1].reject(Error("Offline"));
      await pending;
      const cancel = descendants(browser.elements.jobs).find(
        (node) => node.className === "button-danger" && !node.id,
      );
      assert.ok(
        !cancel.disabled && cancel.textContent === "Cancel",
        "Failed cancellation remains retryable",
      );
      assert.ok(
        !browser.elements.jobsNotice.hidden &&
          browser.elements.jobsNotice.textContent === "Offline",
        "Error is shown above the list",
      );
    });
    test("hidden tabs stop polling and visibility performs one fresh load", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job()] });
      await browser.visible(false);
      assert.equal(
        browser.polls().length,
        0,
        "Hidden page must stop its timer",
      );
      await browser.visible(true);
      browser.click();
      assert.equal(
        browser.requests.length,
        2,
        "Visibility and manual requests must coalesce",
      );
      await browser.respond(1, { jobs: [job()] });
      assert.equal(
        browser.polls().length,
        1,
        "Visibility resumes active polling",
      );
      await browser.visible(false);
      browser.api.refresh();
      await browser.respond(2, { jobs: [job()] });
      assert.equal(
        browser.polls().length,
        0,
        "Response received while hidden must not schedule a timer",
      );
      await browser.visible(true);
      await browser.respond(3, { jobs: [job("done", 100)] });
      assert.equal(
        browser.polls().length,
        0,
        "Visibility must not resume a terminal list",
      );
    });
    test("new uploads immediately refresh an idle list and start polling", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [] });
      const pending = browser.upload();
      assert.equal(
        browser.requests[1].options.method,
        "POST",
        "Upload request",
      );
      await browser.respond(1, { id: "b".repeat(32) });
      assert.equal(
        browser.requests[2].url,
        "/api/jobs",
        "Immediate refresh after upload",
      );
      await browser.respond(2, { jobs: [job("queued", 0, "b".repeat(32))] });
      await pending;
      assert.equal(
        browser.polls().length,
        1,
        "New queued job must start polling",
      );
    });
    test("upload during an old list request queues a fresh non-concurrent load", async () => {
      const browser = createBrowser();
      const pending = browser.upload();
      await browser.respond(1, { id: "b".repeat(32) });
      assert.equal(
        browser.requests.length,
        2,
        "Wait for old GET before issuing another",
      );
      await browser.respond(0, { jobs: [] });
      assert.equal(browser.requests.length, 3, "Mutation needs a fresh GET");
      assert.equal(
        browser.elements.jobs.replacements,
        0,
        "Do not render the stale list",
      );
      await browser.respond(2, { jobs: [job("queued", 0, "b".repeat(32))] });
      await pending;
      assert.equal(browser.polls().length, 1, "Poll the newly created job");
    });
    test("a mutation arriving as a list request settles still triggers a fresh load", async () => {
      const browser = createBrowser();
      const host = browser.elements.jobs;
      const replace = host.replaceChildren;
      host.replaceChildren = function (...children) {
        replace.apply(this, children);
        this.replaceChildren = replace;
        // Model a successful POST continuation queued just before GET cleanup.
        Promise.resolve().then(() => browser.api.refresh(true));
      };
      await browser.respond(0, { jobs: [] });
      assert.equal(
        browser.requests.length,
        2,
        "Do not lose a refresh during promise cleanup",
      );
      await browser.respond(1, { jobs: [job("queued", 0)] });
      assert.equal(browser.polls().length, 1, "New work needs polling");
    });
    test("retry restarts polling even if the immediate list refresh fails", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job("failed")] });
      const pending = browser.api.retry("a".repeat(32));
      await browser.respond(1, { status: "queued" });
      assert.ok(browser.requests[1].url.endsWith("/retry"), "Retry endpoint");
      await browser.respond(2, {}, false);
      await pending;
      assert.equal(
        browser.polls().length,
        1,
        "Known new work must retry the failed GET",
      );
      await browser.tick(6000);
      await browser.respond(3, { jobs: [job("running")] });
      assert.equal(browser.polls().length, 1, "Recovered active list");
    });
    test("a refused retry is reported above the list and the next action clears it", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job("failed")] });
      const refused = browser.api.retry("a".repeat(32));
      await browser.respond(
        1,
        { error_code: "retry_limit", error: "The retry limit was reached." },
        false,
      );
      await refused;
      assert.ok(
        !browser.elements.jobsNotice.hidden &&
          browser.elements.jobsNotice.textContent ===
            translations.en["error.retry_limit"],
        "Refusal shown without blocking the page",
      );
      assert.equal(
        browser.polls().length,
        0,
        "A refused retry starts no polling",
      );
      const accepted = browser.api.retry("a".repeat(32));
      assert.ok(
        browser.elements.jobsNotice.hidden,
        "A new action hides the previous outcome",
      );
      await browser.respond(2, { status: "queued" });
      await browser.respond(3, { jobs: [job("queued")] });
      await accepted;
      assert.ok(
        browser.elements.jobsNotice.hidden && browser.polls().length === 1,
        "Accepted retry polls the queued job",
      );
    });
    test("transient list errors preserve cards and recover without unnecessary rendering", async () => {
      const browser = createBrowser();
      const jobs = [job()];
      await browser.respond(0, { jobs });
      const article = browser.article();
      await browser.tick(6000);
      browser.requests[1].reject(Error("Offline"));
      await flush();
      assert.ok(
        browser.article() === article && !browser.elements.jobsError.hidden,
        "Keep cards and show an error",
      );
      assert.equal(
        browser.polls().length,
        1,
        "Keep polling last known active work",
      );
      await browser.tick(6000);
      await browser.respond(2, { jobs });
      assert.ok(
        browser.article() === article && browser.elements.jobsError.hidden,
        "Recover without rebuilding cards",
      );
    });
    test("initial failure can be recovered manually without idle polling", async () => {
      const browser = createBrowser();
      await browser.respond(0, {}, false);
      assert.equal(browser.polls().length, 0, "No active work is known");
      browser.click();
      await browser.respond(1, { jobs: [job("queued", 0)] });
      assert.equal(
        browser.polls().length,
        1,
        "Manual recovery starts active polling",
      );
    });
  });

  describe("Live output and visibility", () => {
    test("closing active Live ignores its pending response and visibility never reopens it", async () => {
      const browser = createBrowser();
      const id = "a".repeat(32);
      await browser.respond(0, { jobs: [job()] });
      browser.api.toggleLive(id);
      const output = browser.document.getElementById("live-content");
      browser.api.toggleLive(id);
      assert.equal(output.isConnected, false);
      await browser.respond(1, {
        content: "Late private output",
        active: true,
      });
      assert.equal(output.textContent, "Waiting for streamed output…");
      assert.equal(browser.document.getElementById("live-content"), null);
      assert.equal(browser.livePolls().length, 0);
      await browser.visible(false);
      await browser.visible(true);
      await browser.respond(2, { jobs: [job()] });
      assert.equal(
        browser.requests.length,
        3,
        "Only the jobs list reloads on visibility",
      );
      assert.equal(
        browser.requests.filter((request) => request.url.endsWith("/live"))
          .length,
        1,
      );
      assert.equal(browser.livePolls().length, 0);
    });

    test("repeated visibility changes coalesce active requests and keep late hidden output out", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job()] });
      browser.api.toggleLive("a".repeat(32));
      await browser.visible(false);
      await browser.visible(true);
      await browser.visible(false);
      assert.equal(
        browser.requests.length,
        3,
        "One Live and one visibility GET stay pending",
      );
      await browser.respond(1, { content: "Hidden output", active: true });
      await browser.respond(2, { jobs: [job()] });
      assert.equal(
        browser.document.getElementById("live-content").textContent,
        "Waiting for streamed output…",
      );
      assert.equal(browser.polls().length, 0);
      assert.equal(browser.livePolls().length, 0);
      await browser.visible(true);
      assert.equal(browser.requests.length, 5);
      await browser.respond(3, { jobs: [job()] });
      await browser.respond(4, { content: "Visible output", active: true });
      assert.equal(
        browser.document.getElementById("live-content").textContent,
        "Visible output",
      );
      assert.equal(browser.polls().length, 1);
      assert.equal(browser.livePolls().length, 1);
    });

    test("Live schedules one timeout after each request without overlaps or list reloads", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job()] });
      browser.api.toggleLive("a".repeat(32));
      assert.ok(
        browser.requests[1].url.endsWith("/live"),
        "Opening Live only loads its endpoint",
      );
      browser.api.loadLive();
      assert.equal(
        browser.livePolls().length,
        0,
        "Do not schedule while a request is pending",
      );
      const reasoningTab = browser.document.getElementById(
        "live-" + "a".repeat(32),
      ).children[0].children[0].children[1];
      reasoningTab.handlers.click[0]();
      assert.equal(
        browser.requests.length,
        2,
        "Live requests must not overlap",
      );
      await browser.respond(1, {
        phase: "Analysis",
        content: "Preview",
        reasoning: "",
        active: true,
      });
      assert.ok(
        browser.requests.length === 3 &&
          browser.requests[2].url.endsWith("?include_reasoning=true"),
        "A pending channel change fetches opt-in reasoning",
      );
      await browser.respond(2, {
        phase: "Analysis",
        content: "Preview",
        reasoning: "Reasoning preview",
        active: true,
      });
      assert.ok(
        browser.livePolls().length === 1 && !browser.livePolls()[0].interval,
        "Use one cancellable timeout",
      );
      const panel = browser.document.getElementById("live-" + "a".repeat(32));
      const output = browser.document.getElementById("live-content");
      output.scrollTop = 25;
      await browser.tick(6000);
      await browser.respond(3, { jobs: [job("running", 70)] });
      assert.equal(
        browser.document.getElementById("live-" + "a".repeat(32)),
        panel,
        "Reuse the Live panel when progress changes",
      );
      assert.ok(
        output.textContent === "Reasoning preview" && output.scrollTop === 25,
        "Preserve the selected Live tab, content and scroll position",
      );
      assert.equal(
        browser.requests.length,
        4,
        "List updates do not cause extra Live loads",
      );
      await browser.tick(1700);
      await browser.respond(4, {
        phase: "Analysis",
        content: "Preview",
        reasoning: "Reasoning preview",
        active: true,
      });
      await browser.visible(false);
      assert.equal(
        browser.livePolls().length,
        0,
        "Hidden tabs remove the Live timer",
      );
      browser.api.loadLive();
      assert.equal(browser.requests.length, 5, "No hidden Live requests");
      browser.api.toggleLive("a".repeat(32));
      assert.equal(
        browser.livePolls().length,
        0,
        "No timer when Live is closed",
      );
    });
    test("Live follows a panel reopened or switched while a request is pending", async () => {
      for (const nextId of ["a".repeat(32), "b".repeat(32)]) {
        const browser = createBrowser();
        await browser.respond(0, {
          jobs: [job(), job("running", 20, "b".repeat(32))],
        });
        browser.api.toggleLive("a".repeat(32));
        if (nextId === "a".repeat(32)) browser.api.toggleLive(nextId);
        browser.api.toggleLive(nextId);
        assert.equal(
          browser.requests.length,
          2,
          "Keep one pending Live request",
        );
        await browser.respond(1, {
          phase: "Old pass",
          content: "Old preview",
          active: true,
        });
        const output = browser.document.getElementById("live-content");
        assert.equal(
          output.textContent,
          "Waiting for streamed output…",
          "Ignore responses for a replaced panel",
        );
        assert.equal(
          browser.requests.length,
          3,
          "Immediately load the current panel once the old request settles",
        );
        assert.equal(
          browser.requests[2].url,
          `/api/jobs/${nextId}/live`,
          "Fetch the current job",
        );
        await browser.respond(2, {
          phase: "New pass",
          content: "Current preview",
          active: true,
        });
        assert.equal(
          output.textContent,
          "Current preview",
          "Update the current panel",
        );
      }
    });
    test("Live keeps polling queued and running jobs between inactive model passes", async () => {
      for (const status of ["queued", "running"]) {
        const browser = createBrowser();
        await browser.respond(0, { jobs: [job(status)] });
        assert.equal(
          browser.livePolls().length,
          0,
          "Closed panels have no Live timer",
        );
        browser.api.toggleLive("a".repeat(32));
        await browser.respond(1, { content: "Between passes", active: false });
        assert.equal(
          browser.livePolls().length,
          1,
          "Job activity governs polling, not model-pass activity",
        );
        await browser.tick(1700);
        await browser.respond(2, { content: "Next pass", active: true });
        assert.equal(
          browser.livePolls().length,
          1,
          "Resume output without reopening the panel",
        );
        browser.api.toggleLive("a".repeat(32));
        assert.equal(
          browser.livePolls().length,
          0,
          "Closing cancels the timeout",
        );
      }
    });
    test("terminal Live panels load on demand and stay idle across other job updates", async () => {
      for (const status of ["done", "failed", "cancelled"]) {
        const browser = createBrowser();
        await browser.respond(0, {
          jobs: [job(status), job("running", 20, "b".repeat(32))],
        });
        browser.api.toggleLive("a".repeat(32));
        await browser.respond(1, { content: "Old answer", active: false });
        assert.equal(
          browser.livePolls().length,
          0,
          "Opening history performs only one load",
        );
        await browser.tick(6000);
        await browser.respond(2, {
          jobs: [job(status), job("running", 30, "b".repeat(32))],
        });
        assert.ok(
          browser.requests.length === 3 && browser.livePolls().length === 0,
          "Another job must not poll a historical panel",
        );
        const tabs = browser.document.getElementById("live-" + "a".repeat(32))
          .children[0].children[0].children;
        tabs[1].handlers.click[0]();
        await browser.respond(3, {
          content: "Old answer",
          reasoning: "Old reasoning",
          active: false,
        });
        assert.equal(
          browser.document.getElementById("live-content").textContent,
          "Old reasoning",
          "Manual reasoning selection still loads",
        );
        tabs[0].handlers.click[0]();
        await browser.respond(4, { content: "Old answer", active: false });
        assert.equal(
          browser.livePolls().length,
          0,
          "Channel changes never start history polling",
        );
      }
    });
    test("a terminal job gets one final preview before Live polling stops", async () => {
      for (const status of ["done", "failed", "cancelled"]) {
        const browser = createBrowser();
        await browser.respond(0, { jobs: [job()] });
        browser.api.toggleLive("a".repeat(32));
        await browser.respond(1, { content: "Partial", active: true });
        await browser.tick(6000);
        await browser.respond(2, { jobs: [job(status)] });
        assert.ok(
          browser.livePolls().length === 0 && browser.requests.length === 4,
          "Cancel the timer and fetch the final preview",
        );
        await browser.respond(3, { content: "Final", active: false });
        assert.ok(
          browser.document.getElementById("live-content").textContent ===
            "Final" && browser.livePolls().length === 0,
          "Keep the last useful text without a timer",
        );
      }
    });
    test("termination during a Live request coalesces one final reload", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job()] });
      browser.api.toggleLive("a".repeat(32));
      await browser.tick(6000);
      await browser.respond(2, { jobs: [job("done")] });
      assert.equal(
        browser.requests.length,
        3,
        "Do not overlap the pending preview",
      );
      await browser.respond(1, { content: "Older snapshot", active: true });
      assert.equal(
        browser.requests.length,
        4,
        "Follow the changed job status with one final request",
      );
      await browser.respond(3, { content: "Completed", active: false });
      assert.equal(
        browser.livePolls().length,
        0,
        "No restart after the final response",
      );
    });
    test("visibility cancels Live timers and late hidden responses never restart them", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job()] });
      browser.api.toggleLive("a".repeat(32));
      await browser.visible(false);
      await browser.respond(1, { content: "Hidden snapshot", active: true });
      assert.equal(
        browser.livePolls().length,
        0,
        "An in-flight response cannot restart hidden polling",
      );
      await browser.visible(true);
      assert.equal(
        browser.requests.length,
        4,
        "Visibility performs one list and one active Live load",
      );
      await browser.respond(2, { jobs: [job()] });
      await browser.respond(3, { content: "Current", active: false });
      assert.equal(
        browser.livePolls().length,
        1,
        "Visible running jobs resume between passes",
      );
      await browser.visible(false);
      assert.equal(
        browser.livePolls().length,
        0,
        "Cancel an already planned timeout too",
      );
    });
    test("destroying or resetting UI prevents late Live and list responses from restarting polling", async () => {
      for (const cleanup of ["stop", "resetState"]) {
        const browser = createBrowser();
        await browser.respond(0, { jobs: [job()] });
        browser.api.toggleLive("a".repeat(32));
        browser.api.refresh();
        browser.api[cleanup]();
        assert.ok(
          browser.polls().length === 0 && browser.livePolls().length === 0,
          "Cleanup releases both polling timers",
        );
        await browser.respond(1, { content: "Old response", active: true });
        await browser.respond(2, { jobs: [job()] });
        assert.ok(
          browser.polls().length === 0 &&
            browser.livePolls().length === 0 &&
            browser.requests.length === 3,
          "Late old responses do not schedule or request anything",
        );
      }
    });
    test("reinitializing UI isolates pending requests and schedules only new work", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job()] });
      browser.api.toggleLive("a".repeat(32));
      browser.api.start();
      assert.equal(
        browser.requests.length,
        3,
        "Restart requests a fresh jobs list",
      );
      await browser.respond(2, { jobs: [job("running", 70)] });
      browser.api.toggleLive("a".repeat(32));
      await browser.respond(1, { content: "Old UI response", active: true });
      assert.ok(
        browser.requests.length === 4 && browser.livePolls().length === 0,
        "Old completion cannot unlock or schedule over a new request",
      );
      await browser.respond(3, { content: "New UI response", active: true });
      assert.ok(
        browser.livePolls().length === 1 && browser.polls().length === 1,
        "Only the new UI schedules polling",
      );
    });
    test("UI cleanup clears already scheduled Live and list timeouts", async () => {
      for (const cleanup of ["stop", "resetState"]) {
        const browser = createBrowser();
        await browser.respond(0, { jobs: [job()] });
        browser.api.toggleLive("a".repeat(32));
        await browser.respond(1, { content: "Current", active: true });
        assert.ok(
          browser.livePolls().length === 1 && browser.polls().length === 1,
          "Both timers are scheduled",
        );
        browser.api[cleanup]();
        assert.ok(
          browser.livePolls().length === 0 && browser.polls().length === 0,
          "Cleanup cancels both scheduled timers",
        );
      }
    });
    test("termination observed while hidden still gets a final preview on visibility", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job()] });
      browser.api.toggleLive("a".repeat(32));
      await browser.respond(1, { content: "Partial", active: true });
      browser.api.refresh();
      await browser.visible(false);
      await browser.respond(2, { jobs: [job("done")] });
      assert.equal(
        browser.requests.length,
        3,
        "No hidden final preview request",
      );
      await browser.visible(true);
      assert.equal(
        browser.requests.length,
        5,
        "Visibility requests the final preview once",
      );
      await browser.respond(3, { jobs: [job("done")] });
      await browser.respond(4, { content: "Final", active: false });
      assert.ok(
        browser.livePolls().length === 0 &&
          browser.document.getElementById("live-content").textContent ===
            "Final",
        "Final output stays idle",
      );
    });

    for (const status of ["done", "failed", "cancelled"]) {
      test(`${status} final response received while hidden is recovered once on visibility`, async () => {
        const browser = createBrowser();
        await browser.respond(0, { jobs: [job()] });
        browser.api.toggleLive("a".repeat(32));
        await browser.respond(1, { content: "Partial", active: true });
        await browser.tick(6000);
        await browser.respond(2, { jobs: [job(status)] });
        assert.ok(
          browser.requests.length === 4 && browser.livePolls().length === 0,
          "Terminal observation starts exactly one final load",
        );
        await browser.visible(false);
        await browser.respond(3, { content: "Final", active: false });
        const output = browser.document.getElementById("live-content");
        assert.ok(
          output.textContent === "Partial" && browser.livePolls().length === 0,
          "Hidden response is not treated as rendered",
        );
        await browser.visible(true);
        assert.ok(
          browser.requests.length === 6 &&
            browser.requests[5].url.endsWith("/live"),
          "Visibility starts one list and one needed final load",
        );
        await browser.respond(4, { jobs: [job(status)] });
        assert.equal(
          browser.requests.length,
          6,
          "Unchanged terminal list does not duplicate the pending final load",
        );
        await browser.respond(5, { content: "Final", active: false });
        assert.ok(
          output.textContent === "Final" && browser.livePolls().length === 0,
          "Render final text and leave no live timer",
        );
        await browser.visible(false);
        await browser.visible(true);
        await browser.respond(6, { jobs: [job(status)] });
        browser.click();
        await browser.respond(7, { jobs: [job(status)] });
        assert.ok(
          browser.requests.length === 8 &&
            browser.livePolls().length === 0 &&
            browser.polls().length === 0,
          "Confirmed final output needs no more requests or polling",
        );
        assert.equal(
          output.textContent,
          "Final",
          "Confirmed final text survives subsequent visibility and list refreshes",
        );
      });
    }
    test("a terminal load failing while hidden remains pending without background retries", async () => {
      for (const failure of ["network", "http", "timeout"]) {
        const browser = createBrowser();
        await browser.respond(0, { jobs: [job()] });
        browser.api.toggleLive("a".repeat(32));
        await browser.respond(1, { content: "Partial", active: true });
        await browser.tick(6000);
        await browser.respond(2, { jobs: [job("done")] });
        await browser.visible(false);
        if (failure === "network") {
          browser.requests[3].reject(Error("Offline"));
          await flush();
        } else if (failure === "http") await browser.respond(3, {}, false);
        else await browser.tick(15000);
        assert.ok(
          browser.requests.length === 4 && browser.livePolls().length === 0,
          "Hidden failures never start retries",
        );
        assert.equal(
          browser.document.getElementById("live-content").textContent,
          "Partial",
          "Preserve useful text on failure",
        );
        await browser.visible(true);
        await browser.respond(4, { jobs: [job("done")] });
        assert.equal(
          browser.requests.length,
          6,
          "Retry the unconfirmed final preview once on visibility",
        );
        await browser.respond(5, { content: "Recovered final", active: false });
        if (failure === "timeout")
          await browser.respond(3, {
            content: "Expired response",
            active: false,
          });
        assert.equal(
          browser.document.getElementById("live-content").textContent,
          "Recovered final",
          "Late expired responses cannot overwrite recovery",
        );
        assert.ok(
          browser.requests.length === 6 && browser.livePolls().length === 0,
          "Successful recovery leaves no extra request or timer",
        );
      }
    });
    test("visibility during a pending final request coalesces recovery and bounds repeated failures", async () => {
      for (const succeeds of [true, false]) {
        const browser = createBrowser();
        await browser.respond(0, { jobs: [job()] });
        browser.api.toggleLive("a".repeat(32));
        await browser.respond(1, { content: "Partial", active: true });
        await browser.tick(6000);
        await browser.respond(2, { jobs: [job("done")] });
        await browser.visible(false);
        await browser.visible(true);
        await browser.respond(4, { jobs: [job("done")] });
        assert.equal(
          browser.requests.length,
          5,
          "Returning visible never overlaps the pending final request",
        );
        if (succeeds) {
          await browser.respond(3, { content: "Final", active: false });
          assert.ok(
            browser.requests.length === 5 &&
              browser.document.getElementById("live-content").textContent ===
                "Final",
            "A relevant successful response satisfies recovery directly",
          );
        } else {
          browser.requests[3].reject(Error("Offline"));
          await flush();
          assert.equal(
            browser.requests.length,
            6,
            "One pending visibility recovery follows a failed request",
          );
          browser.requests[5].reject(Error("Still offline"));
          await flush();
          browser.click();
          await browser.respond(6, { jobs: [job("done")] });
          assert.ok(
            browser.requests.length === 7 &&
              browser.document.getElementById("live-content").textContent ===
                "Partial",
            "Repeated failure does not create an immediate or list-triggered retry loop",
          );
          assert.ok(
            browser.document
              .getElementById("live-note")
              .textContent.includes("Still offline"),
            "Keep the recovery error visible",
          );
        }
        assert.ok(
          browser.livePolls().length === 0 && browser.polls().length === 0,
          "Terminal recovery never creates permanent polling",
        );
      }
    });
    test("closing a terminal panel before a hidden final response prevents recovery requests", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job()] });
      browser.api.toggleLive("a".repeat(32));
      await browser.respond(1, { content: "Partial", active: true });
      await browser.tick(6000);
      await browser.respond(2, { jobs: [job("done")] });
      await browser.visible(false);
      browser.api.toggleLive("a".repeat(32));
      await browser.respond(3, { content: "Final", active: false });
      await browser.visible(true);
      await browser.respond(4, { jobs: [job("done")] });
      assert.ok(
        !browser.document.getElementById("live-content"),
        "The closed panel is not recreated by its response",
      );
      assert.ok(
        browser.requests.length === 5 && browser.livePolls().length === 0,
        "Closed panels never request a final recovery",
      );
      browser.api.toggleLive("a".repeat(32));
      assert.equal(
        browser.requests.length,
        6,
        "Reopening starts exactly one on-demand load",
      );
      await browser.respond(5, {
        content: "Final after reopening",
        active: false,
      });
      assert.ok(
        browser.document.getElementById("live-content").textContent ===
          "Final after reopening" && browser.livePolls().length === 0,
        "Reopened terminal panel renders and stays idle",
      );
    });
    test("reopening a terminal panel while its old final request is pending ignores the old response", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job()] });
      browser.api.toggleLive("a".repeat(32));
      await browser.respond(1, { content: "Partial", active: true });
      await browser.tick(6000);
      await browser.respond(2, { jobs: [job("done")] });
      browser.api.toggleLive("a".repeat(32));
      browser.api.toggleLive("a".repeat(32));
      assert.equal(
        browser.requests.length,
        4,
        "Reopening waits for the existing request to settle",
      );
      await browser.respond(3, { content: "Old panel final", active: false });
      assert.ok(
        browser.requests.length === 5 &&
          browser.document.getElementById("live-content").textContent ===
            "Waiting for streamed output…",
        "Ignore replaced panel response and load current panel once",
      );
      await browser.respond(4, {
        content: "Current panel final",
        active: false,
      });
      assert.ok(
        browser.document.getElementById("live-content").textContent ===
          "Current panel final" && browser.livePolls().length === 0,
        "Only current final text is confirmed",
      );
    });
    test("accepted retry invalidates a previous attempt response before the refreshed list arrives", async () => {
      const browser = createBrowser();
      const id = "a".repeat(32);
      await browser.respond(0, { jobs: [job("failed")] });
      browser.api.toggleLive(id);
      const retry = browser.api.retry(id);
      await browser.respond(2, { status: "queued" });
      assert.equal(
        browser.requests.length,
        4,
        "Accepted retry refreshes the list without overlapping live requests",
      );
      await browser.respond(1, { content: "Previous attempt", active: false });
      assert.equal(
        browser.document.getElementById("live-content").textContent,
        "Waiting for streamed output…",
        "Previous attempt is rejected immediately after retry acknowledgement",
      );
      assert.ok(
        browser.requests.length === 4 && browser.livePolls().length === 0,
        "Wait for fresh job state before starting new live work",
      );
      await browser.respond(3, {
        jobs: [{ ...job("queued"), queued_at: "2026-11-05T14:00:00Z" }],
      });
      await retry;
      assert.equal(
        browser.requests.length,
        5,
        "Queued retry resumes one serialized live load",
      );
      await browser.respond(4, { content: "", active: false });
      await browser.tick(6000);
      await browser.respond(5, {
        jobs: [{ ...job("running"), queued_at: "2026-11-05T14:00:00Z" }],
      });
      await browser.respond(6, { content: "New attempt", active: true });
      assert.ok(
        browser.document.getElementById("live-content").textContent ===
          "New attempt" && browser.livePolls().length === 1,
        "New attempt streams normally",
      );
    });
    test("old final responses cannot survive a retry status cycle back to the same terminal status", async () => {
      const browser = createBrowser();
      const id = "a".repeat(32);
      await browser.respond(0, { jobs: [job("failed")] });
      browser.api.toggleLive(id);
      browser.click();
      await browser.respond(2, { jobs: [job("queued")] });
      browser.click();
      await browser.respond(3, { jobs: [job("running")] });
      browser.click();
      await browser.respond(4, { jobs: [job("failed")] });
      assert.equal(
        browser.requests.length,
        5,
        "Status changes share the existing live request",
      );
      await browser.respond(1, {
        content: "Old failed attempt",
        active: false,
      });
      assert.equal(
        browser.document.getElementById("live-content").textContent,
        "Waiting for streamed output…",
        "Same final status does not make an old attempt response relevant again",
      );
      assert.equal(
        browser.requests.length,
        6,
        "Load the new attempt final exactly once after settlement",
      );
      await browser.respond(5, {
        content: "New failed attempt",
        active: false,
      });
      assert.ok(
        browser.document.getElementById("live-content").textContent ===
          "New failed attempt" && browser.livePolls().length === 0,
        "Render the current attempt without terminal polling",
      );
    });
    test("a changed queue timestamp rejects an older attempt even when its terminal status is unchanged", async () => {
      const browser = createBrowser();
      const id = "a".repeat(32);
      await browser.respond(0, {
        jobs: [{ ...job("cancelled"), queued_at: "2026-11-05T14:00:00Z" }],
      });
      browser.api.toggleLive(id);
      browser.click();
      await browser.respond(2, {
        jobs: [{ ...job("cancelled"), queued_at: "2026-11-05T14:01:00Z" }],
      });
      await browser.respond(1, {
        content: "Old cancelled attempt",
        active: false,
      });
      assert.ok(
        browser.requests.length === 4 &&
          browser.document.getElementById("live-content").textContent ===
            "Waiting for streamed output…",
        "Queue timestamp invalidates a missed retry transition",
      );
      await browser.respond(3, {
        content: "New cancelled attempt",
        active: false,
      });
      assert.ok(
        browser.document.getElementById("live-content").textContent ===
          "New cancelled attempt" && browser.livePolls().length === 0,
        "New terminal output is confirmed without polling",
      );
    });
  });

  describe("Request deadlines", () => {
    test("a stalled jobs request times out and releases its shared promise", async () => {
      const browser = createBrowser();
      await browser.tick(15000);
      assert.ok(!browser.elements.jobsError.hidden, "Timeout must be visible");
      browser.click();
      assert.equal(
        browser.requests.length,
        2,
        "Retry after a timeout must issue a new request",
      );
      await browser.respond(1, { jobs: [job("queued")] });
      assert.equal(browser.polls().length, 1, "Polling recovers");
      await browser.respond(0, { jobs: [] });
      assert.equal(
        browser.polls().length,
        1,
        "Late timed-out response must not overwrite current work",
      );
    });
    test("JSON decoding is also covered by the request deadline", async () => {
      const browser = createBrowser();
      browser.requests[0].resolve({
        ok: true,
        status: 200,
        json: () => new Promise(() => {}),
      });
      await flush();
      await browser.tick(15000);
      browser.click();
      assert.equal(
        browser.requests.length,
        2,
        "Stalled response body must release the lock",
      );
      await browser.respond(1, { jobs: [] });
      assert.ok(
        browser.elements.jobsError.hidden,
        "Recover after stalled JSON",
      );
    });
    test("a stalled Live request can be retried without starting inference", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job()] });
      browser.api.toggleLive("a".repeat(32));
      await browser.tick(15000);
      await browser.tick(1700);
      assert.equal(
        browser.requests.length,
        3,
        "Live timeout must release liveBusy",
      );
      assert.ok(
        browser.requests.every(
          (r) => !r.options?.method || r.options.method === "GET",
        ),
        "Live must issue only GETs",
      );
      await browser.respond(2, { content: "Recovered", active: true });
      assert.equal(
        browser.document.getElementById("live-content").textContent,
        "Recovered",
        "Live recovers",
      );
    });
  });

  describe("History selection, deletion and keyboard focus", () => {
    test("deleting an open historical Live panel ignores its late response while other work polls", async () => {
      const browser = createBrowser();
      const id = "a".repeat(32);
      const active = job("running", 20, "b".repeat(32));
      await browser.respond(0, { jobs: [job("done"), active] });
      browser.api.toggleLive(id);
      const output = browser.document.getElementById("live-content");
      browser.api.confirmDeletion([id], false);
      const deletion = browser.api.submitDeletion();
      await browser.respond(2, { id, deleted: true });
      assert.equal(browser.document.getElementById("live-content"), null);
      await browser.respond(1, {
        content: "Deleted job output",
        active: false,
      });
      await browser.respond(3, { jobs: [active] });
      await deletion;
      assert.equal(output.textContent, "Waiting for streamed output…");
      assert.equal(browser.requests.length, 4);
      assert.equal(browser.livePolls().length, 0);
      assert.equal(
        browser.polls().length,
        1,
        "Another active analysis still polls",
      );
      assert.equal(
        browser.document.activeElement,
        browser.document.getElementById("toggleSelection"),
      );
    });

    test("removing the focused selected row keeps remaining history unselected and focus visible", async () => {
      const browser = createBrowser();
      const remaining = job("failed", 10, "b".repeat(32));
      await browser.respond(0, { jobs: [job("done"), remaining] });
      toggleSelection(browser);
      setSelected(selectionCheckboxes(browser)[0]);
      const old = selectionCheckboxes(browser)[0];
      old.focus();
      browser.click();
      await browser.respond(1, { jobs: [remaining] });
      assert.equal(old.isConnected, false);
      assert.equal(selectionCheckboxes(browser).length, 1);
      assert.equal(selectionCheckboxes(browser)[0].checked, false);
      assert.equal(
        browser.document.getElementById("selectAllJobs").indeterminate,
        false,
      );
      assert.equal(
        browser.document.getElementById("selectionActions").hidden,
        true,
      );
      const focused = browser.document.activeElement;
      assert.equal(focused, browser.document.getElementById("toggleSelection"));
      assert.equal(focused.isConnected, true);
      assert.equal(focused.disabled, false);
      assert.equal(focused.closest("[hidden]"), null);
    });

    test("hidden selection controls cannot select jobs outside selection mode", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job("done")] });
      const checkbox = selectionCheckboxes(browser)[0];
      assert.equal(
        checkbox.parentElement.tag,
        "div",
        "Normal filename must not activate a hidden checkbox label",
      );
      setSelected(checkbox);
      assert.ok(
        !checkbox.checked &&
          browser.document.getElementById("selectionActions").hidden,
        "Ignore hidden checkbox changes",
      );
      setSelected(browser.document.getElementById("selectAllJobs"));
      assert.ok(
        !selectionCheckboxes(browser)[0].checked,
        "Ignore hidden select-all changes",
      );
      toggleSelection(browser);
      assert.equal(
        selectionCheckboxes(browser)[0].parentElement.tag,
        "label",
        "Visible selection keeps its accessible label",
      );
    });
    test("selection mode starts hidden, toggles controls and resets on cancel", async () => {
      const browser = createBrowser();
      await browser.respond(0, {
        jobs: [
          job("done"),
          job("failed", 0, "b".repeat(32)),
          job("running", 20, "c".repeat(32)),
        ],
      });
      const get = (id) => browser.document.getElementById(id),
        trash = () =>
          descendants(browser.elements.jobs).filter((n) => n.dataset.deleteJob);
      assert.ok(
        get("toggleSelection").textContent === "Select" &&
          get("toggleSelection").attributes["aria-pressed"] === "false",
        "Normal history header",
      );
      assert.ok(
        selectionCheckboxes(browser).every((n) => n.hidden) &&
          get("historyControls").hidden &&
          get("selectionActions").hidden,
        "All selection UI hidden initially",
      );
      assert.ok(
        trash().every((n) => !n.hidden),
        "Individual delete remains visible",
      );
      const status = descendants(browser.elements.jobs).find(
        (n) => n.className === "job-status" && n.children.length === 2,
      );
      assert.ok(
        status.children[0].className.includes("pill") &&
          status.children[1].dataset.deleteJob,
        "Badge and trash stay grouped",
      );
      toggleSelection(browser);
      assert.ok(
        get("toggleSelection").textContent === "Cancel" &&
          get("toggleSelection").attributes["aria-pressed"] === "true",
        "Active selection header",
      );
      assert.ok(
        selectionCheckboxes(browser).every((n) => !n.hidden) &&
          !get("historyControls").hidden &&
          trash().every((n) => n.hidden),
        "Selection controls replace trash",
      );
      assert.ok(
        get("selectionSummary").textContent === "0 analyses selected" &&
          get("selectionActions").hidden,
        "Zero count without delete bar",
      );
      setSelected(selectionCheckboxes(browser)[0]);
      assert.ok(
        !get("selectionActions").hidden &&
          get("deleteSelection").textContent === "Delete (1)",
        "Counted delete action",
      );
      const history = browser.elements.jobs.children[1];
      assert.equal(
        history.children[history.children.length - 1],
        get("selectionActions"),
        "Bulk action bar below the list",
      );
      toggleSelection(browser);
      assert.ok(
        selectionCheckboxes(browser).every((n) => n.hidden && !n.checked) &&
          get("selectionActions").hidden &&
          get("historyControls").hidden,
        "Cancel fully resets selection",
      );
      assert.ok(
        trash().every((n) => !n.hidden),
        "Cancel restores individual trash",
      );
      toggleSelection(browser);
      assert.ok(
        selectionCheckboxes(browser).every((n) => !n.checked),
        "Reopening does not restore old selection",
      );
    });
    test("empty history supports entering and cancelling selection without deletion", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [] });
      toggleSelection(browser);
      assert.ok(
        browser.document.getElementById("selectAllJobs").disabled,
        "Nothing to select in empty history",
      );
      assert.ok(
        selectionCheckboxes(browser).length === 0 &&
          browser.document.getElementById("selectionActions").hidden,
        "No checkbox rows or bulk action",
      );
      assert.ok(
        descendants(browser.elements.jobs).some(
          (n) => n.textContent === "No analysis history yet.",
        ),
        "Keep empty state",
      );
      toggleSelection(browser);
      assert.ok(
        browser.document.getElementById("historyControls").hidden,
        "Cancel empty selection",
      );
    });
    test("selection is visible-only, supports indeterminate and survives progress refreshes", async () => {
      const browser = createBrowser();
      const list = [
        job("done"),
        job("failed", 10, "b".repeat(32)),
        job("running", 20, "c".repeat(32)),
      ];
      await browser.respond(0, { jobs: list });
      assert.equal(
        selectionCheckboxes(browser).length,
        2,
        "Active analyses are not selectable",
      );
      toggleSelection(browser);
      setSelected(selectionCheckboxes(browser)[0]);
      assert.ok(
        browser.document.getElementById("selectAllJobs").indeterminate,
        "Partial selection",
      );
      assert.equal(
        browser.document.getElementById("selectionCount").textContent,
        "1 analyses selected",
        "Selection count",
      );
      selectionCheckboxes(browser)[0].focus();
      browser.click();
      await browser.respond(1, {
        jobs: [...list.slice(0, 2), { ...list[2], progress_percent: 60 }],
      });
      assert.ok(
        selectionCheckboxes(browser)[0].checked,
        "Selection survives a progress refresh",
      );
      assert.equal(
        browser.document.activeElement,
        selectionCheckboxes(browser)[0],
        "Keyboard focus survives a progress refresh",
      );
      setSelected(browser.document.getElementById("selectAllJobs"));
      assert.ok(
        selectionCheckboxes(browser).every((n) => n.checked),
        "Select all visible terminal analyses",
      );
      setSelected(browser.document.getElementById("selectAllJobs"), false);
      assert.ok(
        selectionCheckboxes(browser).every((n) => !n.checked) &&
          browser.document.getElementById("selectionActions").hidden,
        "Deselect all",
      );
      setSelected(selectionCheckboxes(browser)[0]);
      browser.click();
      await browser.respond(2, { jobs: [list[1]] });
      assert.ok(
        browser.document.getElementById("selectionActions").hidden,
        "Invisible selection is discarded",
      );
    });
    test("individual and bulk confirmations never delete on cancel or Escape", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job("done")] });
      const remove = descendants(browser.elements.jobs).find(
        (n) => n.dataset.deleteJob,
      );
      remove.handlers.click[0]();
      assert.ok(
        browser.elements.deleteDialog.open &&
          browser.elements.deleteDocument.textContent === "example.pdf",
        "Named confirmation",
      );
      assert.equal(
        browser.requests.length,
        1,
        "No deletion before confirmation",
      );
      browser.elements.deleteCancel.handlers.click[0]();
      assert.ok(
        !browser.elements.deleteDialog.open && browser.requests.length === 1,
        "Cancelled without request",
      );
      toggleSelection(browser);
      setSelected(selectionCheckboxes(browser)[0]);
      browser.document.getElementById("deleteSelection").handlers.click[0]();
      assert.ok(
        browser.elements.deleteMessage.textContent.includes("1 analyses"),
        "Exact bulk count",
      );
      browser.elements.deleteDialog.handlers.cancel[0]({ preventDefault() {} });
      assert.ok(
        !browser.elements.deleteDialog.open &&
          selectionCheckboxes(browser)[0].checked &&
          browser.requests.length === 1,
        "Escape preserves selection",
      );
    });
    test("selection and confirmation never include queued or running analyses", async () => {
      const browser = createBrowser();
      const active = [
        job("queued", 0, "b".repeat(32)),
        job("running", 20, "c".repeat(32)),
      ];
      await browser.respond(0, { jobs: [job("done"), ...active] });
      assert.equal(
        descendants(browser.elements.jobs).filter((n) => n.dataset.deleteJob)
          .length,
        1,
        "Only finished analyses have trash buttons",
      );
      browser.api.confirmDeletion(
        active.map((j) => j.id),
        true,
      );
      assert.ok(
        !browser.elements.deleteDialog.open && browser.requests.length === 1,
        "Active-only confirmation is refused",
      );
      toggleSelection(browser);
      setSelected(browser.document.getElementById("selectAllJobs"));
      browser.document.getElementById("deleteSelection").handlers.click[0]();
      const pending = browser.api.submitDeletion();
      assert.deepEqual(
        JSON.parse(browser.requests[1].options.body).ids,
        ["a".repeat(32)],
        "Send only the visible terminal ID",
      );
      await browser.respond(1, {
        results: [{ id: "a".repeat(32), deleted: true }],
      });
      await browser.respond(2, { jobs: active });
      await pending;
    });
    test("selection toggle retains focus and accessible state across refresh and cancellation", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job("done")] });
      toggleSelection(browser);
      const first = browser.document.getElementById("toggleSelection");
      assert.ok(
        browser.document.activeElement === first &&
          first.attributes["aria-pressed"] === "true" &&
          first.attributes["aria-controls"] === "historyControls",
        "Toggle keeps focus and announces its controls",
      );
      setSelected(selectionCheckboxes(browser)[0]);
      browser.click();
      await browser.respond(1, {
        jobs: [job("done"), job("running", 1, "b".repeat(32))],
      });
      const current = browser.document.getElementById("toggleSelection");
      assert.ok(
        current !== first &&
          !first.isConnected &&
          browser.document.activeElement === current,
        "Focus the replacement toggle after rendering",
      );
      toggleSelection(browser);
      assert.ok(
        browser.document.activeElement ===
          browser.document.getElementById("toggleSelection") &&
          !selectionCheckboxes(browser)[0].checked,
        "Cancel clears selection and preserves focus",
      );
    });
    test("dialog cancellation restores a refreshed initiator or a visible fallback", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job("done")] });
      const trash = () =>
        descendants(browser.elements.jobs).find((n) => n.dataset.deleteJob);
      const original = trash();
      original.focus();
      original.handlers.click[0]();
      assert.equal(
        browser.document.activeElement,
        browser.elements.deleteCancel,
        "Dialog opens on the safe cancel control",
      );
      browser.click();
      await browser.respond(1, {
        jobs: [{ ...job("done"), filename: "changed.pdf" }],
      });
      browser.elements.deleteCancel.handlers.click[0]();
      assert.ok(
        !original.isConnected && browser.document.activeElement === trash(),
        "Cancel returns to the replacement trash button",
      );
      toggleSelection(browser);
      setSelected(selectionCheckboxes(browser)[0]);
      browser.document.getElementById("deleteSelection").handlers.click[0]();
      browser.click();
      await browser.respond(2, { jobs: [] });
      browser.elements.deleteDialog.handlers.cancel[0]({ preventDefault() {} });
      assert.equal(
        browser.document.activeElement,
        browser.document.getElementById("toggleSelection"),
        "Do not focus a button inside a hidden action bar",
      );
      assert.equal(
        browser.requests.length,
        3,
        "Cancel and Escape never send deletion requests",
      );
    });
    test("deletion locks selection changes and restores focus after completion", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job("done")] });
      toggleSelection(browser);
      setSelected(selectionCheckboxes(browser)[0]);
      browser.document.getElementById("deleteSelection").handlers.click[0]();
      const pending = browser.api.submitDeletion();
      assert.ok(
        browser.document.getElementById("toggleSelection").disabled &&
          browser.document.getElementById("selectAllJobs").disabled &&
          selectionCheckboxes(browser)[0].disabled,
        "Disable all selection controls during deletion",
      );
      toggleSelection(browser);
      setSelected(selectionCheckboxes(browser)[0], false);
      setSelected(browser.document.getElementById("selectAllJobs"), false);
      assert.ok(
        selectionCheckboxes(browser)[0].checked &&
          browser.document.getElementById("toggleSelection").attributes[
            "aria-pressed"
          ] === "true",
        "Even stale events cannot alter the in-flight selection",
      );
      await browser.respond(1, {
        results: [{ id: "a".repeat(32), deleted: true }],
      });
      await browser.respond(2, { jobs: [] });
      await pending;
      assert.equal(
        browser.document.activeElement,
        browser.document.getElementById("toggleSelection"),
        "Empty history falls back to its enabled toggle",
      );
    });
    test("deselect keeps selection mode open and moves focus out of the hidden action bar", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job("done")] });
      toggleSelection(browser);
      setSelected(selectionCheckboxes(browser)[0]);
      const clear = browser.document.getElementById("clearSelection");
      clear.focus();
      clear.handlers.click[0]();
      assert.ok(
        selectionCheckboxes(browser).every((n) => !n.checked) &&
          browser.document.getElementById("selectionActions").hidden,
        "Deselect clears all visible checks",
      );
      assert.equal(
        browser.document.getElementById("toggleSelection").attributes[
          "aria-pressed"
        ],
        "true",
        "Deselect preserves selection mode",
      );
      assert.equal(
        browser.document.activeElement,
        browser.document.getElementById("selectAllJobs"),
        "Focus the still-visible select-all control",
      );
    });
    test("a focused terminal analysis becoming active loses selection and returns focus safely", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job("done")] });
      toggleSelection(browser);
      setSelected(selectionCheckboxes(browser)[0]);
      const old = selectionCheckboxes(browser)[0];
      old.focus();
      browser.click();
      await browser.respond(1, { jobs: [job("running")] });
      assert.ok(
        !old.isConnected && selectionCheckboxes(browser).length === 0,
        "An active job has no selection checkbox",
      );
      assert.ok(
        browser.document.getElementById("selectionActions").hidden,
        "Discard selection when a job becomes active",
      );
      assert.equal(
        browser.document.activeElement,
        browser.document.getElementById("toggleSelection"),
        "Keep focus on a visible history control",
      );
    });
    test("individual deletion removes only the confirmed card and shows an empty state", async () => {
      const browser = createBrowser();
      const id = "a".repeat(32);
      await browser.respond(0, { jobs: [job("done")] });
      browser.api.confirmDeletion([id], false);
      const pending = browser.api.submitDeletion();
      browser.api.submitDeletion();
      assert.ok(
        browser.requests.length === 2 &&
          browser.requests[1].url === `/api/jobs/${id}/delete`,
        "One individual POST",
      );
      assert.ok(
        selectionCheckboxes(browser)[0].disabled,
        "Selection disabled while deleting",
      );
      await browser.respond(1, { id, deleted: true, warning_code: null });
      assert.ok(
        !browser.article() && !browser.elements.jobsNotice.hidden,
        "Immediately remove successful entry",
      );
      assert.equal(
        browser.elements.jobsNotice.textContent,
        "Deleted: 1.",
        "Success notice",
      );
      await browser.respond(2, { jobs: [] });
      await pending;
      assert.ok(
        descendants(browser.elements.jobs).some(
          (n) => n.textContent === "No analysis history yet.",
        ),
        "Empty history",
      );
      assert.equal(
        browser.polls().length,
        0,
        "Deletion must not start active polling",
      );
    });
    test("partial batch deletion preserves failed entries and their selection", async () => {
      const browser = createBrowser();
      const first = "a".repeat(32);
      const second = "b".repeat(32);
      await browser.respond(0, {
        jobs: [job("done"), job("failed", 0, second)],
      });
      toggleSelection(browser);
      setSelected(browser.document.getElementById("selectAllJobs"));
      browser.document.getElementById("deleteSelection").handlers.click[0]();
      const pending = browser.api.submitDeletion();
      assert.equal(
        JSON.parse(browser.requests[1].options.body).ids.length,
        2,
        "Exact bulk IDs",
      );
      await browser.respond(1, {
        results: [
          { id: first, deleted: true },
          { id: second, deleted: false, error_code: "delete_database" },
        ],
      });
      assert.ok(
        selectionCheckboxes(browser).length === 1 &&
          selectionCheckboxes(browser)[0].checked,
        "Only failed analysis stays selected",
      );
      assert.ok(
        browser.elements.jobsNotice.textContent.includes(
          "Could not delete: 1.",
        ),
        "Partial success is explicit",
      );
      await browser.respond(2, { jobs: [job("failed", 0, second)] });
      await pending;
      assert.ok(
        selectionCheckboxes(browser)[0].checked &&
          !selectionCheckboxes(browser)[0].disabled,
        "Failed analysis remains retryable",
      );
    });
    test("network deletion errors preserve the card and selection", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [job("done")] });
      toggleSelection(browser);
      setSelected(selectionCheckboxes(browser)[0]);
      browser.api.confirmDeletion(["a".repeat(32)], false);
      const pending = browser.api.submitDeletion();
      browser.requests[1].reject(Error("Offline"));
      await pending;
      assert.ok(
        browser.article() &&
          selectionCheckboxes(browser)[0].checked &&
          !selectionCheckboxes(browser)[0].disabled,
        "Failed request keeps visible selection",
      );
      assert.equal(
        browser.elements.jobsNotice.textContent,
        "Offline",
        "Visible error",
      );
    });
    test("cleanup warnings are shown and old GETs cannot resurrect deleted cards", async () => {
      const browser = createBrowser();
      const id = "a".repeat(32);
      await browser.respond(0, { jobs: [job("done")] });
      browser.click();
      browser.api.confirmDeletion([id], false);
      const pending = browser.api.submitDeletion();
      await browser.respond(2, {
        id,
        deleted: true,
        warning_code: "delete_files_failed",
      });
      assert.ok(
        !browser.article() &&
          browser.elements.jobsNotice.textContent.includes(
            "Some associated files were retained.",
          ),
        "Durable removal and explicit cleanup warning",
      );
      await browser.respond(1, { jobs: [job("done")] });
      assert.ok(!browser.article(), "Ignore stale GET");
      await browser.respond(3, { jobs: [] });
      await pending;
    });
    test("report buttons follow all content and unavailable reports have no links", async () => {
      const browser = createBrowser();
      await browser.respond(0, {
        jobs: [{ ...job("done"), question: "Custom", question_custom: 1 }],
      });
      const article = browser.article();
      const footer = article.children[article.children.length - 1];
      assert.ok(
        footer.className === "actions report-actions" &&
          footer.children.length === 2,
        "Two report buttons at card bottom",
      );
      assert.ok(
        footer.children.every((n) => n.className.includes("report-button")),
        "Explicit button styling",
      );
      browser.click();
      await browser.respond(1, {
        jobs: [{ ...job("done"), report_available: false }],
      });
      assert.ok(
        !descendants(browser.article()).some((n) => n.tag === "a"),
        "No unavailable report links",
      );
      assert.ok(
        descendants(browser.article()).some(
          (n) => n.textContent === "Report unavailable",
        ),
        "Unavailable message",
      );
    });
  });

  describe("Configurable analysis resume", () => {
    const id = "a".repeat(32);
    const config = {
      fingerprint: "selected-revision",
      profile: "high",
      customized: true,
      chunk_tokens: 2048,
      analysis_tokens: 3072,
      merge_tokens: 4096,
      final_tokens: 5120,
      context_tokens: 16384,
      report_language: "fr",
      detail_level: "detailed",
      temperature: 0.3,
      thinking_section: "inherit",
    };
    const failed = (extra = {}) => ({
      ...job("failed"),
      configuration: { ...config },
      phase_code: "failed",
      error_code: "output_limit",
      ...extra,
    });
    const trigger = (browser, jobId = id) =>
      descendants(browser.elements.jobs).find(
        (node) => node.dataset.resumeJob === jobId,
      );
    const open = (browser) => trigger(browser).handlers.click[0]();
    const submit = (browser) =>
      browser.elements.resumeForm.handlers.submit[0]({ preventDefault() {} });
    const escape = (browser) => {
      let prevented = false;
      browser.elements.resumeDialog.handlers.cancel[0]({
        preventDefault() {
          prevented = true;
        },
      });
      assert.ok(prevented, "Cancel handling controls safe dismissal");
    };

    test("failed and cancelled cards open with effective saved values and safe focus", async () => {
      for (const status of ["failed", "cancelled"]) {
        const browser = createBrowser();
        const saved = failed({ status, filename: "<analysis>.pdf" });
        await browser.respond(0, { jobs: [saved] });
        open(browser);
        const e = browser.elements;
        assert.ok(e.resumeDialog.open);
        assert.equal(browser.document.activeElement, e.resumeCancel);
        assert.equal(trigger(browser).attributes["aria-haspopup"], "dialog");
        assert.equal(e.resumeDocument.textContent, "<analysis>.pdf");
        for (const name of [
          "analysis_tokens",
          "merge_tokens",
          "final_tokens",
        ]) {
          assert.equal(e["resume_" + name].value, String(config[name]));
          assert.equal(e["resume_" + name].min, String(config[name]));
        }
        assert.match(e.resumeConfig.textContent, /analysis 3072 · merge 4096/);
        assert.equal(browser.requests.length, 1, "Opening needs no preflight");
        if (status === "cancelled")
          assert.match(e.resumeFailure.textContent, /cancelled/);
        e.resumeCancel.handlers.click[0]();
        assert.ok(!e.resumeDialog.open);
        assert.equal(browser.document.activeElement, trigger(browser));
      }
    });

    test("only eligible cards offer resume and missing saved budgets retain retry", async () => {
      const browser = createBrowser();
      await browser.respond(0, {
        jobs: [job("done"), job("running", 50, "b".repeat(32))],
      });
      assert.equal(trigger(browser), undefined);
      browser.api.openResumeDialog(id);
      assert.ok(!browser.elements.resumeDialog.open);
      browser.api.refresh();
      await browser.respond(1, { jobs: [job("failed")] });
      open(browser);
      assert.ok(browser.elements.resumeAdjusted.disabled);
      assert.ok(!browser.elements.resumeRetry.disabled);
      assert.match(
        browser.elements.resumeConfig.textContent,
        /Saved budgets are unavailable/,
      );
      browser.elements.resumeRetry.handlers.click[0]();
      assert.equal(browser.requests[2].url, `/api/jobs/${id}/retry`);
    });

    test("native dialog and form expose labels, validation messages and keyboard controls", () => {
      assert.match(
        template,
        /<dialog\s+id="resumeDialog"\s+aria-labelledby="resumeTitle"\s+aria-describedby="resumeDocument resumeFailure"/,
      );
      for (const name of ["analysis_tokens", "merge_tokens", "final_tokens"]) {
        assert.match(template, new RegExp(`for="resume_${name}"`));
        assert.match(
          template,
          new RegExp(
            `id="resume_${name}"[\\s\\S]*?aria-describedby="resumeBudgetHelp resumeError"`,
          ),
        );
      }
      assert.match(template, /id="resumeAdjusted"\s+type="submit"/);
      assert.match(template, /id="resumeError"[^>]+role="alert"/);
      assert.match(template, /id="resumeStatus"[^>]+role="status"/);
      assert.match(template, /id="resumeCancel"[^>]+autofocus/);
    });

    test("Escape restores a refreshed trigger, repeated openings keep one listener", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [failed()] });
      const first = trigger(browser);
      open(browser);
      browser.api.refresh();
      await browser.respond(1, { jobs: [failed({ done_chunks: 2 })] });
      escape(browser);
      assert.ok(!first.isConnected);
      assert.equal(browser.document.activeElement, trigger(browser));
      for (let i = 0; i < 4; i++) {
        open(browser);
        escape(browser);
      }
      assert.equal(browser.elements.resumeDialog.handlers.cancel.length, 1);
      assert.equal(browser.elements.resumeForm.handlers.submit.length, 1);
      assert.equal(browser.requests.length, 2);
    });

    test("Retry ignores edited inputs and uses the existing bodyless endpoint", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [failed()] });
      open(browser);
      browser.elements.resume_merge_tokens.value = "invalid";
      browser.elements.resumeRetry.handlers.click[0]();
      const request = browser.requests[1];
      assert.equal(request.url, `/api/jobs/${id}/retry`);
      assert.equal(request.options.method, "POST");
      assert.equal(request.options.body, undefined);
      assert.ok(
        browser.elements.resumeDialog.open,
        "Wait for server acknowledgement",
      );
      await browser.respond(1, { status: "queued" });
      assert.equal(browser.requests[2].url, "/api/jobs");
      await browser.respond(2, { jobs: [{ ...failed(), status: "queued" }] });
      assert.ok(!browser.elements.resumeDialog.open);
      assert.equal(browser.polls().length, 1);
    });

    test("resume submits only changed allowed budgets, including multiple changes", async () => {
      for (const changes of [
        { analysis_tokens: 3500 },
        { merge_tokens: 4500 },
        { final_tokens: 6000 },
        { analysis_tokens: 3500, merge_tokens: 4500, final_tokens: 6000 },
      ]) {
        const browser = createBrowser();
        const other = failed({
          id: "b".repeat(32),
          configuration: { ...config, merge_tokens: 9000 },
        });
        const saved = failed();
        const before = JSON.stringify([saved, other]);
        await browser.respond(0, { jobs: [saved, other] });
        open(browser);
        for (const [name, value] of Object.entries(changes))
          browser.elements["resume_" + name].value = String(value);
        submit(browser);
        const request = browser.requests[1];
        assert.equal(request.url, `/api/jobs/${id}/resume`);
        assert.equal(request.options.method, "POST");
        assert.equal(
          request.options.headers["Content-Type"],
          "application/json",
        );
        assert.deepEqual(JSON.parse(request.options.body), changes);
        assert.equal(
          JSON.stringify([saved, other]),
          before,
          "Saved configurations stay immutable",
        );
        await browser.respond(1, {
          status: "queued",
          configuration: { ...config, ...changes },
        });
        await browser.respond(2, {
          jobs: [
            {
              ...saved,
              status: "queued",
              configuration: { ...config, ...changes },
            },
            other,
          ],
        });
        assert.ok(!browser.elements.resumeDialog.open);
        assert.equal(browser.requests.length, 3);
      }
    });

    test("saved settings and the new-analysis form never enter the resume payload", async () => {
      const storage = new Map([["document-processor-profile", "low"]]);
      const browser = createBrowser({ withSettings: true, storage });
      await browser.respond(0, { jobs: [failed()] });
      open(browser);
      browser.elements.profile.value = "ultra";
      browser.elements.analysis_tokens.value = "10000";
      browser.elements.temperature.value = "1.2";
      browser.elements.resume_merge_tokens.value = "4500";
      submit(browser);
      assert.deepEqual(JSON.parse(browser.requests[2].options.body), {
        merge_tokens: 4500,
      });
      assert.equal(browser.elements.analysis_tokens.value, "10000");
      assert.equal(storage.get("document-processor-profile"), "low");
      assert.match(browser.elements.resumeConfig.textContent, /analysis 3072/);
    });

    test("invalid values, decreases and unchanged budgets never make requests", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [failed()] });
      open(browser);
      submit(browser);
      assert.match(
        browser.elements.resumeError.textContent,
        /Increase at least one/,
      );
      for (const name of ["analysis_tokens", "merge_tokens", "final_tokens"]) {
        const input = browser.elements["resume_" + name];
        for (const value of [
          "",
          "0",
          "-2",
          "1.5",
          "1e4",
          "0x2000",
          "NaN",
          "Infinity",
          "9007199254740992",
          String(config[name] - 1),
        ]) {
          input.value = value;
          submit(browser);
          assert.equal(browser.requests.length, 1, `${name}: ${value}`);
          assert.equal(input.attributes["aria-invalid"], "true");
          assert.equal(browser.document.activeElement, input);
          assert.ok(!browser.elements.resumeError.hidden);
        }
        input.value = String(config[name]);
      }
      browser.elements.resume_merge_tokens.value = "4500";
      submit(browser);
      assert.equal(browser.requests.length, 2, "Corrected inputs can submit");
      assert.ok(browser.elements.resumeError.hidden);
    });

    test("duplicate submits and conflicting actions are disabled until confirmation and refresh", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [failed()] });
      open(browser);
      browser.elements.resume_merge_tokens.value = "4500";
      submit(browser);
      submit(browser);
      browser.elements.resumeRetry.handlers.click[0]();
      browser.api.retry(id);
      escape(browser);
      const e = browser.elements;
      for (const control of [
        e.resumeRetry,
        e.resumeAdjusted,
        e.resumeCancel,
        e.resume_analysis_tokens,
        e.resume_merge_tokens,
        e.resume_final_tokens,
        trigger(browser),
      ])
        assert.ok(control.disabled);
      assert.equal(e.resumeDialog.attributes["aria-busy"], "true");
      assert.equal(e.resumeStatus.textContent, "Submitting…");
      assert.ok(e.resumeDialog.open);
      assert.equal(browser.requests.length, 2);
      await browser.respond(1, { status: "queued" });
      submit(browser);
      assert.ok(e.resumeDialog.open && e.resumeRetry.disabled);
      assert.equal(browser.requests.length, 3);
      await browser.respond(2, { jobs: [{ ...failed(), status: "queued" }] });
      assert.ok(!e.resumeDialog.open);
    });

    test("server validation uses translated codes or plain safe text and preserves edits", async () => {
      for (const data of [
        { error_code: "context_insufficient", error: "server detail" },
        { error_code: "invalid_budget", error: "Invalid merge_tokens budget." },
        {
          error_code: "unknown_validation",
          error: "<img src=x onerror=alert(1)>",
        },
      ]) {
        const browser = createBrowser();
        await browser.respond(0, { jobs: [failed()] });
        open(browser);
        browser.elements.resume_merge_tokens.value = "4500";
        submit(browser);
        await browser.respond(1, data, false, 400);
        const e = browser.elements;
        assert.equal(
          e.resumeError.textContent,
          translations.en["error." + data.error_code] || data.error,
        );
        assert.equal(e.resumeError.children.length, 0);
        assert.equal(e.resume_merge_tokens.value, "4500");
        assert.ok(
          e.resumeDialog.open &&
            !e.resumeRetry.disabled &&
            !e.resumeAdjusted.disabled,
        );
        assert.equal(
          browser.requests.length,
          2,
          "No validation preflight or redundant refresh",
        );
      }
    });

    test("404 and 409 keep backend errors visible, refresh stale state and block resubmission", async () => {
      for (const [status, data] of [
        [404, { error_code: "not_found", error: "Analysis not found." }],
        [409, { error_code: "resume_conflict", error: "Conflict" }],
        [409, { error_code: "retry_limit", error: "Limit" }],
      ]) {
        const browser = createBrowser();
        await browser.respond(0, { jobs: [failed()] });
        open(browser);
        browser.elements.resume_merge_tokens.value = "4500";
        submit(browser);
        await browser.respond(1, data, false, status);
        assert.equal(browser.requests[2].url, "/api/jobs");
        await browser.respond(2, { jobs: status === 404 ? [] : [failed()] });
        const e = browser.elements;
        assert.equal(
          e.resumeError.textContent,
          translations.en["error." + data.error_code] || data.error,
        );
        assert.ok(
          e.resumeDialog.open &&
            e.resumeRetry.disabled &&
            e.resumeAdjusted.disabled,
        );
        assert.ok(!e.resumeStale.hidden && !e.resumeCancel.disabled);
        submit(browser);
        assert.equal(browser.requests.length, 3);
        escape(browser);
        if (status === 404)
          assert.equal(browser.document.activeElement.id, "toggleSelection");
      }
    });

    test("network errors and timeouts unlock the dialog without success or lost edits", async () => {
      for (const timeout of [false, true]) {
        const browser = createBrowser();
        await browser.respond(0, { jobs: [failed()] });
        open(browser);
        browser.elements.resume_merge_tokens.value = "4500";
        submit(browser);
        if (timeout) await browser.tick(15000);
        else await browser.reject(1);
        const e = browser.elements;
        assert.equal(
          e.resumeError.textContent,
          timeout ? "Request timed out." : "Offline",
        );
        assert.ok(e.resumeDialog.open && !e.resumeAdjusted.disabled);
        assert.equal(e.resume_merge_tokens.value, "4500");
        assert.equal(browser.requests.length, 2);
        escape(browser);
        assert.equal(browser.document.activeElement, trigger(browser));
      }
    });

    test("polling preserves open dialog values, focus and selected history", async () => {
      const browser = createBrowser();
      const running = job("running", 20, "b".repeat(32));
      await browser.respond(0, { jobs: [failed(), running] });
      toggleSelection(browser);
      setSelected(selectionCheckboxes(browser)[0]);
      open(browser);
      const input = browser.elements.resume_merge_tokens;
      input.value = "4500";
      input.focus();
      await browser.tick(6000);
      await browser.respond(1, {
        jobs: [failed(), { ...running, progress_percent: 40 }],
      });
      assert.equal(input.value, "4500");
      assert.equal(browser.document.activeElement, input);
      assert.ok(
        browser.elements.resumeDialog.open &&
          !browser.elements.resumeAdjusted.disabled,
      );
      assert.ok(selectionCheckboxes(browser)[0].checked);
      escape(browser);
      assert.equal(browser.document.activeElement, trigger(browser));
      browser.api.cancelJob(running.id);
      assert.equal(browser.requests[2].url, `/api/jobs/${running.id}/cancel`);
    });

    test("external status, queue or configuration changes require reopening without resetting edits", async () => {
      for (const changed of [
        { ...failed(), status: "running" },
        failed({
          configuration: {
            ...config,
            fingerprint: "new-revision",
            merge_tokens: 8000,
          },
        }),
        failed({ queued_at: "2026-11-20T12:00:00Z" }),
        null,
      ]) {
        const browser = createBrowser();
        await browser.respond(0, { jobs: [failed()] });
        open(browser);
        browser.elements.resume_merge_tokens.value = "4500";
        browser.api.refresh();
        await browser.respond(1, { jobs: changed ? [changed] : [] });
        assert.ok(
          browser.elements.resumeAdjusted.disabled &&
            browser.elements.resumeRetry.disabled,
        );
        assert.ok(!browser.elements.resumeStale.hidden);
        assert.equal(browser.elements.resume_merge_tokens.value, "4500");
        submit(browser);
        assert.equal(browser.requests.length, 2);
        escape(browser);
        if (changed?.configuration.merge_tokens === 8000) {
          open(browser);
          assert.equal(browser.elements.resume_merge_tokens.value, "8000");
          assert.ok(!browser.elements.resumeAdjusted.disabled);
        }
      }
    });

    test("successful resume rejects an older list response and survives a failed refresh", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [failed()] });
      open(browser);
      browser.api.refresh();
      browser.elements.resume_merge_tokens.value = "4500";
      submit(browser);
      await browser.respond(2, { status: "queued" });
      assert.equal(browser.requests.length, 3, "Reuse pending list request");
      await browser.respond(1, { jobs: [failed()] });
      assert.equal(
        browser.requests[3].url,
        "/api/jobs",
        "Older response requires a fresh read",
      );
      await browser.reject(3);
      assert.ok(!browser.elements.resumeDialog.open);
      assert.ok(!browser.elements.jobsError.hidden);
      assert.equal(
        browser.polls().length,
        1,
        "Known queued work restarts polling",
      );
    });

    test("resume invalidates previous-attempt live output immediately after acknowledgement", async () => {
      const browser = createBrowser();
      await browser.respond(0, { jobs: [failed()] });
      browser.api.toggleLive(id);
      open(browser);
      browser.elements.resume_merge_tokens.value = "4500";
      submit(browser);
      await browser.respond(2, { status: "queued" });
      await browser.respond(1, { content: "Old attempt", active: false });
      assert.equal(
        browser.document.getElementById("live-content").textContent,
        "Waiting for streamed output…",
      );
      assert.equal(browser.requests.length, 4);
      await browser.respond(3, {
        jobs: [
          { ...failed(), status: "queued", queued_at: "2026-11-20T12:00:00Z" },
        ],
      });
      assert.equal(browser.requests.length, 5);
      await browser.respond(4, { content: "New attempt", active: true });
      assert.equal(
        browser.document.getElementById("live-content").textContent,
        "New attempt",
      );
    });

    test("verified failed-attempt phases select the correct translated phase and budget", async () => {
      for (const language of ["en", "fr"]) {
        for (const [phase, budget] of [
          ["section", "analysis_tokens"],
          ["merge", "merge_tokens"],
          ["final", "final_tokens"],
        ]) {
          const browser = createBrowser({
            storage: new Map([["document-processor-language", language]]),
          });
          await browser.respond(0, {
            jobs: [
              failed({
                phase_args: {
                  failed_phase: {
                    attempt_id: 2,
                    code: phase,
                    error_code: "output_limit",
                  },
                },
              }),
            ],
          });
          open(browser);
          assert.ok(
            browser.elements.resumePhase.textContent.includes(
              translations[language]["resume.phase." + phase],
            ),
          );
          assert.ok(
            browser.elements.resumeGuidance.textContent.includes(budget),
          );
          assert.ok(!browser.elements.resumePhase.textContent.includes("{"));
          assert.equal(
            browser.requests.length,
            1,
            "No extra history or preview request",
          );
        }
      }
    });

    test("missing, incomplete and inconsistent failure metadata keeps generic guidance", async () => {
      for (const failure of [
        undefined,
        null,
        {},
        { code: "merge", error_code: "output_limit" },
        { attempt_id: 0, code: "merge", error_code: "output_limit" },
        { attempt_id: 2, code: "merge" },
        { attempt_id: 2, code: "merge", error_code: "deadline" },
        { attempt_id: 2, code: "unknown", error_code: "output_limit" },
      ]) {
        const browser = createBrowser();
        await browser.respond(0, {
          jobs: [failed({ phase_args: { failed_phase: failure } })],
        });
        open(browser);
        assert.match(
          browser.elements.resumePhase.textContent,
          /phase is unavailable/,
        );
        assert.match(
          browser.elements.resumeGuidance.textContent,
          /affected phase/,
        );
        assert.ok(
          !browser.elements.resumeGuidance.textContent.includes("merge_tokens"),
        );
        assert.ok(!browser.elements.resumeRetry.disabled);
      }
    });

    test("unknown failure types never receive token advice even with a known failed stage", async () => {
      const browser = createBrowser();
      await browser.respond(0, {
        jobs: [
          failed({
            error_code: "future_error",
            phase_args: {
              failed_phase: {
                attempt_id: 2,
                code: "final",
                error_code: "future_error",
              },
            },
          }),
        ],
      });
      open(browser);
      assert.match(browser.elements.resumePhase.textContent, /Final report/);
      assert.equal(browser.elements.resumeGuidance.textContent, "");
    });

    test("a newer failed attempt in the same second makes an open dialog stale", async () => {
      const browser = createBrowser();
      const withAttempt = (attempt) =>
        failed({
          phase_args: {
            failed_phase: {
              attempt_id: attempt,
              code: "merge",
              error_code: "output_limit",
            },
          },
        });
      await browser.respond(0, { jobs: [withAttempt(1)] });
      open(browser);
      browser.elements.resume_merge_tokens.value = "4500";
      browser.api.refresh();
      await browser.respond(1, { jobs: [withAttempt(2)] });
      assert.ok(browser.elements.resumeAdjusted.disabled);
      assert.ok(!browser.elements.resumeStale.hidden);
      assert.equal(browser.elements.resume_merge_tokens.value, "4500");
      submit(browser);
      assert.equal(browser.requests.length, 2);
      escape(browser);
      open(browser);
      assert.ok(!browser.elements.resumeAdjusted.disabled);
    });

    test("failure guidance distinguishes output limits, context limits and other errors", async () => {
      for (const [code, phase, expected, budget] of [
        [
          "output_limit",
          "section",
          /does not enlarge the context window/,
          "analysis_tokens",
        ],
        [
          "output_limit",
          "merge",
          /does not enlarge the context window/,
          "merge_tokens",
        ],
        [
          "output_limit",
          "final",
          /does not enlarge the context window/,
          "final_tokens",
        ],
        ["output_limit", "failed", /affected phase/, null],
        ["context_insufficient", "failed", /context-window limitation/, null],
        ["merge_impossible", "failed", /context-window limitation/, null],
        ["empty_output", "failed", /^$/, null],
        ["analysis_failed", "failed", /^$/, null],
      ]) {
        const browser = createBrowser();
        await browser.respond(0, {
          jobs: [
            failed({
              error_code: code,
              phase_code: phase,
              phase_args: { level: 1, index: 1, total: 2, pages: "1–2" },
            }),
          ],
        });
        open(browser);
        assert.match(browser.elements.resumeGuidance.textContent, expected);
        if (budget) {
          assert.ok(
            browser.elements.resumeGuidance.textContent.includes(budget),
          );
          assert.match(
            browser.elements.resumePhase.textContent,
            /Last processing phase:/,
          );
        } else
          assert.match(
            browser.elements.resumePhase.textContent,
            /phase is unavailable/,
          );
      }
    });

    test("French and English translations cover dialog text and survive switching with edits", async () => {
      const browser = createBrowser({ withSettings: true });
      await browser.respond(0, { jobs: [failed()] });
      open(browser);
      browser.elements.resume_merge_tokens.value = "1";
      submit(browser);
      for (const language of ["fr", "en"]) {
        browser.elements.uiLanguage.value = language;
        browser.elements.uiLanguage.handlers.change[0]();
        assert.equal(
          trigger(browser).textContent,
          translations[language]["Resume…"],
        );
        assert.equal(
          browser.elements.resumeFailure.textContent,
          translations[language]["error.output_limit"],
        );
        assert.ok(
          browser.elements.resumeError.textContent.includes(
            translations[language]["Merge output tokens"],
          ),
        );
        assert.equal(browser.elements.resume_merge_tokens.value, "1");
        assert.equal(
          browser.document.activeElement,
          browser.elements.resume_merge_tokens,
        );
      }
      const dialogTemplate = template.slice(
        template.indexOf('id="resumeDialog"'),
        template.indexOf('id="deleteDialog"'),
      );
      for (const [, key] of dialogTemplate.matchAll(/data-i18n="([^"]+)"/g)) {
        assert.ok(translations.en[key] && translations.fr[key], key);
      }
      for (const key of Object.keys(translations.en))
        assert.ok(translations.fr[key], key);
      browser.elements.theme.value = "dark";
      browser.elements.theme.handlers.change[0]();
      assert.equal(browser.elements.resume_merge_tokens.value, "1");
      assert.ok(browser.elements.resumeDialog.open);
    });

    test("a late acknowledgement from an old page cannot change a new dialog", async () => {
      const old = createBrowser();
      await old.respond(0, { jobs: [failed()] });
      open(old);
      old.elements.resume_merge_tokens.value = "4500";
      submit(old);
      const current = createBrowser();
      await current.respond(0, { jobs: [failed()] });
      open(current);
      await old.respond(1, { status: "queued" });
      assert.ok(
        current.elements.resumeDialog.open &&
          !current.elements.resumeRetry.disabled,
      );
      assert.equal(current.requests.length, 1);
    });
  });

  describe("Settings and translations", () => {
    const resolved = {
      profile: "low",
      chunk_tokens: 3000,
      analysis_tokens: 3072,
      merge_tokens: 2048,
      final_tokens: 3072,
      context_tokens: 16384,
      warnings: [],
      thinking_section: "inherit",
      thinking_merge: "inherit",
      thinking_final: "inherit",
    };
    async function settingsReady() {
      const browser = createBrowser({ withSettings: true });
      await browser.respond(0, { jobs: [] });
      await browser.respond(1, { thinking_supported: false });
      await browser.respond(2, { configuration: resolved });
      return browser;
    }
    test("history selection stays translated and selected across language and theme changes", async () => {
      const browser = await settingsReady();
      browser.click();
      await browser.respond(3, { jobs: [job("done")] });
      browser.elements.uiLanguage.value = "fr";
      browser.elements.uiLanguage.handlers.change[0]();
      assert.equal(
        browser.document.getElementById("toggleSelection").textContent,
        "Sélectionner",
        "French select label",
      );
      assert.equal(
        descendants(browser.elements.jobs).find((n) => n.dataset.deleteJob)
          .title,
        "Supprimer cette analyse",
        "French trash tooltip",
      );
      toggleSelection(browser);
      setSelected(selectionCheckboxes(browser)[0]);
      assert.equal(
        browser.document.getElementById("toggleSelection").textContent,
        "Annuler",
        "French cancel label",
      );
      assert.equal(
        browser.document.getElementById("deleteSelection").textContent,
        "Supprimer (1)",
        "French counted delete label",
      );
      browser.elements.theme.value = "dark";
      browser.elements.theme.handlers.change[0]();
      assert.ok(
        selectionCheckboxes(browser)[0].checked &&
          !selectionCheckboxes(browser)[0].hidden,
        "Dark theme preserves selection",
      );
      browser.elements.uiLanguage.value = "en";
      browser.elements.uiLanguage.handlers.change[0]();
      assert.ok(
        browser.document.getElementById("toggleSelection").textContent ===
          "Cancel" &&
          browser.document.getElementById("deleteSelection").textContent ===
            "Delete (1)",
        "English labels preserve selection",
      );
      toggleSelection(browser);
      assert.ok(
        selectionCheckboxes(browser)[0].hidden &&
          !selectionCheckboxes(browser)[0].checked,
        "Cancel resets after language changes",
      );
    });
    test("profile preview is server-resolved and keeps advanced reasoning disabled when unsupported", async () => {
      const browser = await settingsReady();
      assert.ok(
        !browser.elements.send.disabled,
        "Validated preview enables submission",
      );
      assert.ok(
        browser.elements.thinking_section.disabled,
        "Unsupported reasoning must be disabled",
      );
      browser.elements.analysis_tokens.value = "1200";
      browser.elements.reportLanguage.value = "fr";
      const pending = browser.upload();
      const request = browser.requests[3];
      const options = JSON.parse(request.options.body.get("options"));
      assert.ok(
        options.profile === "low" &&
          options.overrides.analysis_tokens === 1200 &&
          options.report_language === "fr",
        "Only explicit controls are submitted",
      );
      assert.ok(
        !("system_prompt" in options) && !("snapshot" in options),
        "Private/resolved snapshot must not be client-authoritative",
      );
      await browser.respond(3, { id: "b".repeat(32) });
      await browser.respond(4, { jobs: [job("queued", 0, "b".repeat(32))] });
      await pending;
    });
    test("invalid configuration keeps submission disabled and late previews are ignored", async () => {
      const browser = await settingsReady();
      const first = browser.api.previewConfig();
      browser.elements.analysis_tokens.value = "-1";
      browser.api.schedulePreview();
      await browser.respond(3, { configuration: resolved });
      await first;
      assert.ok(
        browser.elements.send.disabled,
        "A stale valid response must not enable changed settings",
      );
      await browser.tick(250);
      await browser.respond(
        4,
        { error_code: "invalid_budget", error: "Invalid budget" },
        false,
      );
      assert.ok(
        browser.elements.send.disabled,
        "Rejected budgets must disable submission",
      );
    });
    test("language and theme changes preserve the open Live text and scroll position", async () => {
      const browser = await settingsReady();
      browser.click();
      await browser.respond(3, { jobs: [job()] });
      browser.api.toggleLive("a".repeat(32));
      const text =
        "L’assemblée générale a décidé de vérifier la présence des élèves à Grenoble. Résumé : température −2,5 °C, échantillon n° 3, précision ±0,2 %. English œ «quotes» µm ∑ 中文 العربية 😀";
      // Parse the JSON wire representation before passing it to the browser logic.
      await browser.respond(
        4,
        JSON.parse(JSON.stringify({ content: text, active: true })),
      );
      const panel = browser.document.getElementById("live-" + "a".repeat(32));
      const output = browser.document.getElementById("live-content");
      output.scrollTop = 17;
      assert.equal(
        output.textContent,
        text,
        "JSON preview must retain every Unicode character",
      );
      browser.elements.uiLanguage.value = "fr";
      browser.elements.uiLanguage.handlers.change[0]();
      assert.equal(
        browser.document.documentElement.lang,
        "fr",
        "HTML language must update",
      );
      assert.ok(
        browser.document.getElementById("live-" + "a".repeat(32)) === panel &&
          output.textContent === text &&
          output.scrollTop === 17,
        "Language change must preserve live content",
      );
      browser.elements.theme.value = "dark";
      browser.elements.theme.handlers.change[0]();
      assert.equal(
        browser.document.documentElement.dataset.theme,
        "dark",
        "Theme must update",
      );
      assert.equal(
        output.textContent,
        text,
        "Theme must not alter model output",
      );
    });
  });
});
