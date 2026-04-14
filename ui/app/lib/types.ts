export interface SlackUser {
  user_id: string;
  display_name: string;
  role?: string;
  channel_ids: string[];
}

export interface SlackChannel {
  channel_id: string;
  name: string;
  topic?: string;
  member_ids: string[];
}

export interface SlackMessage {
  message_id: string;
  thread_id: string;
  channel_id: string;
  user_id: string;
  display_name: string;
  text: string;
  timestamp: string;
  is_thread_root: boolean;
  reaction_counts: Record<string, number>;
  reply_count: number;
  mentions: string[];
}

export interface RankingFeatures {
  user_affinity: number;
  importance: number;
  urgency: number;
  momentum: number;
  novelty: number;
  recency: number;
  embedding_affinity: number;
  weights: Record<string, number>;
  final_score: number;
}

export interface RankedDigestItem {
  event_id: string;
  title: string;
  summary?: string;
  why_shown?: string;
  signal_level: 'high' | 'medium' | 'low';
  event_type: string;
  confidence: number;
  score: number;
  reason_features: RankingFeatures;
  source_thread_ids: string[];
  source_message_ids: string[];
}

export interface ExcludedDigestItem {
  event_id: string;
  title: string;
  score: number;
  top_exclusion_reason: string;
}

export interface DailyDigest {
  user_id: string;
  date: string;
  headline: string;
  items: RankedDigestItem[];
  generated_at: string;
  total_candidates_considered: number;
  llm_used: boolean;
  excluded_items: ExcludedDigestItem[];
}

export interface ThreadDetail {
  thread_id: string;
  channel_id: string;
  channel_name: string;
  started_at: string;
  last_activity_at: string;
  messages: SlackMessage[];
}

export interface WorkspaceData {
  users: SlackUser[];
  channels: SlackChannel[];
}
