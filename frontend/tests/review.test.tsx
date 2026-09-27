import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { api } from "../src/api/client";
import Activity from "../src/pages/Activity";
import Settings from "../src/pages/Settings";
import { Modal } from "../src/components/common";

vi.mock("../src/api/client", () => ({ api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn() } }));

const clients: QueryClient[] = [];
function mount(element: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  clients.push(client);
  return render(<QueryClientProvider client={client}><MemoryRouter>{element}</MemoryRouter></QueryClientProvider>);
}
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.get).mockResolvedValue([]); });
afterEach(() => { cleanup(); clients.forEach((client) => client.clear()); clients.length = 0; vi.unstubAllGlobals(); });

const event = { id: 1, event: "grabbed", series_title: "Test", detail: "Found a release", source_name: "getcomics", created_at: "2026-09-26T12:00:00Z" };

test("history filters stay usable when the selected filter is empty", async () => {
  vi.mocked(api.get).mockImplementation(async (path) => path.startsWith("/history") && !path.includes("event=failed") ? [event] : []);
  mount(<Activity />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "History", exact: true }));
  await screen.findByText("Found a release");
  await user.click(screen.getByRole("button", { name: "failed", exact: true }));
  await screen.findByText("No events match this filter");
  expect(screen.getByRole("button", { name: "all", exact: true })).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "Clear filter" }));
  await screen.findByText("Found a release");
});

test("history supports older pages and returning from the last page", async () => {
  vi.mocked(api.get).mockImplementation(async (path) => {
    if (!path.startsWith("/history")) return [];
    return path.includes("offset=50") ? [{ ...event, id: 52, detail: "Older event" }] : Array.from({ length: 51 }, (_, id) => ({ ...event, id, detail: `Event ${id}` }));
  });
  mount(<Activity />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "History", exact: true }));
  await screen.findByText("Event 0");
  await user.click(screen.getByRole("button", { name: "Next", exact: true }));
  await screen.findByText("Older event");
  expect(screen.getByRole("button", { name: "Next", exact: true }).hasAttribute("disabled")).toBe(true);
  await user.click(screen.getByRole("button", { name: "Previous", exact: true }));
  await screen.findByText("Event 0");
});

const download = { id: 4, title: "Test release", series_title: "Test", series_id: 7, status: "queued", kind: "direct", source_name: "getcomics", progress: 0, attempt_count: 2, next_retry_at: "2099-01-01T00:00:00Z", error: "" };

test("queue removal errors are visible and retain selection", async () => {
  vi.mocked(api.get).mockResolvedValue([download]);
  vi.mocked(api.post).mockRejectedValue(new Error("Import is still finishing"));
  mount(<Activity />);
  const user = userEvent.setup();
  const select = await screen.findByRole("checkbox", { name: "Select Test release" });
  await user.click(select);
  await user.click(screen.getByRole("button", { name: "Remove selected" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Import is still finishing");
  expect((select as HTMLInputElement).checked).toBe(true);
  expect(screen.getByText(/Attempt 2/).textContent).toContain("Retry in");
});

test("manual repair links to file mapping and resolves without a retry", async () => {
  vi.mocked(api.get).mockResolvedValue([{ ...download, status: "needs_attention" }]);
  vi.mocked(api.post).mockResolvedValue(undefined);
  mount(<Activity />);
  const user = userEvent.setup();
  const map = await screen.findByRole("link", { name: "Map files" });
  expect(map.getAttribute("href")).toBe("/series/7?files=1");
  await user.click(screen.getByRole("button", { name: "Mark resolved" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/queue/4/resolve"));
  expect(api.post).not.toHaveBeenCalledWith("/queue/4/retry");
});

test("settings draft survives navigation and can be discarded without storing secrets", async () => {
  const store = vi.fn();
  vi.stubGlobal("localStorage", { setItem: store });
  vi.stubGlobal("sessionStorage", { setItem: store });
  vi.mocked(api.get).mockImplementation(async (path) => path === "/settings" ? {
    metron_enabled: "false", source_priority: "getcomics", source_getcomics_enabled: "true", naming_template: "{series}",
  } : []);
  function Navigation() {
    const [settings, setSettings] = useState(true);
    return <><button onClick={() => setSettings(!settings)}>Navigate</button>{settings ? <Settings /> : <p>Library</p>}</>;
  }
  mount(<Navigation />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("switch", { name: "Enable Metron" }));
  await user.click(screen.getByRole("button", { name: "Navigate" }));
  await user.click(screen.getByRole("button", { name: "Navigate" }));
  expect((await screen.findByRole("switch", { name: "Enable Metron" })).getAttribute("aria-checked")).toBe("true");
  expect(screen.getByText(/Unsaved draft/)).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "Discard changes" }));
  expect(screen.getByRole("switch", { name: "Enable Metron" }).getAttribute("aria-checked")).toBe("false");
  expect(store).not.toHaveBeenCalled();
});

test("modal traps keyboard focus and restores the opener", async () => {
  function Example() {
    const [open, setOpen] = useState(false);
    return <><button onClick={() => setOpen(true)}>Open</button>{open && <Modal title="Test dialog" onClose={() => setOpen(false)}><button>Last action</button></Modal>}</>;
  }
  mount(<Example />);
  const user = userEvent.setup();
  const opener = screen.getByRole("button", { name: "Open" });
  await user.click(opener);
  const close = screen.getByRole("button", { name: "Close dialog" });
  expect(document.activeElement).toBe(close);
  await user.tab({ shift: true });
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Last action" }));
  await user.tab();
  expect(document.activeElement).toBe(close);
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(opener);
});
