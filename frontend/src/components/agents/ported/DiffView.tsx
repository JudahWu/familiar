// @ts-nocheck — ported from open-swe-app (Electron).
import { useEffect, useMemo, useRef, useState } from 'react';
import { diffLines } from 'diff';
import {  getSingletonHighlighter } from 'shiki';
import type {ThemedToken} from 'shiki';
import type { DiffData } from '@/lib/agents/types';
import { useTheme } from '@/lib/theme';

interface DiffViewProps {
  diffData: DiffData;
}

// Map file extensions to shiki language IDs
function getLanguageFromPath(filePath: string): string {
  const ext = filePath.split('.').pop()?.toLowerCase() ?? '';
  const map: Record<string, string> = {
    ts: 'typescript', tsx: 'tsx', js: 'javascript', jsx: 'jsx',
    py: 'python', rb: 'ruby', rs: 'rust', go: 'go', java: 'java',
    kt: 'kotlin', swift: 'swift', c: 'c', cpp: 'cpp', cs: 'csharp',
    html: 'html', css: 'css', scss: 'scss', json: 'json',
    yaml: 'yaml', yml: 'yaml', toml: 'toml', md: 'markdown', mdx: 'mdx',
    sh: 'bash', bash: 'bash', zsh: 'bash', fish: 'fish', sql: 'sql',
    xml: 'xml', php: 'php', r: 'r', lua: 'lua', ex: 'elixir',
    exs: 'elixir', elm: 'elm', clj: 'clojure', hs: 'haskell',
    scala: 'scala', vue: 'vue', svelte: 'svelte', graphql: 'graphql',
    tf: 'hcl', hcl: 'hcl', ini: 'ini',
  };
  const filename = filePath.split('/').pop()?.toLowerCase() ?? '';
  if (filename === 'dockerfile') return 'dockerfile';
  if (filename === 'makefile') return 'makefile';
  return map[ext] ?? 'text';
}

// Highlighted line cache: maps "lang::lineText" -> ThemedToken[]
type TokenCache = Map<string, Array<ThemedToken>>;

// Renders a line of text as highlighted token spans, falling back to plain text
function HighlightedLine({ tokens, fallback, isAdd, isRemove }: {
  tokens: Array<ThemedToken> | null;
  fallback: string;
  isAdd: boolean;
  isRemove: boolean;
}) {
  if (!tokens) {
    return (
      <span className={isAdd ? 'text-success' : isRemove ? 'text-destructive' : 'text-muted-foreground'}>
        {fallback}
      </span>
    );
  }
  return (
    <>
      {tokens.map((token, i) => (
        <span key={i} style={{ color: token.color }}>
          {token.content}
        </span>
      ))}
    </>
  );
}

const CONTEXT_LINES = 3;
const MAX_COLLAPSED_LINES = 20;

type DiffLineData = {
  type: 'context' | 'remove' | 'add' | 'separator';
  text: string;
  oldLineNum?: number;
  newLineNum?: number;
};

function toLineArray(text: string): Array<string> {
  if (text.length === 0) return [];
  const lines = text.split('\n');
  if (text.endsWith('\n')) lines.pop();
  return lines;
}

function computeDiffLines(
  originalContent: string | null,
  newContent: string
): Array<DiffLineData> {
  const result: Array<DiffLineData> = [];
  const parts = diffLines(originalContent ?? '', newContent, {
    ignoreWhitespace: false,
    newlineIsToken: false,
  });

  let oldLineNum = 1;
  let newLineNum = 1;

  for (const part of parts) {
    const lines = toLineArray(part.value);

    if (part.added) {
      for (const line of lines) {
        result.push({
          type: 'add',
          text: line,
          newLineNum,
        });
        newLineNum += 1;
      }
      continue;
    }

    if (part.removed) {
      for (const line of lines) {
        result.push({
          type: 'remove',
          text: line,
          oldLineNum,
        });
        oldLineNum += 1;
      }
      continue;
    }

    for (const line of lines) {
      result.push({
        type: 'context',
        text: line,
        oldLineNum,
        newLineNum,
      });
      oldLineNum += 1;
      newLineNum += 1;
    }
  }

  return result;
}

function filterToHunks(lines: Array<DiffLineData>, contextLines: number = CONTEXT_LINES): Array<DiffLineData> {
  const changeIndices: Array<number> = [];
  lines.forEach((line, idx) => {
    if (line.type === 'add' || line.type === 'remove') {
      changeIndices.push(idx);
    }
  });

  if (changeIndices.length === 0) {
    return [];
  }

  const includeSet = new Set<number>();
  for (const idx of changeIndices) {
    for (let i = Math.max(0, idx - contextLines); i <= Math.min(lines.length - 1, idx + contextLines); i++) {
      includeSet.add(i);
    }
  }

  const result: Array<DiffLineData> = [];
  let lastIncluded = -2;

  for (let i = 0; i < lines.length; i++) {
    if (includeSet.has(i)) {
      if (lastIncluded >= 0 && i - lastIncluded > 1) {
        result.push({ type: 'separator', text: '···' });
      }
      result.push(lines[i]);
      lastIncluded = i;
    }
  }

  return result;
}

