"""Sequential official EPUBCheck validation of three input/output pairs."""
import collections
import json
import pathlib
import shutil
import subprocess

BASE = pathlib.Path(__file__).resolve().parents[3]
AUDIT = BASE / "audit/implementation/epub_validation"
JAR = BASE / "audit/validator" / "epubcheck-5.4.0" / "epubcheck.jar"
java = shutil.which("java")
results = []
for label, prefix in [("ontario", "L'Ontario"), ("kursk", "Kursk"), ("lotr", "The Lord")]:
    original = next((BASE / "test books").glob(prefix + "*.epub"))
    output = AUDIT / "artifacts/outputs" / original.name
    for variant, path in [("input", original), ("output", output)]:
        name = f"epubcheck_{label}_{variant}"
        report = AUDIT / (name + ".json")
        with (AUDIT / (name + ".log")).open("w", encoding="utf-8") as log:
            proc = subprocess.run([java, "-Xmx512m", "-jar", str(JAR), str(path), "--json", str(report)],
                                  stdout=log, stderr=subprocess.STDOUT, text=True, timeout=240)
        data = json.loads(report.read_text(encoding="utf-8"))
        grouped = collections.Counter((m["ID"], m["severity"]) for m in data.get("messages", []))
        results.append({"pair": label, "variant": variant, "exit_code": proc.returncode,
                        "checker": data["checker"], "messages": [{"id": k[0], "severity": k[1], "count": v} for k,v in sorted(grouped.items())]})
        (AUDIT / "epubcheck_pairs_summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(json.dumps({"pair": label, "variant": variant, "exit_code": proc.returncode,
                          "errors": data["checker"]["nError"], "warnings": data["checker"]["nWarning"],
                          "grouped_messages": results[-1]["messages"]}), flush=True)
