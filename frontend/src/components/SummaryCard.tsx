interface SummaryCardProps {
  label: string;
  value: string | number;
  hint: string;
  tone: "neutral" | "success" | "danger" | "warning";
}

export function SummaryCard({ label, value, hint, tone }: SummaryCardProps) {
  return (
    <article className={`summary-card tone-${tone}`}>
      <p className="summary-label">{label}</p>
      <p className="summary-value">{value}</p>
      <p className="summary-hint">{hint}</p>
    </article>
  );
}
