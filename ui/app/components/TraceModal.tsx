'use client';

import { useState, useEffect } from 'react';
import { fetchEventTrace } from '../lib/api';
import type { EventPipelineTrace, StageTrace } from '../lib/types';

interface TraceModalProps {
  eventId: string;
  eventTitle: string;
  onClose: () => void;
}

const STAGE_META: Record<string, { icon: string; color: string }> = {
  candidate:     { icon: '◎', color: 'text-gray-500' },
  enrichment:    { icon: '◈', color: 'text-blue-600' },
  issue_linking: { icon: '⬡', color: 'text-indigo-600' },
  issue_memory:  { icon: '◷', color: 'text-purple-600' },
  ownership:     { icon: '◉', color: 'text-teal-600' },
  drift:         { icon: '◬', color: 'text-orange-500' },
  graph:         { icon: '◈', color: 'text-red-500' },
};

const TOP_DRIVER_LABELS: Record<string, string> = {
  importance: 'Importance',
  urgency: 'Urgency',
  momentum: 'Momentum',
  novelty: 'Novelty',
  user_affinity: 'User affinity',
  embedding_affinity: 'Semantic affinity',
};

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (typeof v === 'number') return v.toFixed(3);
  if (Array.isArray(v)) return v.length === 0 ? '—' : v.join(', ');
  return String(v);
}

function ScoreBar({ value, max = 1 }: { value: number; max?: number }) {
  const pct = Math.min((value / max) * 100, 100);
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-gray-100 rounded-full overflow-hidden">
        <div
          className="h-full bg-[#1264a3] rounded-full transition-all"
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-[10px] font-mono text-gray-500 w-10 text-right">
        {value.toFixed(3)}
      </span>
    </div>
  );
}

