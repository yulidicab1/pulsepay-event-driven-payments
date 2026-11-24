import { useEffect, useState } from "react";

import {
  ApiError,
  fetchSession,
  login,
  logout,
  createPayment,
  fetchMetrics,
  fetchPayment,
  fetchPaymentEvents,
  fetchPaymentProcessing,
  fetchPayments,
} from "./api/client";
import { PaymentDetailPanel } from "./components/PaymentDetailPanel";
import { PaymentForm } from "./components/PaymentForm";
import { PaymentTable } from "./components/PaymentTable";
import { LoginForm } from "./components/LoginForm";
import type { SessionInfo } from "./types";
import { SummaryCard } from "./components/SummaryCard";
import type {
  CreatePaymentInput,
  PaymentEventItem,
  PaymentMetrics,
  PaymentProcessingSnapshot,
  PaymentRecord,
  PaymentStatus,
} from "./types";

const EMPTY_METRICS: PaymentMetrics = {
  total: 0,
  pending: 0,
  processing: 0,
  completed: 0,
  failed: 0,
};

function Dashboard() {
  const [metrics, setMetrics] = useState<PaymentMetrics>(EMPTY_METRICS);
  const [payments, setPayments] = useState<PaymentRecord[]>([]);
  const [selectedPaymentId, setSelectedPaymentId] = useState<string | null>(null);
  const [selectedPayment, setSelectedPayment] = useState<PaymentRecord | null>(null);
  const [processingSnapshot, setProcessingSnapshot] = useState<PaymentProcessingSnapshot | null>(null);
  const [paymentEvents, setPaymentEvents] = useState<PaymentEventItem[]>([]);
  const [activeStatus, setActiveStatus] = useState<PaymentStatus | "ALL">("ALL");
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [formSubmitting, setFormSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    async function loadOverview() {
      try {
        const [nextMetrics, nextPayments] = await Promise.all([fetchMetrics(), fetchPayments()]);
        if (!active) {
          return;
        }

        setMetrics(nextMetrics);
        setPayments(nextPayments);
        setSelectedPaymentId((current) => current ?? nextPayments[0]?.id ?? null);
        setPageError(null);
      } catch (error) {
        if (!active) {
          return;
        }
        setPageError(error instanceof Error ? error.message : "Unable to load dashboard data.");
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    }

    void loadOverview();
    const interval = window.setInterval(() => {
      void loadOverview();
    }, 4000);

    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    if (!selectedPaymentId) {
      setSelectedPayment(null);
      setProcessingSnapshot(null);
      setPaymentEvents([]);
      return;
    }

    let active = true;
    setDetailLoading(true);
    const paymentId = selectedPaymentId;

    async function loadPaymentDetail() {
      try {
        const [payment, processing, events] = await Promise.all([
          fetchPayment(paymentId),
          fetchPaymentProcessing(paymentId),
          fetchPaymentEvents(paymentId),
        ]);
        if (!active) {
          return;
        }
        setSelectedPayment(payment);
        setProcessingSnapshot(processing);
        setPaymentEvents(events);
      } catch (error) {
        if (!active) {
          return;
        }
        setPageError(error instanceof Error ? error.message : "Unable to load payment details.");
      } finally {
        if (active) {
          setDetailLoading(false);
        }
      }
    }

    void loadPaymentDetail();
    const interval = window.setInterval(() => {
      void loadPaymentDetail();
    }, 4000);

    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [selectedPaymentId]);

  async function refreshOverview() {
    const [nextMetrics, nextPayments] = await Promise.all([fetchMetrics(), fetchPayments()]);
    setMetrics(nextMetrics);
    setPayments(nextPayments);
  }

  async function handleCreatePayment(payload: CreatePaymentInput) {
    try {
      setFormSubmitting(true);
      setFormError(null);
      const createdPayment = await createPayment(payload);
      await refreshOverview();
      setSelectedPaymentId(createdPayment.id);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "Unable to create payment.");
    } finally {
      setFormSubmitting(false);
    }
  }

  const filteredPayments = payments.filter((payment) =>
    activeStatus === "ALL" ? true : payment.status === activeStatus,
  );

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
          <span className="live-dot" />
          Live view refreshes every few seconds
        </div>
      </section>

      {pageError ? <p className="page-error">{pageError}</p> : null}

      <section className="summary-grid">
        <SummaryCard
          label="Total payments"
          value={metrics.total}
          hint="All payments created in this workspace"
          tone="neutral"
        />
        <SummaryCard
          label="Completed"
          value={metrics.completed}
          hint="Successfully processed payments"
          tone="success"
        />
        <SummaryCard
          label="Failed"
          value={metrics.failed}
          hint="Payments that require review"
          tone="danger"
        />
        <SummaryCard
          label="Pending / Processing"
          value={metrics.pending + metrics.processing}
          hint="Currently moving through the queue"
          tone="warning"
        />
      </section>

      <section className="dashboard-grid">
        <div className="left-column">
          <PaymentForm
            submitting={formSubmitting}
            error={formError}
            onSubmit={handleCreatePayment}
          />
          <PaymentTable
            payments={filteredPayments}
            activeStatus={activeStatus}
            selectedPaymentId={selectedPaymentId}
            onStatusChange={setActiveStatus}
            onSelectPayment={setSelectedPaymentId}
          />
        </div>

        <PaymentDetailPanel
          payment={selectedPayment}
          processing={processingSnapshot}
          events={paymentEvents}
          loading={loading || detailLoading}
        />
      </section>
    </main>
  );
}

export default function App() {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const expired = () => setSession(null);
    window.addEventListener("session-expired", expired);
    fetchSession().then((next) => {
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
      window.removeEventListener("session-expired", expired);
    };
  }, []);

  if (checking) return <main className="app-shell" role="status">Checking your session...</main>;
  if (error && !session) return <main className="app-shell"><p role="alert">{error}</p></main>;
  if (!session) return <LoginForm onSubmit={async (email, password) => setSession(await login(email, password))} />;

  return (
    <>
      <header className="app-shell account-header">
        <span>{session.email}</span>
        <button type="button" onClick={async () => {
          try {
            await logout();
            setSession(null);
            setError(null);
          } catch (failure) {
            setError(failure instanceof Error ? failure.message : "Unable to sign out.");
          }
        }}>Sign out</button>
        {error ? <p role="alert">{error}</p> : null}
      </header>
      <Dashboard key={session.merchant_id} />
    </>
  );
}
