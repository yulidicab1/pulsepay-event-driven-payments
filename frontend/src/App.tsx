import { useEffect, useRef, useState } from "react";
import { ApiError, fetchSession, login, logout, createPayment } from "./api/client";
import { PaymentDetailPanel } from "./components/PaymentDetailPanel";
import { PaymentForm } from "./components/PaymentForm";
import { PaymentTable } from "./components/PaymentTable";
import { LoginForm } from "./components/LoginForm";
import { SummaryCard } from "./components/SummaryCard";
import { usePayments } from "./hooks/usePayments";
import type { CreatePaymentInput, PaymentFilter, PaymentMetrics, SessionInfo } from "./types";

const EMPTY_METRICS: PaymentMetrics = {
  total: 0,
  pending: 0,
  processing: 0,
  completed: 0,
  failed: 0,
};

function Dashboard() {
  const { payments, selectedId, selectPayment, selectedPayment, events, loading,
    detailLoading, error: pageError, detailError, recordCreated } = usePayments();
  const [activeStatus, setActiveStatus] = useState<PaymentFilter>("ALL");
  const [formSubmitting, setFormSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<string | null>(null);
  const submitting = useRef(false);
  const creation = useRef<{ payload: string; key: string } | null>(null);
  const metrics = payments.reduce((result, payment) => {
    result.total += 1;
    const key = payment.status.toLowerCase() as "pending" | "processing" | "completed" | "failed";
    result[key] += 1;
    return result;
  }, { ...EMPTY_METRICS });

  async function handleCreatePayment(payload: CreatePaymentInput): Promise<boolean> {
    if (submitting.current) return false;
    submitting.current = true;
    setFormSubmitting(true);
    setFormError(null);
    setConfirmation(null);
    const fingerprint = JSON.stringify(payload);
    if (creation.current?.payload !== fingerprint) creation.current = { payload: fingerprint, key: crypto.randomUUID() };
    try {
      const created = await createPayment(payload, creation.current.key);
      recordCreated(created);
      creation.current = null;
      setConfirmation("Payment saved. Follow its status in the payment list.");
      return true;
    } catch (failure) {
      setFormError(failure instanceof ApiError && (failure.status === 0 || failure.status >= 500)
        ? "Creation could not be confirmed. Retry this unchanged form to check the same request."
        : failure instanceof Error ? failure.message : "Unable to create payment.");
      return false;
    } finally {
      submitting.current = false;
      setFormSubmitting(false);
    }
  }

  const overviewUnavailable = loading || (Boolean(pageError) && payments.length === 0);

  const filteredPayments = payments.filter((payment) => activeStatus === "ALL" ||
    (activeStatus === "UNKNOWN" || activeStatus === "REVIEW_REQUIRED"
      ? payment.provider_state === activeStatus.toLowerCase() : payment.status === activeStatus));

  return (
    <main className="app-shell">
      <section className="hero-banner">
        <div>
          <p className="eyebrow">PulsePay</p>
          <h1>Payment operations at a glance.</h1>
          <p className="lede">
            A lightweight dashboard for seeing which payments are succeeding, which are waiting,
            and which ones need attention.
          </p>
        </div>
        <div className="hero-note">
          <span className={pageError ? "live-dot updates-paused" : "live-dot"} />
          {pageError ? "Live updates interrupted" : loading ? "Loading payments..." : "Live view refreshes every few seconds"}
        </div>
      </section>

      {pageError ? <p className="page-error" role="alert">{pageError}</p> : null}

      <section className="summary-grid">
        <SummaryCard
          label="Total payments"
          value={overviewUnavailable ? "—" : metrics.total}
          hint="All payments created in this workspace"
          tone="neutral"
        />
        <SummaryCard
          label="Completed"
          value={overviewUnavailable ? "—" : metrics.completed}
          hint="Successfully processed payments"
          tone="success"
        />
        <SummaryCard
          label="Failed"
          value={overviewUnavailable ? "—" : metrics.failed}
          hint="Confirmed terminal failures"
          tone="danger"
        />
        <SummaryCard
          label="Pending / Processing"
          value={overviewUnavailable ? "—" : metrics.pending + metrics.processing}
          hint="Waiting, processing or awaiting reconciliation"
          tone="warning"
        />
      </section>

      <section className="dashboard-grid">
        <div className="left-column">
          <PaymentForm
            submitting={formSubmitting}
            error={formError}
            confirmation={confirmation}
            onSubmit={handleCreatePayment}
          />
          <PaymentTable
            payments={filteredPayments}
            loading={loading}
            unavailable={Boolean(pageError) && payments.length === 0}
            activeStatus={activeStatus}
            selectedPaymentId={selectedId}
            onStatusChange={setActiveStatus}
            onSelectPayment={selectPayment}
          />
        </div>

        <PaymentDetailPanel
          payment={selectedPayment}
          events={events}
          error={detailError}
          loading={loading || detailLoading}
        />
      </section>
    </main>
  );
}

export default function App() {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [signingOut, setSigningOut] = useState(false);
  const [logoutUnconfirmed, setLogoutUnconfirmed] = useState(false);
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const expired = () => { setSession(null); setError(null); };
    window.addEventListener("session-expired", expired);
    fetchSession(controller.signal).then((next) => {
      if (active) setSession(next);
    }).catch((failure) => {
      if (active && (!(failure instanceof ApiError) || failure.status !== 401)) {
        setError("Unable to connect. Refresh the page to try again.");
      }
    }).finally(() => {
      if (active) setChecking(false);
    });
    return () => {
      active = false;
      controller.abort();
      window.removeEventListener("session-expired", expired);
    };
  }, []);

  async function handleSignOut() {
    // Hide merchant data immediately, even if the revocation response is lost.
    setSession(null);
    setError(null);
    setSigningOut(true);
    try {
      await logout();
      setLogoutUnconfirmed(false);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) {
        setLogoutUnconfirmed(false);
      } else {
        setLogoutUnconfirmed(true);
        setError("Sign out could not be confirmed. Retry to revoke this browser session.");
      }
    } finally {
      setSigningOut(false);
    }
  }

  if (checking || signingOut) return <main className="app-shell" role="status">{signingOut ? "Signing out..." : "Checking your session..."}</main>;
  if (error && !session) return <main className="app-shell session-error">
    <p role="alert">{error}</p>
    {logoutUnconfirmed ? <button type="button" onClick={() => void handleSignOut()}>Retry sign out</button> : null}
  </main>;
  if (!session) return <LoginForm onSubmit={async (email, password) => setSession(await login(email, password))} />;

  return (
    <>
      <header className="app-shell account-header">
        <span>{session.email}</span>
        <button type="button" onClick={() => void handleSignOut()}>Sign out</button>
        {error ? <p role="alert">{error}</p> : null}
      </header>
      <Dashboard key={session.merchant_id} />
    </>
  );
}
