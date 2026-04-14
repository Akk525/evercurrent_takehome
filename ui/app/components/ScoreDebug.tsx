'use client';

import { useState } from 'react';
import { RankingFeatures } from '../lib/types';

interface ScoreDebugProps {
  features: RankingFeatures;
}

const FEATURE_LABELS: Record<string, string> = {
  user_affinity: 'User Affinity',
  importance: 'Importance',
  urgency: 'Urgency',
  momentum: 'Momentum',
  novelty: 'Novelty',
  recency: 'Recency',
  embedding_affinity: 'Semantic Affinity',
};

const FEATURE_COLORS: Record<string, string> = {
  user_affinity: 'bg-purple-400',
  importance: 'bg-red-400',
  urgency: 'bg-orange-400',
  momentum: 'bg-blue-400',
  novelty: 'bg-green-400',
  recency: 'bg-teal-400',
  embedding_affinity: 'bg-indigo-400',
};

export default function ScoreDebug({ features }: ScoreDebugProps) {
  const [open, setOpen] = useState(false);

  const featureKeys = ['user_affinity', 'importance', 'urgency', 'momentum', 'novelty', 'recency', 'embedding_affinity'] as const;

  return (
    <div className="mt-2">
      <button
        onClick={() => setOpen(!open)}
        className="text-xs text-gray-400 hover:text-gray-600 flex items-center gap-1"
      >
        <span>{open ? '▾' : '▸'}</span>
        <span>Debug scores</span>
        <span className="ml-1 font-mono text-gray-500">score={features.final_score.toFixed(3)}</span>
      </button>

      {open && (
        <div className="mt-2 p-3 bg-gray-50 rounded border border-gray-200">
          <div className="space-y-1.5">
            {featureKeys.map(key => {
              const value = features[key] as number;
              const weight = features.weights[key] ?? 0;
              return (
                <div key={key} className="grid grid-cols-[120px_1fr_40px] gap-2 items-center text-xs">
                  <span className="text-gray-600 text-right">{FEATURE_LABELS[key]}</span>
                  <div className="relative h-3 bg-gray-200 rounded-full overflow-hidden">
                    <div
                      className={`absolute left-0 top-0 h-full rounded-full ${FEATURE_COLORS[key]}`}
                      style={{ width: `${Math.min(value * 100, 100)}%` }}
                    />
                  </div>
                  <span className="text-gray-500 font-mono">{value.toFixed(2)}</span>
                </div>
              );
            })}
          </div>
          <div className="mt-2 pt-2 border-t border-gray-200 text-xs text-gray-500 font-mono">
            Final score: {features.final_score.toFixed(3)}
          </div>
        </div>
      )}
    </div>
  );
}
