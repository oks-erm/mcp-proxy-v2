import { useEffect, useRef } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

export function LoginPage() {
  const { state, loginWithGoogleCredential } = useAuth();
  const btnRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (
      !state.googleClientId ||
      !window.google?.accounts?.id ||
      !btnRef.current
    )
      return;
    window.google.accounts.id.initialize({
      client_id: state.googleClientId,
      callback: (res: { credential: string }) => {
        void loginWithGoogleCredential(res.credential);
      },
      auto_select: false,
    });
    window.google.accounts.id.renderButton(btnRef.current, {
      theme: "filled_black",
      size: "large",
      width: 320,
      text: "continue_with",
    });
  }, [state.googleClientId, loginWithGoogleCredential]);

  if (state.user?.status === "active") {
    const to = state.user.role === "admin" ? "/dashboard" : "/account";
    return <Navigate to={to} replace />;
  }

  return (
    <div className="min-h-[80vh] flex flex-col items-center justify-center gap-8 px-4">
      <div className="text-center max-w-md">
        <h1 className="text-3xl font-semibold text-white mb-2">MCP Proxy</h1>
        <p className="text-slate-400 text-sm leading-relaxed">
          Sign in with Google — your first sign-in registers you; after that it
          signs you in. If you are not approved yet, you stay on the approval
          list until an administrator activates your account; once approved, you
          are signed in normally. Administrators manage servers, users, and
          approvals. Approved members get{" "}
          <strong className="text-slate-300">MCP setup</strong> (API key and
          snippets for Cursor, Claude, ChatGPT, Amazon Q, Gemini CLI, and other
          supported clients) and{" "}
          <strong className="text-slate-300">activity logs</strong> in Cloud
          Logging.
        </p>
      </div>
      <div ref={btnRef} className="min-h-[40px]" />
      {state.error && (
        <p className="text-amber-400 text-sm max-w-md text-center" role="alert">
          {state.error}
        </p>
      )}
      {state.user?.status === "pending" && (
        <p className="text-slate-400 text-sm">
          Waiting for an administrator to approve your account.
        </p>
      )}
    </div>
  );
}
