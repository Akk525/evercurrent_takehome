'use client';

import { useState, useEffect } from 'react';
import { fetchDigest, fetchWorkspace } from '../lib/api';
import { DailyDigest, SlackUser } from '../lib/types';
import DigestItem from './DigestItem';
import MessageComposer from './MessageComposer';

interface DigestViewProps {
  userId: string;
}

function formatDigestTime(iso: string): string {
  return new Date(iso).toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
}

function formatDigestDate(dateStr: string): string {
  // "2026-04-10" → "April 10"
  const d = new Date(dateStr + 'T12:00:00');
  return d.toLocaleDateString('en-US', { month: 'long', day: 'numeric' });
}

function introLine(digest: DailyDigest, user: SlackUser | null): string {
  const name = user?.display_name?.split(' ')[0] || 'there';
  const date = formatDigestDate(digest.date);
  return `Hey ${name} — here's your daily digest for ${date}.`;
}

function headlineLine(digest: DailyDigest): string {
  const n = digest.items.length;
  const total = digest.total_candidates_considered;
  return `I scanned ${total} threads and surfaced ${n} item${n !== 1 ? 's' : ''} worth your attention. ${digest.headline}`;
}

/** Digest Bot avatar — consistent "DB" initials in purple */
function BotAvatar() {
  return (
    <div className="w-8 h-8 bg-[#4A154B] rounded flex items-center justify-center text-white text-[11px] font-bold flex-shrink-0 leading-none">
      DB
    </div>
  );
}

export default function DigestView({ userId }: DigestViewProps) {
  const [digest, setDigest] = useState<DailyDigest | null>(null);
  const [user, setUser] = useState<SlackUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const [digestData, workspaceData] = await Promise.all([
          fetchDigest(userId),
          fetchWorkspace(),
        ]);
        if (!cancelled) {
          setDigest(digestData);
          setUser(
            workspaceData.users.find((u: SlackUser) => u.user_id === userId) || null,
          );
          setError(null);
        }
      } catch {
        if (!cancelled) setError('Could not load digest. Is the API server running?');
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    poll();
    const id = setInterval(poll, 10000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [userId]);

  return (
    <div className="flex flex-col h-full">
      {/* DM header */}
      <div className="px-4 py-2.5 border-b border-gray-200 flex-shrink-0 flex items-center gap-2">
        <BotAvatar />
        <div>
          <h2 className="font-bold text-[#1d1c1d] text-[15px] leading-tight">Digest Bot</h2>
          <p className="text-[12px] text-gray-500 leading-tight">
            App &middot; Personalized for {user?.display_name || userId}
          </p>
        </div>
      </div>

      {/* Message timeline */}
      <div className="flex-1 overflow-y-auto">
        {loading && (
          <div className="px-4 py-8 text-center text-gray-400 text-sm">Loading digest...</div>
        )}
        {error && (
          <div className="px-4 py-8 text-center">
            <p className="text-red-500 text-sm">{error}</p>
            <p className="text-gray-400 text-xs mt-1">
              Start the API:{' '}
              <code className="bg-gray-100 px-1 rounded">
                uvicorn api.server:app --reload --port 8000
              </code>
            </p>
          </div>
        )}

        {!loading && !error && digest && (
          <>
            {/* Day separator */}
            <div className="flex items-center gap-3 px-4 py-4">
              <div className="flex-1 h-px bg-gray-200" />
              <span className="text-[11px] text-gray-500 font-medium">
                {formatDigestDate(digest.date)}
              </span>
              <div className="flex-1 h-px bg-gray-200" />
            </div>

            {/* Bot message 1: greeting */}
            <BotMessage time={formatDigestTime(digest.generated_at)}>
              <p className="text-[14px] text-[#1d1c1d] leading-snug">
                {introLine(digest, user)}
              </p>
              <p className="text-[14px] text-[#1d1c1d] leading-snug mt-1">
                {headlineLine(digest)}
              </p>
            </BotMessage>

            {/* Bot message 2: digest items as attachment list */}
            <BotMessage time={formatDigestTime(digest.generated_at)} showAvatar={false}>
              <div className="mt-0.5">
                {digest.items.map((item, i) => (
                  <DigestItem key={item.event_id} item={item} rank={i + 1} />
                ))}
              </div>

              {/* Excluded items — subtle footnote */}
              {digest.excluded_items.length > 0 && (
                <details className="mt-3">
                  <summary className="text-[11px] text-gray-400 cursor-pointer hover:text-gray-600 select-none">
                    {digest.excluded_items.length} other threads considered but not selected
                  </summary>
                  <div className="mt-1.5 space-y-0.5 pl-1">
                    {digest.excluded_items.slice(0, 5).map(ex => (
                      <div key={ex.event_id} className="text-[11px] text-gray-400">
                        <span className="font-medium text-gray-500">{ex.title}</span>
                        <span className="ml-1.5">— {ex.top_exclusion_reason.split(';')[0]}</span>
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </BotMessage>
          </>
        )}
      </div>

      {/* Composer */}
      <MessageComposer placeholder="Message Digest Bot" />
    </div>
  );
}

/** Reusable bot message row */
function BotMessage({
  children,
  time,
  showAvatar = true,
}: {
  children: React.ReactNode;
  time: string;
  showAvatar?: boolean;
}) {
  return (
    <div className="flex gap-2.5 px-4 py-0.5 hover:bg-[#f8f8f8] group">
      {/* Avatar column — always takes the same width for alignment */}
      <div className="w-8 flex-shrink-0 flex flex-col items-center pt-0.5">
        {showAvatar ? <BotAvatar /> : null}
      </div>

      <div className="flex-1 min-w-0 pb-1">
        {showAvatar && (
          <div className="flex items-baseline gap-2 mb-0.5">
            <span className="font-bold text-[14px] text-[#1d1c1d]">Digest Bot</span>
            <span className="text-[10px] bg-[#1d7a5c] text-white px-1 py-px rounded-sm font-medium">
              APP
            </span>
            <span className="text-[11px] text-gray-400 opacity-0 group-hover:opacity-100 transition-opacity">
              {time}
            </span>
          </div>
        )}
        {children}
      </div>
    </div>
  );
}
