"""Build the AI admission summary: what gets sent to the model, and whether it may be sent at all."""

import os

SYSTEM_PROMPT = """You help a clinical researcher review one de-identified hospital admission from the MIMIC database.
Write a concise summary with these sections: Presentation and diagnoses; Hospital course (ICU stays, notable abnormal labs and trends); Medications; Outcome.
Use only the facts provided. If something isn't in the data, say it isn't recorded. Dates are shifted for de-identification; describe timing relative to admission.
Do not give treatment recommendations. This is for research and education, not clinical care."""

LLM_OVERRIDE_ENV = "ALLOW_LLM_WITH_CREDENTIALED_DATA"


def llm_allowed(credentialed: bool) -> tuple[bool, str]:
    """Credentialed MIMIC data may only go to a model the operator has vetted (PhysioNet DUA / LLM policy)."""
    if not credentialed:
        return True, ""
    if os.getenv(LLM_OVERRIDE_ENV, "").lower() == "true":
        return True, ("Credentialed data is sent to the configured model endpoint. Its operator has confirmed it meets "
                      "PhysioNet's requirements (no data retention, no training on data, no human review).")
    return False, (f"AI summaries are off for credentialed data. PhysioNet's data use agreement forbids sending it to "
                   f"third-party services unless they guarantee no retention, no training and no human review. "
                   f"After verifying your model endpoint meets those terms, set {LLM_OVERRIDE_ENV}=true in app.yaml.")


def _lines(rows: list[dict], fmt) -> list[str]:
    return [fmt(r) for r in rows] or ["(none recorded)"]


def build_context(adm: dict, diagnoses: list[dict], labs: list[dict], meds: list[dict], icu: list[dict],
                  notes: list[dict] | None = None, max_chars: int = 12000) -> str:
    """Compact, model-friendly text for one admission. Notes are truncated to fit max_chars."""
    parts = [
        "## Admission",
        f"Age {adm['age']}{'+' if adm['age'] >= 90 else ''}, {adm.get('gender') or 'unknown sex'}; "
        f"{adm['admission_type']} admission via {adm.get('admission_location') or 'unknown'}; "
        f"length of stay {adm['los_days']} days; discharged to {adm.get('discharge_location') or 'unknown'}; "
        f"in-hospital death: {'yes' if adm['hospital_expire_flag'] else 'no'}.",
        "## Diagnoses (by priority)",
        *_lines(diagnoses, lambda d: f"{d['seq_num']}. {d.get('long_title') or 'ICD code'} ({d['icd_code']}, ICD-{d['icd_version']})"),
        "## ICU stays",
        *_lines(icu, lambda s: f"{s['first_careunit']}: {s['los']:.1f} days" if s.get("los") is not None
                else f"{s['first_careunit']}"),
        "## Labs flagged abnormal (count, min-max, last value)",
        *_lines(labs, lambda l: f"{l['label']}: {l['n_abnormal']}x, {l['min_value']}-{l['max_value']} {l['unit'] or ''}, "
                                f"last {l['last_value']}"),
        "## Medications (most frequent)",
        *_lines(meds, lambda m: f"{m['drug']} ({m['n']} orders; {m.get('route') or 'route n/a'})"),
    ]
    text = "\n".join(parts)
    if notes:
        budget = max_chars - len(text) - 100
        note_parts = ["## Clinical notes (excerpts)"]
        for n in notes:
            if budget <= 200:
                break
            excerpt = n["text"][: budget - 50]
            note_parts.append(f"### {n['note_type']} ({n['charttime']})\n{excerpt}")
            budget -= len(excerpt) + 50
        text += "\n" + "\n".join(note_parts)
    return text[:max_chars]
