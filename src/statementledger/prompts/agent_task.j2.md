Statement to parse: {{ name }} ({{ pages }} pages, {{ lines }} lines).
{% if others %}
Full statement set (the `stmt` tool argument indexes it):
{{ others }}
{% endif %}
The look tools read the frames in-process. Aim first: grep for labels and
stop words, render the regions they point at, table for the grid. Then
write parser.py and run it.
