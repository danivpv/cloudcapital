"use client";

import { ask, type AskEvent } from "@/lib/api";
import { useRef, useState } from "react";

interface Msg {
  role: "user" | "assistant";
  text: string;
  chart?: string;
  usage?: AskEvent["usage"];
  latencyMs?: number;
}

export function AskPanel() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  async function send() {
    const q = input.trim();
    if (!q || busy) return;
    setInput("");
    setBusy(true);
    setMessages((m) => [...m, { role: "user", text: q }]);
    const assistant: Msg = { role: "assistant", text: "" };
    setMessages((m) => [...m, assistant]);
    abortRef.current = new AbortController();
    try {
      await ask(
        q,
        (e) => {
          setMessages((m) => {
            const last = m[m.length - 1];
            if (last?.role !== "assistant") return m;
            const next = { ...last };
            if (e.kind === "bars") next.chart = JSON.stringify(e.data).slice(0, 200);
            if (e.text) next.text = e.text;
            if (e.delta) next.text += e.delta;
            if (e.usage) next.usage = e.usage;
            if (e.latency_ms) next.latencyMs = e.latency_ms;
            return [...m.slice(0, -1), next];
          });
        },
        abortRef.current.signal
      );
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        setMessages((m) => {
          const last = m[m.length - 1];
          return [...m.slice(0, -1), { ...last, text: "Couldn't reach the ask service." }];
        });
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="border border-line rounded-2xl bg-card overflow-hidden">
      <div className="px-6 py-4 border-b border-line flex items-center justify-between">
        <h2 className="text-sm text-muted tracking-widest uppercase">Ask anything</h2>
        {messages.length > 0 && (
          <button
            onClick={() => {
              abortRef.current?.abort();
              setMessages([]);
            }}
            className="text-xs text-muted hover:text-foreground transition-colors"
          >
            clear
          </button>
        )}
      </div>
      <div className="px-6 py-4 space-y-4 min-h-40 max-h-96 overflow-y-auto">
        {messages.length === 0 && (
          <p className="text-sm text-muted">
            Try{" "}
            <button className="text-accent hover:underline" onClick={() => setInput("what is the split of savings?")}>
              &ldquo;what is the split of savings?&rdquo;
            </button>{" "}
            or{" "}
            <button className="text-accent hover:underline" onClick={() => setInput("what is driving the spend?")}>
              &ldquo;what is driving the spend?&rdquo;
            </button>
          </p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`text-sm ${m.role === "user" ? "text-right" : ""}`}>
            <div
              className={`inline-block max-w-[85%] rounded-2xl px-4 py-2.5 ${
                m.role === "user" ? "bg-foreground text-background" : "bg-line/60"
              }`}
            >
              {m.text || (busy && m.role === "assistant" ? <span className="animate-pulse">…</span> : null)}
              {m.chart && <pre className="text-xs mt-2 text-muted overflow-hidden">{m.chart}</pre>}
            </div>
            {m.usage && (
              <div className="text-[11px] text-muted mt-1 num">
                {m.usage.total_tokens} tokens · ${m.usage.total_cost_usd.toFixed(4)}
                {m.latencyMs ? ` · ${(m.latencyMs / 1000).toFixed(1)}s` : ""}
              </div>
            )}
          </div>
        ))}
      </div>
      <form
        className="px-6 py-4 border-t border-line flex gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Why is the saving smaller for that account?"
          className="flex-1 bg-transparent outline-none text-sm placeholder:text-muted/60"
        />
        <button
          type="submit"
          disabled={busy || !input.trim()}
          className="px-4 py-1.5 rounded-full bg-accent text-black text-sm font-medium disabled:opacity-40 transition-opacity"
        >
          ask
        </button>
      </form>
    </div>
  );
}
