"""Create two tiny git repos with opposite human-AI designs, to try `sips-score repo` before your own project.

  loops/approve-every-step   asks a person to approve every action, no rules, no evals, no learning
  loops/designed-loop        guardrails, a rules file, an eval set, escalation on blockers, feedback fed back
"""
import subprocess
from pathlib import Path

HERE = Path(__file__).parent / "loops"
FILES = {
    "approve-every-step": {
        "agent.py": 'from anthropic import Anthropic\n\ndef run(task, model):\n    for step in model.plan(task):\n        ok = input(f"Approve step {step}? [y/n] ")\n        if ok == "y":\n            model.execute(step)\n',
    },
    "designed-loop": {
        "loop.py": ('from anthropic import Anthropic\n\nMAX_SPEND = 50  # guardrail: daily spend cap\n\n'
                    'def handle(email, model, memory):\n'
                    '    draft = model.reply(email, examples=memory.examples())\n'
                    '    if draft.confidence < 0.6 or email.get("order_id") is None:\n'
                    '        return escalate_to_human(email, reason="missing order id or low confidence")\n'
                    '    log_decision(email, draft)\n'
                    '    return draft\n\n'
                    'def learn(feedback, memory):\n'
                    '    memory.add(feedback)  # store the correction\n'
                    '    update_prompt(memory)\n'),
        "prompts/system.md": "Never promise a refund. Always include the order id.\n",
        "AGENTS.md": "# Rules\n- Never send a reply without an order id.\n",
        "evals/cases.jsonl": '{"email": "Where is order 123?", "expected": "Order 123 ships Monday"}\n',
        "tests/test_loop.py": 'def test_reply():\n    expected = "Order 123 ships Monday"\n',
    },
}
for name, files in FILES.items():
    d = HERE / name
    d.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    if not (d / ".git").exists():
        git = lambda *a: subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)
        git("init", "-q")
        git("add", "-A")
        git("-c", "user.email=demo@example.com", "-c", "user.name=demo", "commit", "-q", "-m", "example loop")
print("wrote", ", ".join(FILES))
