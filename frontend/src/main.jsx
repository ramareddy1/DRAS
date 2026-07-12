import React, { useEffect } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router-dom";
import "./index.css";

/** Reset scroll to top on every route change (React Router doesn't by default). */
function ScrollToTop() {
  const { pathname } = useLocation();
  useEffect(() => { window.scrollTo(0, 0); }, [pathname]);
  return null;
}
import UploadPage from "./pages/UploadPage.jsx";
import ResultsPage from "./pages/ResultsPage.jsx";
import HistoryPage from "./pages/HistoryPage.jsx";
import OnboardingPage from "./pages/OnboardingPage.jsx";
import InboxPage from "./pages/InboxPage.jsx";
import ConversationPage from "./pages/ConversationPage.jsx";
import RulesPage from "./pages/RulesPage.jsx";
import ObservationsPage from "./pages/ObservationsPage.jsx";
import ComparePage from "./pages/ComparePage.jsx";
import MetricsPage from "./pages/MetricsPage.jsx";
import Layout from "./components/Layout.jsx";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <BrowserRouter>
      <ScrollToTop />
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<UploadPage />} />
          <Route path="/onboarding" element={<OnboardingPage />} />
          <Route path="/inbox" element={<InboxPage />} />
          <Route path="/conversation" element={<ConversationPage />} />
          <Route path="/rules" element={<RulesPage />} />
          <Route path="/observations" element={<ObservationsPage />} />
          <Route path="/metrics" element={<MetricsPage />} />
          <Route path="/results/:id" element={<ResultsPage />} />
          <Route path="/results/:id/compare/:prevId" element={<ComparePage />} />
          <Route path="/history" element={<HistoryPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </React.StrictMode>
);
