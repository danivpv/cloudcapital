"use client";

import { fetchEconomics, type Economics } from "@/lib/api";
import Link from "next/link";
import { useEffect, useState } from "react";
import { SavingsSplit } from "./components/SavingsSplit";
import { AssistantDrawer } from "./components/AssistantDrawer";
import { NarrationFeed } from "./components/NarrationFeed";

const fmt = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

export default function Home() {
  const [econ, setEcon] = useState<Economics | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchEconomics().then(setEcon).catch((e) => setError(String(e)));
  }, []);

  if (error) {
    return (
      <main className="max-w-5xl mx-auto px-8 py-24">
        <p className="text-muted">Couldn&apos;t reach the economics service ({error}).</p>
        <p className="text-muted mt-2">
          Start it with <code className="font-mono text-sm">make dev</code>, then reload.
        </p>
      </main>
    );
  }

  if (!econ) {
    return (
      <main className="max-w-5xl mx-auto px-8 py-24">
        <div className="h-8 w-64 bg-line rounded animate-pulse" />
        <div className="h-20 w-96 bg-line rounded animate-pulse mt-6" />
      </main>
    );
  }

  const rate = (econ.customer_savings_rate * 100).toFixed(1);

  return (
    <main className="max-w-5xl mx-auto px-8 py-16 md:py-24">
      <div className="flex items-center justify-between mb-12">
        <span className="text-sm text-muted tracking-widest uppercase">Cloud Capital</span>
        <div className="flex gap-2">
          <Link
            href="/split"
            className="text-sm border border-line rounded-full px-4 py-1.5 hover:border-accent hover:text-accent transition-colors"
          >
            Understand the split
          </Link>
          <Link
            href="/explore"
            className="text-sm border border-line rounded-full px-4 py-1.5 hover:border-accent hover:text-accent transition-colors"
          >
            Explore the spend →
          </Link>
        </div>
      </div>

      {/* Hero — the thirty-second answer */}
      <section className="fade-up">
        <p className="text-sm text-muted tracking-widest uppercase">
          Proposal · Compute Savings Plan · 12-month
        </p>
        <h1 className="mt-4 text-5xl md:text-7xl font-semibold tracking-tight leading-[1.05]">
          You keep{" "}
          <span className="text-accent num">
            {fmt.format(econ.customer_savings)}
          </span>
        </h1>
        <p className="mt-3 text-xl md:text-2xl text-foreground/90">
          <span className="text-accent num font-semibold">{rate}%</span> of covered spend,
          guaranteed. {fmt.format(econ.net_savings)} net of waste.
        </p>
        <p className="mt-2 text-sm text-muted">
          Net of {fmt.format(econ.wasted_commitment)} in unused commitment — we pay for the
          hours the commitment sits idle, not you.
        </p>
      </section>

      {/* Where it lands */}
      <section className="mt-16 fade-up fade-up-1 grid md:grid-cols-2 gap-8">
        <div className="border border-line rounded-2xl p-6 bg-card">
          <h2 className="text-sm text-muted tracking-widest uppercase">Where the savings come from</h2>
          <div className="mt-4 space-y-3">
            <Bar label="Covered on-demand" value={econ.covered_on_demand_cost} max={econ.covered_on_demand_cost} />
            <Bar label="Net savings" value={econ.net_savings} max={econ.covered_on_demand_cost} accent />
            <Bar label="Committed cost" value={econ.committed_cost} max={econ.covered_on_demand_cost} />
            <Bar label="Wasted commitment" value={econ.wasted_commitment} max={econ.covered_on_demand_cost} warn />
          </div>
        </div>
        <div className="border border-line rounded-2xl p-6 bg-card">
          <h2 className="text-sm text-muted tracking-widest uppercase">The split</h2>
          <SavingsSplit econ={econ} />
        </div>
      </section>

      {/* Narration */}
      <section className="mt-16 fade-up fade-up-3">
        <NarrationFeed />
      </section>
      <AssistantDrawer context={{
        screen: "proposal",
        period: "2026-05",
        group_by: "service",
        metric: "on_demand_cost",
        customer_savings: econ.customer_savings,
        customer_savings_rate: econ.customer_savings_rate,
      }} />
    </main>
  );
}

function Bar({
  label,
  value,
  max,
  accent,
  warn,
}: {
  label: string;
  value: number;
  max: number;
  accent?: boolean;
  warn?: boolean;
}) {
  const pct = Math.max(0, Math.min(100, (Math.abs(value) / max) * 100));
  return (
    <div>
      <div className="flex justify-between text-sm mb-1">
        <span>{label}</span>
        <span className={`num ${accent ? "text-accent" : warn ? "text-amber-400" : ""}`}>
          {fmt.format(value)}
        </span>
      </div>
      <div className="h-2 rounded-full bg-line overflow-hidden">
        <div
          className={`h-full rounded-full ${accent ? "bg-accent" : warn ? "bg-amber-400" : "bg-foreground/40"}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
