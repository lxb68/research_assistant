"use client";

import styles from "./TokenLimitControl.module.css";

type TokenLimitControlProps = {
  domainTreeValue: string;
  semanticValue: string;
  requestTimeoutValue: string;
  domainTreeDefault?: number;
  semanticDefault?: number;
  requestTimeoutDefault?: number;
  upperBound?: number;
  error?: string;
  disabled?: boolean;
  onDomainTreeChange: (value: string) => void;
  onSemanticChange: (value: string) => void;
  onRequestTimeoutChange: (value: string) => void;
};

type NumericFieldProps = {
  id: string;
  title: string;
  value: string;
  defaultValue?: number;
  minimum?: number;
  upperBound?: number;
  unit: string;
  disabled: boolean;
  onChange: (value: string) => void;
};

function NumericField({
  id,
  title,
  value,
  defaultValue,
  minimum = 1,
  upperBound,
  unit,
  disabled,
  onChange,
}: NumericFieldProps) {
  const hasOverride = value.trim().length > 0;
  return (
    <label
      className={`${styles.field}${hasOverride ? ` ${styles.fieldOverride}` : ""}`}
      htmlFor={id}
    >
      <span className={styles.fieldHeader}>
        <strong>{title}</strong>
        <small className={hasOverride ? styles.overrideBadge : ""}>
          {hasOverride ? `本次 ${value}` : `默认 ${defaultValue ?? "加载中"}`}
        </small>
      </span>
      <span className={styles.inputShell}>
        <input
          id={id}
          type="number"
          min={minimum}
          max={upperBound}
          step={1}
          inputMode="numeric"
          value={value}
          placeholder={defaultValue === undefined ? "" : String(defaultValue)}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        />
        <span aria-hidden="true">{unit}</span>
      </span>
    </label>
  );
}

/** 独立呈现领域树任务的模型输出与等待预算，避免全局表单样式相互覆盖。 */
export function TokenLimitControl({
  domainTreeValue,
  semanticValue,
  requestTimeoutValue,
  domainTreeDefault,
  semanticDefault,
  requestTimeoutDefault,
  upperBound,
  error = "",
  disabled = false,
  onDomainTreeChange,
  onSemanticChange,
  onRequestTimeoutChange,
}: TokenLimitControlProps) {
  return (
    <section className={styles.panel} aria-labelledby="domain-tree-token-limit-title">
      <header className={styles.header}>
        <h3 id="domain-tree-token-limit-title" className={styles.kicker}>模型预算</h3>
        <p>
          输出上限控制模型可生成的内容长度；请求超时控制单次等待模型响应的最长时间。
          提高预算可减少长 JSON 被截断或慢响应失败，但任务可能更久、费用更高。
        </p>
      </header>

      <div className={styles.grid}>
        <NumericField
          id="domain-tree-max-output-tokens"
          title="领域树生成"
          value={domainTreeValue}
          defaultValue={domainTreeDefault}
          upperBound={upperBound}
          unit="tokens"
          disabled={disabled}
          onChange={onDomainTreeChange}
        />
        <NumericField
          id="semantic-graph-max-output-tokens"
          title="语义分块抽取"
          value={semanticValue}
          defaultValue={semanticDefault}
          upperBound={upperBound}
          unit="tokens"
          disabled={disabled}
          onChange={onSemanticChange}
        />
        <NumericField
          id="domain-tree-request-timeout-seconds"
          title="单次请求超时"
          value={requestTimeoutValue}
          defaultValue={requestTimeoutDefault}
          minimum={5}
          upperBound={600}
          unit="秒"
          disabled={disabled}
          onChange={onRequestTimeoutChange}
        />
      </div>

      <footer className={styles.footer}>
        <span>单次任务设置</span>
        <span>安全上限：{upperBound ?? "加载中"} tokens / 600 秒</span>
      </footer>
      {error ? <div className={styles.error} role="alert">{error}</div> : null}
    </section>
  );
}
