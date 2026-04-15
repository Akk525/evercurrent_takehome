'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { fetchDMMessages, sendDMMessage, fetchWorkspace } from '../lib/api';
import { DMMessage, SlackUser } from '../lib/types';
import { useWorkspace } from '../context/WorkspaceContext';
import Avatar from './Avatar';
import MessageComposer from './MessageComposer';

interface DMViewProps {
  userId: string; // the other person's user ID
}

function formatTime(ts: string): string {
  return new Date(ts).toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
}

function formatDaySeparator(ts: string): string {
  return new Date(ts).toLocaleDateString('en-US', {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
  });
}

export default function DMView({ userId }: DMViewProps) {
  const { currentUser } = useWorkspace();
  const [messages, setMessages] = useState<DMMessage[]>([]);
  const [otherUser, setOtherUser] = useState<SlackUser | null>(null);
  const [loading, setLoading] = useState(true);
  const bottomRef = useRef<HTMLDivElement>(null);
  const prevCountRef = useRef(0);

  // Load the other user's info
  useEffect(() => {
    fetchWorkspace()
      .then(data => {
        const found = data.users.find((u: SlackUser) => u.user_id === userId);
        setOtherUser(found ?? null);
      })
      .catch(console.error);
  }, [userId]);

  const poll = useCallback(async () => {
    try {
      const data = await fetchDMMessages(userId, currentUser);
      setMessages(prev => {
        if (prev.length !== data.messages.length) return data.messages;
        return prev;
      });
    } catch {
      // silently ignore poll errors
    } finally {
      setLoading(false);
    }
  }, [userId, currentUser]);

  // Initial fetch + polling
  useEffect(() => {
    setLoading(true);
    setMessages([]);
    prevCountRef.current = 0;
    poll();
    const interval = setInterval(poll, 1500);
    return () => clearInterval(interval);
  }, [poll]);

  // Scroll to bottom when new messages arrive
  useEffect(() => {
    if (messages.length > prevCountRef.current) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
      prevCountRef.current = messages.length;
    }
  }, [messages.length]);

  async function handleSend(text: string) {
    try {
      const msg = await sendDMMessage(userId, currentUser, text);
      setMessages(prev => [...prev, msg]);
    } catch (err) {
      console.error('Send failed', err);
    }
  }

  const otherName = otherUser?.display_name ?? userId;

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-4 py-2.5 border-b border-gray-200 flex-shrink-0 flex items-center gap-2">
        <Avatar name={otherName} size="sm" />
        <h2 className="font-bold text-[#1d1c1d] text-[15px]">{otherName}</h2>
        {otherUser?.role && (
          <>
            <span className="text-gray-300 text-sm">|</span>
            <span className="text-[13px] text-gray-500">{otherUser.role}</span>
          </>
        )}
      </div>

      {/* Message list */}
      <div className="flex-1 overflow-y-auto pt-4 pb-2">
        {loading && (
          <div className="px-4 py-8 text-center text-gray-400 text-sm">
            Loading messages...
          </div>
        )}

        {!loading && messages.length === 0 && (
          <div className="px-6 py-8">
            <div className="flex items-center gap-3 mb-3">
              <Avatar name={otherName} size="lg" />
            </div>
            <p className="font-bold text-[#1d1c1d] text-lg">{otherName}</p>
            {otherUser?.role && (
              <p className="text-sm text-gray-500 mt-0.5">{otherUser.role}</p>
            )}
            <p className="text-[13px] text-gray-500 mt-3">
              This is the beginning of your direct message history with{' '}
              <span className="font-semibold">{otherName}</span>.
            </p>
          </div>
        )}

        {!loading && messages.length > 0 && (
          <>
            {/* Day separator */}
            <div className="flex items-center gap-3 px-4 mb-3">
              <div className="flex-1 h-px bg-gray-200" />
              <span className="text-[11px] text-gray-500 font-medium">
                {formatDaySeparator(messages[0].timestamp)}
              </span>
              <div className="flex-1 h-px bg-gray-200" />
            </div>

            {messages.map(msg => {
              const isSelf = msg.sender_id === currentUser;
              const senderName = isSelf ? 'You' : otherName;
              return (
                <div
                  key={msg.message_id}
                  className="flex gap-2.5 px-4 py-0.5 hover:bg-[#f8f8f8] group transition-colors"
                >
                  <div className="flex-shrink-0 w-9 pt-0.5">
                    <Avatar name={isSelf ? (currentUser) : otherName} size="sm" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-baseline gap-2">
                      <span className="font-bold text-[14px] text-[#1d1c1d]">
                        {senderName}
                      </span>
                      <span className="text-[11px] text-gray-400 opacity-0 group-hover:opacity-100 transition-opacity">
                        {formatTime(msg.timestamp)}
                      </span>
                    </div>
                    <p className="text-[14px] text-[#1d1c1d] leading-[1.46668] break-words">
                      {msg.text}
                    </p>
                  </div>
                </div>
              );
            })}
          </>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Composer */}
      <MessageComposer
        placeholder={`Message ${otherName}`}
        onSend={handleSend}
      />
    </div>
  );
}
