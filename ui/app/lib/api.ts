import type { DMMessage } from './types';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export async function fetchWorkspace() {
  const res = await fetch(`${API_URL}/api/workspace`);
  if (!res.ok) throw new Error('Failed to fetch workspace');
  return res.json();
}

export async function fetchChannelMessages(channelId: string) {
  const res = await fetch(`${API_URL}/api/channels/${channelId}/messages`);
  if (!res.ok) throw new Error(`Failed to fetch messages for ${channelId}`);
  return res.json();
}

export async function fetchThread(threadId: string) {
  const res = await fetch(`${API_URL}/api/threads/${threadId}`);
  if (!res.ok) throw new Error(`Failed to fetch thread ${threadId}`);
  return res.json();
}

export async function fetchDigest(userId: string) {
  const res = await fetch(`${API_URL}/api/digest/${userId}`);
  if (!res.ok) throw new Error(`Failed to fetch digest for ${userId}`);
  return res.json();
}

export async function fetchDMMessages(
  otherUserId: string,
  asUserId: string,
): Promise<{ messages: DMMessage[] }> {
  const res = await fetch(
    `${API_URL}/api/dm/${otherUserId}?as=${encodeURIComponent(asUserId)}`,
    { cache: 'no-store' },
  );
  if (!res.ok) throw new Error('Failed to fetch DMs');
  return res.json();
}

export async function sendDMMessage(
  otherUserId: string,
  asUserId: string,
  text: string,
): Promise<DMMessage> {
  const res = await fetch(
    `${API_URL}/api/dm/${otherUserId}?as=${encodeURIComponent(asUserId)}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    },
  );
  if (!res.ok) throw new Error('Failed to send DM');
  return res.json();
}
