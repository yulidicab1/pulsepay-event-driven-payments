import type { PaymentRecord } from "./types";

export function providerNotice(payment: PaymentRecord) {
  if (payment.processing_backend !== "stripe" || ["COMPLETED", "FAILED"].includes(payment.status)) return null;
  if (payment.provider_state === "review_required") return {
    label: "REVIEW_REQUIRED" as const, title: "Review required",
    message: "Automatic processing is paused. An operator must verify the provider result. This is not a confirmed payment failure; do not create a replacement charge.",
  };
  if (payment.provider_state === "unknown") return {
    label: "UNKNOWN" as const, title: "Result not confirmed",
    message: "The provider result is not yet confirmed. Reconciliation will continue when safe. Do not create a replacement charge to resolve this uncertainty.",
  };
  if (payment.provider_state === "requires_action" || ["requires_action", "requires_confirmation", "requires_capture"].includes(payment.provider_status ?? "")) return {
    label: "REQUIRES_ACTION" as const, title: "Provider action required",
    message: "The provider needs an additional action before this payment can finish. No new charge is created automatically.",
  };
  return null;
}

export function describePayment(payment: PaymentRecord) {
  const notice = providerNotice(payment);
  if (notice) return notice.message;
  if (payment.processing_backend === null && ["PENDING", "PROCESSING"].includes(payment.status)) {
    return "This historical payment has no classified provider. Its data is preserved and automatic processing is paused.";
  }
  if (payment.status === "COMPLETED") return payment.processing_backend === "stripe"
    ? "Stripe confirmed this PaymentIntent as succeeded." : "This payment is recorded as completed.";
  if (payment.status === "FAILED") return "Processing reached a confirmed terminal failure. No automatic retry is scheduled.";
  if (payment.status === "PROCESSING") return "Processing or provider reconciliation is in progress. The outcome is not final.";
  return "This payment is waiting for its assigned processor. The simulator is used for new dashboard payments.";
}
