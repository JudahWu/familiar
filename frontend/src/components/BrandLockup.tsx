import { Link } from "@tanstack/react-router";

/** 品牌标识：Familiar 小精灵徽标 + 产品名。 */

interface BrandLockupProps {
  /** 折叠态只显示徽标，不显示名称。 */
  collapsed?: boolean;
  className?: string;
}

export function BrandLockup({ collapsed = false, className }: BrandLockupProps) {
  return (
    <Link
      to="/agents"
      aria-label="Familiar"
      className={`flex items-center gap-2 ${className ?? ""}`}
    >
      <img
        src="/logo-mark.png"
        alt=""
        width={24}
        height={24}
        className="size-6 shrink-0 rounded-[6px]"
      />
      {!collapsed && (
        <span className="text-[13px] font-medium tracking-tight text-[var(--ui-text)]">
          Familiar
        </span>
      )}
    </Link>
  );
}

/** 侧栏折叠成图标栏时使用的 32px 徽标。 */
export function BrandRailMark() {
  return (
    <Link
      to="/agents"
      aria-label="Familiar"
      className="flex size-8 items-center justify-center rounded-md transition-opacity hover:opacity-90"
    >
      <img
        src="/logo-mark.png"
        alt=""
        width={28}
        height={28}
        className="size-7 rounded-[7px]"
      />
    </Link>
  );
}
