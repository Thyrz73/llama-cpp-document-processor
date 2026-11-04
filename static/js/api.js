// JSON requests to the processor API.
import { t } from "./i18n.js";

// Fetch url and decode its JSON body within timeout milliseconds.
// The deadline covers both the headers and the body, so a stalled request
// always settles and releases the polling locks. Resolve to
// { ok, status, json() } like a fetch response; reject on a network error,
// a body that is not JSON, or the deadline.
export async function jsonFetch(url, options = {}, timeout = 15000) {
  const controller = new AbortController();
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(Error(t("Request timed out.")));
    }, timeout);
  });
  try {
    return await Promise.race([
      (async () => {
        const response = await fetch(url, {
          ...options,
          signal: controller.signal,
        });
        const data = await response.json();
        return {
          ok: response.ok,
          status: response.status,
          json: async () => data,
        };
      })(),
      deadline,
    ]);
  } finally {
    clearTimeout(timer);
  }
}
