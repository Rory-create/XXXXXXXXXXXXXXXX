#!/usr/bin/env python3
"""
Generate one-time access codes for the School Drive Archiver.

Usage:
    python3 webapp/gen_codes.py [--count 30] [--output codes.json]

Running this adds N new codes to the "unused" pool in codes.json.
Print or copy the codes to DM to classmates after they pay.
"""

import argparse
import json
import os
import uuid

DEFAULT_CODES_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "codes.json")


def load(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"unused": [], "used": []}


def save(path: str, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate access codes.")
    parser.add_argument("--count", type=int, default=30, help="Number of codes to generate (default: 30)")
    parser.add_argument("--output", default=DEFAULT_CODES_FILE, help="Path to codes.json")
    args = parser.parse_args()

    data = load(args.output)
    new_codes = [str(uuid.uuid4()) for _ in range(args.count)]
    data.setdefault("unused", []).extend(new_codes)

    save(args.output, data)

    print(f"Generated {args.count} new codes → {args.output}")
    print(f"Pool: {len(data['unused'])} unused, {len(data.get('used', []))} used\n")
    print("New codes (DM one per paying classmate):")
    print("-" * 44)
    for code in new_codes:
        print(f"  {code}")
    print("-" * 44)


if __name__ == "__main__":
    main()
