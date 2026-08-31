"use client";

import { evaluateCommitment, fetchEconomics, type Economics } from "@/lib/api";
import Link from "next/link";
import { startTransition, useEffect, useState } from "react";
import { AssistantDrawer } from "../components/AssistantDrawer";

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

export default function SplitPage() {
  const [economics, setEconomics] = useState<Economics | null>(null);
  const [draftLevel, setDraftLevel] = useState(16);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchEconomics().then(setEconomics).catch((err) => setError(String(err)));
  }, []);

  async function applyScenario() {
    setLoading(true);
    setError(null);
    try {
      const next = await evaluateCommitment(draftLevel);
      startTransition(() => setEconomics(next));
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }

  if (!economics) {
    return <main className="max-w-5xl mx-auto px-8 py-16 text-muted">Loading the proposal…</main>;
  }

  const net = economics.net_savings;
  const keptPct = net ? (economics.customer_savings / net) * 100 : 0;
  const reservePct = net ? (economics.reserve / net) * 100 : 0;
  const profitPct = net ? (economics.profit / net) * 100 : 0;

  return (
    <main className="max-w-5xl mx-auto px-8 py-12 md:py-16">
      <header className="flex items-center justify-between">
        <div>
          <p className="text-xs text-muted tracking-[0.18em] uppercase">The honest split</p>
          <h1 className="text-3xl md:text-5xl tracking-tight font-semibold mt-2">What you keep. What we carry.</h1>
        </div>
        <div className="flex gap-4 text-sm text-muted">
          <Link href="/" className="hover:text-foreground">Proposal</Link>
          <Link href="/explore" className="hover:text-foreground">Explore</Link>
        </div>
      </header>

      <section className="mt-8 border border-line rounded-2xl p-7 bg-card">
        <ScenarioControl
          draftLevel={draftLevel}
          currentLevel={economics.proposal.commitment_per_hour}
          loading={loading}
          error={error}
          onChange={setDraftLevel}
          onApply={applyScenario}
        />
      </section>

      <section className="mt-10 grid lg:grid-cols-[1.2fr_0.8fr] gap-8">
        <div className="border border-line rounded-2xl bg-card p-7">
          <p className="text-sm text-muted">Guaranteed customer share</p>
          <p className="mt-2 text-5xl font-semibold text-accent num">{usd.format(economics.customer_savings)}</p>
          <p className="mt-2 text-lg">
            {`${(economics.customer_savings_rate * 100).toFixed(1)}%`} of covered on-demand spend.
          </p>
          <p className="mt-6 text-sm text-muted leading-6">
            This is after {usd.format(economics.wasted_commitment)} of unused commitment. We absorb that waste;
            it is not silently removed to make the rate look better.
          </p>
        </div>
        <div className="border border-line rounded-2xl bg-card p-7">
          <p className="text-sm text-muted">Net savings available to allocate</p>
          <p className="mt-2 text-4xl font-semibold num">{usd.format(net)}</p>
          <p className="mt-2 text-sm text-muted">Gross discount minus unused commitment. This is then divided between you, the risk reserve, and Cloud Capital.</p>
          <div className="mt-6 h-3 bg-line rounded-full overflow-hidden flex" title="Each net savings dollar is divided between customer savings, risk reserve, and Cloud Capital profit.">
            <div className="bg-accent" style={{ width: `${Math.max(0, keptPct)}%` }} />
            <div className="bg-slate-500" style={{ width: `${Math.max(0, reservePct)}%` }} />
            <div className="bg-slate-700" style={{ width: `${Math.max(0, profitPct)}%` }} />
          </div>
        </div>
      </section>

      <section className="mt-8 grid md:grid-cols-3 gap-4">
        <SplitCard label="You keep" value={economics.customer_savings} pct={keptPct} color="text-accent" detail="Your guaranteed portion of net savings." />
        <SplitCard label="Reserve we hold" value={economics.reserve} pct={reservePct} color="text-slate-300" detail="A portion held because Cloud Capital carries the downside if actual usage falls short." />
        <SplitCard label="Cloud Capital profit" value={economics.profit} pct={profitPct} color="text-slate-400" detail="A visible fixed 10% of net savings." />
      </section>

      <section className="mt-8 max-w-2xl">
        <div className="border border-line rounded-2xl p-7 bg-card">
          <h2 className="font-medium">Why a reserve exists</h2>
          <p className="mt-3 text-sm text-muted leading-6">
            We promise your rate even though usage varies hour to hour. The reserve is money we hold to absorb
            the downside if the commitment is less used than observed history suggests.
          </p>
          <dl className="mt-6 grid grid-cols-2 gap-4 text-sm">
            <div className="border-t border-line pt-3" title="Share of observed hours where eligible discounted spend covered this commitment level.">
              <dt className="text-muted">Protection in history</dt>
              <dd className="mt-1 text-xl font-medium num">{(economics.protection * 100).toFixed(0)}%</dd>
              <dd className="text-xs text-muted mt-1">of observed hours fully used this commitment level</dd>
            </div>
            <div className="border-t border-line pt-3" title="The service's normalized historical trend signal, not a forecast.">
              <dt className="text-muted">Reserve rate</dt>
              <dd className="mt-1 text-xl font-medium num">{economics.cost_of_risk_points.toFixed(1)}%</dd>
              <dd className="text-xs text-muted mt-1">of net savings held; lower means lower modeled risk</dd>
            </div>
          </dl>
        </div>
      </section>

      <p className="mt-10 text-xs text-muted max-w-3xl leading-5">
        Deliberately absent: a forecast, invented confidence intervals, or a claim that this exact scenario will
        recur. The service evaluates observed usage and applies its given risk model; it does not predict the future.
      </p>
      <AssistantDrawer context={{
        screen: "split",
        period: "2026-05",
        group_by: "service",
        metric: "on_demand_cost",
        commitment_per_hour: economics.proposal.commitment_per_hour,
        customer_savings: economics.customer_savings,
        reserve: economics.reserve,
        protection: economics.protection,
      }} />
    </main>
  );
}

