"use client";

import { useState } from "react";
import type { Economics } from "@/lib/api";

const fmt = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

export function SavingsSplit({ econ }: { econ: Economics }) {
  const total = econ.customer_savings + econ.reserve + econ.profit;
  const parts = [
    { label: "You keep", value: econ.customer_savings, color: "#34d399", detail: "your guaranteed share" },
    { label: "Risk reserve", value: econ.reserve, color: "#5b6470", detail: `${econ.cost_of_risk_points} points · we eat the downside if usage falls` },
    { label: "Our profit", value: econ.profit, color: "#2a343d", detail: "10% of net savings" },
  ];
  const [hover, setHover] = useState<number | null>(null);

  const r = 70;
  const c = 2 * Math.PI * r;
  let offset = 0;

  return (
    <div className="flex items-center gap-8">
      <div className="relative w-44 h-44 shrink-0">
        <svg viewBox="0 0 180 180" className="w-full h-full -rotate-90">
          <circle cx="90" cy="90" r={r} fill="none" stroke="#1f2426" strokeWidth="22" />
          {parts.map((p, i) => {
            const len = total > 0 ? (p.value / total) * c : 0;
            const seg = (
              <circle
                key={p.label}
                cx="90"
                cy="90"
                r={r}
                fill="none"
                stroke={p.color}
                strokeWidth="22"
                strokeDasharray={`${len} ${c - len}`}
                strokeDashoffset={-offset}
                onMouseEnter={() => setHover(i)}
                onMouseLeave={() => setHover(null)}
                className="cursor-pointer transition-opacity"
                opacity={hover === null || hover === i ? 1 : 0.35}
              />
            );
            offset += len;
            return seg;
          })}
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center text-center">
          <span className="text-2xl font-semibold text-accent num">
            {(econ.customer_savings_rate * 100).toFixed(1)}%
          </span>
          <span className="text-xs text-muted">guaranteed</span>
        </div>
      </div>
      <div className="space-y-2 text-sm">
        {parts.map((p, i) => (
          <div
            key={p.label}
            className={`flex items-center gap-2 cursor-default ${hover === i ? "" : ""}`}
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover(null)}
          >
            <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ background: p.color }} />
            <span>
              <span className="font-medium">{p.label}</span>{" "}
              <span className="num">{fmt.format(p.value)}</span>
              {hover === i && (
                <span className="block text-xs text-muted mt-0.5">{p.detail}</span>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
