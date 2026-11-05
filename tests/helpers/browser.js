// Browser-only dependencies are simulated; application modules remain real ES modules.
import assert from "node:assert/strict";
import { setImmediate } from "node:timers";
import { start, stop } from "../../static/js/app.js";
import * as jobs from "../../static/js/jobs.js";
import * as live from "../../static/js/live.js";
import * as settings from "../../static/js/settings.js";
import * as history from "../../static/js/history.js";
import { resetState } from "../../static/js/state.js";

// Drain promise continuations without advancing simulated timers or fetching anything.
export const flush = () => new Promise((resolve) => setImmediate(resolve));
export const job = (
  status = "running",
  progress = 52,
  id = "a".repeat(32),
) => ({
  id,
  status,
  progress_percent: progress,
  filename: "example.pdf",
  created_at: "2026-10-31T12:00:00Z",
});
export const descendants = (node) =>
  node.children.flatMap((child) => [child, ...descendants(child)]);

function createClock() {
  const timers = new Map();
  let timerId = 0;
  let now = 0;
  const pending = (delay) =>
    [...timers.values()].filter((timer) => timer.delay === delay);
  const schedule = (fn, delay, interval) => {
    timers.set(++timerId, { fn, delay, interval, due: now + delay });
    return timerId;
  };
  async function run(id, timer) {
    now = Math.max(now, timer.due);
    if (timer.interval) timer.due = now + timer.delay;
    else timers.delete(id);
    timer.fn();
    await flush();
  }
  return {
    get now() {
      return now;
    },
    schedule,
    cancel: (id) => timers.delete(id),
    clear: () => timers.clear(),
    pending,
    // Fire one selected timeout, useful for races where other requests stay pending.
    async fire(delay) {
      const matches = [...timers.entries()].filter(
        ([, timer]) => timer.delay === delay,
      );
      assert.equal(matches.length, 1, `Expected one timer at ${delay}ms`);
      await run(...matches[0]);
    },
    // Advance every timer in due-time order; no wall-clock delay is involved.
    async advance(milliseconds) {
      const target = now + milliseconds;
      while (true) {
        const next = [...timers.entries()]
          .filter(([, timer]) => timer.due <= target)
          .sort((a, b) => a[1].due - b[1].due)[0];
        if (!next) break;
        await run(...next);
      }
      now = target;
    },
  };
}

