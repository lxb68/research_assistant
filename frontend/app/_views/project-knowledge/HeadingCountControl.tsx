"use client";

import { DOMAIN_TREE_HEADING_COUNT_MAX } from "@/lib/constants";
import styles from "./HeadingCountControl.module.css";

type HeadingCountStepperProps = {
  id: string;
  label: string;
  description: string;
  value: string;
  min: number;
  max: number;
  disabled?: boolean;
  onChange: (value: string) => void;
};

type HeadingCountControlProps = {
  primaryValue: string;
  secondaryValue: string;
  primaryCount: number;
  secondaryCount: number;
  error?: string;
  disabled?: boolean;
  onPrimaryChange: (value: string) => void;
  onSecondaryChange: (value: string) => void;
};

/** 提供可键盘输入、可点击步进的单项标题数量设置。 */
function HeadingCountStepper({
  id,
  label,
  description,
  value,
  min,
  max,
  disabled = false,
  onChange,
}: HeadingCountStepperProps) {
  const parsedValue = Number(value);
  const currentValue = Number.isInteger(parsedValue) ? parsedValue : min;
  const updateByStep = (offset: number) => {
    onChange(String(Math.min(max, Math.max(min, currentValue + offset))));
  };

  return (
    <div className={styles.optionCard}>
      <div className={styles.optionCopy}>
        <label htmlFor={id}>{label}</label>
        <small>{description}</small>
      </div>
      <div className={styles.stepper}>
        <button
          type="button"
          aria-label={`减少${label}`}
          disabled={disabled || currentValue <= min}
          onClick={() => updateByStep(-1)}
        >
          <span aria-hidden="true">−</span>
        </button>
        <div className={styles.numberField}>
          <input
            id={id}
            type="number"
            aria-label={label}
            value={value}
            min={min}
            max={max}
            step={1}
            inputMode="numeric"
            disabled={disabled}
            onChange={(event) => onChange(event.target.value)}
          />
        </div>
        <button
          type="button"
          aria-label={`增加${label}`}
          disabled={disabled || currentValue >= max}
          onClick={() => updateByStep(1)}
        >
          <span aria-hidden="true">+</span>
        </button>
      </div>
    </div>
  );
}

/** 集中呈现领域树层级规模，避免页面级表单样式干扰计数器布局。 */
export function HeadingCountControl({
  primaryValue,
  secondaryValue,
  primaryCount,
  secondaryCount,
  error = "",
  disabled = false,
  onPrimaryChange,
  onSecondaryChange,
}: HeadingCountControlProps) {
  return (
    <section
      id="domain-tree-heading-count-control"
      className={styles.panel}
      aria-label="结构规模"
    >
      <header className={styles.header}>
        <span className={styles.kicker}>结构规模</span>
        <div className={styles.summary} aria-label="当前标题数量设置" aria-live="polite">
          <span>
            <strong>{Number.isInteger(primaryCount) ? primaryCount : "—"}</strong>
            个一级标题
          </span>
          <i aria-hidden="true">×</i>
          <span>
            每项最多 <strong>{Number.isInteger(secondaryCount) ? secondaryCount : "—"}</strong> 个二级标题
          </span>
        </div>
      </header>

      <div className={styles.options}>
        <HeadingCountStepper
          id="domain-tree-primary-heading-count"
          label="一级标题"
          description="领域树的顶层分类数量"
          value={primaryValue}
          min={1}
          max={DOMAIN_TREE_HEADING_COUNT_MAX}
          disabled={disabled}
          onChange={onPrimaryChange}
        />
        <HeadingCountStepper
          id="domain-tree-secondary-heading-count"
          label="每项二级标题"
          description="每个一级标题下的子分类；设为 0 时只生成一级标题"
          value={secondaryValue}
          min={0}
          max={DOMAIN_TREE_HEADING_COUNT_MAX}
          disabled={disabled}
          onChange={onSecondaryChange}
        />
      </div>

      {error ? <div className={styles.feedbackError} role="alert">{error}</div> : null}
    </section>
  );
}
