# Task: code review of inventory.py

Review `inventory.py` in the project directory. The docstrings are the specification.

Report every **real bug**: places where the code does not do what its docstring says, crashes on
input the docstring allows, corrupts data, or is unsafe under the concurrency the docstring promises.

Do not report style, naming, missing type hints, performance, missing tests, or validation the
docstring does not ask for. Wrong reports count against you, so report only what you are confident
is a bug. Some functions are correct.

Do not modify `inventory.py`. You may run code to confirm a suspicion.

Write your findings to `findings.json` in the project directory as a JSON array, one object per
distinct bug:

```json
[{"function": "name_of_function", "line": 42, "title": "short title", "explanation": "what goes wrong and with which input"}]
```
