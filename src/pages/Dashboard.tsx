import { useState } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import { Menu, Mic } from "lucide-react";
import { Sidebar } from "@/components/Sidebar";
import { HomePage } from "@/components/HomePage";
import { HistoryPage } from "@/components/HistoryPage";
import { SettingsTab } from "@/components/SettingsTab";
import { HotkeyStatusBanner } from "@/components/HotkeyStatusBanner";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetTitle,
} from "@/components/ui/sheet";

export function Dashboard() {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  return (
    <div className="flex h-screen bg-background">
      <div className="hidden md:block">
        <Sidebar />
      </div>

      <header className="md:hidden fixed top-0 left-0 right-0 z-40 h-14 bg-sidebar border-b border-sidebar-border px-4 flex items-center gap-3">
        <Button
          variant="ghost"
          size="icon"
          className="text-cream"
          onClick={() => setMobileMenuOpen(true)}
          aria-label="Open navigation menu"
          aria-expanded={mobileMenuOpen}
          aria-controls="mobile-navigation-sheet"
        >
          <Menu className="h-5 w-5" />
        </Button>
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 bg-primary rounded-md flex items-center justify-center">
            <Mic className="h-4 w-4 text-primary-foreground" strokeWidth={2.5} />
          </div>
          <span className="font-display font-semibold text-lg text-cream tracking-tight leading-none">
            VoiceFlow
          </span>
        </div>
      </header>

      <Sheet open={mobileMenuOpen} onOpenChange={setMobileMenuOpen}>
        <SheetContent
          id="mobile-navigation-sheet"
          side="left"
          className="p-0 w-64 bg-sidebar border-sidebar-border"
        >
          <SheetTitle className="sr-only">Navigation Menu</SheetTitle>
          <Sidebar onNavigate={() => setMobileMenuOpen(false)} />
        </SheetContent>
      </Sheet>

      {/* Banner sits in the right column above main so the hotkey warning stays pinned and doesn't scroll with content */}
      <div className="flex-1 flex flex-col min-w-0 pt-14 md:pt-0">
        <HotkeyStatusBanner />
        <main className="flex-1 overflow-auto">
          <Routes>
            <Route index element={<HomePage />} />
            <Route path="history" element={<HistoryPage />} />
            <Route path="settings" element={<SettingsTab />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
