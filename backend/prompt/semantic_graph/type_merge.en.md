# Task

Partition the supplied research knowledge-graph entity type labels into groups of semantically equivalent labels and choose one canonical English label for each group.

# Input

Labels and occurrence counts:

{{type_counts}}

# Requirements

1. Merge only labels with the same meaning. Never merge parent/child categories, broader/narrower categories, or merely related types.
2. Case, singular/plural, abbreviation, and cross-language variants may be grouped only when they are truly equivalent.
3. `canonical` must be a concise English category label equivalent to every member and must preserve their specificity.
4. If a suitable English member exists, copy that member exactly as `canonical`.
5. Create a new canonical label only to translate a group that contains no suitable English member.
6. Every input label must appear exactly once in exactly one `members` array. Preserve each input label verbatim.
7. Do not use occurrence counts as evidence that non-equivalent types should be merged.

# Output format

Return valid JSON only, without Markdown, explanation, or extra fields:

```json
{"groups":[{"canonical":"English type label","members":["equivalent label 1","equivalent label 2"]}]}
```

# Pre-output checklist

- Each group contains only equivalent labels.
- Every input label appears once and only once.
- Every canonical is concise English and is equivalent to all members.
- The JSON parses directly and its only root field is `groups`.
