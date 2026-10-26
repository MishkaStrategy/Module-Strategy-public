import type { ControlComponentId } from "../control/contracts";

export interface NavigationItem {
  label: string;
  path: string;
  matchPrefixes: string[];
  component?: ControlComponentId;
  available: boolean;
  exact?: boolean;
}

export interface NavigationGroup {
  label: string;
  items: NavigationItem[];
}

export const NAVIGATION: NavigationGroup[] = [
  {
    label: "System",
    items: [
      {
        label: "Overview",
        path: "/overview",
        matchPrefixes: ["/overview"],
        available: true,
      },
      {
        label: "Health",
        path: "/system",
        matchPrefixes: ["/system"],
        available: true,
        exact: true,
      },
      {
        label: "Resources",
        path: "/system/resources",
        matchPrefixes: ["/system/resources"],
        available: true,
      },
    ],
  },
  {
    label: "Market Data",
    items: [
      {
        label: "Datasets",
        path: "/datasets",
        matchPrefixes: ["/datasets"],
        component: "market_data",
        available: true,
      },
      {
        label: "Artifacts",
        path: "/artifacts",
        matchPrefixes: ["/artifacts"],
        component: "market_data",
        available: true,
      },
      {
        label: "Jobs",
        path: "/jobs",
        matchPrefixes: ["/jobs"],
        component: "market_data",
        available: true,
      },
    ],
  },
  {
    label: "Strategy & Research",
    items: [
      {
        label: "Strategies",
        path: "/strategies",
        matchPrefixes: ["/strategies"],
        component: "strategy",
        available: true,
      },
      {
        label: "Backtests",
        path: "/backtests",
        matchPrefixes: ["/backtests"],
        component: "backtest",
        available: true,
      },
      {
        label: "Research",
        path: "/research",
        matchPrefixes: ["/research"],
        component: "research",
        available: true,
      },
    ],
  },
  {
    label: "Operations",
    items: [
      {
        label: "Paper Trading",
        path: "/paper",
        matchPrefixes: ["/paper"],
        component: "paper_runtime",
        available: true,
      },
      {
        label: "Acceptance",
        path: "/acceptance",
        matchPrefixes: ["/acceptance"],
        component: "acceptance",
        available: true,
      },
      {
        label: "Recovery",
        path: "/recovery",
        matchPrefixes: ["/recovery"],
        available: true,
      },
      {
        label: "Notifications",
        path: "/notifications",
        matchPrefixes: ["/notifications"],
        component: "signal_bot",
        available: true,
      },
      {
        label: "Logs & Events",
        path: "/events",
        matchPrefixes: ["/events"],
        available: true,
      },
      {
        label: "Evidence",
        path: "/evidence",
        matchPrefixes: ["/evidence"],
        component: "evidence",
        available: true,
      },
    ],
  },
];

export function isNavigationItemActive(item: NavigationItem, path: string): boolean {
  return item.matchPrefixes.some((prefix) => {
    if (item.exact) return path === prefix;
    return path === prefix || (prefix !== "/" && path.startsWith(`${prefix}/`));
  });
}

export function findComponentForPath(path: string): ControlComponentId | undefined {
  for (const group of NAVIGATION) {
    for (const item of group.items) {
      if (item.component && isNavigationItemActive(item, path)) return item.component;
    }
  }
  return undefined;
}
