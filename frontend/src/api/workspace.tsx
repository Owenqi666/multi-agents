import { createContext, useContext, type ReactNode } from 'react';
import { api } from './client';
import { useRead } from './reads';
import type { Workspace } from './types';

function useWorkspaceRead(enabled = true) {
  return useRead<Workspace>('workspace', () => api.workspace(), { locking: false, enabled });
}

const WorkspaceContext = createContext<ReturnType<typeof useWorkspaceRead> | null>(null);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const workspace = useWorkspaceRead();
  return <WorkspaceContext.Provider value={workspace}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace() {
  const shared = useContext(WorkspaceContext);
  const local = useWorkspaceRead(shared === null);
  return shared ?? local;
}

export function useArchiveMode() {
  return useContext(WorkspaceContext)?.data?.read_only === true;
}

export function useWorkspaceWritesAllowed() {
  const workspace = useContext(WorkspaceContext);
  return workspace === null || workspace.data?.read_only === false;
}
