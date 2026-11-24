import type {
  CreatePaymentInput,
  PaymentEventItem,
  PaymentMetrics,
  PaymentProcessingSnapshot,
  PaymentRecord,
  SessionInfo,
} from "../types";

let csrfToken: string | null = null;

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const unsafe = init?.method && !["GET", "HEAD", "OPTIONS"].includes(init.method);
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(unsafe && csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    try {
      const body = await response.json();
      message = typeof body.detail === "string" ? body.detail : "Check the supplied fields.";
    } catch {
      // The status remains available when an intermediary returns a non-JSON error.
    }
    if (response.status === 401 && !path.startsWith("/auth/login")) {
      csrfToken = null;
      window.dispatchEvent(new Event("session-expired"));
    }
    throw new ApiError(message, response.status);
  }
  return response.status === 204 ? undefined as T : response.json();
}

export async function fetchSession() {
  const session = await request<SessionInfo>("/auth/session");
  csrfToken = session.csrf_token;
  return session;
}

export async function login(email: string, password: string) {
  const verification = await request<{ csrf_token: string }>("/auth/csrf");
  csrfToken = verification.csrf_token;
  const session = await request<SessionInfo>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  csrfToken = session.csrf_token;
  return session;
}

export async function logout() {
  await request<void>("/auth/logout", { method: "POST" });
  csrfToken = null;
}

export function fetchMetrics() {
  return request<PaymentMetrics>("/payments/metrics");
}

export function fetchPayments() {
  return request<PaymentRecord[]>("/payments");
}

export function fetchPayment(paymentId: string) {
  return request<PaymentRecord>(`/payments/${paymentId}`);
}

export function fetchPaymentProcessing(paymentId: string) {
  return request<PaymentProcessingSnapshot>(`/payments/${paymentId}/processing`);
}

export function fetchPaymentEvents(paymentId: string) {
  return request<PaymentEventItem[]>(`/payments/${paymentId}/events`);
}

export function createPayment(payload: CreatePaymentInput) {
  return request<PaymentRecord>("/payments", { method: "POST", body: JSON.stringify(payload) });
}
