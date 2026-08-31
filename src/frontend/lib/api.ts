export interface Economics {
  proposal: { scope: string; commitment_per_hour: number; term_months: number; payment_option: string };
  covered_on_demand_cost: number;
  committed_cost: number;
  gross_savings: number;
  wasted_commitment: number;
  net_savings: number;
  cost_of_risk_points: number;
  protection: number;
  trend: number;
  infeasible: boolean;
  profit: number;
  reserve: number;
  customer_savings: number;
  customer_savings_rate: number;
}

export interface NarrationItem {
  title: string;
  body: string;
}

export interface Period {
  period: string;
  partial: boolean;
  first_ts: string;
  last_ts: string;
}

export interface Vocab {
  dims: string[];
  metrics: string[];
  periods: Period[];
  complete_periods: string[];
}

export interface DeltaRow {
  [dim: string]: string | number | null;
  value_a: number;
  value_b: number;
  delta: number;
  pct: number | null;
}

export interface SliceRow {
  [dim: string]: string | number | null;
  value: number;
}

export interface DrillRow {
  timestamp: string;
  account_id: string;
  product_code: string;
  usage_type: string;
  instance_type: string | null;
  commitment_key: string | null;
  usage_amount: number;
  on_demand_cost: number;
}

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8001";

export async function fetchVocab(): Promise<Vocab> {
  const res = await fetch(`${API}/vocab`, { cache: "no-store" });
  if (!res.ok) throw new Error(`/vocab ${res.status}`);
  return res.json();
}

export async function fetchDelta(body: {
  group_by: string;
  metric: string;
  window_a: [string, string];
  window_b: [string, string];
  top_n: number;
}): Promise<{ rows: DeltaRow[]; group_by: string; metric: string; window_a: string[]; window_b: string[] }> {
  const res = await fetch(`${API}/delta`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`/delta ${res.status}`);
  return res.json();
}

export async function fetchSlice(body: {
  group_by: string;
  metric: string;
  window: [string, string] | null;
  top_n: number;
}): Promise<{ rows: SliceRow[]; group_by: string; metric: string; window: string[] | null }> {
  const res = await fetch(`${API}/slice`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`/slice ${res.status}`);
  return res.json();
}

export async function fetchDrill(body: {
  group_by: string;
  value: string;
  window: [string, string] | null;
  limit: number;
}): Promise<{ rows: DrillRow[]; group_by: string; value: string }> {
  const res = await fetch(`${API}/drill`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`/drill ${res.status}`);
  return res.json();
}

export async function savingsReady(): Promise<boolean> {
  const res = await fetch(`${API}/impact-status`, { cache: "no-store" });
  if (!res.ok) return false;
  return (await res.json()).ready;
}

export async function warmSavings(): Promise<void> {
  const res = await fetch(`${API}/warm`, { method: "POST" });
  if (!res.ok) throw new Error(`/warm ${res.status}`);
}

export interface AskEvent {
  route?: string;
  params?: unknown;
  kind?: string;
  data?: unknown;
  text?: string;
  delta?: string;
  numbers?: Record<string, number>;
  steps?: number;
  latency_ms?: number;
  usage?: { total_tokens: number; total_cost_usd: number };
}

export interface AssistantEvent {
  message?: string;
  sql?: string;
  rows?: number;
  delta?: string;
  latency_ms?: number;
  usage?: { total_tokens: number; total_cost_usd: number };
  intent?: string;
  query_count?: number;
}

export async function fetchEconomics(): Promise<Economics> {
  const res = await fetch(`${API}/economics`, { cache: "no-store" });
  if (!res.ok) throw new Error(`/economics ${res.status}`);
  return res.json();
}

export async function evaluateCommitment(commitment_per_hour: number): Promise<Economics> {
  const res = await fetch(`${API}/econ/economics`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ commitment_per_hour }),
  });
  if (!res.ok) throw new Error(`/econ/economics ${res.status}`);
  return res.json();
}

export async function fetchNarration(): Promise<NarrationItem[]> {
  const res = await fetch(`${API}/narration`, { cache: "no-store" });
  if (!res.ok) return [];
  const data = await res.json();
  return data.items;
}

export async function ask(question: string, onEvent: (e: AskEvent) => void, signal?: AbortSignal) {
  const res = await fetch(`${API}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`/ask ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const eventLine = chunk.split("\n").find((l) => l.startsWith("event: "));
      const dataLine = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (!eventLine || !dataLine) continue;
      const name = eventLine.slice(7).trim();
      const data = JSON.parse(dataLine.slice(6));
      onEvent({ ...data, _event: name } as AskEvent);
    }
  }
}

export async function investigate(
  question: string,
  context: Record<string, unknown>,
  onEvent: (event: string, data: AssistantEvent) => void,
  signal?: AbortSignal,
) {
  const res = await fetch(`${API}/assistant`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, context }),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`/assistant ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index;
    while ((index = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      const eventLine = block.split("\n").find((line) => line.startsWith("event: "));
      const dataLine = block.split("\n").find((line) => line.startsWith("data: "));
      if (!eventLine || !dataLine) continue;
      onEvent(eventLine.slice(7).trim(), JSON.parse(dataLine.slice(6)));
    }
  }
}
