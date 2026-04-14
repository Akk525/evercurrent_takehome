'use client';

import { SlackMessage } from '../lib/types';
import Avatar from './Avatar';

interface MessageItemProps {
  message: SlackMessage;
  onThreadClick?: (threadId: string) => void;
  isThreadReply?: boolean;
}

function formatTime(ts: string): string {
  return new Date(ts).toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
}

export default function MessageItem({
  message,
  onThreadClick,
  isThreadReply = false,
}: MessageItemProps) {
  return (
    <div
      className={`flex gap-2.5 px-4 py-0.5 hover:bg-[#f8f8f8] group transition-colors ${
        isThreadReply ? 'pl-14' : ''
      }`}
    >
      {/* Avatar column */}
      <div className="w-8 flex-shrink-0 pt-0.5">
        {!isThreadReply ? (
          <Avatar name={message.display_name} size="md" />
        ) : (
          /* Indent placeholder for replies */
          <span className="block w-8" />
        )}
      </div>

      <div className="flex-1 min-w-0 pb-1">
        {/* Name + timestamp — timestamp reveals on hover */}
        <div className="flex items-baseline gap-1.5">
          <span className="font-bold text-[14px] text-[#1d1c1d]">{message.display_name}</span>
          <span className="text-[11px] text-gray-400 opacity-0 group-hover:opacity-100 transition-opacity">
            {formatTime(message.timestamp)}
          </span>
        </div>

        {/* Message text */}
        <p className="text-[14px] text-[#1d1c1d] leading-snug whitespace-pre-wrap break-words">
          {message.text}
        </p>

        {/* Reactions */}
        {Object.keys(message.reaction_counts).length > 0 && (
          <div className="flex gap-1 mt-1 flex-wrap">
            {Object.entries(message.reaction_counts).map(([emoji, count]) => (
              <span
                key={emoji}
                className="inline-flex items-center gap-0.5 bg-[#f8f8f8] hover:bg-[#e8e8e8] border border-gray-200 rounded px-1.5 py-0.5 text-[12px] cursor-pointer transition-colors"
              >
                {emoji}
                <span className="text-[#1264a3] font-medium ml-0.5">{count}</span>
              </span>
            ))}
          </div>
        )}

        {/* Thread reply indicator — Slack style */}
        {message.is_thread_root && message.reply_count > 0 && onThreadClick && (
          <button
            onClick={() => onThreadClick(message.thread_id)}
            className="mt-1.5 flex items-center gap-1.5 group/reply"
          >
            {/* Small avatar-like dots to suggest participants */}
            <span className="flex -space-x-1">
              <span className="w-4 h-4 rounded bg-blue-400 border border-white" />
              {message.reply_count > 1 && (
                <span className="w-4 h-4 rounded bg-purple-400 border border-white" />
              )}
            </span>
            <span className="text-[12px] text-[#1264a3] font-semibold group-hover/reply:underline">
              {message.reply_count} {message.reply_count === 1 ? 'reply' : 'replies'}
            </span>
          </button>
        )}
      </div>
    </div>
  );
}
