'use client';

import { useState } from 'react';
import { RankedDigestItem } from '../lib/types';
import ScoreDebug from './ScoreDebug';
import ThreadModal from './ThreadModal';
import TraceModal from './TraceModal';

interface DigestItemProps {
  item: RankedDigestItem;
  rank: number;
}

const SIGNAL_BORDER: Record<string, string> = {
  high: 'border-l-red-500',
  medium: 'border-l-yellow-400',
  low: 'border-l-green-500',
};

const SIGNAL_DOT: Record<string, string> = {
  high: 'bg-red-500',
  medium: 'bg-yellow-400',
  low: 'bg-green-500',
};

const EVENT_TYPE_LABEL: Record<string, string> = {
  blocker: 'Blocker',
  risk: 'Risk',
  decision: 'Decision',
  status_update: 'Status update',
  request_for_input: 'Input needed',
  noise: 'General',
};

export default function DigestItem({ item, rank }: DigestItemProps) {
  const [activeThreadId, setActiveThreadId] = useState<string | null>(null);
  const [showTrace, setShowTrace] = useState(false);

  return (
    <>
      {/* Slack-style attachment: left accent bar only, hover bg, no outer box */}
      <div className={`border-l-4 ${SIGNAL_BORDER[item.signal_level]} pl-3 pr-2 py-2 my-0.5 hover:bg-gray-50 rounded-r group transition-colors`}>

        {/* Title row */}
        <div className="flex items-start gap-1.5">
          <span
            className={`mt-1.5 w-1.5 h-1.5 rounded-full flex-shrink-0 ${SIGNAL_DOT[item.signal_level]}`}
            title={`${item.signal_level} signal`}
          />
          <div className="flex-1 min-w-0">
            <div className="flex items-baseline gap-2 flex-wrap">
              <span className="font-semibold text-[13px] text-[#1d1c1d] leading-snug">{item.title}</span>
              <span className="text-[11px] text-gray-400 font-normal">
                {EVENT_TYPE_LABEL[item.event_type] || item.event_type}
              </span>
            </div>

            {/* Summary */}
            {item.summary && (
              <p className="text-[13px] text-gray-700 leading-snug mt-0.5">{item.summary}</p>
            )}

            {/* Why shown */}
            {item.why_shown && (
              <p className="text-[12px] text-gray-400 mt-0.5 italic">{item.why_shown}</p>
            )}

            {/* Footer: metadata + view thread */}
            <div className="flex items-center gap-3 mt-1.5 flex-wrap">
              <span className="text-[11px] text-gray-400">
                {(item.confidence * 100).toFixed(0)}% confidence
              </span>
              {item.source_thread_ids.length > 0 && (
                <button
                  onClick={() => setActiveThreadId(item.source_thread_ids[0])}
                  className="text-[11px] text-[#1264a3] hover:underline"
                >
                  View thread →
                </button>
              )}
              <button
                onClick={() => setShowTrace(true)}
                className="text-[11px] text-gray-400 hover:text-gray-600 hover:underline"
              >
                Decision trace →
              </button>
            </div>

            {/* Debug panel */}
            <ScoreDebug features={item.reason_features} />
          </div>

          {/* Rank badge — subtle, right-aligned */}
          <span className="text-[10px] text-gray-300 font-mono flex-shrink-0 mt-0.5">#{rank}</span>
        </div>
      </div>

      {activeThreadId && (
        <ThreadModal
          threadId={activeThreadId}
          onClose={() => setActiveThreadId(null)}
        />
      )}
      {showTrace && (
        <TraceModal
          eventId={item.event_id}
          eventTitle={item.title}
          onClose={() => setShowTrace(false)}
        />
      )}
    </>
  );
}
