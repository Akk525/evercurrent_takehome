'use client';

import { createContext, useContext, useState } from 'react';

interface WorkspaceContextValue {
  currentUser: string;
  setCurrentUser: (id: string) => void;
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const [currentUser, setCurrentUser] = useState('u_alice');
  return (
    <WorkspaceContext.Provider value={{ currentUser, setCurrentUser }}>
      {children}
    </WorkspaceContext.Provider>
  );
}

export function useWorkspace() {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) throw new Error('useWorkspace must be used within WorkspaceProvider');
  return ctx;
}
