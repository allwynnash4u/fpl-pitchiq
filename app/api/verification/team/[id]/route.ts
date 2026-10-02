import { NextResponse } from "next/server";

const ENGINE_URL = (process.env.FPL_ENGINE_URL || "http://127.0.0.1:8765").replace(/\/$/, "");

export async function GET(_: Request, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  const teamId = Number(id);
  if (!Number.isInteger(teamId) || teamId <= 0) {
    return NextResponse.json({ ok: false, error: "Invalid public FPL Team ID." }, { status: 400 });
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30_000);
  try {
    const response = await fetch(ENGINE_URL + "/api/verification/team/" + teamId, {
      cache: "no-store",
      signal: controller.signal,
      headers: { accept: "application/json" },
    });
    const body = await response.text();
    let payload: unknown;
    try { payload = body ? JSON.parse(body) : {}; } catch { payload = { ok: false, error: "FPL engine returned invalid JSON." }; }
    return NextResponse.json(payload, { status: response.status, headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    const message = error instanceof Error && error.name === "AbortError" ? "FPL verification request timed out." : error instanceof Error ? error.message : "Unable to reach FPL engine.";
    return NextResponse.json({ ok: false, error: message }, { status: 502 });
  } finally {
    clearTimeout(timeout);
  }
}
