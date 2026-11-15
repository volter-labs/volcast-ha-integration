# readonly-sample golden

Synthetic registry snapshot of an imaginary read-only integration (domain `example_readonly`), used to
exercise the generic profile test for `access: "entities_only"` profiles.

- `entities.json`: one config entry, two devices, ten entities (sensors plus a number, a select and a switch
  that the profile must not map), states of the five mapped sensors. Unique ids follow `<scope>_<topic>`.
- `expected.json`: canonical values after the profile's transforms (battery power is negated: the source
  reports charging as positive, the canonical sign is positive = discharging) and the exact entity map.
