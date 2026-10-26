import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { installDashboardAccessibility } from "./accessibility";
import DashboardApp from "./dashboard/DashboardApp";
import "./styles.css";
import "./dashboard/dashboard.css";
import "./accessibility.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("Modular Strategy Dashboard root element is missing");
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: false,
    },
  },
});

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <DashboardApp />
    </QueryClientProvider>
  </StrictMode>,
);

installDashboardAccessibility();
