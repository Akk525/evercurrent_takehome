'use client';

import { useState, useEffect } from 'react';
import { fetchThread } from '../lib/api';
import { ThreadDetail, SlackMessage } from '../lib/types';
import Avatar from './Avatar';
import MessageComposer from './MessageComposer';

interface ThreadModalProps {
  threadId: string;
  onClose: () => void;
}

function formatTime(ts: string): string {
  return new Date(ts).toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
}

function ThreadMessage({
  msg,
  isRoot,
}: {
  msg: SlackMessage;
  isRoot: boolean;
}) {
  return (
    <div className={`flex gap-2.5 px-4 py-1 hover:bg-[#f8f8f8] group transition-colors`}>
      <div className="w-8 flex-shrink-0 pt-0.5">
        <Avatar name={msg.display_name} size={isRoot ? 'md' : 'sm'} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-1.5">
          <span className={`font-bold text-[#1d1c1d] ${isRoot ? 'text-[14px]' : 'text-[13px]'}`}>
            {msg.display_name}
          </span>
          <span className="text-[11px] text-gray-400 opacity-0 group-hover:opacity-100 transition-opacity">
            {formatTime(msg.timestamp)}
          </span>
        </div>
        <p
          className={`text-[#1d1c1d] leading-snug whitespace-pre-wrap break-words ${
            isRoot ? 'text-[14px]' : 'text-[13px]'
          }`}
        >
          {msg.text}
        </p>
        {Object.keys(msg.reaction_counts).length > 0 && (
          <div className="flex gap-1 mt-1 flex-wrap">
            {Object.entries(msg.reaction_counts).map(([emoji, count]) => (
              <span
                key={emoji}
                className="inline-flex items-center gap-0.5 bg-[#f8f8f8] border border-gray-200 rounded px-1.5 py-0.5 text-[12px] cursor-pointer hover:bg-[#e8e8e8] transition-colors"
              >
                {emoji}
                <span className="text-[#1264a3] font-medium ml-0.5">{count}</span>
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default function ThreadModal({ threadId, onClose }: ThreadModalProps) {
  const [thread, setThread] = useState<ThreadDetail | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    fetchThread(threadId)
      .then(setThread)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [threadId]);

  const [root, ...replies] = thread?.messages ?? [];

  return (
    <div className="fixed inset-0 z-50 flex" onClick={onClose}>
      {/* Scrim */}
      <div className="flex-1 bg-black/10" />

      {/* Panel */}
      <div
        className="w-[420px] bg-white shadow-2xl flex flex-col h-full border-l border-gray-200"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-4 py-3 border-b border-gray-200 flex items-center justify-between flex-shrink-0">
          <div>
            <h3 className="font-bold text-[#1d1c1d] text-[15px]">Thread</h3>
            {thread && (
              <p className="text-[12px] text-gray-500">#{thread.channel_name}</p>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-700 text-xl leading-none w-8 h-8 flex items-center justify-center rounded hover:bg-gray-100 transition-colors"
          >
            ×
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto py-3">
          {loading && (
            <div className="px-4 py-8 text-center text-gray-400 text-sm">Loading thread...</div>
          )}

          {!loading && root && (
            <>
              {/* Root message */}
              <ThreadMessage msg={root} isRoot />

              {/* Replies separator */}
              {replies.length > 0 && (
                <div className="flex items-center gap-3 px-4 py-3">
                  <span className="text-[11px] text-gray-500 font-semibold">
                    {replies.length} {replies.length === 1 ? 'reply' : 'replies'}
                  </span>
                  <div className="flex-1 h-px bg-gray-200" />
                </div>
              )}

              {/* Replies */}
              {replies.map(msg => (
                <ThreadMessage key={msg.message_id} msg={msg} isRoot={false} />
              ))}
            </>
          )}
        </div>

        {/* Reply composer */}
        <MessageComposer placeholder="Reply…" />
      </div>
    </div>
  );
}
