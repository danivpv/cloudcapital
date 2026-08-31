"use client";

import { investigate, type AssistantEvent } from "@/lib/api";
import { useRef, useState } from "react";

export function AssistantDrawer({ context }: { context: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [status, setStatus] = useState("Ask about the view you are looking at.");
  const [sql, setSql] = useState<string[]>([]);
  const [meta, setMeta] = useState<AssistantEvent | null>(null);
  const [busy, setBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  async function send() {
    const prompt = question.trim();
    if (!prompt || busy) return;
    setBusy(true);
    setQuestion("");
    setAnswer("");
    setSql([]);
    setMeta(null);
    setStatus("Starting investigation…");
    abortRef.current = new AbortController();
    try {
      await investigate(prompt, context, (event, data) => {
        if (event === "status") setStatus(data.message ?? "Working…");
        if (event === "sql" && data.sql) setSql((current) => [...current, data.sql!]);
        if (event === "text" && data.delta) setAnswer((current) => current + data.delta);
        if (event === "meta") {
          setMeta(data);
          setStatus("Complete");
        }
        if (event === "error") setStatus(data.message ?? "The investigation could not run.");
      }, abortRef.current.signal);
    } catch (error) {
      if ((error as Error).name !== "AbortError") setStatus("The assistant could not reach the analytics backend.");
    } finally {
      setBusy(false);
    }
  }

  return <>
    <button onClick={() => setOpen(true)} className="fixed bottom-6 right-6 z-40 rounded-full bg-accent text-black px-5 py-3 text-sm font-medium shadow-2xl shadow-black/50">Ask about this view</button>
    {open && <div className="fixed inset-0 z-50 bg-black/55" onClick={() => setOpen(false)} />}
    <aside className={`fixed right-0 top-0 z-50 h-dvh w-full max-w-md border-l border-line bg-[#0b0e10] shadow-2xl transition-transform duration-200 ${open ? "translate-x-0" : "translate-x-full"}`}>
      <div className="h-full flex flex-col">
        <header className="px-6 py-5 border-b border-line flex items-start justify-between gap-4">
          <div><p className="text-xs tracking-[0.18em] text-muted uppercase">Investigation assistant</p><h2 className="mt-1 text-lg font-medium">Ask about this view</h2></div>
          <button onClick={() => { abortRef.current?.abort(); setOpen(false); }} className="text-sm text-muted hover:text-foreground">Close</button>
        </header>
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
          <p className="text-sm text-muted leading-6">The assistant can run at most two safe SQL queries over the current screen context. It never receives raw billing rows.</p>
          <p className="text-sm text-muted">{status}</p>
          {answer && <p className="text-sm leading-7 whitespace-pre-wrap">{answer}</p>}
          {sql.length > 0 && <details className="border border-line rounded-xl p-4"><summary className="cursor-pointer text-sm">Show SQL used</summary><div className="mt-4 space-y-4">{sql.map((statement, index) => <pre key={`sql:${index}`} className="overflow-x-auto whitespace-pre-wrap text-xs text-muted font-mono">{statement}</pre>)}</div></details>}
          {meta?.usage && <p className="text-xs text-muted num">{meta.query_count ?? 0} query steps · {meta.usage.total_tokens} tokens · ${meta.usage.total_cost_usd.toFixed(4)} · {((meta.latency_ms ?? 0) / 1000).toFixed(1)}s</p>}
        </div>
        <form className="border-t border-line p-5" onSubmit={(event) => { event.preventDefault(); send(); }}>
          <textarea value={question} onChange={(event) => setQuestion(event.target.value)} rows={3} placeholder="Why did this change?" className="w-full resize-none rounded-xl border border-line bg-background px-3 py-2 text-sm outline-none focus:border-accent" />
          <button disabled={busy || !question.trim()} className="mt-3 w-full rounded-xl bg-accent py-2 text-sm font-medium text-black disabled:opacity-40">{busy ? "Investigating…" : "Investigate"}</button>
        </form>
      </div>
    </aside>
  </>;
}
