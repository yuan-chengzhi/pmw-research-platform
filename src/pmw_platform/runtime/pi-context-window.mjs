// Per-session Pi model-window override owned and pinned by the Python adapter.
//
// The supported Pi build exposes ExtensionContext.model as the live model
// object for this single-session process.  Mutating one numeric field in place
// avoids Pi's persistent settings setter and therefore never writes the shared
// account directory.  The host verifies the exact value with get_state before
// it is allowed to issue a prompt.

const FLAG = "pmw-context-window-tokens";
const MAXIMUM_TOKENS = 2_147_483_647;

function parseTokens(value) {
  if (typeof value !== "string" || !/^[1-9][0-9]*$/.test(value)) {
    throw new Error("PMW_CONTEXT_WINDOW_FLAG_INVALID");
  }
  const tokens = Number(value);
  if (!Number.isSafeInteger(tokens) || tokens > MAXIMUM_TOKENS) {
    throw new Error("PMW_CONTEXT_WINDOW_FLAG_INVALID");
  }
  return tokens;
}

export default function registerPmwContextWindow(pi) {
  pi.registerFlag(FLAG, {
    description: "Host-authenticated active model context window",
    type: "string",
  });

  pi.on("session_start", (_event, ctx) => {
    const raw = pi.getFlag(FLAG);
    if (raw === undefined) return;
    const tokens = parseTokens(raw);
    const model = ctx.model;
    if (model === undefined) {
      throw new Error("PMW_CONTEXT_WINDOW_MODEL_UNAVAILABLE");
    }
    model.contextWindow = tokens;
    if (ctx.model === undefined || ctx.model.contextWindow !== tokens) {
      throw new Error("PMW_CONTEXT_WINDOW_OVERRIDE_FAILED");
    }
  });
}
