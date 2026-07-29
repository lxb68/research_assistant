# Task

Correct only the entity `type` values listed below because they violate the English-only type contract:

{{invalid_types}}

# Constraints

- Replace only those invalid `type` values with concise, semantically equivalent English category labels.
- Preserve every other field, value, array order, relation, identifier, and quote exactly.
- Do not add or remove entities or relations.

# Output

Return the complete corrected JSON object only, without Markdown or explanation. Before returning, verify that no listed invalid type remains and no unrelated value changed.
