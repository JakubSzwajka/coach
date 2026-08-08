"use client";

import { ThemeProvider as NextThemesProvider, useTheme } from "next-themes";
import { useEffect } from "react";

function ThemeProvider({ children, ...props }: React.ComponentProps<typeof NextThemesProvider>) {
  return <NextThemesProvider {...props}>{children}</NextThemesProvider>;
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.matches("input, textarea, select, [contenteditable='true']") ||
    target.isContentEditable ||
    target.closest("[contenteditable='true']") !== null
  );
}

function ThemeHotkey() {
  const { resolvedTheme, setTheme } = useTheme();

  useEffect(() => {
    const toggleTheme = (event: KeyboardEvent) => {
      const shortcut =
        event.key.toLowerCase() === "d" &&
        event.shiftKey &&
        (event.metaKey || event.ctrlKey) &&
        !event.altKey;
      if (!shortcut || event.repeat || isEditableTarget(event.target)) return;

      event.preventDefault();
      setTheme(resolvedTheme === "dark" ? "light" : "dark");
    };

    window.addEventListener("keydown", toggleTheme);
    return () => window.removeEventListener("keydown", toggleTheme);
  }, [resolvedTheme, setTheme]);

  return null;
}

export { ThemeHotkey, ThemeProvider };
