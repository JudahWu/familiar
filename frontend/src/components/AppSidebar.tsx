import { Link } from "@tanstack/react-router";
import { BooksIcon, MoonIcon, SunIcon } from "@phosphor-icons/react";

import type { SessionUser } from "@/lib/api";
import { SidebarUserMenu } from "@/components/SidebarUserMenu";
import { SidebarCollapseButton, SidebarFrame, useSidebarLayout } from "@/components/sidebar-layout";
import { BrandLockup, BrandRailMark } from "@/components/BrandLockup";
import { useTheme } from "@/lib/theme";

export function AppSidebar({ user }: { user: SessionUser }) {
  const layout = useSidebarLayout();
  const { theme, setTheme } = useTheme();

  return (
    <SidebarFrame
      {...layout}
      className="border-r border-[var(--ui-border)] bg-[var(--ui-sidebar)]"
      rail={
        <div className="flex flex-col items-center gap-1">
          <BrandRailMark />
          <button
            type="button"
            aria-label="展开侧栏"
            onClick={layout.toggle}
            className="mt-auto flex size-8 items-center justify-center rounded-md text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]"
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3">
              <rect x="2" y="3" width="12" height="10" rx="2" />
              <path d="M6.4 3v10" />
            </svg>
          </button>
        </div>
      }
    >
      <div className="flex items-center gap-2 px-3 pt-4 pb-3">
        <BrandLockup />
        <div className="ml-auto flex items-center gap-0.5">
          <button
            type="button"
            aria-label={theme === "dark" ? "切换到浅色" : "切换到深色"}
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            className="flex size-5 items-center justify-center rounded text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]"
          >
            {theme === "dark" ? <SunIcon className="size-3.5" /> : <MoonIcon className="size-3.5" />}
          </button>
          <SidebarCollapseButton onToggle={layout.toggle} />
        </div>
      </div>

      <nav className="px-2 pt-1">
        <Link
          to="/knowledge"
          className="flex items-center gap-2 rounded-md px-2 py-1.5 text-[12.5px] text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]"
          activeProps={{
            className:
              "flex items-center gap-2 rounded-md px-2 py-1.5 text-[12.5px] bg-[var(--ui-sidebar-hover)] text-[var(--ui-text)]",
          }}
        >
          <BooksIcon className="size-3.5" />
          知识库
        </Link>
      </nav>

      <div className="flex-1" />

      <div className="border-t border-[var(--ui-border)] p-2">
        <SidebarUserMenu user={user} />
      </div>
    </SidebarFrame>
  );
}
