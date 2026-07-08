import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { initClient } from "./api/client";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { refetchOnWindowFocus: false, retry: 1 },
  },
});

initClient()
  .then(() => {
    ReactDOM.createRoot(document.getElementById("root")!).render(
      <React.StrictMode>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </QueryClientProvider>
      </React.StrictMode>,
    );
  })
  .catch((err) => {
    const root = document.getElementById("root")!;
    root.textContent = "";
    const wrap = document.createElement("div");
    wrap.style.cssText = "padding:40px;font-family:sans-serif;color:#e1e2e6";
    const title = document.createElement("h2");
    title.textContent = "Pullarr backend unreachable";
    const message = document.createElement("p");
    message.textContent = err.message;
    wrap.append(title, message);
    root.append(wrap);
  });
