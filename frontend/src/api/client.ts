import type {
  CreatePaymentInput,
  PaymentEventItem,
  PaymentMetrics,
  PaymentProcessingSnapshot,
  PaymentRecord,
} from "../types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
    ...init,
  });

  if (!response.ok) {
    const fallbackMessage = `Request failed with status ${response.status}`;
    try {
      const errorBody = (await response.json()) as { detail?: string };
      const message = errorBody.detail ?? fallbackMessage;
      throw new Error(message);
    } catch (error) {
      if (error instanceof Error && error.message !== fallbackMessage) {
        throw error;
      }
      throw new Error(fallbackMessage);
    }
  }

  return response.json() as Promise<T>;
}

export function fetchMetrics() {
  return request<PaymentMetrics>("/api/v1/payments/metrics");
}

export function fetchPayments() {
  return request<PaymentRecord[]>("/api/v1/payments");
}

export function fetchPayment(paymentId: string) {
  return request<PaymentRecord>(`/api/v1/payments/${paymentId}`);
}

export function fetchPaymentProcessing(paymentId: string) {
  return request<PaymentProcessingSnapshot>(`/api/v1/payments/${paymentId}/processing`);
}

export function fetchPaymentEvents(paymentId: string) {
  return request<PaymentEventItem[]>(`/api/v1/payments/${paymentId}/events`);
}

export function createPayment(payload: CreatePaymentInput) {
  return request<PaymentRecord>("/api/v1/payments", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
