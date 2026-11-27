export type PaymentStatus = "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED";
export type QueueMessageStatus = "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED";

export interface PaymentMetrics {
  total: number;
  pending: number;
  processing: number;
  completed: number;
  failed: number;
}

export type ProviderState = "prepared" | "in_flight" | "pending" | "requires_action" | "unknown" | "review_required" | "succeeded" | "failed";
export type ProviderStatus = "succeeded" | "canceled" | "processing" | "requires_payment_method" | "requires_confirmation" | "requires_action" | "requires_capture";
export type PaymentFilter = PaymentStatus | "ALL" | "UNKNOWN" | "REVIEW_REQUIRED";

export interface PaymentProviderInfo {
  processing_backend: "simulator" | "stripe" | null;
  provider_reference: string | null;
  provider_state: ProviderState | null;
  provider_status: ProviderStatus | null;
}

export interface PaymentRecord extends PaymentProviderInfo {
  id: string;
  merchant_reference: string;
  amount: string;
  currency: string;
  customer_email: string;
  description: string | null;
  status: PaymentStatus;
  failure_reason: string | null;
  retry_count: number;
  created_at: string;
  updated_at: string;
}

export interface PaymentEventItem {
  id: string;
  payment_id: string;
  queue_name: string;
  event_type: string;
  status: QueueMessageStatus;
  attempts: number;
  last_error: string | null;
  available_at: string;
  created_at: string;
  updated_at: string;
}

export interface PaymentProcessingSnapshot extends PaymentProviderInfo {
  id: string;
  merchant_reference: string;
  status: PaymentStatus;
  retry_count: number;
  failure_reason: string | null;
  latest_event_status: QueueMessageStatus | null;
  latest_event_attempts: number | null;
  latest_event_error: string | null;
}

export interface CreatePaymentInput {
  merchant_reference: string;
  amount: string;
  currency: string;
  customer_email: string;
  description?: string;
}

export interface SessionInfo {
  email: string;
  merchant_id: string;
  csrf_token: string;
}
