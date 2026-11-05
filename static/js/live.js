// Live panel: the latest model output of the job whose panel is open.
// Poll only an open active analysis, 1.7 seconds after the previous request settles.
import { $, el } from "./dom.js";
import { state, ACTIVE_STATUSES } from "./state.js";
import { t, phaseLabel } from "./i18n.js";
import { jsonFetch } from "./api.js";

const TAB_LABELS = {
  content: "Generated text",
  reasoning: "Reasoning (if available)",
};
const LIVE_REFRESH_MS = 1700;

export function stopLivePolling() {
  if (state.liveTimer !== null) clearTimeout(state.liveTimer);
  state.liveTimer = null;
}

function liveJobStatus() {
  return state.jobs.find((job) => job.id === state.expandedId)?.status;
}

function canPollLive() {
  return state.uiActive && !document.hidden && state.expandedId &&
    $("live-content") && ACTIVE_STATUSES.has(liveJobStatus());
}

function scheduleLivePolling() {
  if (!canPollLive() || state.liveBusy || state.liveTimer !== null) return;
  state.liveTimer = setTimeout(() => {
    state.liveTimer = null;
    loadLive();
  }, LIVE_REFRESH_MS);
}

// A new panel or job status needs one fresh preview, including the final one.
// Unrelated list rebuilds keep the current timer and leave historical previews idle.
export function syncLivePolling(resume = false) {
  const output = $("live-content"), status = liveJobStatus();
  const changed = output !== state.liveOutput || status !== state.liveJobStatus;
  if (!canPollLive()) stopLivePolling();
  if (!state.uiActive || document.hidden) return;
  state.liveOutput = output;
  state.liveJobStatus = status;
  if (!output) return;
  if (changed || (resume && canPollLive())) loadLive();
  else scheduleLivePolling();
}

// Build the live panel of job id; loadLive() fills it afterwards.
export function createLive(id) {
  const root = el("div", undefined, "live");
  root.id = "live-" + id;
  const header = el("div", undefined, "live-head"),
    tabs = el("div", undefined, "tabs");
  for (const tab of ["content", "reasoning"]) {
    const button = el(
      "button",
      t(TAB_LABELS[tab]),
      "button-secondary" + (state.liveTab === tab ? " button-active" : ""),
    );
    button.type = "button";
    button.dataset.tab = tab;
    button.addEventListener("click", () => {
      state.liveTab = tab;
      root
        .querySelectorAll("[data-tab]")
        .forEach((other) =>
          other.classList.toggle("button-active", other.dataset.tab === tab),
        );
      loadLive();
    });
    tabs.append(button);
  }
  header.append(tabs);
  const passLabel = el("span", t("Latest model pass"), "micro muted");
  passLabel.dataset.i18n = "Latest model pass";
  header.append(passLabel);
  root.append(header);
  const output = el("div", t("Waiting for streamed output…"), "live-output");
  output.id = "live-content";
  output.setAttribute("role", "log");
  root.append(output);
  const note = el(
    "p",
    t(
      "The panel displays the current or most recent model pass. Model reasoning is shown only if emitted by the model.",
    ),
    "live-note",
  );
  note.id = "live-note";
  root.append(note);
  return root;
}

// Translate the tab labels of the open panel after a language change.
export function translateLive() {
  const live = state.expandedId ? $("live-" + state.expandedId) : null;
  if (live)
    for (const button of live.querySelectorAll("[data-tab]"))
      button.textContent = t(TAB_LABELS[button.dataset.tab]);
}

// Load the preview of the open panel. Requests never overlap and are not
// made while the browser tab is hidden. A response for a panel closed or
// replaced in the meantime is ignored. The text follows new output only when
// the reader is already at the bottom.
export async function loadLive() {
  if (!state.uiActive || document.hidden || state.liveBusy) return;
  stopLivePolling();
  const id = state.expandedId,
    output = $("live-content"),
    note = $("live-note");
  if (!id || !output || !note) return;
  const generation = state.uiGeneration, tab = state.liveTab, status = liveJobStatus();
  state.liveBusy = true;
  try {
    const res = await jsonFetch(
      `/api/jobs/${id}/live` +
        (tab === "reasoning" ? "?include_reasoning=true" : ""),
      { cache: "no-store" },
    );
    if (!res.ok) throw Error(`HTTP ${res.status}`);
    const d = await res.json();
    if (generation !== state.uiGeneration || !state.uiActive || document.hidden ||
        id !== state.expandedId || output !== $("live-content") || tab !== state.liveTab) return;
    // Render the requested channel; pending channel changes load after settlement.
    const reasoning = state.liveTab === "reasoning";
    const content = reasoning ? d.reasoning : d.content;
    const truncated =
      (reasoning ? d.reasoning_chars : d.content_chars) > (content || "").length;
    const label = reasoning
      ? t(
          "No reasoning text was emitted for this pass. The model may not support it, or the server may not expose it.",
        )
      : t("No generated text yet for this pass.");
    const nearBottom =
      output.scrollHeight - output.scrollTop - output.clientHeight < 45;
    if (output.textContent !== (content || label))
      output.textContent = content || label;
    if (nearBottom) output.scrollTop = output.scrollHeight;
    note.textContent =
      phaseLabel(d) +
      " · " +
      t(
        d.active
          ? d.metrics?.first_event_ms == null
            ? "Waiting for model output…"
            : "Generating…"
          : "Last available output",
      ) +
      (truncated ? " · " + t("Showing recent text only") : "") +
      " · " +
      liveMetrics(d);
  } catch (err) {
    if (generation === state.uiGeneration && state.uiActive && !document.hidden &&
        id === state.expandedId && output === $("live-content"))
      note.textContent = t("Cannot load live output: ") + err.message;
  } finally {
    if (generation !== state.uiGeneration) return;
    state.liveBusy = false;
    // Follow a changed panel, channel or terminal status once this request settles.
    if ($("live-content") && (output !== $("live-content") || tab !== state.liveTab || status !== liveJobStatus()))
      loadLive();
    else scheduleLivePolling();
  }
}

// Elapsed time and token counts of the latest pass; unknown counts stay
// unavailable rather than being estimated from the text length.
function liveMetrics(data) {
  const m = data.metrics || {},
    parts = [];
  if (data.elapsed_seconds != null)
    parts.push(t("Elapsed: {seconds}s", { seconds: data.elapsed_seconds }));
  if (m.output_tokens != null)
    parts.push(t("Generated: {tokens} tokens", { tokens: m.output_tokens }));
  if (m.decode_tokens_per_second != null)
    parts.push(
      t("Decode: {rate} tokens/s (engine)", {
        rate: m.decode_tokens_per_second.toFixed(2),
      }),
    );
  if (m.output_tokens == null) parts.push(t("Token metrics unavailable"));
  return parts.join(" · ");
}
