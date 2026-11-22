# Task: security review of app.py

`app.py` in the project directory is a Flask API for a customer portal. Perform a security review.

Report every **exploitable vulnerability**. For each one you must be able to say what an attacker
sends and what they gain. Do not report generic hardening advice (rate limiting, security headers,
logging, CSRF on token-authenticated JSON endpoints, dependency versions) and do not report code
that is actually safe: wrong reports count against you. Some endpoints are safe.

Do not modify `app.py`. The app cannot be run here (its dependencies and database are absent);
this is a read-only review.

Write your findings to `findings.json` in the project directory as a JSON array, one object per
distinct vulnerability. Use `"<module>"` as the function for top-level code:

```json
[{"function": "name_of_function", "line": 42, "vulnerability": "class of vulnerability", "explanation": "what the attacker sends and what they gain"}]
```
