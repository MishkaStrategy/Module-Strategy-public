import { NAVIGATION, isNavigationItemActive } from "./dashboard/navigation";

const DASHBOARD_MAIN_SELECTOR = ".dashboard-main";
const DASHBOARD_MAIN_ID = "dashboard-main";
const SKIP_LINK_ID = "dashboard-skip-link";
const TITLE_SUFFIX = "Modular Strategy";

export function routePathFromHash(hash: string): string {
  const value = hash.replace(/^#/, "").split("?", 1)[0] ?? "";
  return value.startsWith("/") && value.length > 0 ? value : "/overview";
}

export function pageLabelForPath(path: string): string {
  if (path === "/") return "Overview";
  for (const group of NAVIGATION) {
    for (const item of group.items) {
      if (isNavigationItemActive(item, path)) return item.label;
    }
  }
  return "Control Dashboard";
}

export function documentTitleForPath(path: string): string {
  return `${pageLabelForPath(path)} · ${TITLE_SUFFIX}`;
}

function dashboardMain(): HTMLElement | null {
  const main = document.querySelector<HTMLElement>(DASHBOARD_MAIN_SELECTOR);
  if (!main) return null;
  main.id = DASHBOARD_MAIN_ID;
  main.tabIndex = -1;
  return main;
}

function focusDashboardMain(): void {
  dashboardMain()?.focus();
}

export function installDashboardAccessibility(): () => void {
  const skipLink = document.getElementById(SKIP_LINK_ID);
  const syncDocumentTitle = () => {
    document.title = documentTitleForPath(routePathFromHash(window.location.hash));
  };
  const handleSkipLink = (event: MouseEvent) => {
    event.preventDefault();
    focusDashboardMain();
  };
  const handleRouteChange = () => {
    syncDocumentTitle();
    window.requestAnimationFrame(focusDashboardMain);
  };

  skipLink?.addEventListener("click", handleSkipLink);
  window.addEventListener("hashchange", handleRouteChange);
  syncDocumentTitle();
  const initialFrame = window.requestAnimationFrame(() => dashboardMain());

  return () => {
    window.cancelAnimationFrame(initialFrame);
    skipLink?.removeEventListener("click", handleSkipLink);
    window.removeEventListener("hashchange", handleRouteChange);
  };
}
