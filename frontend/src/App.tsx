import { useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import Sidebar from "./components/Sidebar";
import Library from "./pages/Library";
import AddSeries from "./pages/AddSeries";
import SeriesDetail from "./pages/SeriesDetail";
import Activity from "./pages/Activity";
import Wanted from "./pages/Wanted";
import Settings from "./pages/Settings";
import Calendar from "./pages/Calendar";
import LibraryImport from "./pages/LibraryImport";
import CommandPalette from "./components/CommandPalette";

export default function App() {
  const [paletteOpen, setPaletteOpen] = useState(false);
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault(); setPaletteOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, []);
  return (
    <div className="app">
      <Sidebar onGoTo={() => setPaletteOpen(true)} />
      <div className="main">
        <Routes>
          <Route path="/" element={<Library />} />
          <Route path="/add" element={<AddSeries />} />
          <Route path="/library/import" element={<LibraryImport />} />
          <Route path="/calendar" element={<Calendar />} />
          <Route path="/series/:id" element={<SeriesDetail />} />
          <Route path="/activity" element={<Activity />} />
          <Route path="/wanted" element={<Wanted />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
      {paletteOpen && <CommandPalette onClose={() => setPaletteOpen(false)} />}
    </div>
  );
}
