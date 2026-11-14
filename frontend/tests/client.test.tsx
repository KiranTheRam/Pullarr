import { afterEach, expect, test, vi } from "vitest";
import { api, browserAuthMode, initClient } from "../src/api/client";

afterEach(() => vi.unstubAllGlobals());

test("bootstrap is never cached and reports the server's reason", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ detail: "Unrecognized Host. Use an IP address." }), { status: 400 }),
  );
  vi.stubGlobal("fetch", fetchMock);
  await expect(initClient()).rejects.toThrow("Unrecognized Host");
  expect(fetchMock).toHaveBeenCalledWith("/initialize.json", { cache: "no-store" });
});

test("bootstrap falls back to the status for non-JSON responses", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("<html>", { status: 502 })));
  await expect(initClient()).rejects.toThrow("HTTP 502");
});


test.each(["managed", "environment"] as const)("%s browser requests use the session without an integration key", async (mode) => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ apiKey: "", authMode: mode, version: "0.2.0" })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true })));
  vi.stubGlobal("fetch", fetchMock);
  await initClient();
  expect(browserAuthMode()).toBe(mode);
  await api.put("/settings", {});
  const [url, options] = fetchMock.mock.calls[1];
  expect(url).toBe("/api/v1/settings");
  expect(options.headers).toEqual({ "Content-Type": "application/json" });
});

test("externally authenticated browser requests retain bootstrap API-key compatibility", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ apiKey: "fixture", authMode: "external", version: "0.2.0" })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true })));
  vi.stubGlobal("fetch", fetchMock);
  await initClient();
  await api.get("/settings");
  expect(fetchMock.mock.calls[1][1].headers).toEqual({ "X-Api-Key": "fixture" });
});
