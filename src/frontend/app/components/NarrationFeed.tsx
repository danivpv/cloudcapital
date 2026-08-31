"use client";

import { fetchNarration, type NarrationItem } from "@/lib/api";
import { useEffect, useState } from "react";

export function NarrationFeed() {
  const [items, setItems] = useState<NarrationItem[]>([]);

  useEffect(() => {
    fetchNarration().then(setItems).catch(() => {});
  }, []);

  if (items.length === 0) return null;

  return (
    <div>
      <h2 className="text-sm text-muted tracking-widest uppercase">What&apos;s happening</h2>
      <div className="mt-4 grid md:grid-cols-2 gap-4">
        {items.map((it) => (
          <div key={it.title} className="border border-line rounded-xl p-5 bg-card">
            <p className="text-sm font-medium">{it.title}</p>
            <p className="text-sm text-muted mt-1.5">{it.body}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
