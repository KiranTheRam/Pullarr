import { afterEach, expect, test, vi } from "vitest";
import { initClient } from "../src/api/client";

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
