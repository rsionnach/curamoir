# opensrm-oh27: nthlayer-workers: retrospective silently skips unparseable manifests

Found during opensrm-ih0v. Pre-existing; ih0v only made it observable.

src/nthlayer_workers/learn/retrospective.py:250-254

    for spec_file in specs_path.glob("*.yaml"):
        try:
            manifest = load_manifest(str(spec_file))
        except ManifestLoadError:
            continue

No log, no counter, no re-raise. A manifest that fails to parse drops out
of the financial retrospective entirely and leaves no trace anywhere.

WHY IT MATTERS
The retrospective computes financial impact per service. A silently
skipped manifest does not produce a wrong number that someone might
question -- it produces a CONFIDENT number computed over a subset, with
the missing service indistinguishable from a service that genuinely had
no impact. That is the worst failure shape for a financial figure.

INCONSISTENT WITH THE SAME LOOP
The very next branch (:255-261) DOES log when it skips a duplicate:

    log.warning("manifest_duplicate_skipped", service=..., spec_file=...)

So the loop already establishes the convention; the parse-failure branch
just does not follow it.

HOW IT SURFACED
opensrm-ih0v made spec.service.type required. Seven tests in
tests/learn/test_retrospective_financial.py began failing with assertions
like `assert {} == {'svc-a': [...], 'svc-b': []}` -- an empty result,
not an error. It took reading the source to learn why. Had the skip been
logged, the cause would have been immediate.

FIX
At minimum, mirror the duplicate branch:

    except ManifestLoadError as exc:
        log.warning("manifest_parse_failed", spec_file=str(spec_file),
                    error=str(exc))
        continue

Then decide whether silent-continue is right AT ALL here. Arguments for
failing loudly: this is a financial computation, and a specs dir with an
unparseable manifest is a deployment error, not a data condition. Consider
a strict mode, or surfacing a parse-failure count on the result object so
callers can decide.

AUDIT THE REST: grep nthlayer-workers for other `except ManifestLoadError`
and bare `except Exception` around manifest loading. retrospective.py:539
has `except Exception as exc` around a decision-store write which at least
logs, but the same question applies.

LABELS: v1.5
