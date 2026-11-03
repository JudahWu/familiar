import { MessageSquare } from 'lucide-react';
import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import type { Thread } from '@/lib/agents/types';
import { useStore } from '../../store';

function getTimeGroup(timestamp: number): string {
  const now = Date.now();
  const diff = now - timestamp;
  const days = Math.floor(diff / (1000 * 60 * 60 * 24));

  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return `${days}d ago`;
  if (days < 14) return '1w ago';
  return `${Math.floor(days / 7)}w ago`;
}

function groupThreadsByTime(threads: Thread[]): { group: string; threads: Thread[] }[] {
  const map = new Map<string, Thread[]>();
  const sorted = [...threads].sort((a, b) => b.updatedAt - a.updatedAt);

  for (const thread of sorted) {
    const group = getTimeGroup(thread.updatedAt);
    const existing = map.get(group) || [];
    map.set(group, [...existing, thread]);
  }

  return Array.from(map.entries()).map(([group, threads]) => ({ group, threads }));
}

interface ThreadPickerProps {
  projectId: string;
  onSelect: (thread: Thread) => void;
  onClose: () => void;
}

export function ThreadPicker({ projectId, onSelect, onClose }: ThreadPickerProps) {
  const tabs = useStore((state) => state.tabs);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [search, setSearch] = useState('');
  const listRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const activeSessionIds = useMemo(
    () => new Set(Object.values(tabs).map((tab) => tab.sessionId)),
    [tabs],
  );

  useEffect(() => {
    window.storage.loadThreadsForProject(projectId).then((loaded) => {
      setThreads(
        loaded
          .filter((thread) => !activeSessionIds.has(thread.id))
          .sort((a, b) => b.updatedAt - a.updatedAt),
      );
      setLoading(false);
    });
  }, [projectId, activeSessionIds]);

  const filtered = search
    ? threads.filter((t) => t.title.toLowerCase().includes(search.toLowerCase()))
    : threads;

  const groups = groupThreadsByTime(filtered);
  const flatList = groups.flatMap((g) => g.threads);

  // Reset selection when search changes
  useEffect(() => {
    setSelectedIndex(0);
  }, [search]);

  // Scroll selected item into view
  useEffect(() => {
    const selected = listRef.current?.querySelector('[data-selected="true"]');
    selected?.scrollIntoView({ block: 'nearest' });
  }, [selectedIndex]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        onClose();
        return;
      }

      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSelectedIndex((prev) => Math.min(prev + 1, flatList.length - 1));
        return;
      }

      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSelectedIndex((prev) => Math.max(prev - 1, 0));
        return;
      }

      if (e.key === 'Enter' && flatList.length > 0) {
        e.preventDefault();
        onSelect(flatList[selectedIndex]);
        return;
      }
    },
    [flatList, selectedIndex, onSelect, onClose],
  );

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown, true);
    return () => document.removeEventListener('keydown', handleKeyDown, true);
  }, [handleKeyDown]);

  // Auto-focus search
  useEffect(() => {
    searchRef.current?.focus();
  }, [loading]);

  const messageCount = (thread: Thread) => {
    const count = thread.messages.filter((m) => m.author === 'user' || m.author === 'agent').length;
    return `${count} msg${count !== 1 ? 's' : ''}`;
  };

  const formatDate = (timestamp: number) => {
    const d = new Date(timestamp);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };

  let currentFlatIndex = 0;

  return (
    <div className="absolute inset-0 z-40 flex flex-col items-center bg-black/30 pt-16 px-4 backdrop-blur-[2px]">
      <div className="flex max-h-[70vh] w-full max-w-lg flex-col overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)] shadow-2xl">
        <div className="shrink-0 border-b border-[var(--border)] p-3">
          <input
            ref={searchRef}
            type="text"
            placeholder="Search conversations..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full rounded-md border border-[var(--border)] bg-[var(--muted)] px-3 py-2 text-sm text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] focus:border-[var(--primary)] focus:outline-none"
          />
        </div>

        <div ref={listRef} className="flex-1 overflow-y-auto">
          {loading ? (
            <div className="p-6 text-center text-sm text-[var(--muted-foreground)]">Loading...</div>
          ) : flatList.length === 0 ? (
            <div className="p-6 text-center text-sm text-[var(--muted-foreground)]">
              {search ? 'No matching conversations' : 'No previous conversations for this project'}
            </div>
          ) : (
            groups.map(({ group, threads: groupThreads }) => (
              <div key={group}>
                <div className="sticky top-0 bg-[var(--card)] px-4 py-2 text-xs font-medium text-[var(--muted-foreground)]">
                  {group}
                </div>
                {groupThreads.map((thread) => {
                  const idx = currentFlatIndex++;
                  const isSelected = idx === selectedIndex;

                  return (
                    <button
                      key={thread.id}
                      data-selected={isSelected}
                      onClick={() => onSelect(thread)}
                      className={`flex w-full items-center gap-3 px-4 py-3 text-left transition-colors ${
                        isSelected ? 'bg-[var(--sidebar-accent)]' : 'hover:bg-[var(--muted)]'
                      }`}
                    >
                      <ChatIcon />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm text-[var(--foreground)]">{thread.title}</div>
                        <div className="mt-0.5 text-xs text-[var(--muted-foreground)]">
                          {messageCount(thread)} — {formatDate(thread.updatedAt)}
                        </div>
                      </div>
                      {isSelected && (
                        <span className="shrink-0 text-xs text-[var(--muted-foreground)]">Enter</span>
                      )}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>

        <div className="flex shrink-0 gap-4 border-t border-[var(--border)] px-4 py-2 text-xs text-[var(--muted-foreground)]">
          <span><kbd className="rounded bg-[var(--secondary)] px-1 py-0.5 text-[var(--foreground)]">Up/Down</kbd> navigate</span>
          <span><kbd className="rounded bg-[var(--secondary)] px-1 py-0.5 text-[var(--foreground)]">Enter</kbd> select</span>
          <span><kbd className="rounded bg-[var(--secondary)] px-1 py-0.5 text-[var(--foreground)]">Esc</kbd> cancel</span>
        </div>
      </div>
    </div>
  );
}

function ChatIcon() {
  return <MessageSquare size={14} strokeWidth={2} className="flex-shrink-0 text-[var(--muted-foreground)]" />;
}
