// The sample's one TypeScript file. It exists so E2-R6 can show the rule
// format is not Python-specific; see PLANTED.json before changing it.

export function notify(message: string): void {
  console.log(`notify: ${message}`); // PLANTED: structured-logging (TypeScript)
}

// NOT a finding, deliberately: `no-console-log` matches `console.log` only
// (see its rule file), and this line is what proves its pattern is not the
// over-broad `console.$M(...)`. In spirit it is the same structured-logging
// lapse as the plant above, so it is recorded in PLANTED.json under
// `bundled_rule_non_findings`, and like that plant it is not one of E3's
// `violations`: the fixture's structured-logging principle is about `print`.
export function notifyFailure(message: string): void {
  console.error(`notify failed: ${message}`);
}