function SplitCard({ label, value, pct, color, detail }: { label: string; value: number; pct: number; color: string; detail: string }) {
  return (
    <div className="border border-line rounded-xl bg-card p-5" title={detail}>
      <p className="text-sm text-muted">{label}</p>
      <p className={`mt-2 text-2xl font-semibold num ${color}`}>{usd.format(value)}</p>
      <p className="mt-1 text-xs text-muted num">{pct.toFixed(1)}¢ of each net savings dollar</p>
    </div>
  );
}

function ScenarioControl({ draftLevel, currentLevel, loading, error, onChange, onApply }: {
  draftLevel: number;
  currentLevel: number;
  loading: boolean;
  error: string | null;
  onChange: (level: number) => void;
  onApply: () => void;
}) {
  return <div className="max-w-2xl">
    <p className="text-xs text-muted tracking-[0.18em] uppercase">Scenario control</p>
    <h2 className="mt-2 font-medium text-xl">Test a different commitment level.</h2>
    <p className="mt-2 text-sm text-muted leading-6">A Savings Plan commits a fixed maximum of dollars per hour. Change that level, then evaluate it against the same observed history. This is a scenario, not a forecast.</p>
    <div className="mt-6 flex flex-wrap items-end gap-5">
      <label className="flex-1 min-w-64"><span className="flex justify-between text-sm"><span>Commitment level</span><span className="num">${draftLevel.toFixed(2)}/hr</span></span><input aria-label="Commitment level per hour" className="w-full mt-3 accent-emerald-400" type="range" min="8" max="28" step="0.5" value={draftLevel} onChange={(event) => onChange(Number(event.target.value))} /></label>
      <button onClick={onApply} disabled={loading || draftLevel === currentLevel} className="bg-accent text-black rounded-full px-4 py-2 text-sm font-medium disabled:opacity-40">{loading ? "Evaluating…" : "Evaluate this level"}</button>
    </div>
    {error && <p className="mt-3 text-sm text-rose-300">{error}</p>}
  </div>;
}
