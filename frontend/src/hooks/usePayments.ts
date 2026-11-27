import { useEffect, useRef, useState } from "react";

import { fetchPaymentEvents, fetchPayments } from "../api/client";
import type { PaymentEventItem, PaymentRecord } from "../types";

// Schedule after settlement, not on an interval: slow reads never overlap.
function startPolling(load: (signal: AbortSignal) => Promise<void>) {
  const controller = new AbortController();
  let timer: number | undefined;
  let running = false;
  async function tick() {
    if (controller.signal.aborted || running) return;
    window.clearTimeout(timer);
    running = true;
    try {
      if (!document.hidden) await load(controller.signal);
    } finally {
      running = false;
      if (!controller.signal.aborted) timer = window.setTimeout(() => void tick(), 4000);
    }
  }
  const visible = () => { if (!document.hidden) void tick(); };
  document.addEventListener("visibilitychange", visible);
  void tick();
  return () => {
    controller.abort();
    window.clearTimeout(timer);
    document.removeEventListener("visibilitychange", visible);
  };
}

export function usePayments() {
  const [payments, setPayments] = useState<PaymentRecord[]>([]);
  const [selectedId, selectPayment] = useState<string | null>(null);
  const [events, setEvents] = useState<PaymentEventItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const revision = useRef(0);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const generation = revision.current;
    return startPolling(async (signal) => {
      try {
        const next = await fetchPayments(signal);
        if (signal.aborted || generation !== revision.current) return;
        setPayments(next);
        selectPayment((current) => next.some((payment) => payment.id === current) ? current : next[0]?.id ?? null);
        setError(null);
      } catch (failure) {
        if (!signal.aborted && generation === revision.current) {
          setError(failure instanceof Error ? failure.message : "Unable to load payments.");
        }
      } finally {
        if (!signal.aborted && generation === revision.current) setLoading(false);
      }
    });
  }, [refresh]);

  useEffect(() => {
    setEvents([]);
    setDetailError(null);
    setDetailLoading(Boolean(selectedId));
    if (!selectedId) return;
    return startPolling(async (signal) => {
      try {
        const next = await fetchPaymentEvents(selectedId, signal);
        if (!signal.aborted) { setEvents(next); setDetailError(null); }
      } catch (failure) {
        if (!signal.aborted) setDetailError(failure instanceof Error ? failure.message : "Unable to load queue history.");
      } finally {
        if (!signal.aborted) setDetailLoading(false);
      }
    });
  }, [selectedId]);

  function recordCreated(payment: PaymentRecord) {
    // Invalidate an older list response before React runs effect cleanup.
    revision.current += 1;
    setPayments((current) => [payment, ...current.filter((item) => item.id !== payment.id)]);
    selectPayment(payment.id);
    setRefresh((current) => current + 1);
  }

  return { payments, selectedId, selectPayment, events, loading, detailLoading, error, detailError,
    selectedPayment: payments.find((payment) => payment.id === selectedId) ?? null, recordCreated };
}
