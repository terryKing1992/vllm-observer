#!/usr/bin/env python
"""Render a stage flame graph (speedscope JSON) from observer JSONL events.

Usage:
    python scripts/stage_speedscope.py events.jsonl -o speedscope.json

Input: newline-delimited observer events (ConsoleFilter stdout output).
Output: speedscope "sampled" profile where frame width == accumulated seconds.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def stage_path(name):
    root = "serve-model-request"
    # Exported names may carry a module prefix (api-server./engine-core.);
    # strip it so the flame graph frames stay short and stable.
    stripped = name.split(".", 1)[-1] if "." in name else name
    engine = {
        "engine_handoff",
        "waiting_to_first_schedule",
        "prefill",
        "decode",
        "engine_finalize",
    }
    api = {"render_input", "input_process", "submit_engine", "sampling_params"}
    if name == root:
        return [root]
    if stripped in ("queue",):
        return [root, "generate-response"]
    if stripped in engine:
        return [root, "generate-response", stripped]
    if stripped in api:
        return [root, "api_prepare", stripped]
    if name == "http_total":
        return [root, "http_total(unpartitioned-diagnostic)"]
    return [root, name]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    totals = defaultdict(float)
    with args.input.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip().lstrip("\ufeff")
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                print(f"skip non-JSON line: {line[:60]!r}", file=sys.stderr)
                continue
            for name, aggregate in event.get("stages", {}).items():
                totals[tuple(stage_path(name))] += float(
                    aggregate.get("total_seconds", 0.0)
                )

    frames = []
    frame_index = {}
    samples = []
    values = []

    def frame_id(path):
        name = path[-1]
        if name not in frame_index:
            frame_index[name] = len(frames)
            frames.append({"name": name})
        return frame_index[name]

    for path, seconds in sorted(totals.items(), key=lambda kv: -kv[1]):
        samples.append([frame_id(path[: i + 1]) for i in range(len(path))])
        values.append(seconds)

    profile = {
        "type": "sampled",
        "name": "vllm-observer stages",
        "unit": "seconds",
        "samples": samples,
        "weights": values,
    }
    args.output.write_text(
        json.dumps(
            {"version": "0.0.1", "shared": {"frames": frames}, "profiles": [profile]}
        ),
        encoding="utf-8",
    )
    total = sum(values)
    print(f"wrote {args.output} ({len(samples)} stage paths, {total:.3f}s total)")


if __name__ == "__main__":
    main()
