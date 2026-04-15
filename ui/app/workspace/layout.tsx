import Sidebar from '../components/Sidebar';
import { WorkspaceProvider } from '../context/WorkspaceContext';

export default function WorkspaceLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <WorkspaceProvider>
      <div className="flex h-screen overflow-hidden bg-white">
        <Sidebar />
        <main className="flex-1 overflow-hidden flex flex-col">{children}</main>
      </div>
    </WorkspaceProvider>
  );
}
