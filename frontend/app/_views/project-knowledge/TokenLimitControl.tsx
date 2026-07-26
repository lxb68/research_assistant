"use client";

import styles from "./TokenLimitControl.module.css";

type TokenLimitControlProps = {
  domainTreeValue: string;
  semanticValue: string;
  domainTreeDefault?: number;
  semanticDefault?: number;
  upperBound?: number;
  error?: string;
  disabled?: boolean;
  onDomainTreeChange: (value: string) => void;
  onSemanticChange: (value: string) => void;
};

type TokenFieldProps = {
  id: string;
  title: string;
  description: string;
  value: string;
  defaultValue?: number;
  upperBound?: number;
  disabled: boolean;
  onChange: (value: string) => void;
};

function TokenField({
  id,
  title,
  description,
  value,
  defaultValue,
  upperBound,
  disabled,
  onChange,
}: TokenFieldProps) {
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
      <span className={styles.description}>{description}</span>
      <span className={styles.inputShell}>
        <input
          id={id}
          type="number"
          min={1}
          max={upperBound}
          step={1}
          inputMode="numeric"
          value={value}
          placeholder={defaultValue === undefined ? "" : String(defaultValue)}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        />
        <span aria-hidden="true">tokens</span>
      </span>
    </label>
  );
}

/** 独立呈现领域树两个模型阶段的输出预算，避免全局表单样式相互覆盖。 */
export function TokenLimitControl({
  domainTreeValue,
  semanticValue,
  domainTreeDefault,
  semanticDefault,
  upperBound,
  error = "",
  disabled = false,
  onDomainTreeChange,
  onSemanticChange,
}: TokenLimitControlProps) {
  return (
    <section className={styles.panel} aria-labelledby="domain-tree-token-limit-title">
      <header className={styles.header}>
        <div>
          <span className={styles.kicker}>模型预算</span>
          <h3 id="domain-tree-token-limit-title">输出 Token 上限</h3>
        </div>
        <p>
          留空时使用后端默认值。提高上限可以降低长 JSON 被截断或推理后正文为空的概率，
          但单次请求可能更慢、费用更高。
        </p>
      </header>

      <div className={styles.grid}>
        <TokenField
          id="domain-tree-max-output-tokens"
          title="领域树生成"
          description="控制分类树 JSON 的单次最大输出，标题数量较多时可适当提高。"
          value={domainTreeValue}
          defaultValue={domainTreeDefault}
          upperBound={upperBound}
          disabled={disabled}
          onChange={onDomainTreeChange}
        />
        <TokenField
          id="semantic-graph-max-output-tokens"
          title="语义分块抽取"
          description="控制每个正文分块的实体与关系 JSON；推理模型返回空正文时可适当提高。"
          value={semanticValue}
          defaultValue={semanticDefault}
          upperBound={upperBound}
          disabled={disabled}
          onChange={onSemanticChange}
        />
      </div>

      <footer className={styles.footer}>
        <span>单次任务设置</span>
        <span>部署安全上限：{upperBound ?? "加载中"} tokens</span>
      </footer>
      {error ? <div className={styles.error} role="alert">{error}</div> : null}
    </section>
  );
}
