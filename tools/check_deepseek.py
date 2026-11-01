"""Small explicit provider contract check; no course data is sent."""
import argparse
from datetime import datetime, timezone
from pathlib import Path

from visual_audio_notes.core import write_json
from visual_audio_notes.providers import ModelConnection, create_json_model


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    if args.out.exists():
        raise ValueError("Choose a new check output; previous evidence is preserved")
    model = create_json_model(ModelConnection(provider="deepseek", model="deepseek-flash"))
    schema = {"type": "object", "additionalProperties": False, "required": ["status"],
              "properties": {"status": {"type": "string", "enum": ["ok"]}}}
    result = model.json("Return a JSON object following the supplied schema.",
                        'Connection test only. Return {"status":"ok"}.',
                        max_tokens=1000, purpose="provider_contract", schema=schema)
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "provider": model.cache_identity, "result": result, "calls": model.calls,
              "scope": "Connectivity and JSON contract only; not semantic quality or audio/OCR/embedding support",
              "source_data_sent": False, "credential_persisted": False}
    write_json(args.out, report)
    print({"contract": result, "calls": model.calls, "report": str(args.out)}, flush=True)


if __name__ == "__main__":
    main()