// A test owns every fake browser it creates. Restore globals even after assertion failures.
export function createBrowserHarness(context, { template, translations }) {
  const globalNames = [
    "document",
    "localStorage",
    "matchMedia",
    "fetch",
    "setTimeout",
    "clearTimeout",
    "setInterval",
    "clearInterval",
    "FormData",
    "alert",
  ];
  const originals = new Map(
    globalNames.map((name) => [
      name,
      Object.getOwnPropertyDescriptor(globalThis, name),
    ]),
  );
  const inlineScripts = [
    ...template.matchAll(/<script>([\s\S]*?)<\/script>/g),
  ].map((match) => match[1]);
  const environments = [];
  context.after(async () => {
    try {
      await flush();
      if (environments.length) {
        stop();
        resetState();
      }
    } finally {
      for (const { clock } of environments) clock.clear();
      for (const [name, descriptor] of originals) {
        if (descriptor) Object.defineProperty(globalThis, name, descriptor);
        else delete globalThis[name];
      }
    }
  });
  function createBrowser({
    withSettings = false,
    storage = new Map(),
    systemDark = false,
  } = {}) {
    // Reset the old page while its globals and timer registry are still installed.
    if (environments.length) {
      stop();
      resetState();
    }
    class Element {
      // Initialize a minimal DOM node and properties used by the scenarios.
      // Keep parent links so detached controls and hidden ancestors behave like DOM nodes.
      constructor(tag) {
        this.tag = tag;
        this.children = [];
        this.parentElement = null;
        this.handlers = {};
        this.style = {};
        this.dataset = {};
        this.attributes = {};
        this.scrollTop = 0;
        this.scrollHeight = 200;
        this.clientHeight = 100;
        this.classList = { add() {}, remove() {}, toggle() {} };
        this.replacements = 0;
        this.value = "";
        this.options = [];
      }
      // Append child nodes to the current node.
      append(...children) {
        for (const child of children) {
          if (child.parentElement) {
            const siblings = child.parentElement.children;
            siblings.splice(siblings.indexOf(child), 1);
          }
          child.parentElement = this;
          this.children.push(child);
        }
      }
      // Replace children and count simulated DOM rebuilds.
      replaceChildren(...children) {
        for (const child of this.children) child.parentElement = null;
        this.children = [];
        this.append(...children);
        this.replacements++;
      }
      // Assign plain text and remove children, as the DOM does.
      set textContent(value) {
        this.text = value;
        for (const child of this.children) child.parentElement = null;
        this.children = [];
      }
      // Read assigned text, or an empty string when none exists.
      get textContent() {
        return this.text || "";
      }
      // Store an attribute after converting its value to text.
      setAttribute(key, value) {
        this.attributes[key] = String(value);
      }
      // Store a handler so the event can be triggered manually.
      addEventListener(type, fn) {
        (this.handlers[type] ??= []).push(fn);
      }
      // Recursively find Live-channel buttons among the children.
      querySelectorAll(selector) {
        const key = selector
          .slice(6, -1)
          .replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
        return this.children.flatMap((c) => [
          ...(c.dataset[key] !== undefined ? [c] : []),
          ...c.querySelectorAll(selector),
        ]);
      }
      get isConnected() {
        for (let node = this; node; node = node.parentElement)
          if (
            node === document.documentElement ||
            Object.values(elements).includes(node)
          )
            return true;
        return false;
      }
      closest(selector) {
        if (selector !== "[hidden]")
          throw Error("Unsupported ancestor selector " + selector);
        for (let node = this; node; node = node.parentElement)
          if (node.hidden) return node;
        return null;
      }
      focus() {
        if (this.isConnected && !this.disabled && !this.closest("[hidden]"))
          document.activeElement = this;
      }
      showModal() {
        this.open = true;
      }
      close() {
        this.open = false;
      }
      // Accept form reset without additional simulation.
      reset() {}
    }
    const elements = Object.fromEntries(
      [
        "file",
        "drop",
        "feedback",
        "theme",
        "uploadForm",
        "fileLabel",
        "send",
        "refresh",
        "jobs",
        "jobsError",
        "jobsNotice",
        "deleteDialog",
        "deleteTitle",
        "deleteMessage",
        "deleteDocument",
        "deleteCancel",
        "deleteConfirm",
      ].map((id) => [id, new Element("div")]),
    );
    if (withSettings) {
      for (const id of [
        "translations",
        "uiLanguage",
        "languageFlag",
        "profile",
        "reportLanguage",
        "detailLevel",
        "profileHelp",
        "configPreview",
        "configWarnings",
        "diagnostics",
        "diagnosticOutput",
        "chunk_tokens",
        "analysis_tokens",
        "merge_tokens",
        "final_tokens",
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "step_timeout_seconds",
        "max_calls",
        "thinking_section",
        "thinking_merge",
        "thinking_final",
      ])
        elements[id] = new Element("div");
      elements.translations.textContent = JSON.stringify(translations);
      elements.profile.value = "low";
      elements.reportLanguage.value = "auto";
      elements.detailLevel.value = "standard";
      for (const name of ["profile", "reportLanguage", "detailLevel"])
        elements[name].options = [{ value: elements[name].value }];
      for (const stage of ["section", "merge", "final"])
        elements["thinking_" + stage].value = "inherit";
    }
    elements.file.files = [{ name: "example.pdf" }];
    // Find a node connected to the tree by its ID.
    const find = (node, id) =>
      node.id === id
        ? node
        : node.children.map((c) => find(c, id)).find(Boolean);
    const events = {};
    // Expose DOM operations used by the template and store its events.
    const document = {
      hidden: false,
      documentElement: new Element("html"),
      createElement: (tag) => new Element(tag),
      createElementNS: (namespace, tag) => new Element(tag),
      getElementById: (id) => elements[id] || find(elements.jobs, id) || null,
      addEventListener(type, fn) {
        events[type] = fn;
      },
      removeEventListener(type, fn) {
        if (events[type] === fn) delete events[type];
      },
    };
    const clock = createClock();
    const requests = [];
    const systemTheme = {
      matches: systemDark,
      handlers: [],
      addEventListener(type, fn) {
        this.handlers.push(fn);
      },
    };
    // Keep each request pending until the scenario resolves it.
    const fetch = (url, options) =>
      new Promise((resolve, reject) =>
        requests.push({ url, options, resolve, reject }),
      );
    // Install the simulated browser, run the page's inline scripts, then start
    // the interface as main.js does. start() resets the state shared by the modules.
    environments.push({ clock });
    Object.assign(globalThis, {
      document,
      localStorage: {
        getItem(key) {
          return storage.has(String(key))
            ? String(storage.get(String(key)))
            : null;
        },
        setItem(key, value) {
          storage.set(String(key), String(value));
        },
        removeItem(key) {
          storage.delete(String(key));
        },
        clear() {
          storage.clear();
        },
      },
      matchMedia: () => systemTheme,
      fetch,
      setTimeout: (fn, delay) => clock.schedule(fn, delay, false),
      clearTimeout: clock.cancel,
      setInterval: (fn, delay) => clock.schedule(fn, delay, true),
      clearInterval: clock.cancel,
      FormData: class {
        constructor() {
          this.values = new Map();
        }
        set(k, v) {
          this.values.set(k, v);
        }
        get(k) {
          return this.values.get(k);
        }
      },
      // The interface never blocks with alert(): a call fails the scenario.
      alert: (message) => {
        throw Error(message);
      },
    });
    for (const code of inlineScripts) new Function(code)();
    const earlyTheme = document.documentElement.dataset.theme;
    start();
    const api = {
      start,
      stop,
      resetState,
      refresh: jobs.refresh,
      toggleLive: jobs.toggleLive,
      loadLive: live.loadLive,
      retry: jobs.retry,
      cancelJob: jobs.cancelJob,
      selectedOptions: settings.selectedOptions,
      previewConfig: settings.previewConfig,
      schedulePreview: settings.schedulePreview,
      confirmDeletion: history.confirmDeletion,
      submitDeletion: jobs.submitDeletion,
      closeDeleteDialog: history.closeDeleteDialog,
    };
    // Resolve request index with payload, then run its asynchronous continuations.
    const respond = async (index, payload, ok = true) => {
      assert.ok(requests[index], "Missing request " + index);
      requests[index].resolve({
        ok,
        status: ok ? 200 : 503,
        json: async () => payload,
      });
      await flush();
    };
    // List only six-second timers dedicated to the jobs list.
    const polls = () => clock.pending(6000);
    const livePolls = () => clock.pending(1700);
    // Trigger the single timer for the requested delay; keep interval timers.
    const tick = clock.fire;
    // Simulate a visibility change and run the page handler.
    const visible = async (value) => {
      document.hidden = !value;
      events.visibilitychange();
      await flush();
    };
    // Simulate a click on the manual refresh button.
    const click = () => elements.refresh.handlers.click[0]({ type: "click" });
    // Simulate form submission and return its processing promise.
    const upload = () =>
      elements.uploadForm.handlers.submit[0]({
        preventDefault() {},
        target: elements.uploadForm,
      });
    const reject = async (index, error = new Error("Offline")) => {
      assert.ok(requests[index], "Missing request " + index);
      requests[index].reject(error);
      await flush();
    };
    return {
      api,
      elements,
      document,
      requests,
      respond,
      reject,
      clock,
      polls,
      livePolls,
      tick,
      visible,
      click,
      upload,
      systemTheme,
      earlyTheme,
      article: () =>
        elements.jobs.children
          .flatMap((section) =>
            section.children.flatMap((node) => node.children),
          )
          .find((node) => node.tag === "article"),
    };
  }
  return createBrowser;
}
