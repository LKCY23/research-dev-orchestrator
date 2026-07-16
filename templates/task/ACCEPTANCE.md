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

- RDO_TEMPLATE_INCOMPLETE: state behavior that proves this task's one primary trust boundary; split unrelated acceptance groups into separate tasks.

## Merge Preconditions

- RDO_TEMPLATE_INCOMPLETE: state conditions required before merge.

## Blocked Conditions

- RDO_TEMPLATE_INCOMPLETE: state when the worker must request `blocked`, including discovery of another independently acceptable trust boundary.

## Pre-Merge Checks

- RDO_TEMPLATE_INCOMPLETE: state non-command checks for the coordinator, or `None.`

## Post-Merge Checks

- RDO_TEMPLATE_INCOMPLETE: state non-command checks after merge, or `None.`
