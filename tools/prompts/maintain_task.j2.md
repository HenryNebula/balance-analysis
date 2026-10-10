Maintaining the registered parser {{ key }}.
Statement set: {{ names }}. frames.json in the working directory holds them
all (first: {{ name }}, {{ pages }} pages, {{ lines }} layout lines).

Baseline verdict (the CURRENT registry code, run on every statement):
{{ verdict }}

Current registered source:
```python
{{ code }}
```

Tweak it minimally so EVERY statement verifies. Keep KEY = "{{ key }}"
exactly. Return the complete revised script in one ```python fence; probe
first when the baseline doesn't explain a failure.
