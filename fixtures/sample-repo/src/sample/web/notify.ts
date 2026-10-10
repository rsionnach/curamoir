// The sample's one TypeScript file. It exists so E2-R6 can show the rule
// format is not Python-specific; see PLANTED.json before changing it.

export function notify(message: string): void {
  console.log(`notify: ${message}`); // PLANTED: structured-logging (TypeScript)
}
