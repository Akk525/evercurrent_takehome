'use client';

import { useState, useEffect } from 'react';
import { fetchChannelMessages } from '../lib/api';
import { SlackMessage } from '../lib/types';
import MessageItem from './MessageItem';
import ThreadModal from './ThreadModal';
import MessageComposer from './MessageComposer';

interface MessageFeedProps {
  channelId: string;
}

function formatDaySeparator(ts: string): string {
  const d = new Date(ts);
  return d.toLocaleDateString('en-US', {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
  });
}

export default function MessageFeed({ channelId }: MessageFeedProps) {
  const [messages, setMessages] = useState<SlackMessage[]>([]);
  const [channelName, setChannelName] = useState('');
  const [topic, setTopic] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeThreadId, setActiveThreadId] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchChannelMessages(channelId)
      .then(data => {
        setMessages(data.messages);
        setChannelName(data.name);
        setTopic(data.topic || '');
      })
      .catch(() => setError('Could not load messages. Is the API server running?'))
      .finally(() => setLoading(false));
  }, [channelId]);

  const rootMessages = messages.filter(m => m.is_thread_root);

  return (
    <div className="flex flex-col h-full">
      {/* Channel header */}
      <div className="px-4 py-2.5 border-b border-gray-200 flex-shrink-0 flex items-center gap-2">
        <span className="text-[#1d1c1d] font-bold text-[18px] leading-none">#</span>
        <h2 className="font-bold text-[#1d1c1d] text-[15px]">{channelName || channelId}</h2>
        {topic && (
          <>
            <span className="text-gray-300 text-sm">|</span>
            <span className="text-[13px] text-gray-500">{topic}</span>
          </>
        )}
      </div>

      {/* Message list */}
      <div className="flex-1 overflow-y-auto pt-4 pb-2">
        {loading && (
          <div className="px-4 py-8 text-center text-gray-400 text-sm">Loading messages...</div>
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
        {!loading && !error && rootMessages.length === 0 && (
          <div className="px-4 py-8 text-center text-gray-400 text-sm">
            No messages in this channel.
          </div>
        )}

        {!loading && !error && rootMessages.length > 0 && (
          <>
            {/* Day separator — single group since mock data is one day */}
            <div className="flex items-center gap-3 px-4 mb-3">
              <div className="flex-1 h-px bg-gray-200" />
              <span className="text-[11px] text-gray-500 font-medium">
                {formatDaySeparator(rootMessages[0].timestamp)}
              </span>
              <div className="flex-1 h-px bg-gray-200" />
            </div>

            {rootMessages.map(msg => (
              <MessageItem
                key={msg.message_id}
                message={msg}
                onThreadClick={setActiveThreadId}
              />
            ))}
          </>
        )}
      </div>

      {/* Composer */}
      <MessageComposer placeholder={`Message #${channelName || channelId}`} />

      {/* Thread panel */}
      {activeThreadId && (
        <ThreadModal
          threadId={activeThreadId}
          onClose={() => setActiveThreadId(null)}
        />
      )}
    </div>
  );
}
