import { Link, useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { GearIcon, SignOutIcon } from "@phosphor-icons/react";

import type {SessionUser} from "@/lib/api";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import {  api } from "@/lib/api";
import { cn } from "@/lib/utils";

export function SidebarUserMenu({ user }: { user: SessionUser }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onClickOutside = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClickOutside);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClickOutside);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const onLogout = async () => {
    setOpen(false);
    await api.logout();
    qc.setQueryData(["session"], null);
    navigate({ to: "/login" });
  };

  const initials = (user.login || "?").slice(0, 2).toUpperCase();

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left outline-none transition-colors hover:bg-sidebar-accent"
      >
        <Avatar className="size-6">
          {user.avatar_url && <AvatarImage src={user.avatar_url} alt={user.login} />}
          <AvatarFallback>{initials}</AvatarFallback>
        </Avatar>
        <span className="min-w-0 flex-1 truncate text-xs text-sidebar-foreground">
          {user.login}
        </span>
        <span className="shrink-0 text-[10px] text-muted-foreground">⌄</span>
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 bottom-full left-0 z-50 mb-2 overflow-hidden rounded-md border border-border bg-popover p-1 text-popover-foreground"
        >
          <Link
            to="/my-settings"
            role="menuitem"
            onClick={() => setOpen(false)}
            className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-xs/relaxed transition-colors hover:bg-muted"
          >
            <GearIcon className="size-3.5" />
            设置
          </Link>
          <button
            type="button"
            role="menuitem"
            onClick={() => void onLogout()}
            className={cn(
              "flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-xs/relaxed",
              "text-destructive transition-colors hover:bg-muted",
            )}
          >
            <SignOutIcon className="size-3.5" />
            退出登录
          </button>
        </div>
      )}
    </div>
  );
}
