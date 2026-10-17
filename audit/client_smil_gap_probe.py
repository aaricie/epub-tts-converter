"""Measure successive SMIL clip gaps without reading or decoding audio."""
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile


def seconds(value):
    if value.endswith("s"):
        return float(value[:-1])
    return sum(float(part) * 60 ** index for index, part in enumerate(value.split(":")[::-1]))


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        r"C:\Users\Aricie\Downloads\Peter; Longerich - Hitler_audio.epub"
    )
    rows = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.lower().endswith(".smil"):
                continue
            root = ET.fromstring(archive.read(name))
            clips = [
                (seconds(e.get("clipBegin")), seconds(e.get("clipEnd")))
                for e in root.iter("{http://www.w3.org/ns/SMIL}audio")
            ]
            if not clips:
                continue
            gaps = [begin - end for (_, end), (begin, _) in zip(clips, clips[1:])]
            rows.append({
                "file": name,
                "clips": len(clips),
                "timeline_s": clips[-1][1] - clips[0][0],
                "clip_sum_s": sum(end - begin for begin, end in clips),
                "positive_gap_s": sum(gap for gap in gaps if gap > 0.0001),
                "negative_gap_s": sum(gap for gap in gaps if gap < -0.0001),
                "gap_count_over_0_1ms": sum(gap > 0.0001 for gap in gaps),
                "max_gap_s": max(gaps, default=0),
                "gaps_ge_100ms": sum(gap >= 0.1 for gap in gaps),
                "gaps_ge_300ms": sum(gap >= 0.3 for gap in gaps),
            })
    summary = {"files": len(rows)}
    for key in ("clips", "timeline_s", "clip_sum_s", "positive_gap_s", "negative_gap_s", "gaps_ge_100ms", "gaps_ge_300ms"):
        summary[key] = sum(row[key] for row in rows)
    summary["max_gap_s"] = max(row["max_gap_s"] for row in rows)
    result = {"summary": summary, "chapters": rows}
    output = Path(__file__).with_name("client_smil_gap_measurements.json")
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(output)


if __name__ == "__main__":
    main()
