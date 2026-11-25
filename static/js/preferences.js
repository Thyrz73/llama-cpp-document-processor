// Preferences are optional. Keep storage guards separate from UI application.
function unavailable(error) {
  if (
    !(error instanceof DOMException) ||
    !["SecurityError", "QuotaExceededError"].includes(error.name)
  )
    throw error;
}

export function getPreference(key) {
  try {
    return globalThis.localStorage?.getItem(key) ?? null;
  } catch (error) {
    unavailable(error);
    return null;
  }
}

export function setPreference(key, value) {
  try {
    globalThis.localStorage?.setItem(key, value);
  } catch (error) {
    unavailable(error);
  }
}
