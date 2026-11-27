import type {
  CreatePaymentInput,
  PaymentEventItem,
  PaymentMetrics,
  PaymentProcessingSnapshot,
  PaymentRecord,
  SessionInfo,
} from "../types";

let csrfToken: string | null = null;
let sessionGeneration = 0;

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const generation = sessionGeneration;
  const unsafe = init?.method && !["GET", "HEAD", "OPTIONS"].includes(init.method);
  const controller = new AbortController();
  const abort = () => controller.abort();
  init?.signal?.addEventListener("abort", abort, { once: true });
  if (init?.signal?.aborted) controller.abort();
  const timeout = window.setTimeout(abort, 15000);
  try {
    const response = await fetch(`/api/v1${path}`, {
      ...init, signal: controller.signal, credentials: "same-origin",
      headers: { "Content-Type": "application/json",
        ...(unsafe && csrfToken ? { "X-CSRF-Token": csrfToken } : {}), ...init?.headers },
    });
    if (!response.ok) {
      let message = response.status >= 500 ? "The service is temporarily unavailable. Please try again."
        : `Request failed with status ${response.status}`;
      if (response.status < 500) {
        try {
          const body = await response.json();
          message = typeof body.detail === "string" ? body.detail : "Check the supplied fields.";
        } catch { /* Keep the safe status message for non-JSON responses. */ }
      }
      if (response.status === 401 && !path.startsWith("/auth/login") && generation === sessionGeneration) {
        csrfToken = null;
        sessionGeneration += 1;
        window.dispatchEvent(new Event("session-expired"));
      }
      throw new ApiError(message, response.status);
    }
    return response.status === 204 ? undefined as T : await response.json();
  } catch (failure) {
    if (init?.signal?.aborted || failure instanceof ApiError) throw failure;
    throw new ApiError("The request could not be confirmed. Check your connection and try again.", 0);
  } finally {
    window.clearTimeout(timeout);
    init?.signal?.removeEventListener("abort", abort);
  }
}

export async function fetchSession(signal?: AbortSignal) {
  const generation = sessionGeneration;
  const session = await request<SessionInfo>("/auth/session", { signal });
  if (generation === sessionGeneration) csrfToken = session.csrf_token;
  return session;
}

export async function login(email: string, password: string) {
  sessionGeneration += 1;
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
  sessionGeneration += 1;
  await request<void>("/auth/logout", { method: "POST" });
  csrfToken = null;
}

export function fetchMetrics() {
  return request<PaymentMetrics>("/payments/metrics");
}

export function fetchPayments(signal?: AbortSignal) {
  return request<PaymentRecord[]>("/payments", { signal });
}

export function fetchPayment(paymentId: string) {
  return request<PaymentRecord>(`/payments/${paymentId}`);
}

export function fetchPaymentProcessing(paymentId: string) {
  return request<PaymentProcessingSnapshot>(`/payments/${paymentId}/processing`);
}

export function fetchPaymentEvents(paymentId: string, signal?: AbortSignal) {
  return request<PaymentEventItem[]>(`/payments/${encodeURIComponent(paymentId)}/events`, { signal });
}

export function createPayment(payload: CreatePaymentInput, idempotencyKey: string) {
  return request<PaymentRecord>("/payments", { method: "POST", body: JSON.stringify(payload),
    headers: { "Idempotency-Key": idempotencyKey } });
}
