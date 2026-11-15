"""Write two synthetic Claude Code projects with opposite styles, for the README and for trying `sips` without your own logs.

  low-leverage/   approves step by step, says "doesn't work", repeats the same rule every session
  high-leverage/  writes the rule into CLAUDE.md once, gives reasons and examples, asks for tests
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent


def session(project: str, name: str, day: int, turns: list):
    """turns: list of (human text, [(tool, target), ...]) — the agent actions that follow each turn."""
    lines, t = [], datetime(2026, 11, day, 9, 0)
    for i, (text, actions) in enumerate(turns):
        t += timedelta(minutes=3)
        lines.append({"type": "user", "uuid": f"{name}-u{i}", "timestamp": t.isoformat() + "Z",
                      "message": {"role": "user", "content": text}})
        for j, (tool, target) in enumerate(actions):
            t += timedelta(seconds=20)
            lines.append({"type": "assistant", "uuid": f"{name}-a{i}-{j}", "timestamp": t.isoformat() + "Z",
                          "message": {"content": [{"type": "tool_use", "name": tool, "input": {"file_path": target}}]}})
    d = HERE / project
    d.mkdir(exist_ok=True)
    (d / f"{name}.jsonl").write_text("\n".join(json.dumps(l) for l in lines))


edit = lambda f: ("Edit", f"/repo/{f}")
read = lambda f: ("Read", f"/repo/{f}")

work = lambda f, n: [read(f)] + [edit(f)] * (n - 1)   # an agent turn touches a file several times

# ---------------------------------------------------------------- low leverage
# Step-by-step approvals: the agent does a little, stops, waits for "ok".
tasks = ["add pagination to the orders endpoint", "add filtering by status", "add sorting by date",
         "add an export to CSV", "add a search box to orders"]
for day, task in enumerate(tasks, 1):
    session("low-leverage", f"s{day}", day, [
        (task, work("api/orders.py", 6) + [edit("db/migrations/0042.py")]),
        ("don't touch the migrations folder", work("api/orders.py", 3)),
        ("doesn't work", work("api/orders.py", 4)),
        ("still wrong", work("api/orders.py", 3)),
        ("ok", [edit("api/orders.py")]),
        ("yes continue", [edit("api/serializers.py")]),
        ("ok", [("Bash", "pytest")]),
        ("I ALREADY SAID DONT TOUCH THE MIGRATIONS" if day > 2 else "ok commit", work("api/orders.py", 2)),
    ])

# ---------------------------------------------------------------- high leverage
# Rules and tests written once; the agent then works longer on its own.
session("high-leverage", "s1", 1, [
    ("From now on, never edit files in db/migrations: they are generated. Add that rule to CLAUDE.md.", [edit("CLAUDE.md")]),
    ("Plan the pagination change first: cursor-based, page_size max 100, and a test per edge case before coding.",
     [read("api/orders.py"), ("Write", "/repo/docs/plan-pagination.md")]),
    ("Add a test for the empty page, for example page_size=10 on an empty table should return [] and next=None.",
     [("Write", "/repo/tests/test_orders_pagination.py")] + work("api/orders.py", 10) + [("Bash", "pytest")]),
    ("It fails because the cursor is encoded before the filter is applied, so the next page skips rows. Fix the order.",
     work("api/orders.py", 6) + [("Bash", "pytest")]),
    ("Does this still hold when two orders share the same created_at?", work("tests/test_orders_pagination.py", 5) + [("Bash", "pytest")]),
])
followups = [
    ("add filtering by status, with a test for each status", "Why a subquery? The index is on (status, created_at), so a plain WHERE should be faster."),
    ("add sorting by date, newest first, and test ties on created_at", "The CSV export should stream rows, because large exports time out at 30s."),
    ("add an export to CSV; reuse the pagination cursor", "Check the other two endpoints as well: do they apply the same page_size cap?"),
    ("add a search box to orders, matching on customer name", "For example 'ana' should match 'Ana María' but not 'Banana Ltd'."),
]
for day, (task, check) in enumerate(followups, 2):
    session("high-leverage", f"s{day}", day, [
        (task, [read("CLAUDE.md")] + work("api/orders.py", 12) + [("Write", f"/repo/tests/test_orders_{day}.py"), ("Bash", "pytest")]),
        (check, work("api/orders.py", 8) + [("Bash", "pytest")]),
        ("ok", [("Bash", "git commit")]),
    ])

print("wrote", *(p.name for p in HERE.iterdir() if p.is_dir()))