function StageCard({ stage, index }: { stage: StageTrace; index: number }) {
  const [open, setOpen] = useState(index <= 1);
  const meta = STAGE_META[stage.name] ?? { icon: '○', color: 'text-gray-400' };

  const statusDot =
    stage.status === 'active'
      ? 'bg-green-400'
      : stage.status === 'empty'
      ? 'bg-yellow-300'
      : 'bg-gray-200';

  const outputEntries = Object.entries(stage.outputs).filter(
    ([, v]) => v !== null && v !== undefined,
  );

  return (
    <div className="relative pl-6">
      {/* Vertical connector line */}
      <div className="absolute left-2.5 top-0 bottom-0 w-px bg-gray-200" />

      {/* Stage dot */}
      <div
        className={`absolute left-1 top-3.5 w-3 h-3 rounded-full border-2 border-white ${statusDot} z-10`}
      />

      <div className="mb-3">
        <button
          onClick={() => setOpen(!open)}
          className="w-full text-left group"
        >
          <div className="flex items-center gap-2 py-2 px-3 rounded-lg hover:bg-gray-50 transition-colors">
            <span className={`text-sm font-mono ${meta.color}`}>{meta.icon}</span>
            <span className="text-[12px] font-semibold text-gray-700 flex-1">
              {stage.label}
            </span>
            {stage.score_delta > 0 && (
              <span className="text-[10px] font-mono text-green-600 bg-green-50 px-1.5 py-0.5 rounded">
                +{stage.score_delta.toFixed(4)}
              </span>
            )}
            <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium
              ${stage.status === 'active' ? 'bg-green-50 text-green-700' :
                stage.status === 'empty' ? 'bg-yellow-50 text-yellow-700' :
                'bg-gray-100 text-gray-300'}`}>
              {stage.status}
            </span>
            <span className="text-gray-300 text-xs">{open ? '▾' : '▸'}</span>
          </div>
        </button>

        {open && outputEntries.length > 0 && (
          <div className="mx-3 mb-2 px-3 py-2.5 bg-gray-50 rounded-lg border border-gray-100">
            <div className="space-y-1.5">
              {outputEntries.map(([key, value]) => (
                <div key={key} className="grid grid-cols-[140px_1fr] gap-2 text-[11px]">
                  <span className="text-gray-400 text-right">{key.replace(/_/g, ' ')}</span>
                  <span className="text-gray-700 font-mono break-all">
                    {formatValue(value)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default function TraceModal({ eventId, eventTitle, onClose }: TraceModalProps) {
  const [trace, setTrace] = useState<EventPipelineTrace | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchEventTrace(eventId)
      .then(setTrace)
      .catch(() => setError('Could not load trace.'))
      .finally(() => setLoading(false));
  }, [eventId]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-end"
      onClick={e => e.target === e.currentTarget && onClose()}
    >
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/20" onClick={onClose} />

      {/* Panel */}
      <div className="relative z-10 w-[480px] h-full bg-white shadow-2xl flex flex-col">
        {/* Header */}
        <div className="flex items-start gap-3 px-4 py-3.5 border-b border-gray-200 flex-shrink-0">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <span className="text-[11px] font-mono text-gray-400 uppercase tracking-wide">
                Decision trace
              </span>
            </div>
            <p className="text-[13px] font-semibold text-[#1d1c1d] truncate mt-0.5">
              {eventTitle}
            </p>
            <p className="text-[11px] text-gray-400 font-mono mt-0.5">{eventId}</p>
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 text-lg leading-none flex-shrink-0 mt-0.5"
          >
            ×
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto">
          {loading && (
            <div className="flex items-center justify-center h-40 text-sm text-gray-400">
              Loading trace...
            </div>
          )}
          {error && (
            <div className="flex items-center justify-center h-40 text-sm text-red-400">
              {error}
            </div>
          )}

          {trace && (
            <>
              {/* Score summary */}
              <div className="px-4 py-3 border-b border-gray-100 bg-gray-50">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-[11px] font-semibold text-gray-500 uppercase tracking-wide">
                    Final relevance score
                  </span>
                  <span className="text-[13px] font-mono font-bold text-[#1264a3]">
                    {trace.final_score.toFixed(3)}
                  </span>
                </div>
                <ScoreBar value={trace.final_score} />
                <p className="text-[11px] text-gray-400 mt-2">
                  Top driver:{' '}
                  <span className="font-semibold text-gray-600">
                    {TOP_DRIVER_LABELS[trace.top_driver] ?? trace.top_driver}
                  </span>{' '}
                  ({trace.top_driver_value.toFixed(3)} weighted contribution)
                </p>
              </div>

              {/* Text preview */}
              <div className="px-4 py-2.5 border-b border-gray-100">
                <p className="text-[11px] text-gray-400 uppercase tracking-wide font-semibold mb-1">
                  Source text
                </p>
                <p className="text-[11px] text-gray-600 leading-relaxed line-clamp-3 font-mono">
                  {trace.text_preview}
                  {trace.text_preview.length >= 200 ? '…' : ''}
                </p>
              </div>

              {/* Pipeline stages */}
              <div className="px-2 pt-3 pb-6">
                <p className="text-[11px] text-gray-400 uppercase tracking-wide font-semibold mb-3 px-2">
                  Pipeline stages
                </p>
                {trace.stages.map((stage, i) => (
                  <StageCard key={stage.name} stage={stage} index={i} />
                ))}

                {/* Terminal node */}
                <div className="relative pl-6">
                  <div className="absolute left-2.5 top-0 h-4 w-px bg-gray-200" />
                  <div className="absolute left-1 top-3.5 w-3 h-3 rounded-full bg-[#1264a3] border-2 border-white z-10" />
                  <div className="py-2 px-3">
                    <span className="text-[12px] font-semibold text-[#1264a3]">
                      Ranked into digest
                    </span>
                    <span className="ml-2 text-[11px] font-mono text-gray-500">
                      score = {trace.final_score.toFixed(3)}
                    </span>
                  </div>
                </div>
              </div>

              <div className="px-4 py-2 border-t border-gray-100">
                <p className="text-[10px] text-gray-300 font-mono">
                  Generated {new Date(trace.generated_at).toLocaleTimeString()}
                </p>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
