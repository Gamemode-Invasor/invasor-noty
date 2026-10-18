import { defineModule, ui, type ModuleCtx } from "invasor";

// Noty: a local web service that shows Steam notifications (as Steam shows an
// achievement). This tab shows whether it's listening, its settings, a test button and
// the last notifications (shown, failed or silenced).

interface Endpoint {
  name: string;
  path: string;
  url: string;
  on: boolean;
  details: string[];
}

interface Status {
  enabled: boolean;
  running: boolean;
  port: number;
  url: string;
  error: string | null;
  dnd: boolean;
  endpoints: Endpoint[];
}

interface Recent {
  time: string;
  source: string;
  title: string;
  ok: boolean;
  error: string | null;
  muted: "endpoint_off" | "do_not_disturb" | null;
}

const MUTED = { endpoint_off: "endpoint off", do_not_disturb: "do not disturb" };

function recentLine(r: Recent) {
  const mark = r.muted ? "🔕" : r.ok ? "✓" : "✗";
  const why = r.muted ? ` — ${MUTED[r.muted]}` : r.error ? ` — ${r.error}` : "";
  return `${r.time}  ${mark} [${r.source}] ${r.title}${why}`;
}

const errorText = (e: unknown) => String((e as Error)?.message ?? e);

let statusEl: HTMLElement | null = null;
let recentEl: HTMLElement | null = null;
let timer: number | undefined;

async function refresh(ctx: ModuleCtx) {
  if (!statusEl || !recentEl) return;
  try {
    const st = await ctx.call<Status>("status");
    const line = !st.enabled
      ? "The web service is off."
      : st.running
        ? `Listening on ${st.url}`
        : `⚠ Not listening: ${st.error ?? "unknown error"}`;
    statusEl.replaceChildren(
      ui.info(line),
      ...(st.running && st.dnd ? [ui.info("🔕 Do not disturb: requests are answered, but nothing is shown.")] : []),
    );
    const list = await ctx.call<Recent[]>("recent");
    recentEl.replaceChildren(
      ...(list.length
        ? list.map((r) => ui.info(recentLine(r)))
        : [ui.info("Nothing sent yet.")]),
    );
  } catch (e) {
    statusEl.replaceChildren(ui.info(errorText(e)));
  }
}

export default defineModule({
  async render(el, ctx) {
    statusEl = document.createElement("div");
    recentEl = document.createElement("div");
    const form = await ui.settingsForm(ctx, { navHints: false });
    el.append(
      statusEl,
      form,
      ui.button({
        label: "Send a test notification",
        onClick: async () => {
          try {
            await ctx.call("test");
          } catch (e) {
            ctx.toast(`Not shown: ${errorText(e)}`, "error");
          }
          void refresh(ctx);
        },
      }),
      ui.section("Last notifications", [recentEl], { open: true }),
    );
    await refresh(ctx);
    // Saving a setting restarts the service, and notifications arrive at any time: keep
    // the state fresh while the tab is on screen.
    timer = window.setInterval(() => {
      if (el.isConnected && el.getClientRects().length) void refresh(ctx);
    }, 3000);
  },
  onShow: (ctx) => void refresh(ctx),
  destroy: () => {
    window.clearInterval(timer);
    statusEl = recentEl = null;
  },
});
