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
