import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { apiFetch, setAccessToken, trySessionRestore } from "../api/client";
import type { UserProfile } from "../types";

interface AuthState {
  user: UserProfile | null;
  loading: boolean;
  error: string | null;
  googleClientId: string;
}

const AuthContext = createContext<{
  state: AuthState;
  loginWithGoogleCredential: (credential: string) => Promise<void>;
  logout: () => Promise<void>;
  reloadMe: () => Promise<void>;
} | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [googleClientId, setGoogleClientId] = useState("");

  const loadConfig = useCallback(async () => {
    const r = await fetch("/auth/config");
    if (r.ok) {
      const d = (await r.json()) as { google_oauth_client_id?: string };
      setGoogleClientId(d.google_oauth_client_id || "");
    }
  }, []);

  const reloadMe = useCallback(async () => {
    const r = await apiFetch("/me");
    if (r.ok) {
      setUser((await r.json()) as UserProfile);
    } else {
      setUser(null);
      setAccessToken(null);
    }
  }, []);

  useEffect(() => {
    (async () => {
      await loadConfig();
      await trySessionRestore();
      await reloadMe();
      setLoading(false);
    })();
  }, [loadConfig, reloadMe]);

  const loginWithGoogleCredential = useCallback(
    async (credential: string) => {
      setError(null);
      const r = await fetch("/auth/login/google", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ credential }),
      });
      const data = await r.json();
      if (!r.ok) {
        setError((data as { detail?: string }).detail || "Login failed");
        return;
      }
      if ("status" in data && data.status === "waiting_for_approval") {
        setUser(null);
        setAccessToken(null);
        setError("Your account is pending approval.");
        return;
      }
      if ("status" in data && data.status === "rejected") {
        setUser(null);
        setAccessToken(null);
        setError("Access was rejected for this account.");
        return;
      }
      const tokens = (data as { tokens?: { access?: string } }).tokens;
      if (tokens?.access) {
        setAccessToken(tokens.access);
        await reloadMe();
      }
    },
    [reloadMe],
  );

  const logout = useCallback(async () => {
    await apiFetch("/auth/logout", { method: "POST" });
    setAccessToken(null);
    setUser(null);
  }, []);

  const value = useMemo(
    () => ({
      state: { user, loading, error, googleClientId },
      loginWithGoogleCredential,
      logout,
      reloadMe,
    }),
    [
      user,
      loading,
      error,
      googleClientId,
      loginWithGoogleCredential,
      logout,
      reloadMe,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
