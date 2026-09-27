import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";

export function Toolbar({
  title,
  className = "",
  children,
}: {
  title?: string;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <div className={`toolbar${className ? ` ${className}` : ""}`}>
      {title && <h1>{title}</h1>}
      {children}
    </div>
  );
}

export function Spinner() {
  return (
    <div className="center">
      <div className="spinner" />
    </div>
  );
}

export function EmptyState({ icon, title, hint }: { icon: string; title: string; hint?: string }) {
  return (
    <div className="empty-state">
      <div className="big">{icon}</div>
      <h3>{title}</h3>
      {hint && <p style={{ marginTop: 8 }}>{hint}</p>}
    </div>
  );
}

export function QueryError({ error, retry }: { error: unknown; retry?: () => void }) {
  return (
    <div className="error-banner" role="alert">
      <strong>Could not load this view.</strong>{" "}
      {error instanceof Error ? error.message : "An unexpected request error occurred."}
      {retry && <button className="btn sm" onClick={retry}>Retry</button>}
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const titleId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const isTopDialog = () => {
      const dialogs = document.querySelectorAll('[role="dialog"]');
      return dialogs[dialogs.length - 1] === dialogRef.current;
    };
    const focusable = () => Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
      'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]',
    ) ?? []).filter((element) => !element.hidden && !element.closest('[hidden]'));
    const onKey = (event: KeyboardEvent) => {
      if (!isTopDialog()) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        onCloseRef.current();
      }
      if (event.key === "Tab") {
        const elements = focusable();
        const first = elements[0];
        const last = elements[elements.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault(); last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault(); first?.focus();
        }
      }
    };
    const keepFocus = (event: FocusEvent) => {
      if (isTopDialog() && !dialogRef.current?.contains(event.target as Node)) closeRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    document.addEventListener("focusin", keepFocus);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("focusin", keepFocus);
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, []);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div ref={dialogRef} className="modal" role="dialog" aria-modal="true" aria-labelledby={titleId} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header" id={titleId}>
          {title}
          <button ref={closeRef} onClick={onClose} aria-label="Close dialog" style={{ fontSize: 18, color: "var(--text-dim)" }}>
            ✕
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

export function Toggle({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return <button type="button" role="switch" aria-label={label} aria-checked={on} className={`toggle${on ? " on" : ""}`} onClick={() => onChange(!on)} />;
}

export function formatBytes(bytes: number): string {
  if (!bytes) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  let v = bytes;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

export function issueLabel(number: number, _volume?: number | null, displayNumber?: string): string {
  const n = displayNumber || (Number.isInteger(number) ? number.toString() : number.toFixed(1));
  return `#${n}`;
}

export const statusPill: Record<string, string> = {
  releasing: "blue",
  finished: "green",
  hiatus: "orange",
  cancelled: "red",
  not_yet_released: "gray",
  unknown: "gray",
  queued: "gray",
  downloading: "blue",
  importing: "orange",
  running: "blue",
  done: "green",
  failed: "red",
  needs_attention: "orange",
  retrying: "orange",
  grabbed: "blue",
  imported: "green",
  deleted: "red",
  removed: "red",
  resolved: "green",
};
