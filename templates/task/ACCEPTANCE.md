# Acceptance

```json rdo-acceptance-contract
{
  "schema_version": 2,
  "required_commands": [
    {
      "id": "RDO_TEMPLATE_INCOMPLETE",
      "argv": [
        "RDO_TEMPLATE_INCOMPLETE"
      ],
      "cwd": ".",
      "timeout_seconds": 120
    }
  ],
  "required_outputs": [
    "RDO_TEMPLATE_INCOMPLETE"
  ],
  "pre_merge_commands": [],
  "post_merge_commands": []
}
```

## Behavioral Checks

- RDO_TEMPLATE_INCOMPLETE: state behavior that a reviewer must verify.

## Merge Preconditions

- RDO_TEMPLATE_INCOMPLETE: state conditions required before merge.

## Blocked Conditions

- RDO_TEMPLATE_INCOMPLETE: state when the worker must request `blocked`.

## Pre-Merge Checks

- RDO_TEMPLATE_INCOMPLETE: state non-command checks for the coordinator, or `None.`

## Post-Merge Checks

- RDO_TEMPLATE_INCOMPLETE: state non-command checks after merge, or `None.`