export function DiffView({ diffData }: DiffViewProps) {
  const [expanded, setExpanded] = useState(false);
  const [tokenCache, setTokenCache] = useState<TokenCache | null>(null);
  const highlightingRef = useRef(false);
  // 语法高亮必须跟随主题：深色用 github-dark，浅色用 github-light，
  // 否则浅色主题下会出现白底浅色代码，基本看不清。
  const { resolvedTheme } = useTheme();
  const shikiTheme = resolvedTheme === 'dark' ? 'github-dark' : 'github-light';

  const { originalContent, newContent, filePath, isNewFile, isBinary } = diffData;
  const language = getLanguageFromPath(filePath);

  const allDiffLines = useMemo(
    () => computeDiffLines(originalContent, newContent),
    [originalContent, newContent]
  );

  const hunkLines = useMemo(
    () => filterToHunks(allDiffLines),
    [allDiffLines]
  );

  const stats = useMemo(() => {
    let additions = 0;
    let deletions = 0;
    for (const line of allDiffLines) {
      if (line.type === 'add') additions++;
      else if (line.type === 'remove') deletions++;
    }
    return { additions, deletions };
  }, [allDiffLines]);

  // Tokenize all visible lines for syntax highlighting
  useEffect(() => {
    if (language === 'text' || isBinary) return;
    if (highlightingRef.current) return;
    highlightingRef.current = true;

    const linesToHighlight = hunkLines.filter(l => l.type !== 'separator');
    // 主题切换后必须丢弃旧缓存，否则会继续渲染上一套主题的 token 颜色。
    setTokenCache(null);

    void (async () => {
      try {
        const highlighter = await getSingletonHighlighter({
          themes: ['github-dark', 'github-light'],
           
          langs: [language],
        });
        // 单例可能由其他组件先用单一主题创建，这里补齐当前主题。
        if (!highlighter.getLoadedThemes().includes(shikiTheme)) {
          await highlighter.loadTheme(shikiTheme);
        }

        const cache: TokenCache = new Map();
        for (const line of linesToHighlight) {
          const cacheKey = `${language}::${line.text}`;
          if (cache.has(cacheKey)) continue;
          try {
            const result = highlighter.codeToTokens(line.text, {
               
              lang: language as any,
              theme: shikiTheme,
            });
            cache.set(cacheKey, result.tokens[0] ?? []);
          } catch {
            // Skip lines that fail tokenization
          }
        }
        setTokenCache(cache);
      } catch (err: unknown) {
        console.warn('[diff-view] Syntax highlighting failed:', err);
      } finally {
        highlightingRef.current = false;
      }
    })();
  }, [hunkLines, language, isBinary, shikiTheme]);

  if (isBinary) {
    return (
      <div className="mt-2 text-muted-foreground text-xs font-mono">
        Binary file - diff not available
      </div>
    );
  }

  const displayLines = expanded ? hunkLines : hunkLines.slice(0, MAX_COLLAPSED_LINES);
  const hasMoreLines = hunkLines.length > MAX_COLLAPSED_LINES;
  const hiddenCount = hunkLines.length - MAX_COLLAPSED_LINES;

  if (hunkLines.length === 0) {
    return (
      <div className="mt-2 text-muted-foreground text-xs font-mono">
        No changes
      </div>
    );
  }

  return (
    <div className="mt-2 font-mono text-xs">
      <div className="flex items-center gap-2 text-muted-foreground mb-1">
        <span className="text-muted-foreground">{filePath.split('/').pop()}</span>
        {isNewFile && <span>(new)</span>}
        <span className="text-success">+{stats.additions}</span>
        <span className="text-destructive">-{stats.deletions}</span>
      </div>

      <div className="max-h-60 overflow-auto border-l border-border pl-2">
        {displayLines.map((line, idx) => {
          if (line.type === 'separator') {
            return (
              <div key={idx} className="py-0.5 text-muted-foreground">
                {line.text}
              </div>
            );
          }

          const isAdd = line.type === 'add';
          const isRemove = line.type === 'remove';
          const cacheKey = `${language}::${line.text}`;
          const tokens = tokenCache?.get(cacheKey) ?? null;

          return (
            <div
              key={idx}
              className={`whitespace-pre ${
                isAdd ? 'bg-success/10' :
                isRemove ? 'bg-destructive/10' : ''
              }`}
            >
              <span className="inline-block w-8 pr-2 text-right text-muted-foreground">
                {line.oldLineNum || line.newLineNum || ''}
              </span>
              <span className={`inline-block w-4 ${
                isAdd ? 'text-success' :
                isRemove ? 'text-destructive' : 'text-muted-foreground'
              }`}>
                {isAdd ? '+' : isRemove ? '-' : ' '}
              </span>
              <HighlightedLine tokens={tokens} fallback={line.text} isAdd={isAdd} isRemove={isRemove} />
            </div>
          );
        })}
      </div>

      {hasMoreLines && !expanded && (
        <button
          onClick={() => setExpanded(true)}
          className="mt-1 text-xs text-primary hover:underline"
        >
          +{hiddenCount} more lines
        </button>
      )}
      {expanded && hasMoreLines && (
        <button
          onClick={() => setExpanded(false)}
          className="mt-1 text-xs text-primary hover:underline"
        >
          Show less
        </button>
      )}
    </div>
  );
}
