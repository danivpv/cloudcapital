"use client";

import {
  fetchDelta,
  fetchDrill,
  fetchSlice,
  fetchVocab,
  savingsReady,
  warmSavings,
  type DeltaRow,
  type DrillRow,
  type SliceRow,
  type Vocab,
} from "@/lib/api";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { AssistantDrawer } from "../components/AssistantDrawer";

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const quantity = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

const DIM_LABELS: Record<string, string> = {
  service: "Service", account: "Account", region: "Region", usage_kind: "Usage type",
  instance_type: "Instance type", commitment_scope: "Commitment scope",
};
const METRIC_LABELS: Record<string, string> = {
  on_demand_cost: "On-demand cost", usage_amount: "Usage amount", amortized_cost: "Amortized cost",
  covered_on_demand_cost: "Covered on-demand cost", committed_cost: "Committed cost", gross_savings: "Gross savings",
};

export default function ExplorePage() {
  const [vocab, setVocab] = useState<Vocab | null>(null);
  const [groupBy, setGroupBy] = useState("service");
  const [metric, setMetric] = useState("on_demand_cost");
  const [period, setPeriod] = useState("");
  const [comparisonPeriod, setComparisonPeriod] = useState("");
  const [compare, setCompare] = useState(false);
  const [topN, setTopN] = useState(10);
  const [sliceRows, setSliceRows] = useState<SliceRow[] | null>(null);
  const [deltaRows, setDeltaRows] = useState<DeltaRow[] | null>(null);
  const [drill, setDrill] = useState<DrillRow[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchVocab().then((next) => {
      setVocab(next);
      const complete = next.complete_periods;
      setPeriod(complete.at(-1) ?? "");
      setComparisonPeriod(complete.at(-2) ?? "");
    }).catch((err) => setError(String(err)));
  }, []);

  const savingsMetric = ["covered_on_demand_cost", "committed_cost", "gross_savings"].includes(metric);

  useEffect(() => {
    if (!vocab || !period || (compare && !comparisonPeriod)) return;
    let cancelled = false;
    const load = async () => {
      try {
        if (savingsMetric && !(await savingsReady())) {
          await warmSavings();
          let attempts = 0;
          while (!(await savingsReady()) && attempts < 20 && !cancelled) {
            await new Promise((resolve) => setTimeout(resolve, 500));
            attempts++;
          }
        }
        if (cancelled) return;
        if (compare) {
          const result = await fetchDelta({
            group_by: groupBy, metric,
            window_a: [comparisonPeriod, comparisonPeriod],
            window_b: [period, period], top_n: topN,
          });
          if (!cancelled) {
            setDeltaRows(result.rows);
            setSliceRows(null);
          }
        } else {
          const result = await fetchSlice({
            group_by: groupBy, metric, window: [period, period], top_n: topN,
          });
          if (!cancelled) {
            setSliceRows(result.rows);
            setDeltaRows(null);
          }
        }
        if (!cancelled) {
          setSelected(null);
          setDrill(null);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError(String(err));
      }
    };
    void load();
    return () => { cancelled = true; };
  }, [vocab, groupBy, metric, period, comparisonPeriod, compare, topN, savingsMetric]);

  const displayedRows = compare ? deltaRows : sliceRows;
  const maxBar = useMemo(() => {
    if (!displayedRows) return 0;
    return displayedRows.reduce((max, row) => {
      const value = compare ? Math.abs((row as DeltaRow).delta) : (row as SliceRow).value;
      return Math.max(max, value);
    }, 0);
  }, [displayedRows, compare]);
  const selectedPartial = vocab?.periods.find((entry) => entry.period === period)?.partial;
  const comparePartial = vocab?.periods.find((entry) => entry.period === comparisonPeriod)?.partial;

  async function drillInto(row: SliceRow | DeltaRow, window: string) {
    const label = String(row[groupBy] ?? "no-prefix");
    setSelected(label);
    try {
      const result = await fetchDrill({ group_by: groupBy, value: label, window: [window, window], limit: 30 });
      setDrill(result.rows);
    } catch (err) {
      setError(String(err));
    }
  }

  const format = (value: number) => metric === "usage_amount" ? quantity.format(value) : money.format(value);

  if (!vocab && error) {
    return <main className="max-w-5xl mx-auto px-8 py-16">
      <p className="text-xs text-muted tracking-[0.18em] uppercase">Explorer unavailable</p>
      <h1 className="mt-2 text-3xl font-semibold">The analytics backend is not responding.</h1>
      <p className="mt-3 text-sm text-muted max-w-xl">Start the complete local stack with <code className="num">make dev</code>. The Explorer needs the backend on port 8001 to load periods and chart data.</p>
      <p className="mt-5 text-xs text-rose-300">{error}</p>
    </main>;
  }

  return (
    <main className="max-w-5xl mx-auto px-8 py-12">
      <header className="flex items-start justify-between gap-6">
        <div>
          <p className="text-xs text-muted tracking-[0.18em] uppercase">Explorer</p>
          <h1 className="text-3xl md:text-5xl tracking-tight font-semibold mt-2">Take the bill apart.</h1>
          <p className="text-sm text-muted mt-3 max-w-2xl">
            Start with one simple breakdown. Turn on comparison only when the question is what changed.
          </p>
        </div>
        <div className="flex gap-4 text-sm text-muted shrink-0">
          <Link href="/" className="hover:text-foreground">Proposal</Link>
          <Link href="/split" className="hover:text-foreground">Split</Link>
        </div>
      </header>

      <section className="mt-8 border border-line rounded-2xl bg-card p-5">
        <div className="flex flex-wrap items-end gap-4">
          <Select label="Break down by" value={groupBy} onChange={setGroupBy} options={vocab?.dims ?? []} labels={DIM_LABELS} prefix="dimension" />
          <Select label="Measure" value={metric} onChange={setMetric} options={vocab?.metrics ?? []} labels={METRIC_LABELS} prefix="metric" />
          <Select label={compare ? "Later period" : "Period"} value={period} onChange={setPeriod} options={vocab?.periods.map((entry) => entry.period) ?? []} labels={periodLabels(vocab)} prefix="period" />
          {compare && <Select label="Compare against" value={comparisonPeriod} onChange={setComparisonPeriod} options={vocab?.periods.map((entry) => entry.period) ?? []} labels={periodLabels(vocab)} prefix="compare" />}
          <label className="text-sm flex items-center gap-2 pb-2 cursor-pointer">
            <input type="checkbox" checked={compare} onChange={(event) => setCompare(event.target.checked)} className="accent-emerald-400" />
            Compare periods
          </label>
          <label className="text-sm min-w-28">
            <span className="block text-xs text-muted uppercase tracking-wider">Top {topN}</span>
            <input className="w-full mt-2 accent-emerald-400" type="range" min="5" max="20" value={topN} onChange={(event) => setTopN(Number(event.target.value))} />
          </label>
        </div>
        {(selectedPartial || comparePartial) && (
          <p className="mt-4 text-xs text-amber-300">
            Partial period selected. June 2026 ends Jun 24, so it is not comparable with a full month.
          </p>
        )}
      </section>

      {error && <p className="mt-5 text-sm text-rose-300">{error}</p>}
      {!displayedRows && !error && <p className="mt-8 text-sm text-muted">Loading breakdown…</p>}

      {displayedRows && (
        <section className="mt-8 border border-line rounded-2xl bg-card p-6">
          <h2 className="text-sm text-muted tracking-widest uppercase">
            {METRIC_LABELS[metric]} by {DIM_LABELS[groupBy]}
          </h2>
          <p className="text-xs text-muted mt-1">
            {compare ? `${comparisonPeriod} compared with ${period}` : `${period} total`}. Hover for exact values; select a bar for raw usage lines.
          </p>
          <div className="mt-5 space-y-3">
            {displayedRows.map((row, index) => {
              const label = String(row[groupBy] ?? "no-prefix");
              const raw = compare ? (row as DeltaRow).delta : (row as SliceRow).value;
              const width = maxBar ? (Math.abs(raw) / maxBar) * 100 : 0;
              const rising = raw >= 0;
              const detail = compare
                ? `${format((row as DeltaRow).value_a)} → ${format((row as DeltaRow).value_b)}`
                : format(raw);
              return (
                <button
                  key={`${groupBy}:${label}:${index}`}
                  onClick={() => drillInto(row, period)}
                  title={detail}
                  className="w-full text-left group"
                >
                  <div className="flex justify-between gap-4 text-sm mb-1">
                    <span className="truncate group-hover:text-accent transition-colors">{label}</span>
                    <span className={`num shrink-0 ${compare && rising ? "text-rose-300" : "text-accent"}`}>
                      {compare && (rising ? "+" : "")}{format(raw)}
                      {compare && (row as DeltaRow).pct != null ? ` (${rising ? "+" : ""}${(row as DeltaRow).pct?.toFixed(0)}%)` : ""}
                    </span>
                  </div>
                  <div className="h-2 rounded-full bg-line overflow-hidden">
                    <div className={`h-full rounded-full ${compare && rising ? "bg-rose-400" : "bg-accent"}`} style={{ width: `${width}%` }} />
                  </div>
                  {compare && <p className="text-[11px] text-muted mt-1 num">{detail}</p>}
                </button>
              );
            })}
          </div>
        </section>
      )}

      {drill && selected && (
        <section className="mt-8 border border-line rounded-2xl bg-card p-6">
          <h2 className="text-sm text-muted tracking-widest uppercase">Raw usage lines — {selected}</h2>
          <p className="text-xs text-muted mt-1">Top 30 rows by on-demand cost in {period}.</p>
          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-xs">
              <thead><tr className="border-b border-line text-muted text-left"><th className="py-2 pr-3">time</th><th className="py-2 pr-3">account</th><th className="py-2 pr-3">usage</th><th className="py-2 pr-3">instance</th><th className="py-2 text-right">on-demand</th></tr></thead>
              <tbody>{drill.map((row, index) => <tr key={`${row.timestamp}:${row.usage_type}:${index}`} className="border-b border-line/50"><td className="py-2 pr-3 num">{row.timestamp.replace("T", " ").slice(0, 16)}</td><td className="py-2 pr-3 num">{row.account_id.slice(0, 8)}…</td><td className="py-2 pr-3 max-w-72 truncate">{row.usage_type}</td><td className="py-2 pr-3">{row.instance_type ?? "—"}</td><td className="py-2 text-right num">{money.format(row.on_demand_cost)}</td></tr>)}</tbody>
            </table>
          </div>
        </section>
      )}
      <AssistantDrawer context={{
        screen: "explorer",
        group_by: groupBy,
        metric,
        period,
        compare,
        comparison_period: comparisonPeriod,
        selected_dimension: selected ? groupBy : undefined,
        selected_value: selected ?? undefined,
      }} />
    </main>
  );
}

function Select({ label, value, onChange, options, labels, prefix }: { label: string; value: string; onChange: (value: string) => void; options: string[]; labels: Record<string, string>; prefix: string }) {
  return <label className="block"><span className="text-xs text-muted uppercase tracking-wider">{label}</span><select value={value} onChange={(event) => onChange(event.target.value)} className="mt-1 block bg-background border border-line rounded-lg px-3 py-2 text-sm outline-none focus:border-accent">{options.map((option, index) => <option key={`${prefix}:${option}:${index}`} value={option}>{labels[option] ?? option}</option>)}</select></label>;
}

function periodLabels(vocab: Vocab | null): Record<string, string> {
  return Object.fromEntries((vocab?.periods ?? []).map((entry) => [entry.period, `${entry.period}${entry.partial ? " (partial)" : ""}`]));
}
