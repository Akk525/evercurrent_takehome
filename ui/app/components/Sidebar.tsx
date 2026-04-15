'use client';

import { useState, useEffect } from 'react';
import { useRouter, usePathname } from 'next/navigation';
import { fetchWorkspace } from '../lib/api';
import { WorkspaceData } from '../lib/types';
import Avatar from './Avatar';
import { useWorkspace } from '../context/WorkspaceContext';

export default function Sidebar() {
  const [workspace, setWorkspace] = useState<WorkspaceData | null>(null);
  const [showUserMenu, setShowUserMenu] = useState(false);
  const { currentUser, setCurrentUser } = useWorkspace();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    fetchWorkspace().then(setWorkspace).catch(console.error);
  }, []);

  const isChannelActive = (channelId: string) =>
    pathname === `/workspace/${channelId}`;
  const isDMActive = (userId: string) =>
    pathname === `/workspace/dm/${userId}`;
  const isAnyDigestActive = pathname?.includes('/workspace/digest/');

  const currentUserData = workspace?.users.find(u => u.user_id === currentUser);

  return (
    <div className="w-64 bg-slack-purple flex flex-col h-full text-white select-none">
      {/* Workspace header */}
      <div className="px-4 py-3 border-b border-purple-800 flex items-center justify-between">
        <div>
          <h1 className="font-bold text-white text-base leading-tight">EverCurrent HW</h1>
          <p className="text-purple-300 text-xs mt-0.5">Hardware Engineering</p>
        </div>
        <span className="text-purple-300 text-lg">&#x2304;</span>
      </div>

      {/* Scrollable nav */}
      <nav className="flex-1 overflow-y-auto py-2">
        {/* Digest Bot DM — pinned at top */}
        <div className="px-2 mb-2">
          <button
            onClick={() => router.push(`/workspace/digest/${currentUser}`)}
            className={`w-full flex items-center gap-2 px-2 py-1.5 rounded text-sm font-semibold transition-colors ${
              isAnyDigestActive
                ? 'bg-slack-blue text-white'
                : 'text-purple-200 hover:bg-slack-purple-dark'
            }`}
          >
            <span className="w-5 h-5 bg-[#4A154B] rounded text-white text-[10px] font-bold flex items-center justify-center flex-shrink-0 leading-none">
              DB
            </span>
            <span>Digest Bot</span>
          </button>
        </div>

        {/* Channels */}
        <div className="px-4 py-1 text-[11px] font-bold text-purple-300 uppercase tracking-wider mb-0.5 flex items-center justify-between">
          <span>Channels</span>
          <span className="text-purple-400 cursor-pointer hover:text-white font-normal text-base leading-none">+</span>
        </div>
        {workspace?.channels.map(channel => (
          <div key={channel.channel_id} className="px-2">
            <button
              onClick={() => router.push(`/workspace/${channel.channel_id}`)}
              className={`w-full flex items-center gap-1.5 px-2 py-0.5 rounded text-[14px] transition-colors ${
                isChannelActive(channel.channel_id)
                  ? 'bg-slack-blue text-white'
                  : 'text-purple-200 hover:bg-slack-purple-dark'
              }`}
            >
              <span className="text-purple-400 text-[13px]">#</span>
              <span className="truncate">{channel.name}</span>
            </button>
          </div>
        ))}

        {/* Direct Messages */}
        <div className="px-4 py-1 mt-4 text-[11px] font-bold text-purple-300 uppercase tracking-wider mb-0.5 flex items-center justify-between">
          <span>Direct Messages</span>
          <span className="text-purple-400 cursor-pointer hover:text-white font-normal text-base leading-none">+</span>
        </div>
        {workspace?.users.map(user => (
          <div key={user.user_id} className="px-2">
            <button
              onClick={() => router.push(`/workspace/dm/${user.user_id}`)}
              className={`w-full flex items-center gap-2 px-2 py-0.5 rounded text-[14px] transition-colors ${
                isDMActive(user.user_id)
                  ? 'bg-slack-blue text-white'
                  : 'text-purple-200 hover:bg-slack-purple-dark'
              }`}
            >
              <span className="relative flex-shrink-0">
                <span className="w-2 h-2 bg-green-400 rounded-full absolute -bottom-0 -right-0 border border-slack-purple" />
                <Avatar name={user.display_name} size="sm" />
              </span>
              <span className="truncate">{user.display_name}</span>
            </button>
          </div>
        ))}
      </nav>

      {/* Current user footer */}
      <div className="px-3 py-3 border-t border-purple-800">
        <button
          onClick={() => setShowUserMenu(!showUserMenu)}
          className="w-full flex items-center gap-2 p-1 rounded hover:bg-slack-purple-dark transition-colors"
        >
          <Avatar name={currentUserData?.display_name || 'User'} size="sm" />
          <div className="flex-1 text-left overflow-hidden">
            <p className="text-sm font-medium text-white truncate">{currentUserData?.display_name || currentUser}</p>
            <p className="text-xs text-purple-300 truncate">{currentUserData?.role || 'Team Member'}</p>
          </div>
          <span className="text-purple-300 text-xs">&#x2304;</span>
        </button>

        {showUserMenu && (
          <div className="mt-1 bg-white rounded shadow-lg border border-gray-200 overflow-hidden">
            <div className="px-3 py-2 text-xs text-gray-500 border-b border-gray-100">Switch user (demo)</div>
            {workspace?.users.map(user => (
              <button
                key={user.user_id}
                onClick={() => {
                  setCurrentUser(user.user_id);
                  setShowUserMenu(false);
                }}
                className={`w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-gray-50 transition-colors ${
                  user.user_id === currentUser ? 'bg-blue-50 text-blue-700' : 'text-gray-700'
                }`}
              >
                <Avatar name={user.display_name} size="sm" />
                <div className="text-left">
                  <p className="font-medium">{user.display_name}</p>
                  <p className="text-xs text-gray-400">{user.role}</p>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
