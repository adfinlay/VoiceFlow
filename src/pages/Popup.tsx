import { useEffect, useState, useLayoutEffect, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { api } from "@/lib/api";

type PopupState = "idle" | "recording" | "processing";

// Brand accent (matches index.css --accent-500). Inline because the popup
// window loads with `transparent` overrides so utility classes can't reliably
// reach the surface.
const ACCENT = "#22c55e";
const ACCENT_DIM = "rgba(34, 197, 94, 0.7)";
const ACCENT_FAINT = "rgba(34, 197, 94, 0.18)";
const SURFACE = "rgba(9, 9, 11, 0.78)";
const BORDER = "rgba(255, 255, 255, 0.08)";

// Compact display label per model. Keeps the pill narrow even for the
// long distilled/large variants.
function modelLabel(model: string | null): string {
  if (!model) return "";
  const m = model.toLowerCase();
  const map: Record<string, string> = {
    "tiny": "tiny",
    "base": "base",
    "small": "small",
    "medium": "med",
    "large-v1": "lg·v1",
    "large-v2": "lg·v2",
    "large-v3": "lg·v3",
    "turbo": "turbo",
    "tiny.en": "tiny·en",
    "base.en": "base·en",
    "small.en": "sm·en",
    "medium.en": "md·en",
    "distil-small.en": "ds·en",
    "distil-medium.en": "dm·en",
    "distil-large-v2": "dl·v2",
    "distil-large-v3": "dl·v3",
  };
  return map[m] ?? m;
}

export function Popup() {
  const [state, setState] = useState<PopupState>("idle");
  const [amplitude, setAmplitude] = useState(0);
  const [model, setModel] = useState<string | null>(null);
  const prefersReducedMotion = useRef(
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );

  useLayoutEffect(() => {
    document.documentElement.style.cssText =
      "background: transparent !important;";
    document.body.style.cssText =
      "background: transparent !important; margin: 0; padding: 0;";
    document.documentElement.classList.add("popup-transparent");

    const root = document.getElementById("root");
    if (root) {
      root.style.cssText = "background: transparent !important;";
    }
  }, []);

  // Fetch current model on mount, then re-fetch every 10s so the label
  // stays in sync if the user changes it from settings.
  useEffect(() => {
    let cancelled = false;
    const fetch = async () => {
      try {
        const s = await api.getSettings();
        if (!cancelled) setModel(s.model);
      } catch {
        // ignore - backend may not be ready yet
      }
    };
    fetch();
    const id = window.setInterval(fetch, 10_000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  useEffect(() => {
    const handleAmplitude = (e: CustomEvent<number>) => setAmplitude(e.detail);
    const handleState = (e: CustomEvent<{ state: PopupState }>) => {
      setState(e.detail.state);
    };

    document.addEventListener("amplitude", handleAmplitude as EventListener);
    document.addEventListener("popup-state", handleState as EventListener);

    return () => {
      document.removeEventListener("amplitude", handleAmplitude as EventListener);
      document.removeEventListener("popup-state", handleState as EventListener);
    };
  }, []);

  const reduced = prefersReducedMotion.current;
  const label = modelLabel(model);

  return (
    <div
      className="w-screen h-screen flex items-center justify-center select-none"
      style={{ background: "transparent" }}
    >
      <AnimatePresence mode="wait">
        {state === "idle" && (
          <motion.div
            key="idle"
            initial={reduced ? false : { opacity: 0, scaleX: 0.6 }}
            animate={{ opacity: 1, scaleX: 1 }}
            exit={reduced ? { opacity: 0 } : { opacity: 0, scaleX: 0.6 }}
            transition={{ duration: 0.18, ease: [0.4, 0, 0.2, 1] }}
            style={{
              width: "32px",
              height: "4px",
              borderRadius: "2px",
              background: "rgba(255, 255, 255, 0.18)",
              transformOrigin: "center",
            }}
          />
        )}

        {state === "recording" && (
          <motion.div
            key="recording"
            initial={reduced ? false : { opacity: 0, y: 4, scale: 0.94 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={reduced ? { opacity: 0 } : { opacity: 0, y: -2, scale: 0.96 }}
            transition={{ duration: 0.22, ease: [0.4, 0, 0.2, 1] }}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "8px",
              padding: "5px 10px 5px 8px",
              borderRadius: "999px",
              background: SURFACE,
              border: `1px solid ${BORDER}`,
              backdropFilter: "blur(14px) saturate(140%)",
              WebkitBackdropFilter: "blur(14px) saturate(140%)",
              boxShadow: "0 4px 20px rgba(0, 0, 0, 0.25)",
            }}
          >
            {/* Live red dot — recording indicator */}
            <span
              style={{
                width: "6px",
                height: "6px",
                borderRadius: "50%",
                background: ACCENT,
                boxShadow: `0 0 0 3px ${ACCENT_FAINT}`,
                animation: reduced ? "none" : "popup-pulse 1.2s ease-in-out infinite",
              }}
            />

            {/* Model label */}
            {label && (
              <span
                style={{
                  fontFamily:
                    "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
                  fontSize: "10px",
                  letterSpacing: "0.02em",
                  color: "rgba(255, 255, 255, 0.65)",
                  lineHeight: 1,
                }}
              >
                {label}
              </span>
            )}

            {/* Divider */}
            {label && (
              <span
                style={{
                  width: "1px",
                  height: "10px",
                  background: "rgba(255, 255, 255, 0.12)",
                }}
              />
            )}

            {/* Amplitude bars */}
            <div style={{ display: "flex", alignItems: "center", gap: "2px", height: "14px" }}>
              {[0, 1, 2, 3, 4].map((i) => {
                const center = 2;
                const distance = Math.abs(i - center);
                const scale = 1 - distance * 0.18;
                const height = Math.max(3, 3 + amplitude * 11 * scale);

                return (
                  <div
                    key={i}
                    style={{
                      width: "2px",
                      height: `${height}px`,
                      borderRadius: "1px",
                      background: ACCENT,
                      transition: "height 60ms cubic-bezier(0.4, 0, 0.2, 1)",
                    }}
                  />
                );
              })}
            </div>
          </motion.div>
        )}

        {state === "processing" && (
          <motion.div
            key="processing"
            initial={reduced ? false : { opacity: 0, y: 4, scale: 0.94 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={reduced ? { opacity: 0 } : { opacity: 0, y: -2, scale: 0.96 }}
            transition={{ duration: 0.22, ease: [0.4, 0, 0.2, 1] }}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "8px",
              padding: "5px 12px 5px 10px",
              borderRadius: "999px",
              background: SURFACE,
              border: `1px solid ${BORDER}`,
              backdropFilter: "blur(14px) saturate(140%)",
              WebkitBackdropFilter: "blur(14px) saturate(140%)",
              boxShadow: "0 4px 20px rgba(0, 0, 0, 0.25)",
            }}
          >
            {/* Spinner */}
            <span
              style={{
                width: "10px",
                height: "10px",
                borderRadius: "50%",
                border: `1.5px solid ${ACCENT_FAINT}`,
                borderTopColor: ACCENT,
                animation: reduced ? "none" : "popup-spin 0.8s linear infinite",
              }}
            />

            {/* Model label */}
            {label && (
              <span
                style={{
                  fontFamily:
                    "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
                  fontSize: "10px",
                  letterSpacing: "0.02em",
                  color: "rgba(255, 255, 255, 0.65)",
                  lineHeight: 1,
                }}
              >
                {label}
              </span>
            )}

            {/* Divider */}
            {label && (
              <span
                style={{
                  width: "1px",
                  height: "10px",
                  background: "rgba(255, 255, 255, 0.12)",
                }}
              />
            )}

            {/* Wave-traveling dots */}
            <div style={{ display: "flex", alignItems: "center", gap: "3px" }}>
              {[0, 1, 2].map((i) => (
                <div
                  key={i}
                  style={{
                    width: "3px",
                    height: "3px",
                    borderRadius: "50%",
                    background: ACCENT_DIM,
                    animation: reduced
                      ? "none"
                      : "popup-wave 1s ease-in-out infinite",
                    animationDelay: `${i * 0.15}s`,
                  }}
                />
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <style>{`
        @keyframes popup-pulse {
          0%, 100% {
            box-shadow: 0 0 0 3px ${ACCENT_FAINT};
            transform: scale(1);
          }
          50% {
            box-shadow: 0 0 0 5px rgba(34, 197, 94, 0.10);
            transform: scale(1.08);
          }
        }
        @keyframes popup-spin {
          to { transform: rotate(360deg); }
        }
        @keyframes popup-wave {
          0%, 100% {
            opacity: 0.35;
            transform: translateY(0);
          }
          50% {
            opacity: 1;
            transform: translateY(-2px);
          }
        }
      `}</style>
    </div>
  );
}
